"""Workspace, harness inventory, and server commands: init, status, doctor, ui, acp, harness."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Annotated

import typer

from merced_ai.bots import discover_bots
from merced_ai.cli.apps import (
    app,
    harness_app,
)
from merced_ai.cli.common import (
    _CAPABILITY_NAMES,
    DEFAULT_WORKSPACE,
    JSON_HELP,
    WORKSPACE_HELP,
    WORKTREES_HELP,
    _capability_labels,
    _fail,
    _print_plugin_errors,
    _render_probe_table,
    console,
    error_console,
)
from merced_ai.harnesses import default_registry
from merced_ai.paths import ensure_project_layout, ensure_user_layout
from merced_ai.profiles import (
    discover_profiles,
)
from merced_ai.sessions import SessionStore


@app.command("init")
def initialize(
    workspace: Annotated[
        Path, typer.Option("--workspace", "-C", help=WORKSPACE_HELP, show_default=False)
    ] = DEFAULT_WORKSPACE,
) -> None:
    """Create project-local Merced AI and OAP directories."""
    root = ensure_project_layout(workspace)
    ensure_user_layout()
    console.print(f"Initialized [bold]{root}[/bold]")


@app.command("status")
def status(
    workspace: Annotated[
        Path, typer.Option("--workspace", "-C", help=WORKSPACE_HELP, show_default=False)
    ] = DEFAULT_WORKSPACE,
    json_output: Annotated[bool, typer.Option("--json", help=JSON_HELP)] = False,
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
    json_output: Annotated[bool, typer.Option("--json", help=JSON_HELP)] = False,
) -> None:
    """List known harnesses and run safe local version probes."""
    registry = default_registry()
    probes = registry.probe_all()
    if json_output:
        typer.echo(json.dumps([probe.model_dump(mode="json") for probe in probes], indent=2))
        return
    _render_probe_table(probes)
    _print_plugin_errors(registry)


@harness_app.command("show")
def harness_show(
    harness_id: Annotated[str, typer.Argument(help="Harness identifier from `harness list`.")],
    json_output: Annotated[bool, typer.Option("--json", help=JSON_HELP)] = False,
) -> None:
    """Show the current probe result for one harness."""
    try:
        adapter = default_registry().get(harness_id)
    except KeyError as exc:
        raise typer.BadParameter(str(exc), param_hint="harness_id") from exc
    probe = adapter.probe()
    if json_output:
        typer.echo(json.dumps(probe.model_dump(mode="json"), indent=2))
        return
    _render_probe_table((probe,))
    spec = getattr(adapter, "spec", None)
    origin = getattr(spec, "origin", "builtin")
    console.print(
        "[bold]Adapter:[/bold] "
        + ("built-in" if origin == "builtin" else f"plugin from {origin} (entry point)")
    )
    console.print(f"[bold]Transport:[/bold] {probe.transport.value if probe.transport else '-'}")
    delivery = probe.prompt_delivery.value if probe.prompt_delivery else "-"
    if delivery == "argv":
        delivery = "command-line argument (size-limited; long prompts are refused)"
    console.print(f"[bold]Prompt delivery:[/bold] {delivery}")
    console.print(
        "[bold]Merced AI implements:[/bold] "
        + (", ".join(_capability_labels(probe.broker_implements, probe)) or "one-shot runs only")
    )
    advertised = [
        _CAPABILITY_NAMES[name]
        for name, enabled in probe.harness_supports.model_dump().items()
        if enabled and not getattr(probe.broker_implements, name) and name != "webmcp"
    ]
    if advertised:
        console.print(
            "[bold]Harness advertises, not used by Merced AI yet:[/bold] " + ", ".join(advertised)
        )
    if probe.harness_supports.webmcp and not probe.broker_implements.webmcp:
        console.print(
            "[bold]WebMCP:[/bold] not available until the installed harness reports readiness"
        )
    for warning in probe.warnings:
        console.print(f"[yellow]Warning:[/yellow] {warning}")


@app.command("doctor")
def doctor() -> None:
    """Summarize local harness availability without changing configuration."""
    registry = default_registry()
    probes = registry.probe_all()
    _print_plugin_errors(registry)
    installed = [probe for probe in probes if probe.path is not None]
    failed = [probe for probe in probes if probe.status.value == "probe_failed"]
    console.print(f"Detected [bold]{len(installed)}[/bold] of {len(probes)} known harnesses.")
    if failed:
        console.print(f"[yellow]{len(failed)} installed harness probe(s) need attention.[/yellow]")
    else:
        console.print("[green]All installed harness version probes completed.[/green]")
    console.print("Authentication is verified only when a real run starts.")


@app.command("ui")
def ui(
    workspace: Annotated[
        Path, typer.Option("--workspace", "-C", help=WORKSPACE_HELP, show_default=False)
    ] = DEFAULT_WORKSPACE,
    host: Annotated[str, typer.Option("--host")] = "127.0.0.1",
    port: Annotated[int, typer.Option("--port", min=1024, max=65535)] = 8765,
    no_open: Annotated[bool, typer.Option("--no-open")] = False,
) -> None:
    """Start the optional loopback-first local web UI."""
    from merced_ai.webui_server import run_web_ui

    try:
        run_web_ui(workspace, host=host, port=port, open_browser=not no_open)
    except (RuntimeError, ValueError) as exc:
        _fail(str(exc), 2)


@app.command("acp")
def acp_serve(
    bot: Annotated[
        list[str],
        typer.Option("--bot", "-b", help="Bot to serve; repeat for a group room (2 to 12)."),
    ],
    workspace: Annotated[
        Path, typer.Option("--workspace", "-C", help=WORKSPACE_HELP, show_default=False)
    ] = DEFAULT_WORKSPACE,
    mode: Annotated[str, typer.Option(help="Room dispatch: mentions, all, or round_robin")] = "all",
    worktrees: Annotated[bool, typer.Option("--worktrees", help=WORKTREES_HELP)] = False,
    allow_resume: Annotated[
        bool,
        typer.Option(
            "--allow-resume",
            help="Let the client continue conversations this process did not start (for example "
            "after the editor restarts). Only conversations whose bots are all served here.",
        ),
    ] = False,
) -> None:
    """Serve a bot or a room as an Agent Client Protocol agent on stdin/stdout.

    Point an ACP client (for example Zed) at this command. Stdout carries only JSON-RPC;
    diagnostics go to stderr.

    Example: merced-ai acp --bot reviewer -C ~/code/project
    """
    from merced_ai.acp_server import MercedAcpAgent

    if mode not in {"mentions", "all", "round_robin"}:
        _fail("mode must be mentions, all, or round_robin", 2)
    try:
        agent = MercedAcpAgent(
            workspace,
            tuple(bot),
            mode=mode,
            isolation="worktree" if worktrees else "shared",
            allow_resume=allow_resume,
        )
    except ValueError as exc:
        _fail(str(exc), 2)
    error_console.print(
        f"Merced AI ACP agent serving {', '.join(bot)} in {workspace.resolve()} (stdio)."
    )
    agent.serve()
