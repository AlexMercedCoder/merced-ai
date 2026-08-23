"""Merced AI command-line interface."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Annotated, Any

import typer
from rich.console import Console
from rich.markdown import Markdown
from rich.table import Table

from merced_ai import __version__
from merced_ai.application import RoutingError, execute, prepare_run
from merced_ai.bots import BotError, create_bot, discover_bots, resolve_bot
from merced_ai.harnesses import default_registry
from merced_ai.harnesses.adapters.command import HarnessRunError
from merced_ai.models import HarnessProbe, ProfileProjection
from merced_ai.paths import ensure_project_layout, ensure_user_layout
from merced_ai.profiles import (
    ProfileError,
    create_profile,
    discover_profiles,
    resolve_profile,
    validate_profile,
)
from merced_ai.sessions import SessionStore, transcript_prompt

app = typer.Typer(
    name="merced-ai",
    help="Create OAP-backed bots that run through existing AI agent harnesses.",
    no_args_is_help=True,
)
harness_app = typer.Typer(help="Discover and inspect installed agent harnesses.")
profile_app = typer.Typer(help="Create, validate, and inspect Open Agent Profiles.")
bot_app = typer.Typer(help="Bind portable profiles to local harness preferences.")
session_app = typer.Typer(help="Inspect durable local conversation sessions.")
app.add_typer(harness_app, name="harness")
app.add_typer(profile_app, name="profile")
app.add_typer(bot_app, name="bot")
app.add_typer(session_app, name="session")
console = Console()
error_console = Console(stderr=True)
DEFAULT_WORKSPACE = Path.cwd()


def _version_callback(value: bool) -> None:
    if value:
        typer.echo(f"merced-ai {__version__}")
        raise typer.Exit()


@app.callback()
def main(
    version: Annotated[
        bool | None,
        typer.Option("--version", callback=_version_callback, is_eager=True, help="Show version."),
    ] = None,
) -> None:
    """Run portable OAP bots on harnesses already installed on this machine."""


@app.command("init")
def initialize(
    workspace: Annotated[Path, typer.Option("--workspace", "-C")] = DEFAULT_WORKSPACE,
) -> None:
    """Create project-local Merced AI and OAP directories."""
    root = ensure_project_layout(workspace)
    ensure_user_layout()
    console.print(f"Initialized [bold]{root}[/bold]")


@app.command("status")
def status(
    workspace: Annotated[Path, typer.Option("--workspace", "-C")] = DEFAULT_WORKSPACE,
    json_output: Annotated[bool, typer.Option("--json")] = False,
) -> None:
    """Summarize profiles, bots, sessions, and installed harnesses."""
    payload = {
        "workspace": str(workspace.resolve()),
        "profiles": len(discover_profiles(workspace)),
        "bots": len(discover_bots(workspace)),
        "sessions": len(SessionStore(workspace).list()),
        "installed_harnesses": sum(1 for item in default_registry().probe_all() if item.path),
    }
    if json_output:
        typer.echo(json.dumps(payload, indent=2))
    else:
        for key, value in payload.items():
            console.print(f"[bold]{key.replace('_', ' ').title()}:[/bold] {value}")


@harness_app.command("list")
def harness_list(
    json_output: Annotated[bool, typer.Option("--json")] = False,
) -> None:
    """List known harnesses and run safe local version probes."""
    probes = default_registry().probe_all()
    if json_output:
        typer.echo(json.dumps([probe.model_dump(mode="json") for probe in probes], indent=2))
        return
    _render_probe_table(probes)


@harness_app.command("show")
def harness_show(
    harness_id: Annotated[str, typer.Argument(help="Harness identifier from `harness list`.")],
    json_output: Annotated[bool, typer.Option("--json")] = False,
) -> None:
    """Show the current probe result for one harness."""
    try:
        probe = default_registry().get(harness_id).probe()
    except KeyError as exc:
        raise typer.BadParameter(str(exc), param_hint="harness_id") from exc
    if json_output:
        typer.echo(json.dumps(probe.model_dump(mode="json"), indent=2))
        return
    _render_probe_table((probe,))
    for warning in probe.warnings:
        console.print(f"[yellow]Warning:[/yellow] {warning}")


@app.command("doctor")
def doctor() -> None:
    """Summarize local harness availability without changing configuration."""
    probes = default_registry().probe_all()
    installed = [probe for probe in probes if probe.path is not None]
    failed = [probe for probe in probes if probe.status.value == "probe_failed"]
    console.print(f"Detected [bold]{len(installed)}[/bold] of {len(probes)} known harnesses.")
    if failed:
        console.print(f"[yellow]{len(failed)} installed harness probe(s) need attention.[/yellow]")
    else:
        console.print("[green]All installed harness version probes completed.[/green]")
    console.print("Authentication is verified only when a real run starts.")


@profile_app.command("list")
def profile_list(
    workspace: Annotated[Path, typer.Option("--workspace", "-C")] = DEFAULT_WORKSPACE,
    json_output: Annotated[bool, typer.Option("--json")] = False,
) -> None:
    """List valid profiles visible in the workspace."""
    profiles = _profile_action(lambda: discover_profiles(workspace))
    if json_output:
        typer.echo(
            json.dumps(
                [item.model_dump(mode="json", exclude={"document"}) for item in profiles], indent=2
            )
        )
        return
    table = Table(title="Open Agent Profiles")
    for column in ("Name", "Source", "Revision", "Description", "Path"):
        table.add_column(column)
    for item in profiles:
        table.add_row(item.name, item.source, str(item.revision), item.description, str(item.path))
    console.print(table)


@profile_app.command("validate")
def profile_validate(
    path: Path,
    json_output: Annotated[bool, typer.Option("--json")] = False,
) -> None:
    """Validate one OAP profile with the reference implementation."""
    record = _profile_action(lambda: validate_profile(path))
    payload = record.model_dump(mode="json", exclude={"document"})
    if json_output:
        typer.echo(json.dumps(payload, indent=2))
    else:
        console.print(f"[green]Valid AgentProfile:[/green] {record.name}")
        console.print(f"Spec digest: {record.spec_digest}")


@profile_app.command("show")
def profile_show(
    reference: str,
    workspace: Annotated[Path, typer.Option("--workspace", "-C")] = DEFAULT_WORKSPACE,
    json_output: Annotated[bool, typer.Option("--json")] = False,
) -> None:
    """Show a discovered profile by name or path."""
    record = _profile_action(lambda: resolve_profile(reference, workspace))
    if json_output:
        typer.echo(record.model_dump_json(indent=2))
    else:
        console.print(f"[bold]{record.name}[/bold] — {record.description}")
        console.print(f"{record.source} · revision {record.revision} · {record.path}")
        console.print(Markdown(record.document["spec"]["role"]["instructions"]))


@profile_app.command("create")
def profile_create(
    name: str,
    description: Annotated[str, typer.Option("--description", "-d")],
    instructions: Annotated[str, typer.Option("--instructions", "-i")],
    workspace: Annotated[Path, typer.Option("--workspace", "-C")] = DEFAULT_WORKSPACE,
) -> None:
    """Create a minimal valid project-local OAP profile."""
    record = _profile_action(lambda: create_profile(name, description, instructions, workspace))
    console.print(f"Created [bold]{record.name}[/bold] at {record.path}")


@profile_app.command("effective")
def profile_effective(
    reference: str,
    harness_id: Annotated[str, typer.Option("--harness")],
    workspace: Annotated[Path, typer.Option("--workspace", "-C")] = DEFAULT_WORKSPACE,
    json_output: Annotated[bool, typer.Option("--json")] = False,
) -> None:
    """Preview how a profile projects onto one harness without running it."""
    record = _profile_action(lambda: resolve_profile(reference, workspace))
    try:
        projection = default_registry().get(harness_id).project_profile(record)
    except (KeyError, NotImplementedError) as exc:
        _fail(str(exc), 4)
    _render_projection(projection, json_output)


@bot_app.command("list")
def bot_list(
    workspace: Annotated[Path, typer.Option("--workspace", "-C")] = DEFAULT_WORKSPACE,
    json_output: Annotated[bool, typer.Option("--json")] = False,
) -> None:
    """List project and user bot bindings."""
    bots = _bot_action(lambda: discover_bots(workspace))
    if json_output:
        typer.echo(json.dumps([item.model_dump(mode="json") for item in bots], indent=2))
        return
    table = Table(title="Merced AI bots")
    for column in ("Name", "Profile", "Harness", "Fallbacks", "Source"):
        table.add_column(column)
    for item in bots:
        table.add_row(
            item.name,
            item.profile,
            item.harness.preferred,
            ", ".join(item.harness.fallbacks) or "-",
            item.source,
        )
    console.print(table)


@bot_app.command("create")
def bot_create(
    name: str,
    profile: Annotated[str, typer.Option("--profile")],
    harness: Annotated[str, typer.Option("--harness")],
    fallback: Annotated[list[str] | None, typer.Option("--fallback")] = None,
    workspace: Annotated[Path, typer.Option("--workspace", "-C")] = DEFAULT_WORKSPACE,
    user: Annotated[bool, typer.Option("--user", help="Create a user-global binding.")] = False,
) -> None:
    """Create a bot binding from an OAP profile and harness preference."""
    try:
        default_registry().get(harness)
        for item in fallback or []:
            default_registry().get(item)
    except KeyError as exc:
        _fail(str(exc), 2)
    binding = _bot_action(
        lambda: create_bot(name, profile, harness, tuple(fallback or ()), workspace, user=user)
    )
    console.print(f"Created [bold]{binding.name}[/bold] at {binding.path}")


@bot_app.command("show")
def bot_show(
    name: str,
    workspace: Annotated[Path, typer.Option("--workspace", "-C")] = DEFAULT_WORKSPACE,
    json_output: Annotated[bool, typer.Option("--json")] = False,
) -> None:
    """Show one bot binding."""
    binding = _bot_action(lambda: resolve_bot(name, workspace))
    if json_output:
        typer.echo(binding.model_dump_json(indent=2))
    else:
        console.print(
            f"[bold]{binding.name}[/bold]: {binding.profile} → {binding.harness.preferred}"
        )
        console.print(str(binding.path))


@app.command("ask")
def ask(
    bot_name: str,
    prompt: str,
    workspace: Annotated[Path, typer.Option("--workspace", "-C")] = DEFAULT_WORKSPACE,
    harness: Annotated[str | None, typer.Option("--harness")] = None,
    dry_run: Annotated[bool, typer.Option("--dry-run")] = False,
    explain: Annotated[bool, typer.Option("--explain")] = False,
    json_output: Annotated[bool, typer.Option("--json")] = False,
) -> None:
    """Run one prompt through a bot's selected harness."""
    try:
        prepared = prepare_run(bot_name, prompt, workspace, harness_override=harness)
    except (BotError, ProfileError, RoutingError) as exc:
        _fail(str(exc), 2)
    if dry_run:
        payload = {
            "bot": prepared.bot.model_dump(mode="json"),
            "profile": prepared.profile.model_dump(mode="json", exclude={"document"}),
            "projection": prepared.projection.model_dump(mode="json"),
        }
        if json_output:
            typer.echo(json.dumps(payload, indent=2))
        else:
            _render_projection(prepared.projection, False)
        return
    if explain and not json_output:
        _render_projection(prepared.projection, False)
    store = SessionStore(workspace)
    session = store.create(bot_name, prepared.request.harness_id, prepared.profile)
    store.append(session, "user", prompt)
    try:
        result = execute(prepared)
    except HarnessRunError as exc:
        _fail(str(exc), exc.exit_code if 0 < exc.exit_code < 126 else 5)
    store.append(session, "assistant", result.output)
    if json_output:
        payload = result.model_dump(mode="json")
        payload["session_id"] = session.id
        typer.echo(json.dumps(payload, indent=2))
    else:
        console.print(Markdown(result.output))
        console.print(f"[dim]{result.harness_id} · {result.duration_ms} ms · {session.id}[/dim]")


@app.command("chat")
def chat(
    bot_name: str,
    workspace: Annotated[Path, typer.Option("--workspace", "-C")] = DEFAULT_WORKSPACE,
    harness: Annotated[str | None, typer.Option("--harness")] = None,
    resume_session: Annotated[str | None, typer.Option("--resume-session")] = None,
) -> None:
    """Chat with a bot; enter /exit or /quit to finish."""
    _chat_loop(bot_name, workspace, harness=harness, resume_session=resume_session)


def _chat_loop(
    bot_name: str,
    workspace: Path,
    *,
    harness: str | None = None,
    resume_session: str | None = None,
) -> None:
    store = SessionStore(workspace)
    existing = None
    if resume_session:
        try:
            existing = store.load(resume_session)
        except ValueError as exc:
            _fail(str(exc), 2)
        if existing.bot_name != bot_name:
            _fail(
                f"session {existing.id!r} belongs to bot {existing.bot_name!r}, not {bot_name!r}",
                2,
            )
        harness = existing.harness_id
    try:
        initial = prepare_run(
            bot_name, "Start the conversation.", workspace, harness_override=harness
        )
    except (BotError, ProfileError, RoutingError) as exc:
        _fail(str(exc), 2)
    session = existing or store.create(bot_name, initial.request.harness_id, initial.profile)
    console.print(
        f"Chatting with [bold]{bot_name}[/bold] through {initial.request.harness_id}. {session.id}"
    )
    while True:
        try:
            prompt = console.input("[bold cyan]you>[/bold cyan] ").strip()
        except (EOFError, KeyboardInterrupt):
            break
        if not prompt:
            continue
        if prompt in {"/exit", "/quit"}:
            break
        expanded = transcript_prompt(session, prompt)
        prepared = prepare_run(
            bot_name, expanded, workspace, harness_override=initial.request.harness_id
        )
        store.append(session, "user", prompt)
        try:
            result = execute(prepared)
        except HarnessRunError as exc:
            console.print(f"[red]{exc}[/red]")
            continue
        store.append(session, "assistant", result.output)
        console.print(Markdown(result.output))


@session_app.command("list")
def session_list(
    workspace: Annotated[Path, typer.Option("--workspace", "-C")] = DEFAULT_WORKSPACE,
    json_output: Annotated[bool, typer.Option("--json")] = False,
) -> None:
    """List durable project sessions."""
    records = SessionStore(workspace).list()
    if json_output:
        typer.echo(json.dumps([item.model_dump(mode="json") for item in records], indent=2))
        return
    table = Table(title="Merced AI sessions")
    for column in ("Session", "Bot", "Harness", "Turns", "Updated"):
        table.add_column(column)
    for item in records:
        table.add_row(
            item.id, item.bot_name, item.harness_id, str(len(item.turns)), item.updated_at
        )
    console.print(table)


@session_app.command("show")
def session_show(
    session_id: str,
    workspace: Annotated[Path, typer.Option("--workspace", "-C")] = DEFAULT_WORKSPACE,
    json_output: Annotated[bool, typer.Option("--json")] = False,
) -> None:
    """Show a stored conversation."""
    try:
        record = SessionStore(workspace).load(session_id)
    except ValueError as exc:
        _fail(str(exc), 2)
    if json_output:
        typer.echo(record.model_dump_json(indent=2))
        return
    console.print(f"[bold]{record.id}[/bold] · {record.bot_name} · {record.harness_id}")
    for turn in record.turns:
        console.print(f"\n[bold]{turn.role}>[/bold]")
        console.print(Markdown(turn.content))


@session_app.command("resume")
def session_resume(
    session_id: str,
    workspace: Annotated[Path, typer.Option("--workspace", "-C")] = DEFAULT_WORKSPACE,
) -> None:
    """Resume a stored conversation using its pinned bot and harness."""
    try:
        record = SessionStore(workspace).load(session_id)
    except ValueError as exc:
        _fail(str(exc), 2)
    _chat_loop(record.bot_name, workspace, resume_session=session_id)


def _render_probe_table(probes: tuple[HarnessProbe, ...]) -> None:
    table = Table(title="Merced AI harness inventory")
    for column in ("Harness", "Status", "Version", "Transport", "Executable"):
        table.add_column(column)
    for probe in probes:
        table.add_row(
            probe.harness_id,
            probe.status.value,
            probe.version or "-",
            probe.transport.value if probe.transport else "-",
            str(probe.path) if probe.path else "-",
        )
    console.print(table)


def _render_projection(projection: ProfileProjection, json_output: bool) -> None:
    if json_output:
        typer.echo(projection.model_dump_json(indent=2))
        return
    console.print(
        f"[bold]Projection:[/bold] {projection.harness_id} · {projection.support_level} · "
        f"{'provisional' if projection.provisional else 'verified'}"
    )
    for item in projection.adjustments:
        console.print(f"- {item.action}: {item.field} — {item.reason}")


def _profile_action(action: Any) -> Any:
    try:
        return action()
    except (ProfileError, OSError) as exc:
        _fail(str(exc), 2)


def _bot_action(action: Any) -> Any:
    try:
        return action()
    except (BotError, ProfileError, OSError) as exc:
        _fail(str(exc), 2)


def _fail(message: str, code: int) -> Any:
    error_console.print(f"[red]Error:[/red] {message}")
    raise typer.Exit(code=code)
