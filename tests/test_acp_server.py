"""Merced AI served as an ACP agent, driven by Merced AI's own ACP client."""

from __future__ import annotations

import io
import os
import sys
import threading
from pathlib import Path
from typing import Any

import pytest

from merced_ai.acp_server import MercedAcpAgent, RpcError
from merced_ai.bots import create_bot
from merced_ai.harnesses.acp import AcpConnection, AcpError
from merced_ai.profiles import create_profile
from merced_ai.sessions import SessionStore

pytestmark = pytest.mark.skipif(
    os.name == "nt", reason="uses the POSIX fake-codex script as the served harness"
)
FAKE_CODEX = Path(__file__).parent / "fixtures" / "fake_codex.py"


class Client:
    def __init__(
        self,
        workspace: Path,
        *bots: str,
        permission: str = "allow",
        extra: list[str] | None = None,
    ) -> None:
        self.updates: list[dict[str, Any]] = []
        self.permission_requests: list[dict[str, Any]] = []
        self.permission = permission
        env = {
            key: value
            for key, value in os.environ.items()
            if key != "COVERAGE_PROCESS_START" and not key.startswith("COV_CORE_")
        }
        env["MERCED_AI_CODEX_PATH"] = str(FAKE_CODEX)
        argv = [sys.executable, "-m", "merced_ai", "acp", "-C", str(workspace)]
        for bot in bots:
            argv += ["--bot", bot]
        argv += extra or []
        self.connection = AcpConnection(
            argv,
            cwd=workspace,
            env=env,
            on_notification=self._notify,
            on_request=self._request,
        )
        self.workspace = workspace

    def _notify(self, method: str, params: dict[str, Any]) -> None:
        if method == "session/update":
            self.updates.append(params["update"])

    def _request(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
        assert method == "session/request_permission"
        self.permission_requests.append(params)
        option = params["options"][0 if self.permission == "allow" else -1]
        return {"outcome": {"outcome": "selected", "optionId": option["optionId"]}}

    def call(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
        return self.connection.request(method, params, timeout=60)

    def text(self) -> str:
        return "".join(
            item["content"]["text"]
            for item in self.updates
            if item.get("sessionUpdate") == "agent_message_chunk"
        )

    def close(self) -> None:
        self.connection.close()


def _bots(workspace: Path) -> None:
    create_profile(
        "reviewer",
        "Reviews.",
        "Review.",
        workspace,
        edit_permission="deny",
        shell_permission="deny",
    )
    create_profile("builder", "Builds.", "Build.", workspace)
    create_profile("fixer", "Fixes.", "Fix.", workspace)
    for name in ("reviewer", "builder", "fixer"):
        create_bot(name, name, "codex", (), workspace)


def test_single_bot_session_prompt_and_load(workspace: Path) -> None:
    _bots(workspace)
    client = Client(workspace, "reviewer")
    try:
        init = client.call("initialize", {"protocolVersion": 1, "clientCapabilities": {}})
        assert init["agentCapabilities"]["loadSession"] is True
        session_id = client.call("session/new", {"cwd": str(workspace), "mcpServers": []})[
            "sessionId"
        ]
        result = client.call(
            "session/prompt",
            {
                "sessionId": session_id,
                "prompt": [
                    {"type": "text", "text": "Review this"},
                    {"type": "resource", "resource": {"uri": "file:///a.py", "text": "x = 1"}},
                ],
            },
        )
        assert result["stopReason"] == "end_turn"
        assert "Browser validation response" in client.text()
        assert client.permission_requests == []  # read-only bot: no consent needed

        stored = SessionStore(workspace).load(session_id)
        assert [turn.role for turn in stored.turns] == ["user", "assistant"]
        assert "file:///a.py" in stored.turns[0].content

        client.updates.clear()
        client.call(
            "session/load", {"sessionId": session_id, "cwd": str(workspace), "mcpServers": []}
        )
        kinds = [item["sessionUpdate"] for item in client.updates]
        assert kinds == ["user_message_chunk", "agent_message_chunk"]
    finally:
        client.close()


def test_room_asks_consent_once_and_attributes_replies(workspace: Path) -> None:
    _bots(workspace)
    client = Client(workspace, "builder", "fixer")
    try:
        client.call("initialize", {"protocolVersion": 1})
        session_id = client.call("session/new", {"cwd": str(workspace), "mcpServers": []})[
            "sessionId"
        ]
        for _ in range(2):
            client.call(
                "session/prompt",
                {"sessionId": session_id, "prompt": [{"type": "text", "text": "Go"}]},
            )
        assert len(client.permission_requests) == 1
        assert "builder, fixer" in client.permission_requests[0]["toolCall"]["title"]
        assert "**builder:**" in client.text() and "**fixer:**" in client.text()
    finally:
        client.close()


def test_declined_consent_runs_nothing(workspace: Path) -> None:
    _bots(workspace)
    client = Client(workspace, "builder", permission="reject")
    try:
        client.call("initialize", {"protocolVersion": 1})
        session_id = client.call("session/new", {"cwd": str(workspace), "mcpServers": []})[
            "sessionId"
        ]
        client.call(
            "session/prompt", {"sessionId": session_id, "prompt": [{"type": "text", "text": "Go"}]}
        )
        assert "Not run" in client.text()
        assert [turn.role for turn in SessionStore(workspace).load(session_id).turns] == []
    finally:
        client.close()


def test_errors_are_json_rpc_errors(workspace: Path, tmp_path: Path) -> None:
    _bots(workspace)
    client = Client(workspace, "reviewer")
    try:
        client.call("initialize", {"protocolVersion": 1})
        with pytest.raises(AcpError, match="serves"):
            client.call("session/new", {"cwd": str(tmp_path), "mcpServers": []})
        with pytest.raises(AcpError, match="not supported"):
            client.call("terminal/create", {})
        with pytest.raises(AcpError, match="not found"):
            client.call("session/prompt", {"sessionId": "session-" + "0" * 32, "prompt": []})
    finally:
        client.close()


def test_unknown_bot_fails_session_creation(workspace: Path) -> None:
    client = Client(workspace, "ghost")
    try:
        client.call("initialize", {"protocolVersion": 1})
        with pytest.raises(AcpError, match="ghost"):
            client.call("session/new", {"cwd": str(workspace), "mcpServers": []})
    finally:
        client.close()


def test_threads_are_not_leaked() -> None:
    assert threading.active_count() < 50


def test_harness_approvals_are_forwarded_to_the_client(workspace: Path) -> None:
    import io

    from aais import create_request, validate

    from merced_ai.acp_server import MercedAcpAgent

    agent = MercedAcpAgent(workspace, ("reviewer",), reader=io.StringIO(), writer=io.StringIO())
    seen: list[dict[str, Any]] = []

    def fake_client(method: str, params: dict[str, Any]) -> dict[str, Any]:
        seen.append(params)
        return {"outcome": {"outcome": "selected", "optionId": "approve-once"}}

    agent.request_client = fake_client  # type: ignore[method-assign]
    envelope = create_request(
        action={
            "kind": "tool.call",
            "name": "shell.exec",
            "summary": "Run tests",
            "arguments": {"command": "pytest"},
        },
        origin={"harness": "magagent", "session_id": "s"},
        risk={"level": "medium", "reasons": ["Runs a process"]},
        choices=[
            {"decision": "approve", "scope": "once", "label": "Allow once"},
            {"decision": "deny", "scope": "once", "label": "Deny"},
        ],
        sequence=1,
    )

    decision = validate(agent._relay_approval("session-x")(envelope, None))

    assert decision["decision"]["decision"] == "approve"
    assert [item["kind"] for item in seen[0]["options"]] == ["allow_once", "reject_once"]
    assert seen[0]["toolCall"]["title"] == "magagent: Run tests"

    agent.request_client = lambda method, params: {"outcome": {"outcome": "cancelled"}}  # type: ignore[method-assign]
    assert validate(agent._relay_approval("s")(envelope, None))["decision"]["decision"] == "cancel"


# ---- session ownership (SEC-2) ---------------------------------------------------------------


def _agent(workspace: Path, *bots: str, allow_resume: bool = False) -> tuple[Any, io.StringIO]:
    out = io.StringIO()
    agent = MercedAcpAgent(
        workspace, bots, reader=io.StringIO(), writer=out, allow_resume=allow_resume
    )
    return agent, out


def _say(session_id: str, text: str = "Review this") -> dict[str, Any]:
    return {"sessionId": session_id, "prompt": [{"type": "text", "text": text}]}


def test_sessions_belong_to_the_process_that_created_them(
    workspace: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _bots(workspace)
    monkeypatch.setenv("MERCED_AI_CODEX_PATH", str(FAKE_CODEX))
    first, _ = _agent(workspace, "reviewer")
    session_id = first.new_session({"cwd": str(workspace)})["sessionId"]
    assert first.prompt(_say(session_id))["stopReason"] == "end_turn"

    other, out = _agent(workspace, "reviewer")
    with pytest.raises(RpcError, match="read-only"):
        other.prompt(_say(session_id))  # prompting blind is refused
    other.load_session({"sessionId": session_id, "cwd": str(workspace)})
    assert "read-only here" in out.getvalue() and "--allow-resume" in out.getvalue()
    with pytest.raises(RpcError, match="read-only"):
        other.prompt(_say(session_id))  # loading shows history but does not grant prompting

    turns = len(SessionStore(workspace).load(session_id).turns)
    assert turns == 2  # nothing ran for the second client


def test_allow_resume_continues_only_conversations_of_served_bots(
    workspace: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _bots(workspace)
    monkeypatch.setenv("MERCED_AI_CODEX_PATH", str(FAKE_CODEX))
    first, _ = _agent(workspace, "reviewer")
    session_id = first.new_session({"cwd": str(workspace)})["sessionId"]
    room, _ = _agent(workspace, "builder", "fixer")
    room_id = room.new_session({"cwd": str(workspace)})["sessionId"]

    resumer, out = _agent(workspace, "reviewer", allow_resume=True)
    with pytest.raises(RpcError, match="session/load"):
        resumer.prompt(_say(session_id))  # still has to load it first
    resumer.load_session({"sessionId": session_id, "cwd": str(workspace)})
    assert "read-only" not in out.getvalue()
    assert resumer.prompt(_say(session_id))["stopReason"] == "end_turn"

    # A server for a read-only bot must never drive a write-capable room.
    resumer.load_session({"sessionId": room_id, "cwd": str(workspace)})
    assert "does not serve" in out.getvalue()
    with pytest.raises(RpcError, match="builder, fixer"):
        resumer.prompt(_say(room_id, "@builder rm -rf build"))


def test_cli_allow_resume_flag_reaches_the_agent(workspace: Path) -> None:
    _bots(workspace)
    first = Client(workspace, "reviewer")
    try:
        first.call("initialize", {"protocolVersion": 1})
        session_id = first.call("session/new", {"cwd": str(workspace), "mcpServers": []})[
            "sessionId"
        ]
    finally:
        first.close()

    second = Client(workspace, "reviewer")
    try:
        second.call("initialize", {"protocolVersion": 1})
        second.call("session/load", {"sessionId": session_id, "cwd": str(workspace)})
        with pytest.raises(AcpError, match="read-only"):
            second.call("session/prompt", _say(session_id))
    finally:
        second.close()

    third = Client(workspace, "reviewer", extra=["--allow-resume"])
    try:
        third.call("initialize", {"protocolVersion": 1})
        third.call("session/load", {"sessionId": session_id, "cwd": str(workspace)})
        assert third.call("session/prompt", _say(session_id))["stopReason"] == "end_turn"
    finally:
        third.close()
