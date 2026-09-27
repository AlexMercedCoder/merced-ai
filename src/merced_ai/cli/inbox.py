"""Review OAP state deltas."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Annotated, Any

import typer
from rich.table import Table

from merced_ai.cli.apps import (
    inbox_app,
)
from merced_ai.cli.common import (
    DEFAULT_WORKSPACE,
    JSON_HELP,
    WORKSPACE_HELP,
    _actor,
    _fail,
    console,
)
from merced_ai.inbox import DeltaInbox, InboxError
from merced_ai.profiles import (
    ProfileError,
)


def _inbox(workspace: Path) -> DeltaInbox:
    return DeltaInbox(workspace)


def _render_inbox_item(item: dict[str, Any]) -> None:
    delta = item["delta"]
    console.print(
        f"[bold]{item['id']}[/bold] · {item['profile']} · [cyan]{item['status']}[/cyan] · "
        f"from {item['source']}"
    )
    if delta.get("summary"):
        console.print(delta["summary"])
    target = delta.get("target", {})
    console.print(f"[dim]Targets revision {target.get('revision')} of {target.get('name')}[/dim]")
    for op in delta.get("operations") or []:
        value = json.dumps(op.get("value"))[:160] if "value" in op else ""
        console.print(f"  {op['op']} {op['path']} {value}")
        if op.get("reason"):
            console.print(f"    [dim]{op['reason']}[/dim]")
    for proposal in item["proposals"]:
        color = "red" if proposal["risk"] == "high" else "yellow"
        console.print(
            f"  [bold]Proposal {proposal['index']}[/bold] [{color}]{proposal['risk']} risk"
            f"[/{color}] · {proposal['status']}: {proposal['op']} {proposal['path']} "
            f"{json.dumps(proposal.get('value'))[:160]}"
        )
        console.print(f"    [dim]{proposal['rationale']}[/dim]")
    if item.get("conflict"):
        console.print(f"[yellow]Conflict:[/yellow] {item['conflict']['message']}")


@inbox_app.command("list")
def inbox_list(
    status: Annotated[
        str | None, typer.Option(help="Only pending, conflict, applied, or rejected items.")
    ] = None,
    workspace: Annotated[
        Path, typer.Option("--workspace", "-C", help=WORKSPACE_HELP, show_default=False)
    ] = DEFAULT_WORKSPACE,
    json_output: Annotated[bool, typer.Option("--json", help=JSON_HELP)] = False,
) -> None:
    """List state deltas waiting for review (and past decisions)."""
    items = _inbox(workspace).items(status)
    if json_output:
        typer.echo(json.dumps(items, indent=2))
        return
    if not items:
        console.print("The inbox is empty. Import a delta with `merced-ai inbox add FILE`.")
        return
    table = Table(title="OAP state inbox")
    for column in ("ID", "Profile", "Status", "Operations", "Proposals", "Source"):
        table.add_column(column, overflow="fold")
    for item in items:
        table.add_row(
            item["id"],
            item["profile"],
            item["status"],
            str(len(item["delta"].get("operations") or [])),
            str(len(item["proposals"])),
            item["source"],
        )
    console.print(table)


@inbox_app.command("show")
def inbox_show(
    item_id: Annotated[str, typer.Argument(help="Inbox item ID from `inbox list`.")],
    workspace: Annotated[
        Path, typer.Option("--workspace", "-C", help=WORKSPACE_HELP, show_default=False)
    ] = DEFAULT_WORKSPACE,
    json_output: Annotated[bool, typer.Option("--json", help=JSON_HELP)] = False,
) -> None:
    """Show one delta's operations, proposals, and history."""
    try:
        item = _inbox(workspace).get(item_id)
    except InboxError as exc:
        _fail(str(exc), 2)
    if json_output:
        typer.echo(json.dumps(item, indent=2))
    else:
        _render_inbox_item(item)


@inbox_app.command("add")
def inbox_add(
    path: Annotated[Path, typer.Argument(help="AgentStateDelta YAML/JSON, or a proposal JSON.")],
    workspace: Annotated[
        Path, typer.Option("--workspace", "-C", help=WORKSPACE_HELP, show_default=False)
    ] = DEFAULT_WORKSPACE,
) -> None:
    """Import a delta into the inbox after validating it. Nothing is applied yet."""
    try:
        item = _inbox(workspace).add_file(path)
    except (InboxError, OSError, ValueError) as exc:
        _fail(str(exc), 2)
    console.print(f"Added [bold]{item['id']}[/bold] for {item['profile']}. Review it with:")
    console.print(f"  merced-ai inbox show {item['id']}")


@inbox_app.command("remember")
def inbox_remember(
    profile: Annotated[str, typer.Argument(help="Profile name.")],
    text: Annotated[str, typer.Argument(help="What the profile should remember.")],
    kind: Annotated[str, typer.Option(help="fact, preference, or thread")] = "fact",
    workspace: Annotated[
        Path, typer.Option("--workspace", "-C", help=WORKSPACE_HELP, show_default=False)
    ] = DEFAULT_WORKSPACE,
) -> None:
    """Propose a state entry from your own statement; it still waits in the inbox for approval.

    Example: merced-ai inbox remember reviewer "Use pytest, not unittest" --kind preference
    """
    try:
        item = _inbox(workspace).remember(profile, text, kind=kind, actor=_actor())
    except (InboxError, ProfileError) as exc:
        _fail(str(exc), 2)
    console.print(
        f"Queued [bold]{item['id']}[/bold]. Apply it with `merced-ai inbox approve {item['id']}`."
    )


@inbox_app.command("approve")
def inbox_approve(
    item_id: Annotated[str, typer.Argument(help="Inbox item ID.")],
    rebase: Annotated[
        bool,
        typer.Option("--rebase", help="Re-target an id-addressed delta at the current revision."),
    ] = False,
    yes: Annotated[bool, typer.Option("--yes", "-y", help="Apply without asking.")] = False,
    workspace: Annotated[
        Path, typer.Option("--workspace", "-C", help=WORKSPACE_HELP, show_default=False)
    ] = DEFAULT_WORKSPACE,
    json_output: Annotated[bool, typer.Option("--json", help=JSON_HELP)] = False,
) -> None:
    """Apply a delta's state operations. Proposals are decided separately with `inbox proposal`."""
    inbox = _inbox(workspace)
    try:
        if not yes and not json_output:
            _render_inbox_item(inbox.get(item_id))
            if not typer.confirm("Apply these state operations?", default=False):
                console.print("Nothing was applied.")
                raise typer.Exit(1)
        item = inbox.approve(item_id, actor=_actor(), rebase=rebase)
    except InboxError as exc:
        _fail(str(exc), 2)
    if json_output:
        typer.echo(json.dumps(item, indent=2))
        return
    if item["status"] == "conflict":
        hint = " Retry with --rebase." if item["conflict"]["rebaseable"] else ""
        _fail(f"{item['conflict']['message']}.{hint}", 3)
    console.print(
        f"Applied to {item['profile']} (now revision {item['applied']['revision']})."
        + (
            f" {len(item['proposals'])} proposal(s) still need a decision."
            if item["proposals"]
            else ""
        )
    )
    for warning in item["applied"]["warnings"]:
        console.print(f"[yellow]Warning:[/yellow] {warning}")


@inbox_app.command("reject")
def inbox_reject(
    item_id: Annotated[str, typer.Argument(help="Inbox item ID.")],
    reason: Annotated[str, typer.Option(help="Why, for the record.")] = "",
    workspace: Annotated[
        Path, typer.Option("--workspace", "-C", help=WORKSPACE_HELP, show_default=False)
    ] = DEFAULT_WORKSPACE,
) -> None:
    """Reject a delta and its proposals; the profile is not changed."""
    try:
        _inbox(workspace).reject(item_id, actor=_actor(), reason=reason)
    except InboxError as exc:
        _fail(str(exc), 2)
    console.print("Rejected.")


@inbox_app.command("proposal")
def inbox_proposal(
    item_id: Annotated[str, typer.Argument(help="Inbox item ID.")],
    index: Annotated[int, typer.Argument(help="Proposal number from `inbox show`.")],
    approve: Annotated[
        bool, typer.Option("--approve/--decline", help="Apply or decline this one proposal.")
    ] = False,
    workspace: Annotated[
        Path, typer.Option("--workspace", "-C", help=WORKSPACE_HELP, show_default=False)
    ] = DEFAULT_WORKSPACE,
) -> None:
    """Decide one proposal to change the profile's metadata or spec (never automatic)."""
    try:
        item = _inbox(workspace).decide_proposal(item_id, index, approve=approve, actor=_actor())
    except InboxError as exc:
        _fail(str(exc), 2)
    proposal = next(p for p in item["proposals"] if p["index"] == index)
    console.print(f"Proposal {index} {proposal['status']}.")
