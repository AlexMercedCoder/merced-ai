"""Shared console, help text, rendering helpers, and error handling for the CLI."""

from __future__ import annotations

import getpass
from pathlib import Path
from typing import Annotated, Any, NoReturn

import typer
from rich.console import Console
from rich.table import Table

from merced_ai import __version__
from merced_ai.application import (
    PreparedRun,
)
from merced_ai.bots import BotError
from merced_ai.cli.apps import (
    app,
)
from merced_ai.harnesses.registry import HarnessRegistry
from merced_ai.models import (
    HarnessCapabilities,
    HarnessProbe,
    ProfileProjection,
    RunResult,
)
from merced_ai.profiles import (
    ProfileError,
)

console = Console()
error_console = Console(stderr=True)
DEFAULT_WORKSPACE = Path.cwd()
WORKSPACE_HELP = "Project directory to use. Defaults to the current directory."
JSON_HELP = "Print machine-readable JSON instead of formatted output."
WORKTREES_HELP = (
    "Give each write-capable bot its own git worktree and branch so they can work at the same "
    "time without touching your files; review with `group diff` and apply with `group apply`."
)
ALLOW_CONCURRENT_WRITES_HELP = (
    "Let write-capable bots that share a workspace run at the same time. By default their "
    "turns run one at a time so they cannot edit the same files concurrently."
)


_CAPABILITY_NAMES = {
    "streaming": "streaming",
    "resume": "native resume",
    "approvals": "approvals",
    "attachments": "attachments",
    "model_listing": "model listing",
    "native_oap": "native OAP",
    "webmcp": "WebMCP",
}


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


def _actor() -> str:
    try:
        return getpass.getuser()
    except (KeyError, OSError):  # pragma: no cover - no login name available
        return "local-user"


def _print_plugin_errors(registry: HarnessRegistry) -> None:
    for name, reason in registry.plugin_errors:
        error_console.print(
            f"[yellow]Warning:[/yellow] harness plugin {name!r} was not loaded: {reason}. "
            "Upgrade or uninstall the package that provides it."
        )


def _render_probe_table(probes: tuple[HarnessProbe, ...]) -> None:
    table = Table(title="Merced AI harness inventory")
    table.add_column("Harness", no_wrap=True)
    table.add_column("Status", no_wrap=True)
    table.add_column("Version")
    table.add_column("Merced AI implements")
    table.add_column("Executable", overflow="fold")
    for probe in probes:
        version = probe.version if probe.status.value != "probe_failed" else None
        table.add_row(
            probe.harness_id,
            probe.status.value,
            (version or "-").splitlines()[0],
            ", ".join(_capability_labels(probe.broker_implements, probe)) or "-",
            _display_path(probe.path),
        )
    console.print(table)


def _display_path(path: Path | None) -> str:
    if path is None:
        return "-"
    try:
        return "~/" + path.relative_to(Path.home()).as_posix()
    except (ValueError, RuntimeError):
        return str(path)


def _capability_labels(capabilities: HarnessCapabilities, probe: HarnessProbe) -> list[str]:
    """Human labels for what the broker delivers; never the harness's own advertised features."""
    labels = []
    if capabilities.native_oap:
        labels.append("native OAP")
    if capabilities.approvals:
        labels.append("AAIS approval relay")
    if capabilities.attachments:
        labels.append("context files")
    if capabilities.webmcp:
        labels.append("WebMCP" if probe.capabilities_verified else "WebMCP (unverified)")
    if capabilities.streaming:
        labels.append("streaming")
    if capabilities.resume:
        labels.append("native resume")
    if capabilities.model_listing:
        labels.append("model listing")
    return labels


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


def _fail(message: str, code: int) -> NoReturn:
    error_console.print(f"[red]Error:[/red] {message}")
    raise typer.Exit(code=code)


def _execute(prepared: PreparedRun, registry: HarnessRegistry | None = None) -> RunResult:
    """Run a prepared turn through ``merced_ai.cli.execute`` (the documented patch point).

    Harnesses that relay approval requests (MagAgent, Loro) get a terminal prompt when stdin and
    stderr are terminals, and a one-line explained denial otherwise.
    """
    import merced_ai.cli as package
    from merced_ai.cli.approvals import approval_handler
    from merced_ai.harnesses.registry import default_registry

    adapter = (registry or default_registry()).get(prepared.request.harness_id)
    if not getattr(adapter, "relays_approvals", False):
        return package.execute(prepared)
    return package.execute(
        prepared,
        approval_handler=approval_handler(prepared.request.workspace, bot=prepared.bot.name),
    )
