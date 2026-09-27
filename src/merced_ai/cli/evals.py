"""Cross-harness evals."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Annotated, Any

import typer
import yaml
from rich.table import Table

from merced_ai.cli.apps import (
    eval_app,
)
from merced_ai.cli.common import (
    DEFAULT_WORKSPACE,
    JSON_HELP,
    WORKSPACE_HELP,
    _fail,
    console,
    error_console,
)
from merced_ai.harnesses import default_registry
from merced_ai.profiles import (
    ProfileError,
)


def _render_eval(record: dict[str, Any]) -> None:
    spec = record["spec"]
    console.print(
        f"[bold]{record['id']}[/bold] · profile {spec['profile']} · "
        f"{len(spec['harnesses'])} harness(es) · {record['duration_ms'] / 1000:.1f}s"
        + (" · run one at a time (write-capable profile)" if record["sequential"] else "")
    )
    table = Table(title="Results (best first)")
    for column in ("Rank", "Harness", "Status", "Checks", "Judge", "Time"):
        table.add_column(column)
    by_harness = {item["harness"]: item for item in record["results"]}
    order = record["ranking"] + [h for h in spec["harnesses"] if h not in record["ranking"]]
    for position, harness in enumerate(order, start=1):
        item = by_harness[harness]
        checks = item["checks"]
        passed = sum(check["passed"] for check in checks)
        table.add_row(
            str(position) if item["status"] == "ok" else "-",
            harness,
            item["status"] if item["status"] == "ok" else f"{item['status']}: {item['error']}",
            f"{passed}/{len(checks)}" if checks else "-",
            f"{item['judge_score']:.1f}" if item["judge_score"] is not None else "-",
            f"{item['duration_ms'] / 1000:.1f}s",
        )
    console.print(table)
    for item in record["results"]:
        if item["status"] != "ok":
            continue
        console.print(f"\n[bold violet]{item['harness']}>[/bold violet]")
        console.print(item["output"][:2000], markup=False, highlight=False)
        for check in item["checks"]:
            mark = "[green]pass[/green]" if check["passed"] else "[red]fail[/red]"
            console.print(f"  {mark} {check['label']}")
        if item["judge_reason"]:
            console.print(f"  [dim]judge: {item['judge_reason']}[/dim]")


@eval_app.command("run")
def eval_run(
    profile: Annotated[str | None, typer.Option("--profile", "-p", help="Profile name.")] = None,
    prompt: Annotated[str | None, typer.Option("--prompt", help="The task to send.")] = None,
    harness: Annotated[
        list[str] | None, typer.Option("--harness", "-H", help="Harness to compare (repeat).")
    ] = None,
    contains: Annotated[
        list[str] | None, typer.Option("--contains", help="Reply must contain this.")
    ] = None,
    not_contains: Annotated[
        list[str] | None, typer.Option("--not-contains", help="Reply must not contain this.")
    ] = None,
    regex: Annotated[
        list[str] | None, typer.Option("--regex", help="Reply must match this pattern.")
    ] = None,
    exact: Annotated[str | None, typer.Option("--exact", help="Reply must equal this.")] = None,
    max_chars: Annotated[
        int | None, typer.Option("--max-chars", help="Reply must be at most this long.")
    ] = None,
    json_reply: Annotated[
        bool, typer.Option("--json-reply", help="Reply must be valid JSON.")
    ] = False,
    judge: Annotated[
        str | None, typer.Option("--judge", help="Harness that scores each reply 0-10.")
    ] = None,
    rubric: Annotated[str | None, typer.Option("--rubric", help="What the judge grades.")] = None,
    spec_file: Annotated[
        Path | None, typer.Option("--file", "-f", help="YAML or JSON eval spec instead of flags.")
    ] = None,
    workspace: Annotated[
        Path, typer.Option("--workspace", "-C", help=WORKSPACE_HELP, show_default=False)
    ] = DEFAULT_WORKSPACE,
    json_output: Annotated[bool, typer.Option("--json", help=JSON_HELP)] = False,
) -> None:
    """Send one prompt through several harnesses with the same profile and score the replies.

    Example: merced-ai eval run -p reviewer --prompt "Say OK" -H claude -H codex --contains OK
    """
    from pydantic import ValidationError

    from merced_ai.evals import EvalCheck, EvalJudge, EvalRunner, EvalSpec

    try:
        if spec_file is not None:
            loaded = yaml.safe_load(spec_file.read_text(encoding="utf-8"))
            spec = EvalSpec.model_validate(loaded)
        else:
            if not (profile and prompt and harness):
                _fail("give --profile, --prompt, and at least one --harness (or --file)", 2)
            checks = [EvalCheck(type="contains", value=item) for item in contains or []]
            checks += [EvalCheck(type="not_contains", value=item) for item in not_contains or []]
            checks += [EvalCheck(type="regex", value=item) for item in regex or []]
            if exact is not None:
                checks.append(EvalCheck(type="exact", value=exact))
            if max_chars is not None:
                checks.append(EvalCheck(type="max_chars", value=max_chars))
            if json_reply:
                checks.append(EvalCheck(type="json"))
            if bool(judge) != bool(rubric):
                _fail("--judge and --rubric go together", 2)
            spec = EvalSpec(
                profile=profile,
                prompt=prompt,
                harnesses=list(harness),
                checks=checks,
                judge=EvalJudge(harness=judge, rubric=rubric) if judge and rubric else None,
            )
    except (ValidationError, OSError, yaml.YAMLError) as exc:
        _fail(f"invalid eval: {exc}", 2)
    runner = EvalRunner(workspace, default_registry())

    def progress(outcome: Any) -> None:
        if not json_output:
            error_console.print(f"[dim]{outcome.harness}: {outcome.status}[/dim]")

    try:
        record = runner.run(spec, on_progress=progress)
    except (ProfileError, KeyError) as exc:
        _fail(str(exc), 2)
    if json_output:
        typer.echo(json.dumps(record, indent=2))
    else:
        _render_eval(record)


@eval_app.command("list")
def eval_list(
    workspace: Annotated[
        Path, typer.Option("--workspace", "-C", help=WORKSPACE_HELP, show_default=False)
    ] = DEFAULT_WORKSPACE,
    json_output: Annotated[bool, typer.Option("--json", help=JSON_HELP)] = False,
) -> None:
    """List earlier evals, newest first."""
    from merced_ai.evals import EvalRunner

    records = EvalRunner(workspace, default_registry()).items()
    if json_output:
        typer.echo(json.dumps(records, indent=2))
        return
    if not records:
        console.print("No evals yet. Start one with `merced-ai eval run --help`.")
        return
    table = Table(title="Evals")
    for column in ("ID", "Profile", "Harnesses", "Best", "When"):
        table.add_column(column, overflow="fold")
    for record in records:
        table.add_row(
            record["id"],
            record["spec"]["profile"],
            ", ".join(record["spec"]["harnesses"]),
            record["ranking"][0] if record["ranking"] else "-",
            record["created_at"],
        )
    console.print(table)


@eval_app.command("show")
def eval_show(
    eval_id: Annotated[str, typer.Argument(help="Eval ID from `eval list`.")],
    workspace: Annotated[
        Path, typer.Option("--workspace", "-C", help=WORKSPACE_HELP, show_default=False)
    ] = DEFAULT_WORKSPACE,
    json_output: Annotated[bool, typer.Option("--json", help=JSON_HELP)] = False,
) -> None:
    """Show one eval's replies, checks, and scores."""
    from merced_ai.evals import EvalRunner

    try:
        record = EvalRunner(workspace, default_registry()).get(eval_id)
    except ValueError as exc:
        _fail(str(exc), 2)
    if json_output:
        typer.echo(json.dumps(record, indent=2))
    else:
        _render_eval(record)
