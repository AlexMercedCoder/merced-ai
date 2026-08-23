from __future__ import annotations

import json
from pathlib import Path

import pytest
from typer.testing import CliRunner

from merced_ai.cli import app
from merced_ai.models import HarnessProbe, HarnessStatus
from merced_ai.profiles import resolve_profile
from merced_ai.sessions import SessionStore

runner = CliRunner()


def test_cli_profile_bot_and_dry_run(workspace: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "merced_ai.harnesses.adapters.command.CommandHarnessAdapter.probe",
        lambda adapter: HarnessProbe(
            harness_id=adapter.descriptor.id,
            status=HarnessStatus.READY,
            path=Path(adapter.descriptor.executable_names[0]),
            transport=adapter.descriptor.transports[0],
            capabilities=adapter.descriptor.capabilities,
        ),
    )
    created = runner.invoke(
        app,
        [
            "profile",
            "create",
            "reviewer",
            "--description",
            "Reviews code for concrete correctness defects before merge.",
            "--instructions",
            "Review code and report defects without editing files.",
            "-C",
            str(workspace),
        ],
    )
    assert created.exit_code == 0, created.output

    bot = runner.invoke(
        app,
        [
            "bot",
            "create",
            "reviewer",
            "--profile",
            "reviewer",
            "--harness",
            "codex",
            "-C",
            str(workspace),
        ],
    )
    assert bot.exit_code == 0, bot.output

    dry_run = runner.invoke(
        app,
        ["ask", "reviewer", "Review README.md", "--dry-run", "--json", "-C", str(workspace)],
    )
    assert dry_run.exit_code == 0, dry_run.output
    payload = json.loads(dry_run.output)
    assert payload["bot"]["name"] == "reviewer"
    assert payload["projection"]["harness_id"] == "codex"
    assert payload["projection"]["support_level"] == "degraded"

    for command in (
        ["status", "--json", "-C", str(workspace)],
        ["profile", "list", "--json", "-C", str(workspace)],
        ["profile", "show", "reviewer", "--json", "-C", str(workspace)],
        [
            "profile",
            "effective",
            "reviewer",
            "--harness",
            "claude",
            "--json",
            "-C",
            str(workspace),
        ],
        ["bot", "list", "--json", "-C", str(workspace)],
        ["bot", "show", "reviewer", "--json", "-C", str(workspace)],
        ["session", "list", "--json", "-C", str(workspace)],
    ):
        result = runner.invoke(app, command)
        assert result.exit_code == 0, result.output

    profile = resolve_profile("reviewer", workspace)
    store = SessionStore(workspace)
    session = store.create("reviewer", "codex", profile)
    store.append(session, "user", "Saved question")
    shown = runner.invoke(app, ["session", "show", session.id, "--json", "-C", str(workspace)])
    assert shown.exit_code == 0, shown.output
    assert json.loads(shown.output)["turns"][0]["content"] == "Saved question"


def test_cli_harness_inventory_and_doctor() -> None:
    inventory = runner.invoke(app, ["harness", "list", "--json"])
    assert inventory.exit_code == 0, inventory.output
    assert any(item["harness_id"] == "codex" for item in json.loads(inventory.output))

    shown = runner.invoke(app, ["harness", "show", "codex", "--json"])
    assert shown.exit_code == 0, shown.output

    doctor = runner.invoke(app, ["doctor"])
    assert doctor.exit_code == 0, doctor.output
    assert "Detected" in doctor.output


def test_cli_rejects_unknown_harness(workspace: Path) -> None:
    result = runner.invoke(
        app,
        [
            "bot",
            "create",
            "reviewer",
            "--profile",
            "missing",
            "--harness",
            "not-real",
            "-C",
            str(workspace),
        ],
    )
    assert result.exit_code == 2
    assert "Unknown harness" in result.output
