"""Merced AI served as an ACP agent, driven by Merced AI's own ACP client."""

from __future__ import annotations

import os
import sys
import threading
from pathlib import Path
from typing import Any

import pytest

from merced_ai.bots import create_bot
from merced_ai.harnesses.acp import AcpConnection, AcpError
from merced_ai.profiles import create_profile
from merced_ai.sessions import SessionStore

pytestmark = pytest.mark.skipif(
    os.name == "nt", reason="uses the POSIX fake-codex script as the served harness"
)
FAKE_CODEX = Path(__file__).parent / "fixtures" / "fake_codex.py"


class Client:
    def __init__(self, workspace: Path, *bots: str, permission: str = "allow") -> None:
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
