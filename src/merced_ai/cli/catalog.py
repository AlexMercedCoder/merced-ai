"""Profile and bot commands."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Annotated

import typer
import yaml
from rich.markdown import Markdown
from rich.markup import escape
from rich.table import Table

from merced_ai.bots import create_bot, discover_bots, resolve_bot
from merced_ai.cli.apps import (
    bot_app,
    profile_app,
)
from merced_ai.cli.common import (
    DEFAULT_WORKSPACE,
    JSON_HELP,
    WORKSPACE_HELP,
    _bot_action,
    _fail,
    _profile_action,
    _render_projection,
    console,
)
from merced_ai.harnesses import default_registry
from merced_ai.profile_generation import generate_profile_proposal
from merced_ai.profiles import (
    create_profile,
    create_profile_document,
    discover_profiles,
    resolve_profile,
    validate_profile,
)


@profile_app.command("list")
def profile_list(
    workspace: Annotated[
        Path, typer.Option("--workspace", "-C", help=WORKSPACE_HELP, show_default=False)
    ] = DEFAULT_WORKSPACE,
    json_output: Annotated[bool, typer.Option("--json", help=JSON_HELP)] = False,
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
    for column in ("Name", "Found in", "Revision", "Description", "Path"):
        table.add_column(column)
    for item in profiles:
        found_in = escape(item.origin or item.source)
        if item.also_in:
            found_in += f" [dim](same file in {escape(', '.join(item.also_in))})[/dim]"
        if item.conflict:
            found_in += " [red](conflict)[/red]"
        table.add_row(
            item.name,
            found_in,
            str(item.revision),
            escape(item.description),
            escape(str(item.path)),
        )
    console.print(table)
    for item in profiles:
        if item.conflict:
            console.print(f"[red]Conflict:[/red] {escape(item.conflict)}.")


@profile_app.command("validate")
def profile_validate(
    path: Path,
    json_output: Annotated[bool, typer.Option("--json", help=JSON_HELP)] = False,
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
    workspace: Annotated[
        Path, typer.Option("--workspace", "-C", help=WORKSPACE_HELP, show_default=False)
    ] = DEFAULT_WORKSPACE,
    json_output: Annotated[bool, typer.Option("--json", help=JSON_HELP)] = False,
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
    description: Annotated[str, typer.Option("--description", "-d", help="One-line summary.")],
    instructions: Annotated[
        str, typer.Option("--instructions", "-i", help="What the bot should do.")
    ],
    edit: Annotated[
        str | None, typer.Option("--edit", help="Request edit permission: ask, allow, or deny.")
    ] = None,
    shell: Annotated[
        str | None, typer.Option("--shell", help="Request shell permission: ask, allow, or deny.")
    ] = None,
    workspace: Annotated[
        Path, typer.Option("--workspace", "-C", help=WORKSPACE_HELP, show_default=False)
    ] = DEFAULT_WORKSPACE,
) -> None:
    """Create a minimal valid project-local OAP profile.

    Example: merced-ai profile create reviewer -d "Reviews" -i "Review." --edit deny --shell deny
    """
    record = _profile_action(
        lambda: create_profile(
            name,
            description,
            instructions,
            workspace,
            edit_permission=edit,
            shell_permission=shell,
        )
    )
    console.print(f"Created [bold]{record.name}[/bold] at {record.path}")


@profile_app.command("generate")
def profile_generate(
    prompt: Annotated[str, typer.Argument(help="Describe the specialist to create.")],
    name: Annotated[str | None, typer.Option("--name")] = None,
    harness: Annotated[str | None, typer.Option("--harness")] = None,
    scope: Annotated[str, typer.Option("--scope")] = "project",
    dry_run: Annotated[bool, typer.Option("--dry-run")] = False,
    yes: Annotated[bool, typer.Option("--yes")] = False,
    workspace: Annotated[
        Path, typer.Option("--workspace", "-C", help=WORKSPACE_HELP, show_default=False)
    ] = DEFAULT_WORKSPACE,
) -> None:
    """Generate and validate a portable OAP profile through an installed harness."""
    proposal = _profile_action(
        lambda: generate_profile_proposal(
            prompt,
            workspace,
            preferred_name=name,
            harness_id=harness,
            autonomous=False,
        )
    )
    document = proposal["document"]
    console.print(Markdown(f"```yaml\n{yaml.safe_dump(document, sort_keys=False)}\n```"))
    if dry_run:
        return
    if not yes and not typer.confirm("Save this validated profile?"):
        console.print("Profile was not saved.")
        return
    record = _profile_action(lambda: create_profile_document(document, workspace, scope=scope))
    console.print(f"Created [bold]{record.name}[/bold] at {record.path}")


@profile_app.command("effective")
def profile_effective(
    reference: str,
    harness_id: Annotated[str, typer.Option("--harness")],
    workspace: Annotated[
        Path, typer.Option("--workspace", "-C", help=WORKSPACE_HELP, show_default=False)
    ] = DEFAULT_WORKSPACE,
    json_output: Annotated[bool, typer.Option("--json", help=JSON_HELP)] = False,
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
    workspace: Annotated[
        Path, typer.Option("--workspace", "-C", help=WORKSPACE_HELP, show_default=False)
    ] = DEFAULT_WORKSPACE,
    json_output: Annotated[bool, typer.Option("--json", help=JSON_HELP)] = False,
) -> None:
    """List project and user bot bindings."""
    bots = _bot_action(lambda: discover_bots(workspace))
    if json_output:
        typer.echo(json.dumps([item.model_dump(mode="json") for item in bots], indent=2))
        return
    profiles = {
        profile.name: profile for profile in _profile_action(lambda: discover_profiles(workspace))
    }
    table = Table(title="Merced AI bots")
    for column in ("Name", "Profile", "Profile found in", "Harness", "Fallbacks", "Source"):
        table.add_column(column)
    for item in bots:
        profile = profiles.get(item.profile)
        if profile is None:
            where = "[yellow]not found[/yellow]"
        elif profile.conflict:
            where = "[red]conflict (see profile list)[/red]"
        else:
            where = escape(profile.origin or profile.source)
        table.add_row(
            item.name,
            item.profile,
            where,
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
    workspace: Annotated[
        Path, typer.Option("--workspace", "-C", help=WORKSPACE_HELP, show_default=False)
    ] = DEFAULT_WORKSPACE,
    user: Annotated[bool, typer.Option("--user", help="Create a user-global binding.")] = False,
    requires_webmcp: Annotated[
        bool,
        typer.Option(
            "--requires-webmcp/--no-requires-webmcp",
            help="Route this bot only through harnesses with native WebMCP support.",
        ),
    ] = False,
) -> None:
    """Create a bot binding from an OAP profile and harness preference."""
    try:
        default_registry().get(harness)
        for item in fallback or []:
            default_registry().get(item)
    except KeyError as exc:
        _fail(str(exc), 2)
    binding = _bot_action(
        lambda: create_bot(
            name,
            profile,
            harness,
            tuple(fallback or ()),
            workspace,
            user=user,
            requires_webmcp=requires_webmcp,
        )
    )
    console.print(f"Created [bold]{binding.name}[/bold] at {binding.path}")


@bot_app.command("show")
def bot_show(
    name: str,
    workspace: Annotated[
        Path, typer.Option("--workspace", "-C", help=WORKSPACE_HELP, show_default=False)
    ] = DEFAULT_WORKSPACE,
    json_output: Annotated[bool, typer.Option("--json", help=JSON_HELP)] = False,
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
