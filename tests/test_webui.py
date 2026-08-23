from __future__ import annotations

from pathlib import Path

import httpx
import pytest

from merced_ai.bots import create_bot
from merced_ai.profiles import create_profile
from merced_ai.sessions import SessionStore
from merced_ai.webui_server import create_web_app


@pytest.mark.anyio
async def test_webui_serves_authenticated_workspace_state(workspace: Path) -> None:
    profile = create_profile(
        "reviewer",
        "Reviews code for concrete correctness defects before merge.",
        "Review code and report verified defects.",
        workspace,
    )
    create_bot("reviewer", "reviewer", "codex", ("claude",), workspace)
    store = SessionStore(workspace)
    session = store.create("reviewer", "codex", profile)
    store.append(session, "user", "Review the authentication changes")
    store.append(session, "assistant", "I found one authorization boundary to inspect.")
    transport = httpx.ASGITransport(app=create_web_app(workspace, "secret-token"))
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        assert (await client.get("/")).status_code == 200
        assert (await client.get("/api/bootstrap")).status_code == 401

        bootstrap = await client.get("/api/bootstrap?token=secret-token")
        projection = await client.get("/api/projection/reviewer?token=secret-token")

    assert bootstrap.status_code == 200
    payload = bootstrap.json()
    assert payload["bots"][0]["name"] == "reviewer"
    assert payload["sessions"][0]["turns"][1]["role"] == "assistant"
    assert projection.status_code == 200
    assert projection.json()["support_level"] == "degraded"
    assert "system_prompt" not in projection.json()
