"""One-bot turns and conversations: ask, chat, and session commands."""

from __future__ import annotations

import json
import sys
import threading
from pathlib import Path
from typing import Annotated, Any

import typer
from aais import create_decision
from rich.markdown import Markdown
from rich.table import Table

from merced_ai.application import (
    PreparedRun,
    RoutingError,
    prepare_group_turn,
    prepare_run,
)
from merced_ai.bots import BotError
from merced_ai.cli.apps import (
    app,
    session_app,
)
from merced_ai.cli.common import (
    ALLOW_CONCURRENT_WRITES_HELP,
    DEFAULT_WORKSPACE,
    JSON_HELP,
    WORKSPACE_HELP,
    _execute,
    _fail,
    _render_projection,
    console,
)
from merced_ai.cli.group import _group_chat_loop
from merced_ai.harnesses import default_registry
from merced_ai.harnesses.adapters.command import HarnessRunError
from merced_ai.harnesses.registry import HarnessRegistry
from merced_ai.models import (
    RunResult,
    SessionRecord,
)
from merced_ai.profiles import (
    ProfileError,
)
from merced_ai.sessions import SessionStore


@app.command("ask")
def ask(
    bot_name: str,
    prompt: str,
    workspace: Annotated[
        Path, typer.Option("--workspace", "-C", help=WORKSPACE_HELP, show_default=False)
    ] = DEFAULT_WORKSPACE,
    harness: Annotated[str | None, typer.Option("--harness")] = None,
    dry_run: Annotated[bool, typer.Option("--dry-run")] = False,
    explain: Annotated[bool, typer.Option("--explain")] = False,
    json_output: Annotated[bool, typer.Option("--json", help=JSON_HELP)] = False,
) -> None:
    """Run one prompt through a bot's selected harness."""
    try:
        prepared = prepare_run(bot_name, prompt, workspace, harness_override=harness)
    except (BotError, ProfileError, RoutingError) as exc:
        _fail(str(exc), 2)
    if dry_run:
        payload: dict[str, Any] = {
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
        result, streamed = _run_turn(prepared, interactive=not json_output)
    except HarnessRunError as exc:
        _fail(str(exc), exc.exit_code if 0 < exc.exit_code < 126 else 5)
    _store_reply(store, session, prepared, result)
    if json_output:
        result_payload = result.model_dump(mode="json")
        result_payload["session_id"] = session.id
        typer.echo(json.dumps(result_payload, indent=2))
    else:
        if not streamed:
            console.print(Markdown(result.output))
        console.print(f"[dim]{result.harness_id} · {result.duration_ms} ms · {session.id}[/dim]")


@app.command("chat")
def chat(
    bot_name: str,
    workspace: Annotated[
        Path, typer.Option("--workspace", "-C", help=WORKSPACE_HELP, show_default=False)
    ] = DEFAULT_WORKSPACE,
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
    # One registry for the whole chat, so routing reuses harness probes between turns.
    registry = default_registry()
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
            bot_name,
            "Start the conversation.",
            workspace,
            harness_override=harness,
            registry=registry,
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
        try:
            session = store.load(session.id)
            prepared = prepare_group_turn(
                session, prompt, workspace, dispatch=bot_name, registry=registry
            )[0]
        except (ValueError, BotError, ProfileError, RoutingError) as exc:
            console.print(f"[red]{exc}[/red]")
            continue
        store.append(session, "user", prompt)
        try:
            result, streamed = _run_turn(prepared, registry=registry, interactive=True)
        except HarnessRunError as exc:
            console.print(f"[red]{exc}[/red]")
            continue
        _store_reply(store, session, prepared, result)
        if not streamed:
            console.print(Markdown(result.output))


def _store_reply(
    store: SessionStore, session: SessionRecord, prepared: PreparedRun, result: RunResult
) -> None:
    store.append(
        session,
        "assistant",
        result.output,
        bot_name=prepared.bot.name,
        harness_id=result.harness_id,
        profile=prepared.profile,
        native_session_id=result.native_session_id,
    )


def _terminal_approval(envelope: dict[str, Any], _cancel: threading.Event | None) -> dict[str, Any]:
    """Ask on the terminal before an ACP agent runs a tool call; default is to deny."""
    request = envelope["request"]
    action = request["action"]
    console.print(
        f"\n[bold yellow]Approval requested[/bold yellow] by {request['origin']['harness']}: "
        f"{action.get('summary', action.get('name'))}"
    )
    if action.get("arguments"):
        console.print(f"[dim]{json.dumps(action['arguments'])[:500]}[/dim]")
    approved = typer.confirm("Allow this once?", default=False)
    return create_decision(
        envelope,
        decision="approve" if approved else "deny",
        scope="once",
        actor={"id": "local-user", "type": "human", "authenticated_by": "merced-ai-terminal"},
        sequence=int(envelope.get("sequence", 1)),
        stream="merced-ai.terminal",
    )


def _run_turn(
    prepared: PreparedRun, *, registry: HarnessRegistry | None = None, interactive: bool
) -> tuple[RunResult, bool]:
    """Run one turn; stream replies and ask for approvals on the terminal when possible.

    Returns the result and whether the reply was already printed while streaming.
    """
    adapter = (registry or default_registry()).get(prepared.request.harness_id)
    acp_available = getattr(adapter, "acp_available", None)
    if not (interactive and callable(acp_available) and acp_available()):
        return _execute(prepared), False
    printed: list[str] = []

    def on_event(event: dict[str, Any]) -> None:
        if event.get("type") == "assistant_delta":
            printed.append(str(event.get("text", "")))
            console.print(printed[-1], end="", markup=False, highlight=False, soft_wrap=True)
        elif event.get("type") == "tool_call":
            call = event.get("tool_call", {})
            console.print(
                f"\n[dim]· {call.get('title') or call.get('kind')} ({call.get('status', '')})[/dim]"
            )

    handler = _terminal_approval if sys.stdin.isatty() else None
    result = adapter.run_cancellable(  # type: ignore[attr-defined]
        prepared.request, None, handler, None, on_event=on_event
    )
    if printed:
        console.print()
    return result, bool(printed)


@session_app.command("list")
def session_list(
    workspace: Annotated[
        Path, typer.Option("--workspace", "-C", help=WORKSPACE_HELP, show_default=False)
    ] = DEFAULT_WORKSPACE,
    json_output: Annotated[bool, typer.Option("--json", help=JSON_HELP)] = False,
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
        bot_label = ", ".join(participant.bot_name for participant in item.participants)
        harness_label = ", ".join(
            dict.fromkeys(participant.harness_id for participant in item.participants)
        )
        table.add_row(item.id, bot_label, harness_label, str(len(item.turns)), item.updated_at)
    console.print(table)


@session_app.command("show")
def session_show(
    session_id: str,
    workspace: Annotated[
        Path, typer.Option("--workspace", "-C", help=WORKSPACE_HELP, show_default=False)
    ] = DEFAULT_WORKSPACE,
    json_output: Annotated[bool, typer.Option("--json", help=JSON_HELP)] = False,
) -> None:
    """Show a stored conversation."""
    try:
        record = SessionStore(workspace).load(session_id)
    except ValueError as exc:
        _fail(str(exc), 2)
    if json_output:
        typer.echo(record.model_dump_json(indent=2))
        return
    participants = ", ".join(item.bot_name for item in record.participants)
    console.print(f"[bold]{record.id}[/bold] · {participants} · {record.mode}")
    for turn in record.turns:
        console.print(f"\n[bold]{turn.bot_name or turn.role}>[/bold]")
        console.print(Markdown(turn.content))


@session_app.command("resume")
def session_resume(
    session_id: str,
    workspace: Annotated[
        Path, typer.Option("--workspace", "-C", help=WORKSPACE_HELP, show_default=False)
    ] = DEFAULT_WORKSPACE,
    allow_concurrent_writes: Annotated[
        bool, typer.Option("--allow-concurrent-writes", help=ALLOW_CONCURRENT_WRITES_HELP)
    ] = False,
) -> None:
    """Resume a stored conversation using its pinned bot and harness."""
    try:
        record = SessionStore(workspace).load(session_id)
    except ValueError as exc:
        _fail(str(exc), 2)
    if record.kind == "group":
        _group_chat_loop(record, workspace, allow_concurrent_writes=allow_concurrent_writes)
    else:
        _chat_loop(record.bot_name, workspace, resume_session=session_id)
