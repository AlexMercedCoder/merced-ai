"""Group rooms, including worktree compare, apply, and cleanup."""

from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path
from typing import Annotated, Literal, cast

import typer
from rich.markdown import Markdown
from rich.markup import escape
from rich.table import Table

from merced_ai.application import (
    PreparedRun,
    RoutingError,
    isolate_group_turn,
    participant_from_run,
    prepare_group,
    prepare_group_turn,
    shared_workspace_writers,
    write_serialization_message,
)
from merced_ai.bots import BotError
from merced_ai.cli.apps import (
    group_app,
)
from merced_ai.cli.common import (
    ALLOW_CONCURRENT_WRITES_HELP,
    DEFAULT_WORKSPACE,
    JSON_HELP,
    WORKSPACE_HELP,
    WORKTREES_HELP,
    _execute,
    _fail,
    console,
    error_console,
)
from merced_ai.harnesses import default_registry
from merced_ai.harnesses.registry import HarnessRegistry
from merced_ai.models import (
    RunResult,
    SessionRecord,
)
from merced_ai.profiles import (
    ProfileError,
)
from merced_ai.sessions import SessionStore
from merced_ai.worktrees import WorktreeError, WorktreeManager


@group_app.command("ask")
def group_ask(
    bot_names: Annotated[list[str], typer.Argument(help="Two or more bot names.")],
    prompt: Annotated[str, typer.Option("--prompt", "-p", help="Message sent to the group.")],
    workspace: Annotated[
        Path, typer.Option("--workspace", "-C", help=WORKSPACE_HELP, show_default=False)
    ] = DEFAULT_WORKSPACE,
    mode: Annotated[str, typer.Option(help="mentions, all, or round_robin")] = "all",
    allow_concurrent_writes: Annotated[
        bool, typer.Option("--allow-concurrent-writes", help=ALLOW_CONCURRENT_WRITES_HELP)
    ] = False,
    worktrees: Annotated[bool, typer.Option("--worktrees", help=WORKTREES_HELP)] = False,
    json_output: Annotated[bool, typer.Option("--json", help=JSON_HELP)] = False,
) -> None:
    """Send one message to a new group conversation.

    Example: merced-ai group ask reviewer builder -p "Assess this change" --json
    """
    session = _create_group_session(tuple(bot_names), workspace, mode, worktrees=worktrees)
    turn = _run_group_turn(
        session,
        prompt,
        workspace,
        dispatch=mode,
        allow_concurrent_writes=allow_concurrent_writes,
    )
    results = turn.results
    if json_output:
        typer.echo(
            json.dumps(
                {
                    "session_id": session.id,
                    "write_serialization": {
                        "serialized": turn.serialized,
                        "bots": list(turn.writers),
                    },
                    "worktrees": turn.worktrees,
                    "responses": [
                        {
                            "bot_name": name,
                            **(
                                {"error": str(result)}
                                if isinstance(result, BaseException)
                                else result.model_dump(mode="json")
                            ),
                        }
                        for name, result in results
                    ],
                },
                indent=2,
            )
        )
        return
    _render_group_results(results)


@group_app.command("chat")
def group_chat(
    bot_names: Annotated[list[str], typer.Argument(help="Two or more bot names.")],
    workspace: Annotated[
        Path, typer.Option("--workspace", "-C", help=WORKSPACE_HELP, show_default=False)
    ] = DEFAULT_WORKSPACE,
    mode: Annotated[str, typer.Option(help="mentions, all, or round_robin")] = "mentions",
    allow_concurrent_writes: Annotated[
        bool, typer.Option("--allow-concurrent-writes", help=ALLOW_CONCURRENT_WRITES_HELP)
    ] = False,
    worktrees: Annotated[bool, typer.Option("--worktrees", help=WORKTREES_HELP)] = False,
) -> None:
    """Chat with a group; use @bot, /all, /round-robin, /exit, or /quit.

    Example: merced-ai group chat reviewer builder tester --mode all
    """
    session = _create_group_session(tuple(bot_names), workspace, mode, worktrees=worktrees)
    _group_chat_loop(session, workspace, allow_concurrent_writes=allow_concurrent_writes)


def _create_group_session(
    bot_names: tuple[str, ...], workspace: Path, mode: str, *, worktrees: bool = False
) -> SessionRecord:
    if mode not in {"mentions", "all", "round_robin"}:
        _fail("mode must be mentions, all, or round_robin", 2)
    try:
        prepared = prepare_group(bot_names, workspace)
        return SessionStore(workspace).create_group(
            tuple(participant_from_run(item) for item in prepared),
            mode=cast(Literal["mentions", "all", "round_robin"], mode),
            isolation="worktree" if worktrees else "shared",
        )
    except (ValueError, BotError, ProfileError, RoutingError) as exc:
        _fail(str(exc), 2)


@dataclass
class GroupTurn:
    results: list[tuple[str, RunResult | Exception]]
    writers: tuple[str, ...]
    serialized: bool
    worktrees: dict[str, str] = field(default_factory=dict)


def _run_group_turn(
    session: SessionRecord,
    prompt: str,
    workspace: Path,
    *,
    dispatch: str | None = None,
    allow_concurrent_writes: bool = False,
    registry: HarnessRegistry | None = None,
) -> GroupTurn:
    try:
        prepared_runs = prepare_group_turn(
            session, prompt, workspace, dispatch=dispatch, registry=registry
        )
    except (ValueError, BotError, ProfileError, RoutingError) as exc:
        _fail(str(exc), 2)
    plan = isolate_group_turn(
        session, prepared_runs, workspace, allow_concurrent_writes=allow_concurrent_writes
    )
    prepared_runs = plan.prepared
    writers = shared_workspace_writers(prepared_runs) if not plan.worktrees else ()
    serialize = bool(plan.serialized)
    if plan.worktrees:
        error_console.print(
            f"[cyan]Isolated:[/cyan] {', '.join(plan.worktrees)} each work in their own git "
            f"worktree. Review with `merced-ai group diff {session.id}` and apply with "
            f"`merced-ai group apply {session.id} BOT`."
        )
    if plan.fallback_reason:
        error_console.print(f"[yellow]Warning:[/yellow] {plan.fallback_reason}")
    if serialize:
        writers = plan.serialized
        error_console.print(
            f"[yellow]Warning:[/yellow] {write_serialization_message(writers)} "
            "Pass --allow-concurrent-writes to run them at the same time."
        )
    elif writers:
        error_console.print(
            "[yellow]Warning:[/yellow] concurrent writes allowed: "
            f"{', '.join(writers)} may edit the same workspace at the same time."
        )
    store = SessionStore(workspace)
    store.append(session, "user", prompt)
    # Write-capable bots that share a workspace run one after another in participant order,
    # on one worker; every other participant still runs concurrently.
    serial = [item for item in prepared_runs if serialize and item.bot.name in writers]
    parallel = [item for item in prepared_runs if item not in serial]

    def run_serially() -> dict[str, RunResult | Exception]:
        outcomes: dict[str, RunResult | Exception] = {}
        for item in serial:
            try:
                outcomes[item.bot.name] = _execute(item)
            except Exception as exc:  # A failed writer must not block the next one.
                outcomes[item.bot.name] = exc
        return outcomes

    def run_one(item: PreparedRun) -> dict[str, RunResult | Exception]:
        try:
            return {item.bot.name: _execute(item)}
        except Exception as exc:  # Keep healthy group participants useful on partial failure.
            return {item.bot.name: exc}

    outcomes: dict[str, RunResult | Exception] = {}
    with ThreadPoolExecutor(max_workers=len(parallel) + (1 if serial else 0)) as pool:
        futures = [pool.submit(run_one, item) for item in parallel]
        if serial:
            futures.append(pool.submit(run_serially))
        for future in futures:
            outcomes.update(future.result())
    results: list[tuple[str, RunResult | Exception]] = []
    for prepared in prepared_runs:
        outcome = outcomes[prepared.bot.name]
        if not isinstance(outcome, Exception):
            store.append(
                session,
                "assistant",
                outcome.output,
                bot_name=prepared.bot.name,
                harness_id=outcome.harness_id,
                profile=prepared.profile,
            )
        results.append((prepared.bot.name, outcome))
    return GroupTurn(results, writers, serialize, plan.worktrees)


def _render_group_results(results: list[tuple[str, RunResult | Exception]]) -> None:
    for bot_name, result in results:
        console.print(f"\n[bold violet]{bot_name}>[/bold violet]")
        if isinstance(result, BaseException):
            console.print(f"[red]{result}[/red]")
        else:
            console.print(Markdown(result.output))
            console.print(f"[dim]{result.harness_id} · {result.duration_ms} ms[/dim]")


def _group_chat_loop(
    session: SessionRecord, workspace: Path, *, allow_concurrent_writes: bool = False
) -> None:
    names = ", ".join(item.bot_name for item in session.participants)
    console.print(f"Group chat with [bold]{names}[/bold] · {session.mode} · {session.id}")
    registry = default_registry()  # Reused across turns so routing probes are cached.
    while True:
        try:
            prompt = console.input("[bold cyan]you>[/bold cyan] ").strip()
        except (EOFError, KeyboardInterrupt):
            break
        if not prompt:
            continue
        if prompt in {"/exit", "/quit"}:
            break
        dispatch = None
        if prompt.startswith("/all "):
            dispatch, prompt = "all", prompt[5:].strip()
        elif prompt.startswith("/round-robin "):
            dispatch, prompt = "round_robin", prompt[13:].strip()
        turn = _run_group_turn(
            session,
            prompt,
            workspace,
            dispatch=dispatch,
            allow_concurrent_writes=allow_concurrent_writes,
            registry=registry,
        )
        _render_group_results(turn.results)


def _worktree_manager(session_id: str, workspace: Path) -> WorktreeManager:
    try:
        session = SessionStore(workspace).load(session_id)
    except ValueError as exc:
        _fail(str(exc), 2)
    if session.isolation != "worktree":
        _fail(
            f"conversation {session_id!r} does not use worktrees; start one with "
            "`merced-ai group chat BOT BOT --worktrees`",
            2,
        )
    try:
        return WorktreeManager(workspace, session_id)
    except WorktreeError as exc:
        _fail(str(exc), 2)


@group_app.command("diff")
def group_diff(
    session_id: Annotated[str, typer.Argument(help="Conversation ID from `session list`.")],
    bot: Annotated[str | None, typer.Argument(help="Show one bot's full patch.")] = None,
    workspace: Annotated[
        Path, typer.Option("--workspace", "-C", help=WORKSPACE_HELP, show_default=False)
    ] = DEFAULT_WORKSPACE,
    json_output: Annotated[bool, typer.Option("--json", help=JSON_HELP)] = False,
) -> None:
    """Compare what each bot changed in its worktree.

    Example: merced-ai group diff session-1234... builder
    """
    manager = _worktree_manager(session_id, workspace)
    names = [bot] if bot else [item.bot_name for item in manager.worktrees()]
    try:
        diffs = [manager.diff(name) for name in names]
    except WorktreeError as exc:
        _fail(str(exc), 2)
    if json_output:
        typer.echo(json.dumps([item.as_dict() for item in diffs], indent=2))
        return
    if not diffs:
        console.print("No bot has worked in a worktree in this conversation yet.")
        return
    table = Table(title="Changes by bot")
    for column in ("Bot", "Branch", "Files", "+", "-"):
        table.add_column(column)
    for item in diffs:
        summary = item.as_dict()
        table.add_row(
            item.bot_name,
            item.branch,
            str(len(item.files)),
            str(summary["insertions"]),
            str(summary["deletions"]),
        )
    console.print(table)
    if bot and diffs[0].patch:
        console.print(diffs[0].patch, markup=False, highlight=False)
        if diffs[0].truncated:
            console.print("[yellow]Patch truncated; see the branch for the rest.[/yellow]")


@group_app.command("apply")
def group_apply(
    session_id: Annotated[str, typer.Argument(help="Conversation ID from `session list`.")],
    bot: Annotated[str, typer.Argument(help="The bot whose changes to apply.")],
    workspace: Annotated[
        Path, typer.Option("--workspace", "-C", help=WORKSPACE_HELP, show_default=False)
    ] = DEFAULT_WORKSPACE,
    yes: Annotated[bool, typer.Option("--yes", "-y", help="Apply without asking.")] = False,
    json_output: Annotated[bool, typer.Option("--json", help=JSON_HELP)] = False,
) -> None:
    """Apply one bot's worktree changes to your workspace, only if they apply cleanly."""
    manager = _worktree_manager(session_id, workspace)
    try:
        diff = manager.diff(bot)
        if not yes and not json_output:
            console.print(f"{bot} changed {len(diff.files)} file(s):")
            for item in diff.files:
                line = f"  {escape(item['path'])} (+{item['insertions']} -{item['deletions']})"
                if item.get("symlink") is not None:
                    line += f" symlink -> {escape(item['symlink'])}"
                if item.get("unsafe"):
                    line += " [red](points outside the repository; apply will refuse)[/red]"
                console.print(line)
            if not typer.confirm("Apply these changes to your workspace?", default=False):
                console.print("Nothing was applied.")
                raise typer.Exit(1)
        result = manager.apply(bot)
    except WorktreeError as exc:
        _fail(str(exc), 3)
    if json_output:
        typer.echo(json.dumps(result, indent=2))
    else:
        console.print(result["message"])


@group_app.command("cleanup")
def group_cleanup(
    session_id: Annotated[str, typer.Argument(help="Conversation ID from `session list`.")],
    workspace: Annotated[
        Path, typer.Option("--workspace", "-C", help=WORKSPACE_HELP, show_default=False)
    ] = DEFAULT_WORKSPACE,
    yes: Annotated[bool, typer.Option("--yes", "-y", help="Remove without asking.")] = False,
) -> None:
    """Delete this conversation's bot worktrees and branches (unapplied changes are lost)."""
    manager = _worktree_manager(session_id, workspace)
    names = [item.bot_name for item in manager.worktrees()]
    if not names:
        console.print("There are no worktrees to remove.")
        return
    if not yes and not typer.confirm(
        f"Remove the worktrees and branches for {', '.join(names)}?", default=False
    ):
        console.print("Nothing was removed.")
        raise typer.Exit(1)
    manager.remove_all()
    console.print(f"Removed worktrees for {', '.join(names)}.")
