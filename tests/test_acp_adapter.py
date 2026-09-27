"""ACP client adapter against a deterministic fake ACP agent."""

from __future__ import annotations

import json
import os
import stat
import sys
import threading
from pathlib import Path

import pytest
from aais import create_decision, validate

from merced_ai.harnesses.acp import AcpHarnessAdapter, choose_mode
from merced_ai.harnesses.acp_launch import AcpLaunch
from merced_ai.harnesses.adapters.command import HarnessRunError
from merced_ai.harnesses.builtin import BUILTIN_BY_ID
from merced_ai.models import RunRequest, TransportKind
from merced_ai.profiles import create_profile, validate_profile

FAKE = Path(__file__).parent / "fixtures" / "fake_acp_agent.py"


def _launcher(directory: Path) -> Path:
    if os.name == "nt":  # pragma: no cover - Windows CI
        path = directory / "fake-acp.cmd"
        path.write_text(f'@"{sys.executable}" "{FAKE}" %*\r\n', encoding="utf-8")
        return path
    path = directory / "fake-acp"
    path.write_text(f"#!{sys.executable}\n" + FAKE.read_text(encoding="utf-8"), encoding="utf-8")
    path.chmod(path.stat().st_mode | stat.S_IEXEC)
    return path


@pytest.fixture
def agent(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[AcpHarnessAdapter, Path]:
    monkeypatch.setenv("MERCED_AI_ACP", "1")
    monkeypatch.setenv("MERCED_AI_GEMINI_ACP_PATH", str(_launcher(tmp_path)))
    monkeypatch.setenv("MERCED_AI_GEMINI_PATH", str(_launcher(tmp_path)))
    log = tmp_path / "acp-log.jsonl"
    monkeypatch.setenv("FAKE_ACP_LOG", str(log))
    adapter = AcpHarnessAdapter(
        BUILTIN_BY_ID["gemini"],
        AcpLaunch(executable_names=("fake-acp-not-on-path",), resumes=True),
    )
    return adapter, log


def _request(
    adapter: AcpHarnessAdapter, workspace: Path, prompt: str, *, deny: bool = False, **extra: str
) -> RunRequest:
    name = "reader" if deny else "helper"
    existing = workspace / ".agents" / f"{name}.agent.yaml"
    profile = (
        validate_profile(existing, "project")
        if existing.exists()
        else create_profile(
            name,
            "Helps with the workspace.",
            "Be concise.",
            workspace,
            edit_permission="deny" if deny else None,
            shell_permission="deny" if deny else None,
        )
    )
    return RunRequest(
        harness_id="gemini",
        prompt=prompt,
        workspace=workspace,
        profile=profile,
        projection=adapter.project_profile(profile),
        timeout_seconds=60,
        **extra,
    )


def _methods(log: Path) -> list[dict]:
    return [json.loads(line) for line in log.read_text().splitlines()]


def test_streams_chunks_and_returns_native_session(agent, workspace: Path) -> None:
    adapter, log = agent
    events: list[dict] = []

    result = adapter.run_cancellable(
        _request(adapter, workspace, "Say hello"), None, on_event=events.append
    )

    assert result.output == "Hello from ACP"
    assert result.native_session_id == "sess-new"
    assert result.raw and result.raw["transport"] == "acp" and result.raw["resumed"] is False
    assert [item["text"] for item in events if item["type"] == "assistant_delta"] == [
        "Hello ",
        "from ACP",
    ]
    sent = _methods(log)
    assert [item.get("method") for item in sent[:3]] == [
        "initialize",
        "session/new",
        "session/prompt",
    ]
    assert sent[0]["params"]["clientCapabilities"]["fs"] == {
        "readTextFile": False,
        "writeTextFile": False,
    }
    prompt_text = sent[2]["params"]["prompt"][0]["text"]
    assert "<open-agent-profile>" in prompt_text and "Say hello" in prompt_text


def test_probe_and_projection_report_acp_honestly(agent, workspace: Path) -> None:
    adapter, _ = agent
    probe = adapter.probe()
    assert probe.transport == TransportKind.ACP_STDIO
    assert probe.broker_implements.streaming and probe.broker_implements.resume
    assert probe.broker_implements.approvals
    projection = _request(adapter, workspace, "x").projection
    assert projection.model is None
    assert any("ACP" in item.reason for item in projection.adjustments)


def test_resume_loads_the_session_and_sends_only_the_new_turn(agent, workspace: Path) -> None:
    adapter, log = agent
    request = _request(
        adapter,
        workspace,
        "FULL TRANSCRIPT",
        native_session_id="sess-old",
        turn_prompt="Just the new message",
    )

    result = adapter.run_cancellable(request, None)

    assert result.output == "Hello from ACP"  # replayed history is not part of the reply
    assert result.raw and result.raw["resumed"] is True
    sent = _methods(log)
    assert sent[1]["method"] == "session/load" and sent[1]["params"]["sessionId"] == "sess-old"
    assert sent[2]["params"]["prompt"][0]["text"] == "Just the new message"


def test_missing_native_session_falls_back_to_a_new_session(agent, workspace: Path) -> None:
    adapter, log = agent
    request = _request(adapter, workspace, "FULL", native_session_id="gone", turn_prompt="new")

    result = adapter.run_cancellable(request, None)

    assert result.native_session_id == "sess-new"
    assert [item.get("method") for item in _methods(log)][1:3] == ["session/load", "session/new"]


def _approve(scope: str):
    seen: list[dict] = []

    def handler(envelope: dict, _cancel: threading.Event | None) -> dict:
        seen.append(validate(envelope))
        return create_decision(
            envelope,
            decision="approve",
            scope=scope,
            actor={"id": "tester", "type": "human", "authenticated_by": "test"},
            sequence=1,
            stream="test",
        )

    return handler, seen


def test_permission_requests_go_through_aais_and_map_to_options(agent, workspace: Path) -> None:
    adapter, _ = agent
    handler, seen = _approve("once")

    result = adapter.run_cancellable(
        _request(adapter, workspace, "PERMISSION:edit please"), None, handler
    )

    assert result.output.startswith("permission=yes ")
    assert seen[0]["request"]["action"]["name"] == "edit"
    assert seen[0]["request"]["origin"]["harness"] == "gemini"
    assert result.raw and result.raw["permissions"][0]["decision"] == "approve"

    handler, _ = _approve("session")
    again = adapter.run_cancellable(_request(adapter, workspace, "PERMISSION:edit"), None, handler)
    assert again.output.startswith("permission=always ")


def test_profile_denial_and_missing_presenter_reject_without_asking(agent, workspace: Path) -> None:
    adapter, _ = agent
    handler, seen = _approve("once")

    denied = adapter.run_cancellable(
        _request(adapter, workspace, "PERMISSION:execute", deny=True), None, handler
    )
    headless = adapter.run_cancellable(_request(adapter, workspace, "PERMISSION:read"), None)

    assert denied.output.startswith("permission=no ") and seen == []
    assert denied.raw and denied.raw["permissions"][0]["by_profile"] is True
    assert headless.output.startswith("permission=no ")


def test_cancellation_sends_session_cancel(agent, workspace: Path) -> None:
    adapter, log = agent
    cancellation = threading.Event()
    threading.Timer(0.5, cancellation.set).start()

    with pytest.raises(HarnessRunError) as error:
        adapter.run_cancellable(_request(adapter, workspace, "SLOW"), cancellation)

    assert error.value.exit_code == 130
    assert "session/cancel" in [item.get("method") for item in _methods(log)]


def test_agent_crash_reports_stderr(agent, workspace: Path) -> None:
    adapter, _ = agent
    with pytest.raises(HarnessRunError, match="crashed on purpose"):
        adapter.run_cancellable(_request(adapter, workspace, "CRASH"), None)


def test_safe_mode_is_selected_and_auto_approve_is_refused(
    agent, workspace: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    adapter, log = agent
    modes = {
        "currentModeId": "auto",
        "availableModes": [{"id": "auto"}, {"id": "approve"}, {"id": "chat"}],
    }
    monkeypatch.setenv("FAKE_ACP_MODES", json.dumps(modes))
    first = adapter.run_cancellable(_request(adapter, workspace, "hi"), None)
    adapter.run_cancellable(_request(adapter, workspace, "hi", deny=True), None)
    assert first.output == "Hello from ACP"  # the mode-change notice is not part of the reply
    chosen = [
        item["params"]["modeId"]
        for item in _methods(log)
        if item.get("method") == "session/set_mode"
    ]
    assert chosen == ["approve", "chat"]

    assert choose_mode({"currentModeId": "default", "availableModes": []}, read_only=False) is None
    with pytest.raises(HarnessRunError, match="auto-approve"):
        choose_mode({"currentModeId": "yolo", "availableModes": [{"id": "yolo"}]}, read_only=False)


def test_acp_off_falls_back_to_the_subprocess_adapter(
    agent, workspace: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    adapter, _ = agent
    monkeypatch.setenv("MERCED_AI_ACP", "0")
    assert adapter.probe().transport == TransportKind.STRUCTURED_SUBPROCESS
    assert not adapter.acp_available()


@pytest.mark.anyio
async def test_web_run_streams_relays_approval_and_resumes_natively(
    tmp_path: Path, workspace: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import asyncio

    import httpx

    from merced_ai.bots import create_bot
    from merced_ai.sessions import SessionStore
    from merced_ai.webui_server import create_web_app

    monkeypatch.setenv("MERCED_AI_ACP", "1")
    monkeypatch.setenv("MERCED_AI_GEMINI_PATH", str(_launcher(tmp_path)))
    log = tmp_path / "web-acp.jsonl"
    monkeypatch.setenv("FAKE_ACP_LOG", str(log))
    create_profile("helper", "Helps.", "Be concise.", workspace)
    create_bot("helper", "helper", "gemini", (), workspace)

    transport = httpx.ASGITransport(app=create_web_app(workspace, "token"))
    async with httpx.AsyncClient(transport=transport, base_url="http://127.0.0.1") as client:
        await client.post("/api/auth", json={"token": "token"})
        session = (await client.post("/api/sessions", json={"bot_name": "helper"})).json()
        path = f"/api/sessions/{session['id']}/messages"

        async def approve_when_asked() -> None:
            for _ in range(600):
                pending = (await client.get("/api/approvals/snapshot")).json()["snapshot"][
                    "pending"
                ]
                if pending:
                    await client.post(
                        "/api/approvals/decisions",
                        json={
                            "request_id": pending[0]["id"],
                            "decision": "approve",
                            "scope": "once",
                        },
                    )
                    return
                await asyncio.sleep(0.05)

        approver = asyncio.create_task(approve_when_asked())
        first = await asyncio.wait_for(
            client.post(path, json={"content": "PERMISSION:edit go", "approved": True}), 60
        )
        await approver
        second = await asyncio.wait_for(
            client.post(path, json={"content": "And again", "approved": True}), 60
        )

    assert "event: assistant_delta" in first.text
    assert "event: tool_call" in first.text
    assert "event: approval_pending" in first.text
    assert "permission=yes" in first.text
    stored = SessionStore(workspace).load(session["id"])
    assert stored.turns[1].native_session_id == "sess-new"
    methods = [json.loads(line).get("method") for line in log.read_text().splitlines()]
    assert "session/load" in methods
    loaded_prompt = [
        json.loads(line)
        for line in log.read_text().splitlines()
        if json.loads(line).get("method") == "session/prompt"
    ][-1]["params"]["prompt"][0]["text"]
    assert loaded_prompt == "And again"
    assert "event: assistant_message" in second.text
