"""Optional loopback-first web UI over Merced AI's application records."""

from __future__ import annotations

import secrets
import threading
import webbrowser
from pathlib import Path
from typing import Any

from merced_ai.bots import discover_bots
from merced_ai.harnesses import default_registry
from merced_ai.profiles import discover_profiles, resolve_profile
from merced_ai.sessions import SessionStore

try:  # Optional dependency boundary.
    from fastapi import FastAPI, HTTPException, Query, Request
    from fastapi.responses import HTMLResponse
    from fastapi.staticfiles import StaticFiles
except ImportError:  # pragma: no cover
    FastAPI = HTTPException = Query = Request = HTMLResponse = StaticFiles = None  # type: ignore[assignment,misc]


def create_web_app(workspace: Path, access_token: str | None = None) -> Any:
    if FastAPI is None:
        raise RuntimeError('Install the UI with: pip install "merced-ai[webui]"')

    workspace = workspace.resolve()
    static_root = Path(__file__).resolve().parent / "webui"
    app = FastAPI(title="Merced AI", docs_url=None, redoc_url=None)
    app.mount("/assets", StaticFiles(directory=static_root), name="assets")

    def authorize(request: Request, token: str | None) -> None:
        if (
            access_token
            and token != access_token
            and request.headers.get("x-merced-ai-token") != access_token
        ):
            raise HTTPException(status_code=401, detail="Invalid local UI token")

    @app.get("/", response_class=HTMLResponse)
    async def index() -> HTMLResponse:
        return HTMLResponse((static_root / "index.html").read_text(encoding="utf-8"))

    @app.get("/api/bootstrap")
    async def bootstrap(
        request: Request, token: str | None = Query(default=None)
    ) -> dict[str, Any]:
        authorize(request, token)
        profiles = discover_profiles(workspace)
        bots = discover_bots(workspace)
        sessions = SessionStore(workspace).list()
        probes = default_registry().probe_all()
        return {
            "workspace": str(workspace),
            "profiles": [item.model_dump(mode="json", exclude={"document"}) for item in profiles],
            "bots": [item.model_dump(mode="json") for item in bots],
            "sessions": [item.model_dump(mode="json") for item in sessions],
            "harnesses": [item.model_dump(mode="json") for item in probes],
        }

    @app.get("/api/projection/{bot_name}")
    async def projection(
        bot_name: str, request: Request, token: str | None = Query(default=None)
    ) -> dict[str, Any]:
        authorize(request, token)
        bot = next((item for item in discover_bots(workspace) if item.name == bot_name), None)
        if bot is None:
            raise HTTPException(status_code=404, detail="Bot not found")
        profile = resolve_profile(bot.profile, workspace)
        try:
            adapter = default_registry().get(bot.harness.preferred)
            report = adapter.project_profile(profile)
        except (KeyError, NotImplementedError) as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        return report.model_dump(mode="json", exclude={"system_prompt"})

    return app


def run_web_ui(
    workspace: Path,
    *,
    host: str = "127.0.0.1",
    port: int = 8765,
    open_browser: bool = True,
) -> None:
    try:
        import uvicorn
    except ImportError as exc:  # pragma: no cover
        raise RuntimeError('Install the UI with: pip install "merced-ai[webui]"') from exc

    if host not in {"127.0.0.1", "localhost", "::1"}:
        raise ValueError("The MVP UI is loopback-only; use 127.0.0.1, localhost, or ::1.")
    token = secrets.token_urlsafe(24)
    url = f"http://{host}:{port}/?token={token}"
    if open_browser:
        threading.Timer(0.8, lambda: webbrowser.open(url)).start()
    print(f"Merced AI UI: {url}")
    uvicorn.run(create_web_app(workspace, token), host=host, port=port, log_level="warning")
