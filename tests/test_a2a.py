"""Experimental A2A endpoint on the local web server."""

from __future__ import annotations

from pathlib import Path

import httpx
import pytest

from merced_ai.bots import create_bot
from merced_ai.harnesses.adapters.command import CommandHarnessAdapter
from merced_ai.models import HarnessProbe, HarnessStatus, RunResult
from merced_ai.profiles import create_profile
from merced_ai.webui_server import create_web_app

AUTH = {"Authorization": "Bearer token"}


@pytest.fixture(autouse=True)
def fake_harness(monkeypatch: pytest.MonkeyPatch) -> None:
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
    monkeypatch.setattr(
        CommandHarnessAdapter,
        "run_cancellable",
        lambda _adapter, request, *_args, **_kwargs: RunResult(
            harness_id=request.harness_id,
            output=f"{request.profile.name} says hi",
            exit_code=0,
            duration_ms=1,
        ),
    )


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
    create_bot("reviewer", "reviewer", "codex", (), workspace)
    create_bot("builder", "builder", "codex", (), workspace)


def _send(text: str, **metadata: object) -> dict:
    return {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "message/send",
        "params": {
            "message": {
                "role": "user",
                "messageId": "m1",
                "parts": [{"kind": "text", "text": text}],
                "metadata": metadata,
            }
        },
    }


@pytest.mark.anyio
async def test_agent_card_requires_the_token_and_lists_bots(workspace: Path) -> None:
    _bots(workspace)
    transport = httpx.ASGITransport(app=create_web_app(workspace, "token"))
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        denied = await client.get("/.well-known/agent-card.json")
        card = (await client.get("/.well-known/agent-card.json", headers=AUTH)).json()

    assert denied.status_code == 401
    assert card["url"] == "http://test/a2a"
    assert {skill["id"] for skill in card["skills"]} == {"reviewer", "builder"}
    assert card["capabilities"] == {"streaming": False, "pushNotifications": False}


@pytest.mark.anyio
async def test_message_send_runs_a_bot_and_continues_the_context(workspace: Path) -> None:
    _bots(workspace)
    transport = httpx.ASGITransport(app=create_web_app(workspace, "token"))
    async with httpx.AsyncClient(
        transport=transport, base_url="http://test", headers=AUTH
    ) as client:
        first = (await client.post("/a2a", json=_send("Hello", bot="reviewer"))).json()["result"]
        follow = _send("Again")
        follow["params"]["message"]["contextId"] = first["contextId"]
        second = (await client.post("/a2a", json=follow)).json()["result"]
        fetched = (
            await client.post(
                "/a2a",
                json={
                    "jsonrpc": "2.0",
                    "id": 2,
                    "method": "tasks/get",
                    "params": {"id": first["id"]},
                },
            )
        ).json()["result"]
        unsupported = (
            await client.post("/a2a", json={"jsonrpc": "2.0", "id": 3, "method": "message/stream"})
        ).json()

    assert first["status"]["state"] == "completed"
    assert first["artifacts"][0]["parts"][0]["text"] == "reviewer says hi"
    assert second["contextId"] == first["contextId"]
    assert fetched["id"] == first["id"]
    assert unsupported["error"]["code"] == -32004


@pytest.mark.anyio
async def test_write_capable_bots_need_explicit_approval(workspace: Path) -> None:
    _bots(workspace)
    transport = httpx.ASGITransport(app=create_web_app(workspace, "token"))
    async with httpx.AsyncClient(
        transport=transport, base_url="http://test", headers=AUTH
    ) as client:
        held = (await client.post("/a2a", json=_send("Build it", bot="builder"))).json()["result"]
        room = (
            await client.post("/a2a", json=_send("Go", bots=["reviewer", "builder"], approved=True))
        ).json()["result"]
        bad = (await client.post("/a2a", json=_send("x", bot="ghost"))).json()

    assert held["status"]["state"] == "input-required"
    assert "metadata.approved" in held["status"]["message"]["parts"][0]["text"]
    assert room["status"]["state"] == "completed"
    assert {item["name"] for item in room["artifacts"]} == {"reviewer", "builder"}
    assert bad["error"]["code"] == -32602
