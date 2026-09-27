"""Compose the local web UI application from its routers and services."""

from __future__ import annotations

import secrets
import sys
import threading
import webbrowser
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, Response
from fastapi.staticfiles import StaticFiles

from merced_ai.aais_presenter import AAISPresenter
from merced_ai.harnesses import default_registry
from merced_ai.harnesses.registry import HarnessRegistry
from merced_ai.paths import ensure_user_layout
from merced_ai.run_supervisor import RunSupervisor
from merced_ai.web.context import WebContext
from merced_ai.web.probes import HarnessProbeCache
from merced_ai.web.routers import catalog, conversations, worktrees
from merced_ai.web.routers import workspace as workspace_routes
from merced_ai.workspace_context import RunStore

STATIC_ROOT = Path(__file__).resolve().parent.parent / "webui"
LOOPBACK_HOSTS = {"127.0.0.1", "localhost", "::1"}
CONTENT_SECURITY_POLICY = (
    "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; "
    "connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'"
)


def create_web_app(
    workspace: Path,
    access_token: str | None = None,
    *,
    registry: HarnessRegistry | None = None,
) -> FastAPI:
    workspace = workspace.resolve()
    registry = registry or default_registry()
    cache_path = ensure_user_layout() / "cache" / "harness-probes.json"
    cache_path.parent.mkdir(parents=True, exist_ok=True)
    supervisor = RunSupervisor(workspace)
    context = WebContext(
        workspace=workspace,
        access_token=access_token,
        registry=registry,
        presenter=AAISPresenter(workspace),
        supervisor=supervisor,
        probes=HarnessProbeCache(registry, cache_path),
    )

    @asynccontextmanager
    async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
        RunStore(workspace).recover_interrupted()
        yield
        await supervisor.close()

    app = FastAPI(
        title="Merced AI", docs_url=None, redoc_url=None, openapi_url=None, lifespan=lifespan
    )
    app.state.web = context
    # Kept for callers and tests that reach for these services directly.
    app.state.run_supervisor = supervisor
    app.state.aais_presenter = context.presenter

    @app.middleware("http")
    async def security_headers(
        request: Request, call_next: Callable[[Request], Awaitable[Response]]
    ) -> Response:
        response = await call_next(request)
        response.headers["Cache-Control"] = "no-store"
        response.headers["Content-Security-Policy"] = CONTENT_SECURITY_POLICY
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["Permissions-Policy"] = "camera=(), microphone=(), geolocation=()"
        return response

    @app.get("/", response_class=HTMLResponse)
    async def index() -> HTMLResponse:
        return HTMLResponse((STATIC_ROOT / "index.html").read_text(encoding="utf-8"))

    app.mount("/assets", StaticFiles(directory=STATIC_ROOT), name="assets")
    for module in (workspace_routes, catalog, conversations, worktrees):
        app.include_router(module.router)
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

    if host not in LOOPBACK_HOSTS:
        raise ValueError("The UI is loopback-only; use 127.0.0.1, localhost, or ::1.")
    token = secrets.token_urlsafe(24)
    url = f"http://{host}:{port}/#token={token}"
    app = create_web_app(workspace, token)
    for notice in app.state.aais_presenter.notices:
        print(
            f"Warning: {notice['message']} The unreadable file was kept at "
            f"{notice['quarantined_path']}.",
            file=sys.stderr,
        )
    if open_browser:
        threading.Timer(0.8, lambda: webbrowser.open(url)).start()
    print(f"Merced AI UI: {url}")
    uvicorn.run(app, host=host, port=port, log_level="warning")
