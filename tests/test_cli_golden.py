"""Golden files for every CLI `--help` screen and every `--json` output shape.

Captured before the CLI was split into a package (S-6) so the split provably changes nothing a
user or script can see. Regenerate intentionally with ``MERCED_AI_UPDATE_GOLDEN=1 pytest
tests/test_cli_golden.py`` and review the diff.
"""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path
from typing import Any

import pytest
import typer
import yaml
from typer.testing import CliRunner

from merced_ai.cli import app
from merced_ai.harnesses.adapters.command import CommandHarnessAdapter
from merced_ai.models import HarnessProbe, HarnessStatus, RunResult

GOLDEN = Path(__file__).parent / "golden"
UPDATE = os.environ.get("MERCED_AI_UPDATE_GOLDEN") == "1"
ENV = {"COLUMNS": "100", "NO_COLOR": "1", "TERM": "dumb"}
runner = CliRunner()


def _command_paths() -> list[tuple[str, ...]]:
    root = typer.main.get_command(app)
    paths: list[tuple[str, ...]] = [()]

    def walk(command: Any, prefix: tuple[str, ...]) -> None:
        # Typer vendors its own click, so test for sub-commands rather than a click class.
        subcommands = getattr(command, "commands", None)
        if isinstance(subcommands, dict):
            for name in sorted(subcommands):
                paths.append((*prefix, name))
                walk(subcommands[name], (*prefix, name))

    walk(root, ())
    return paths


def _check(name: str, actual: str) -> None:
    path = GOLDEN / name
    if UPDATE or not path.exists():
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(actual, encoding="utf-8")
        if not UPDATE:
            pytest.fail(f"created missing golden file {name}; review and commit it")
        return
    assert actual == path.read_text(encoding="utf-8"), f"{name} changed"


@pytest.mark.parametrize("path", _command_paths(), ids=lambda p: " ".join(p) or "root")
def test_help_screens_are_unchanged(path: tuple[str, ...]) -> None:
    result = runner.invoke(app, [*path, "--help"], env=ENV, prog_name="merced-ai")
    assert result.exit_code == 0, result.output
    _check(f"help/{'-'.join(path) or 'root'}.txt", result.stdout)


def shape(value: Any) -> Any:
    """The structure of a JSON value with its data replaced by type names."""
    if isinstance(value, dict):
        return {key: shape(item) for key, item in sorted(value.items())}
    if isinstance(value, list):
        shapes = []
        for item in value:
            item_shape = shape(item)
            if item_shape not in shapes:
                shapes.append(item_shape)
        return shapes
    return type(value).__name__


@pytest.fixture
def world(workspace: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A workspace with a read-only and a write-capable bot, a git repo, and fake harnesses."""
    monkeypatch.setattr(
        CommandHarnessAdapter,
        "probe",
        lambda adapter, workspace=None: HarnessProbe(
            harness_id=adapter.descriptor.id,
            status=HarnessStatus.READY,
            path=Path("/bin/true"),
            version="1.0",
            transport=adapter.descriptor.transports[-1],
            harness_supports=adapter.descriptor.harness_supports,
            broker_implements=adapter.descriptor.broker_implements,
            prompt_delivery=adapter.descriptor.prompt_delivery,
        ),
    )

    def run(adapter, request, *_args, **_kwargs):  # type: ignore[no-untyped-def]
        return RunResult(
            harness_id=adapter.descriptor.id,
            output="OK",
            exit_code=0,
            duration_ms=1,
            native_session_id="native-1",
        )

    monkeypatch.setattr(CommandHarnessAdapter, "run_cancellable", run)
    for command in (
        ["init", "-q", "-b", "main"],
        ["config", "user.email", "t@e.x"],
        ["config", "user.name", "T"],
    ):
        subprocess.run(["git", *command], cwd=workspace, check=True, capture_output=True)
    (workspace / ".gitignore").write_text(".merced-ai/\n.agents/\n", encoding="utf-8")
    subprocess.run(["git", "add", "."], cwd=workspace, check=True, capture_output=True)
    subprocess.run(
        ["git", "commit", "-q", "-m", "init"], cwd=workspace, check=True, capture_output=True
    )
    cli = [
        [
            "profile",
            "create",
            "reviewer",
            "-d",
            "Reviews code.",
            "-i",
            "Review.",
            "--edit",
            "deny",
            "--shell",
            "deny",
        ],
        ["profile", "create", "builder", "-d", "Builds code.", "-i", "Build."],
        ["bot", "create", "reviewer", "--profile", "reviewer", "--harness", "codex"],
        ["bot", "create", "builder", "--profile", "builder", "--harness", "codex"],
    ]
    for args in cli:
        result = runner.invoke(app, [*args, "-C", str(workspace)], env=ENV)
        assert result.exit_code == 0, result.output
    return workspace


def _json(args: list[str], workspace: Path) -> Any:
    result = runner.invoke(app, [*args, "--json", "-C", str(workspace)], env=ENV)
    assert result.exit_code == 0, result.output
    return json.loads(result.stdout)


def test_json_output_shapes_are_unchanged(world: Path) -> None:
    ws = world
    shapes: dict[str, Any] = {}
    shapes["status"] = shape(_json(["status"], ws))
    shapes["profile list"] = shape(_json(["profile", "list"], ws))
    shapes["profile show"] = shape(_json(["profile", "show", "reviewer"], ws))
    shapes["profile validate"] = shape(
        json.loads(
            runner.invoke(
                app,
                ["profile", "validate", str(ws / ".agents" / "reviewer.agent.yaml"), "--json"],
                env=ENV,
            ).stdout
        )
    )
    shapes["profile effective"] = shape(
        _json(["profile", "effective", "reviewer", "--harness", "codex"], ws)
    )
    shapes["bot list"] = shape(_json(["bot", "list"], ws))
    shapes["bot show"] = shape(_json(["bot", "show", "reviewer"], ws))
    shapes["ask --dry-run"] = shape(_json(["ask", "reviewer", "hi", "--dry-run"], ws))
    asked = _json(["ask", "reviewer", "hi"], ws)
    shapes["ask"] = shape(asked)
    shapes["session list"] = shape(_json(["session", "list"], ws))
    shapes["session show"] = shape(_json(["session", "show", asked["session_id"]], ws))
    group = _json(["group", "ask", "reviewer", "builder", "-p", "Go", "--worktrees"], ws)
    shapes["group ask"] = shape(group)
    shapes["group diff"] = shape(_json(["group", "diff", group["session_id"]], ws))
    runner.invoke(app, ["inbox", "remember", "reviewer", "Use pytest", "-C", str(ws)], env=ENV)
    items = _json(["inbox", "list"], ws)
    shapes["inbox list"] = shape(items)
    shapes["inbox show"] = shape(_json(["inbox", "show", items[0]["id"]], ws))
    shapes["inbox approve"] = shape(_json(["inbox", "approve", items[0]["id"], "--yes"], ws))
    evaluated = _json(
        ["eval", "run", "-p", "reviewer", "--prompt", "x", "-H", "codex", "--contains", "OK"], ws
    )
    shapes["eval run"] = shape(evaluated)
    shapes["eval list"] = shape(_json(["eval", "list"], ws))
    shapes["eval show"] = shape(_json(["eval", "show", evaluated["id"]], ws))
    harnesses = runner.invoke(app, ["harness", "list", "--json"], env=ENV)
    shapes["harness list"] = shape(json.loads(harnesses.stdout))
    shown = runner.invoke(app, ["harness", "show", "codex", "--json"], env=ENV)
    shapes["harness show"] = shape(json.loads(shown.stdout))
    _check("json-shapes.yaml", yaml.safe_dump(shapes, sort_keys=True))
