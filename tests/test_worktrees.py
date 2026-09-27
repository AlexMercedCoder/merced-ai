"""Worktree-per-bot group rooms: isolation, compare, apply, cleanup, and fallback."""

from __future__ import annotations

import asyncio
import json
import subprocess
import threading
import time
from pathlib import Path

import httpx
import pytest
from typer.testing import CliRunner

from merced_ai.bots import create_bot
from merced_ai.cli import app
from merced_ai.harnesses.adapters.command import CommandHarnessAdapter
from merced_ai.models import HarnessProbe, HarnessStatus, RunResult
from merced_ai.profiles import create_profile
from merced_ai.sessions import SessionStore
from merced_ai.worktrees import WorktreeError, WorktreeManager, git_toplevel

runner = CliRunner()


def _git(cwd: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=cwd, capture_output=True, text=True, check=True
    ).stdout


@pytest.fixture
def repo(workspace: Path) -> Path:
    _git(workspace, "init", "-q", "-b", "main")
    _git(workspace, "config", "user.email", "test@example.com")
    _git(workspace, "config", "user.name", "Test")
    (workspace / "README.md").write_text("hello\n", encoding="utf-8")
    (workspace / ".gitignore").write_text(".merced-ai/\n.agents/\n", encoding="utf-8")
    _git(workspace, "add", ".")
    _git(workspace, "commit", "-q", "-m", "init")
    return workspace


@pytest.fixture(autouse=True)
def ready(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        CommandHarnessAdapter,
        "probe",
        lambda adapter, workspace=None: HarnessProbe(
            harness_id=adapter.descriptor.id,
            status=HarnessStatus.READY,
            path=Path("/bin/true"),
            broker_implements=adapter.descriptor.broker_implements,
        ),
    )


def _bots(workspace: Path) -> None:
    for name, read_only in (("builder", False), ("fixer", False), ("reviewer", True)):
        create_profile(
            name,
            f"The {name}.",
            f"Act as the {name}.",
            workspace,
            edit_permission="deny" if read_only else None,
            shell_permission="deny" if read_only else None,
        )
        create_bot(name, name, "codex", (), workspace)


class Editor:
    """Fake harness: writes a file named after the bot into the directory it runs in."""

    def __init__(self) -> None:
        self.runs: dict[str, tuple[Path, float, float]] = {}
        self.lock = threading.Lock()

    def run(self, bot: str, workspace: Path, harness_id: str) -> RunResult:
        started = time.monotonic()
        if bot != "reviewer":
            (workspace / f"{bot}.txt").write_text(f"{bot} was here\n", encoding="utf-8")
            (workspace / "README.md").write_text(f"hello from {bot}\n", encoding="utf-8")
        time.sleep(0.3)
        with self.lock:
            self.runs[bot] = (workspace, started, time.monotonic())
        return RunResult(harness_id=harness_id, output=f"{bot} done", exit_code=0, duration_ms=1)

    def overlaps(self, left: str, right: str) -> bool:
        _, a0, a1 = self.runs[left]
        _, b0, b1 = self.runs[right]
        return a0 < b1 and b0 < a1


def test_cli_worktree_room_isolates_compares_applies_and_cleans_up(
    repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _bots(repo)
    editor = Editor()
    monkeypatch.setattr(
        "merced_ai.cli.execute",
        lambda prepared: editor.run(
            prepared.bot.name, prepared.request.workspace, prepared.request.harness_id
        ),
    )

    result = runner.invoke(
        app,
        [
            "group",
            "ask",
            "builder",
            "fixer",
            "reviewer",
            "-p",
            "Go",
            "--worktrees",
            "--json",
            "-C",
            str(repo),
        ],
        env={"COLUMNS": "200"},
    )
    assert result.exit_code == 0, result.output
    payload = json.loads(result.stdout)
    session_id = payload["session_id"]
    assert set(payload["worktrees"]) == {"builder", "fixer"}
    assert payload["write_serialization"]["serialized"] is False
    assert editor.overlaps("builder", "fixer")  # isolated writers run concurrently
    builder_dir, fixer_dir = editor.runs["builder"][0], editor.runs["fixer"][0]
    assert builder_dir != fixer_dir and repo not in (builder_dir, fixer_dir)
    assert editor.runs["reviewer"][0] == repo.resolve()
    assert (repo / "README.md").read_text() == "hello\n"  # user's files untouched
    assert "Isolated:" in result.stderr

    diff = runner.invoke(app, ["group", "diff", session_id, "--json", "-C", str(repo)])
    reports = {item["bot_name"]: item for item in json.loads(diff.stdout)}
    assert {item["path"] for item in reports["builder"]["files"]} == {"README.md", "builder.txt"}
    assert "+hello from builder" in reports["builder"]["patch"]

    applied = runner.invoke(
        app, ["group", "apply", session_id, "builder", "--yes", "--json", "-C", str(repo)]
    )
    assert applied.exit_code == 0, applied.output
    assert json.loads(applied.stdout)["applied"] is True
    assert (repo / "builder.txt").exists()
    assert (repo / "README.md").read_text() == "hello from builder\n"

    conflict = runner.invoke(app, ["group", "apply", session_id, "fixer", "--yes", "-C", str(repo)])
    assert conflict.exit_code == 3
    assert "do not apply cleanly" in " ".join(conflict.stderr.split())
    assert not (repo / "fixer.txt").exists()

    cleaned = runner.invoke(app, ["group", "cleanup", session_id, "--yes", "-C", str(repo)])
    assert cleaned.exit_code == 0, cleaned.output
    assert "merced/" not in _git(repo, "branch", "--list")
    assert not builder_dir.exists()


def test_non_git_workspace_falls_back_to_taking_turns(
    workspace: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _bots(workspace)
    editor = Editor()
    monkeypatch.setattr(
        "merced_ai.cli.execute",
        lambda prepared: editor.run(
            prepared.bot.name, prepared.request.workspace, prepared.request.harness_id
        ),
    )

    result = runner.invoke(
        app,
        [
            "group",
            "ask",
            "builder",
            "fixer",
            "-p",
            "Go",
            "--worktrees",
            "--json",
            "-C",
            str(workspace),
        ],
    )

    assert result.exit_code == 0, result.output
    assert json.loads(result.stdout)["write_serialization"]["serialized"] is True
    assert "not inside a git repository" in " ".join(result.stderr.split())
    assert not editor.overlaps("builder", "fixer")


def test_diff_commands_refuse_rooms_without_worktrees(repo: Path) -> None:
    _bots(repo)
    from merced_ai.application import participant_from_run, prepare_group

    prepared = prepare_group(("builder", "fixer"), repo)
    session = SessionStore(repo).create_group(tuple(participant_from_run(p) for p in prepared))

    result = runner.invoke(app, ["group", "diff", session.id, "-C", str(repo)])

    assert result.exit_code == 2
    assert "does not use worktrees" in " ".join(result.stderr.split())


def test_manager_validates_inputs(repo: Path, workspace: Path) -> None:
    assert git_toplevel(repo) == repo.resolve()
    with pytest.raises(WorktreeError, match="invalid session"):
        WorktreeManager(repo, "../../etc")
    manager = WorktreeManager(repo, "session-" + "a" * 32)
    with pytest.raises(WorktreeError, match="invalid bot name"):
        manager.ensure("../escape")
    with pytest.raises(WorktreeError, match="no worktree"):
        manager.diff("builder")


@pytest.mark.anyio
async def test_web_worktree_room_streams_notice_and_serves_compare_and_apply(
    repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from merced_ai.webui_server import create_web_app

    _bots(repo)
    editor = Editor()

    def fake_run(_adapter, request, _cancellation):  # type: ignore[no-untyped-def]
        return editor.run(request.profile.name, request.workspace, request.harness_id)

    monkeypatch.setattr(CommandHarnessAdapter, "run_cancellable", fake_run)
    transport = httpx.ASGITransport(app=create_web_app(repo, "token"))
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        await client.post("/api/auth", json={"token": "token"})
        session = (
            await client.post(
                "/api/sessions",
                json={"bot_names": ["builder", "fixer"], "mode": "all", "isolation": "worktree"},
            )
        ).json()
        assert session["isolation"] == "worktree"
        streamed = await asyncio.wait_for(
            client.post(
                f"/api/sessions/{session['id']}/messages",
                json={"content": "Go", "approved": True, "dispatch": "all"},
            ),
            60,
        )
        summary = (await client.get(f"/api/sessions/{session['id']}/worktrees")).json()
        patch = (await client.get(f"/api/sessions/{session['id']}/worktrees/fixer/diff")).json()
        applied = await client.post(f"/api/sessions/{session['id']}/worktrees/fixer/apply")
        conflict = await client.post(f"/api/sessions/{session['id']}/worktrees/builder/apply")
        removed = await client.delete(f"/api/sessions/{session['id']}/worktrees")

    assert "event: worktree_isolation" in streamed.text
    assert "event: write_serialization" not in streamed.text
    assert {item["bot_name"] for item in summary["bots"]} == {"builder", "fixer"}
    assert all("patch" not in item for item in summary["bots"])
    assert "+fixer was here" in patch["patch"]
    assert applied.json()["applied"] is True and (repo / "fixer.txt").exists()
    assert conflict.status_code == 409 and "do not apply cleanly" in conflict.json()["detail"]
    assert set(removed.json()["removed"]) == {"builder", "fixer"}
