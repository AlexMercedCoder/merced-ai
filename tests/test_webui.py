from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from pathlib import Path

import httpx
import pytest

from merced_ai.harnesses.adapters.command import CommandHarnessAdapter, HarnessRunError
from merced_ai.models import HarnessProbe, HarnessStatus, RunResult
from merced_ai.webui_server import create_web_app, run_web_ui


@pytest.fixture
def ready_harnesses(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        CommandHarnessAdapter,
        "probe",
        lambda adapter: HarnessProbe(
            harness_id=adapter.descriptor.id,
            status=HarnessStatus.READY,
            path=Path(adapter.descriptor.executable_names[0]),
            version="test 1.0",
            transport=adapter.descriptor.transports[0],
            capabilities=adapter.descriptor.capabilities,
            capabilities_verified=True,
        ),
    )


@asynccontextmanager
async def authenticated_client(
    workspace: Path,
) -> AsyncIterator[tuple[httpx.AsyncClient, httpx.Response]]:
    transport = httpx.ASGITransport(app=create_web_app(workspace, "secret-token"))
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.post("/api/auth", json={"token": "secret-token"})
        yield client, response


@pytest.mark.anyio
async def test_webui_exchanges_token_for_secure_local_cookie_and_headers(workspace: Path) -> None:
    transport = httpx.ASGITransport(app=create_web_app(workspace, "secret-token"))
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        index = await client.get("/")
        rejected = await client.get("/api/bootstrap")
        bad_auth = await client.post("/api/auth", json={"token": "wrong"})
        auth = await client.post("/api/auth", json={"token": "secret-token"})
        bootstrap = await client.get("/api/bootstrap")
        logout = await client.post("/api/logout")
        rejected_after_logout = await client.get("/api/bootstrap")

    assert index.status_code == 200
    assert "Content-Security-Policy" in index.headers
    assert index.headers["X-Frame-Options"] == "DENY"
    assert rejected.status_code == 401
    assert bad_auth.status_code == 401
    assert auth.status_code == 200
    assert "HttpOnly" in auth.headers["set-cookie"]
    assert "SameSite=strict" in auth.headers["set-cookie"]
    assert bootstrap.status_code == 200
    assert logout.json() == {"authenticated": False}
    assert rejected_after_logout.status_code == 401


@pytest.mark.anyio
async def test_webui_profile_bot_session_projection_and_export_workflow(
    workspace: Path, ready_harnesses: None
) -> None:
    async with authenticated_client(workspace) as (client, _):
        profile = await client.post(
            "/api/profiles",
            json={
                "name": "reviewer",
                "description": "Reviews changes before merge.",
                "instructions": "Report concrete defects.",
                "model_provider": "openai",
                "model_id": "gpt-5.4",
                "edit_permission": "deny",
                "shell_permission": "deny",
            },
        )
        duplicate = await client.post(
            "/api/profiles",
            json={
                "name": "reviewer",
                "description": "Duplicate.",
                "instructions": "Duplicate.",
            },
        )
        updated = await client.put(
            "/api/profiles/reviewer",
            json={
                "description": "Reviews correctness and security.",
                "instructions": "Report verified defects only.",
                "model_provider": "openai",
                "model_id": "gpt-5.4",
                "edit_permission": "deny",
                "shell_permission": "deny",
            },
        )
        bot = await client.post(
            "/api/bots",
            json={
                "name": "reviewer",
                "profile": "reviewer",
                "harness": "codex",
                "fallbacks": ["claude"],
            },
        )
        projection = await client.get("/api/projection/reviewer?harness=claude")
        missing_projection = await client.get("/api/projection/missing")
        session = await client.post(
            "/api/sessions", json={"bot_name": "reviewer", "harness": "codex"}
        )
        session_id = session.json()["id"]
        exported = await client.get(f"/api/sessions/{session_id}/export")
        missing_export = await client.get("/api/sessions/session-missing/export")
        bootstrap = await client.get("/api/bootstrap")

    assert profile.status_code == 201
    assert profile.json()["editable"] is True
    assert profile.json()["model"] == {"provider": "openai", "id": "gpt-5.4"}
    assert duplicate.status_code == 409
    assert updated.status_code == 200
    assert updated.json()["revision"] == 2
    assert bot.status_code == 201
    assert projection.status_code == 200
    assert projection.json()["approval_required"] is False
    assert missing_projection.status_code == 404
    assert session.status_code == 201
    assert exported.status_code == 200
    assert exported.headers["content-type"].startswith("text/markdown")
    assert "# reviewer conversation" in exported.text
    assert missing_export.status_code == 404
    assert bootstrap.json()["profiles"][0]["instructions"] == "Report verified defects only.\n"
    assert len(bootstrap.json()["harnesses"]) == 14


@pytest.mark.anyio
async def test_webui_streams_run_lifecycle_and_persists_turns(
    workspace: Path, ready_harnesses: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def inline(function: Callable[..., object], *args: object) -> object:
        return function(*args)

    def fake_run(
        _adapter: CommandHarnessAdapter, _request: object, _cancellation: object
    ) -> RunResult:
        return RunResult(
            harness_id="codex",
            output="Found one verified defect.",
            exit_code=0,
            duration_ms=125,
            raw={"events": [{"type": "tool_completed", "name": "read"}]},
        )

    monkeypatch.setattr(CommandHarnessAdapter, "run_cancellable", fake_run)
    monkeypatch.setattr("merced_ai.webui_server.asyncio.to_thread", inline)
    async with authenticated_client(workspace) as (client, _):
        await client.post(
            "/api/profiles",
            json={
                "name": "safe-reviewer",
                "description": "Read-only reviewer.",
                "instructions": "Review without editing.",
                "edit_permission": "deny",
                "shell_permission": "deny",
            },
        )
        await client.post(
            "/api/bots",
            json={
                "name": "safe-reviewer",
                "profile": "safe-reviewer",
                "harness": "codex",
            },
        )
        session = await client.post("/api/sessions", json={"bot_name": "safe-reviewer"})
        response = await client.post(
            f"/api/sessions/{session.json()['id']}/messages",
            json={"content": "Review this change."},
        )
        bootstrap = await client.get("/api/bootstrap")

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/event-stream")
    assert "event: run_started" in response.text
    assert "event: tool_event" in response.text
    assert "event: assistant_message" in response.text
    assert "event: run_finished" in response.text
    turns = bootstrap.json()["sessions"][0]["turns"]
    assert [item["role"] for item in turns] == ["user", "assistant"]
    assert turns[1]["content"] == "Found one verified defect."


@pytest.mark.anyio
async def test_webui_requires_approval_and_reports_runtime_errors(
    workspace: Path, ready_harnesses: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def inline(function: Callable[..., object], *args: object) -> object:
        return function(*args)

    monkeypatch.setattr("merced_ai.webui_server.asyncio.to_thread", inline)
    async with authenticated_client(workspace) as (client, _):
        await client.post(
            "/api/profiles",
            json={
                "name": "builder",
                "description": "Builds requested changes.",
                "instructions": "Implement the requested change.",
            },
        )
        await client.post(
            "/api/bots",
            json={"name": "builder", "profile": "builder", "harness": "codex"},
        )
        session = await client.post("/api/sessions", json={"bot_name": "builder"})
        path = f"/api/sessions/{session.json()['id']}/messages"
        approval = await asyncio.wait_for(
            client.post(path, json={"content": "Make the change."}), timeout=5
        )

        def fail_run(*_args: object) -> RunResult:
            raise HarnessRunError("provider unavailable", exit_code=5)

        monkeypatch.setattr(CommandHarnessAdapter, "run_cancellable", fail_run)
        failed = await asyncio.wait_for(
            client.post(path, json={"content": "Make the change.", "approved": True}),
            timeout=5,
        )
        missing_session = await asyncio.wait_for(
            client.post("/api/sessions/session-missing/messages", json={"content": "Hello"}),
            timeout=5,
        )
        missing_cancel = await asyncio.wait_for(
            client.post("/api/runs/run-missing/cancel"), timeout=5
        )

    assert "event: approval_required" in approval.text
    assert "event: run_error" in failed.text
    assert "provider unavailable" in failed.text
    assert missing_session.status_code == 409
    assert missing_cancel.status_code == 404


@pytest.mark.anyio
async def test_webui_rejects_cross_origin_mutations(workspace: Path) -> None:
    async with authenticated_client(workspace) as (client, _):
        rejected = await client.post(
            "/api/profiles",
            headers={"Origin": "https://attacker.example"},
            json={"name": "unsafe", "description": "Unsafe.", "instructions": "No."},
        )
        invalid_bot = await client.post(
            "/api/bots",
            json={"name": "bad", "profile": "missing", "harness": "not-real"},
        )
        invalid_session = await client.post("/api/sessions", json={"bot_name": "missing"})

    assert rejected.status_code == 403
    assert invalid_bot.status_code == 409
    assert invalid_session.status_code == 409


def test_webui_rejects_non_loopback_binding(workspace: Path) -> None:
    with pytest.raises(ValueError, match="loopback-only"):
        run_web_ui(workspace, host="0.0.0.0", open_browser=False)
