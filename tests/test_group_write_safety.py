"""Write-capable bots that share a workspace take turns unless the user opts out."""

from __future__ import annotations

import asyncio
import json
import threading
import time
from pathlib import Path

import httpx
import pytest
from typer.testing import CliRunner

from merced_ai.application import (
    prepare_group,
    shared_workspace_writers,
    write_serialization_message,
)
from merced_ai.bots import create_bot
from merced_ai.cli import app
from merced_ai.harnesses.adapters.command import CommandHarnessAdapter
from merced_ai.models import HarnessProbe, HarnessStatus, RunResult
from merced_ai.profiles import create_profile
from merced_ai.webui_server import create_web_app

RUN_SECONDS = 0.4
runner = CliRunner()


@pytest.fixture(autouse=True)
def ready_harnesses(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        CommandHarnessAdapter,
        "probe",
        lambda adapter, workspace=None: HarnessProbe(
            harness_id=adapter.descriptor.id,
            status=HarnessStatus.READY,
            path=Path(adapter.descriptor.executable_names[0]),
            transport=adapter.descriptor.transports[0],
            broker_implements=adapter.descriptor.broker_implements,
            capabilities_verified=True,
        ),
    )


def _bots(workspace: Path) -> None:
    """Two write-capable bots (builder, fixer) and one read-only bot (reviewer)."""
    for name, deny in (("builder", False), ("fixer", False), ("reviewer", True)):
        create_profile(
            name,
            f"The {name} participant.",
            f"Act as the {name}.",
            workspace,
            edit_permission="deny" if deny else None,
            shell_permission="deny" if deny else None,
        )
        create_bot(name, name, "codex", (), workspace)


class Recorder:
    """Fake harness run that records when each bot was inside the harness."""

    def __init__(self) -> None:
        self.intervals: dict[str, tuple[float, float]] = {}
        self.lock = threading.Lock()

    def run(self, bot: str, harness_id: str) -> RunResult:
        started = time.monotonic()
        time.sleep(RUN_SECONDS)
        with self.lock:
            self.intervals[bot] = (started, time.monotonic())
        return RunResult(harness_id=harness_id, output=f"{bot} done", exit_code=0, duration_ms=1)

    def overlaps(self, left: str, right: str) -> bool:
        (a_start, a_end), (b_start, b_end) = self.intervals[left], self.intervals[right]
        return a_start < b_end and b_start < a_end


def test_writers_are_detected_only_when_two_share_a_workspace(workspace: Path) -> None:
    _bots(workspace)

    assert shared_workspace_writers(prepare_group(("builder", "fixer", "reviewer"), workspace)) == (
        "builder",
        "fixer",
    )
    assert shared_workspace_writers(prepare_group(("builder", "reviewer"), workspace)) == ()
    assert "builder and fixer can both change" in write_serialization_message(("builder", "fixer"))
    assert "a, b and c can all change" in write_serialization_message(("a", "b", "c"))


@pytest.mark.parametrize("allow", [False, True])
def test_cli_group_serializes_writers_unless_allowed(
    allow: bool, workspace: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _bots(workspace)
    recorder = Recorder()
    monkeypatch.setattr(
        "merced_ai.cli.execute",
        lambda prepared: recorder.run(prepared.bot.name, prepared.request.harness_id),
    )
    args = ["group", "ask", "builder", "fixer", "reviewer", "-p", "Go", "--json"]
    args += ["-C", str(workspace)] + (["--allow-concurrent-writes"] if allow else [])

    result = runner.invoke(app, args)

    assert result.exit_code == 0, result.output
    payload = json.loads(result.stdout)
    assert payload["write_serialization"] == {"serialized": not allow, "bots": ["builder", "fixer"]}
    assert [item["bot_name"] for item in payload["responses"]] == ["builder", "fixer", "reviewer"]
    assert recorder.overlaps("builder", "fixer") is allow
    # The read-only participant never waits for the writers.
    assert recorder.overlaps("builder", "reviewer")
    warning = " ".join(result.stderr.split())  # Rich wraps long lines.
    if allow:
        assert "concurrent writes allowed" in warning
    else:
        assert "run one at a time" in warning
        assert "--allow-concurrent-writes" in warning
        # Participant order is preserved: builder finishes before fixer starts.
        assert recorder.intervals["builder"][1] <= recorder.intervals["fixer"][0]


def test_cli_help_documents_the_opt_out() -> None:
    for command in (["group", "ask", "--help"], ["group", "chat", "--help"]):
        result = runner.invoke(app, command, env={"COLUMNS": "200"})
        assert result.exit_code == 0
        assert "--allow-concurrent-writes" in result.stdout
        assert "one at a time" in " ".join(result.stdout.split())


@pytest.mark.anyio
@pytest.mark.parametrize("allow", [False, True])
async def test_web_group_serializes_writers_unless_allowed(
    allow: bool, workspace: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _bots(workspace)
    recorder = Recorder()

    def fake_run(
        _adapter: CommandHarnessAdapter, request: object, _cancellation: object
    ) -> RunResult:
        return recorder.run(request.profile.name, request.harness_id)  # type: ignore[attr-defined]

    monkeypatch.setattr(CommandHarnessAdapter, "run_cancellable", fake_run)
    transport = httpx.ASGITransport(app=create_web_app(workspace, "token"))
    async with httpx.AsyncClient(transport=transport, base_url="http://127.0.0.1") as client:
        await client.post("/api/auth", json={"token": "token"})
        bootstrap = (await client.get("/api/bootstrap")).json()
        flags = {bot["name"]: bot["write_capable"] for bot in bootstrap["bots"]}
        assert flags == {"builder": True, "fixer": True, "reviewer": False}
        session = await client.post(
            "/api/sessions", json={"bot_names": ["builder", "fixer", "reviewer"], "mode": "all"}
        )
        response = await asyncio.wait_for(
            client.post(
                f"/api/sessions/{session.json()['id']}/messages",
                json={
                    "content": "Go",
                    "approved": True,
                    "dispatch": "all",
                    "allow_concurrent_writes": allow,
                },
            ),
            timeout=60,
        )

    events = [
        (block.split("\n")[1].removeprefix("event: "), block)
        for block in response.text.split("\n\n")
        if block.startswith("id:")
    ]
    names = [name for name, _ in events]
    notice = json.loads(
        next(block for name, block in events if name == "write_serialization").split("data: ")[1]
    )
    assert notice["serialized"] is not allow
    assert notice["bots"] == ["builder", "fixer"]
    assert ("participant_queued" in names) is not allow
    assert names.count("assistant_message") == 3
    assert recorder.overlaps("builder", "fixer") is allow
    assert recorder.overlaps("builder", "reviewer")
