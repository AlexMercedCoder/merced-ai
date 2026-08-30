from __future__ import annotations

import asyncio
import base64
import threading
import time
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from pathlib import Path

import httpx
import pytest

from merced_ai.harnesses.adapters.command import CommandHarnessAdapter, HarnessRunError
from merced_ai.models import HarnessProbe, HarnessStatus, RunResult
from merced_ai.paths import user_root
from merced_ai.webui_server import create_web_app, run_web_ui


def test_profile_management_surface_renders_discovery_and_trust_warnings() -> None:
    script = (Path(__file__).parents[1] / "src" / "merced_ai" / "webui" / "app.js").read_text(
        encoding="utf-8"
    )

    assert "item.warnings?.length" in script
    assert 'class="profile-warnings" role="status"' in script


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
async def test_webui_context_upload_history_and_handoff(
    workspace: Path, ready_harnesses: None
) -> None:
    (workspace / "brief.md").write_text("ship it", encoding="utf-8")
    async with authenticated_client(workspace) as (client, _):
        profile = await client.post(
            "/api/profiles",
            json={
                "name": "reviewer",
                "description": "Reviews work.",
                "instructions": "Review carefully.",
                "edit_permission": "deny",
                "shell_permission": "deny",
            },
        )
        assert profile.status_code == 201
        bot = await client.post(
            "/api/bots",
            json={"name": "reviewer", "profile": "reviewer", "harness": "codex"},
        )
        assert bot.status_code == 201
        session = await client.post("/api/sessions", json={"bot_name": "reviewer"})
        session_id = session.json()["id"]

        context = await client.get("/api/context")
        upload = await client.post(
            f"/api/sessions/{session_id}/attachments",
            json={
                "name": "diagram.png",
                "media_type": "image/png",
                "content_base64": base64.b64encode(b"image").decode(),
            },
        )
        history = await client.get(f"/api/runs?session_id={session_id}")
        handoff = await client.get("/api/handoff/codex")

    assert any(item["path"] == "brief.md" for item in context.json()["files"])
    assert upload.status_code == 201
    assert upload.json()["path"].endswith("-diagram.png")
    assert history.json() == {"runs": []}
    assert handoff.json()["workspace"] == str(workspace)
    assert handoff.json()["argv"]


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
async def test_webui_bootstrap_is_immediate_and_harness_detection_is_progressive_and_cached(
    workspace: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    started = threading.Event()
    release = threading.Event()

    def controlled_probe(adapter: CommandHarnessAdapter) -> HarnessProbe:
        started.set()
        release.wait(timeout=2)
        return HarnessProbe(
            harness_id=adapter.descriptor.id,
            status=HarnessStatus.READY,
            path=Path(adapter.descriptor.executable_names[0]),
            version="progressive 1.0",
            transport=adapter.descriptor.transports[0],
            capabilities=adapter.descriptor.capabilities,
        )

    monkeypatch.setattr(CommandHarnessAdapter, "probe", controlled_probe)
    transport = httpx.ASGITransport(app=create_web_app(workspace, "secret-token"))
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        await client.post("/api/auth", json={"token": "secret-token"})
        before = time.monotonic()
        bootstrap = await client.get("/api/bootstrap")
        elapsed = time.monotonic() - before

        assert elapsed < 1.0
        assert bootstrap.json()["harness_detection"]["refreshing"] is False
        assert {item["status"] for item in bootstrap.json()["harnesses"]} == {"detecting"}

        refresh = await client.post("/api/harnesses/refresh")
        assert refresh.status_code == 202
        assert refresh.json()["refreshing"] is True
        for _ in range(100):
            if started.is_set():
                break
            await asyncio.sleep(0.01)
        assert started.is_set()
        midway = await client.get("/api/harnesses")
        assert midway.json()["refreshing"] is True
        assert {item["status"] for item in midway.json()["harnesses"]} == {"detecting"}

        release.set()
        for _ in range(100):
            completed = await client.get("/api/harnesses")
            if not completed.json()["refreshing"]:
                break
            await asyncio.sleep(0.01)

    assert completed.json()["refreshing"] is False
    assert {item["status"] for item in completed.json()["harnesses"]} == {"ready"}
    assert (user_root() / "cache" / "harness-probes.json").is_file()

    cached_transport = httpx.ASGITransport(app=create_web_app(workspace, "secret-token"))
    async with httpx.AsyncClient(
        transport=cached_transport, base_url="http://test"
    ) as cached_client:
        await cached_client.post("/api/auth", json={"token": "secret-token"})
        cached = await cached_client.get("/api/bootstrap")
    assert cached.json()["harness_detection"]["cached"] is True
    assert cached.json()["harness_detection"]["stale"] is False
    assert {item["status"] for item in cached.json()["harnesses"]} == {"ready"}

    (user_root() / "cache" / "harness-probes.json").write_text("{invalid", encoding="utf-8")
    fallback_transport = httpx.ASGITransport(app=create_web_app(workspace, "secret-token"))
    async with httpx.AsyncClient(
        transport=fallback_transport, base_url="http://test"
    ) as fallback_client:
        await fallback_client.post("/api/auth", json={"token": "secret-token"})
        fallback = await fallback_client.get("/api/bootstrap")
    assert fallback.json()["harness_detection"]["cached"] is False
    assert {item["status"] for item in fallback.json()["harnesses"]} == {"detecting"}


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
    profiles = {profile["name"]: profile for profile in bootstrap.json()["profiles"]}
    assert profiles["reviewer"]["instructions"] == "Report verified defects only.\n"
    assert len(bootstrap.json()["harnesses"]) == 14


@pytest.mark.anyio
async def test_webui_can_edit_and_delete_local_records(
    workspace: Path, ready_harnesses: None
) -> None:
    async with authenticated_client(workspace) as (client, _):
        await client.post(
            "/api/profiles",
            json={"name": "helper", "description": "Helps.", "instructions": "Help."},
        )
        await client.post(
            "/api/bots",
            json={"name": "helper", "profile": "helper", "harness": "codex"},
        )
        edited = await client.put(
            "/api/bots/helper",
            json={"name": "helper", "profile": "helper", "harness": "loro"},
        )
        session = await client.post("/api/sessions", json={"bot_name": "helper"})
        blocked = await client.delete("/api/profiles/helper")
        removed_session = await client.delete(f"/api/sessions/{session.json()['id']}")
        removed_bot = await client.delete("/api/bots/helper")
        removed_profile = await client.delete("/api/profiles/helper")

    assert edited.status_code == 200
    assert edited.json()["harness"]["preferred"] == "loro"
    assert blocked.status_code == 409
    assert removed_session.status_code == 204
    assert removed_bot.status_code == 204
    assert removed_profile.status_code == 204


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
    assert "event: participant_started" in response.text
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


@pytest.mark.anyio
async def test_webui_group_chat_results_stream_progressively_and_persist_in_order(
    workspace: Path, ready_harnesses: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def inline(function: Callable[..., object], *args: object) -> object:
        return function(*args)

    def fake_run(
        _adapter: CommandHarnessAdapter, request: object, _cancellation: object
    ) -> RunResult:
        profile = request.profile.name  # type: ignore[attr-defined]
        if profile == "tester":
            raise HarnessRunError("tester provider unavailable", exit_code=5)
        return RunResult(
            harness_id=request.harness_id,  # type: ignore[attr-defined]
            output=f"{profile} response",
            exit_code=0,
            duration_ms=10,
        )

    monkeypatch.setattr(CommandHarnessAdapter, "run_cancellable", fake_run)
    monkeypatch.setattr("merced_ai.webui_server.asyncio.to_thread", inline)
    async with authenticated_client(workspace) as (client, _):
        for name in ("reviewer", "builder", "tester"):
            profile = await client.post(
                "/api/profiles",
                json={
                    "name": name,
                    "description": f"{name} profile",
                    "instructions": f"Act as {name}.",
                    "edit_permission": "deny",
                    "shell_permission": "deny",
                },
            )
            assert profile.status_code == 201
            bot = await client.post(
                "/api/bots",
                json={"name": name, "profile": name, "harness": "codex"},
            )
            assert bot.status_code == 201
        created = await client.post(
            "/api/sessions",
            json={"bot_names": ["reviewer", "builder", "tester"], "mode": "mentions"},
        )
        session_id = created.json()["id"]
        response = await client.post(
            f"/api/sessions/{session_id}/messages",
            json={"content": "Review together", "dispatch": "all"},
        )
        exported = await client.get(f"/api/sessions/{session_id}/export")
        bootstrap = await client.get("/api/bootstrap")
        renamed = await client.put(
            f"/api/sessions/{session_id}", json={"title": "Architecture council"}
        )
        targeted_retry = await client.post(
            f"/api/sessions/{session_id}/messages",
            json={"content": "Review together", "dispatch": "tester"},
        )

    assert created.status_code == 201
    assert created.json()["kind"] == "group"
    assert [item["bot_name"] for item in created.json()["participants"]] == [
        "reviewer",
        "builder",
        "tester",
    ]
    assert "reviewer response" in response.text
    assert "builder response" in response.text
    assert response.text.count("event: participant_started") == 3
    assert "event: participant_error" in response.text
    assert "tester provider unavailable" in response.text
    assert '"completed": 2' in response.text
    assert '"failed": 1' in response.text
    session = next(item for item in bootstrap.json()["sessions"] if item["id"] == session_id)
    assert [turn.get("bot_name") for turn in session["turns"]] == [None, "reviewer", "builder"]
    assert "## reviewer (codex)" in exported.text
    assert "## builder (codex)" in exported.text

    assert renamed.json()["title"] == "Architecture council"
    assert '"bot_name": "tester"' in targeted_retry.text
    assert '"bot_name": "reviewer"' not in targeted_retry.text
    assert '"bot_name": "builder"' not in targeted_retry.text


@pytest.mark.anyio
async def test_webui_group_validation_and_approval_aggregation(
    workspace: Path, ready_harnesses: None
) -> None:
    async with authenticated_client(workspace) as (client, _):
        for name in ("writer", "operator"):
            await client.post(
                "/api/profiles",
                json={
                    "name": name,
                    "description": f"{name} profile",
                    "instructions": f"Act as {name}.",
                },
            )
            await client.post("/api/bots", json={"name": name, "profile": name, "harness": "codex"})
        too_small = await client.post("/api/sessions", json={"bot_names": ["writer"]})
        duplicate = await client.post("/api/sessions", json={"bot_names": ["writer", "writer"]})
        created = await client.post(
            "/api/sessions", json={"bot_names": ["writer", "operator"], "mode": "all"}
        )
        approval = await client.post(
            f"/api/sessions/{created.json()['id']}/messages",
            json={"content": "Make changes", "dispatch": "all"},
        )
        derived = await client.post(
            f"/api/sessions/{created.json()['id']}/derive",
            json={
                "bot_names": ["operator", "writer"],
                "mode": "round_robin",
                "title": "Follow-up room",
            },
        )

    assert too_small.status_code == 201  # one-name lists retain single-session compatibility
    assert duplicate.status_code == 422
    assert "event: approval_required" in approval.text
    assert '"bot_name": "writer"' in approval.text
    assert '"bot_name": "operator"' in approval.text
    assert derived.status_code == 201
    assert derived.json()["derived_from"] == created.json()["id"]
    assert derived.json()["title"] == "Follow-up room"
    assert [item["bot_name"] for item in derived.json()["participants"]] == [
        "operator",
        "writer",
    ]


def test_webui_rejects_non_loopback_binding(workspace: Path) -> None:
    with pytest.raises(ValueError, match="loopback-only"):
        run_web_ui(workspace, host="0.0.0.0", open_browser=False)
