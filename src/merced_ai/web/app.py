"""Compose the local web UI application from its routers and services."""

from __future__ import annotations

import secrets
import socket
import sys
import threading
import webbrowser
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, PlainTextResponse, Response
from fastapi.staticfiles import StaticFiles

from merced_ai.aais_presenter import AAISPresenter
from merced_ai.harnesses import default_registry
from merced_ai.harnesses.registry import HarnessRegistry
from merced_ai.paths import ensure_user_layout
from merced_ai.run_supervisor import RunSupervisor
from merced_ai.web.context import WebContext
from merced_ai.web.probes import HarnessProbeCache
from merced_ai.web.routers import a2a, catalog, conversations, evals, inbox, worktrees
from merced_ai.web.routers import workspace as workspace_routes
from merced_ai.workspace_context import RunStore

STATIC_ROOT = Path(__file__).resolve().parent.parent / "webui"
LOOPBACK_HOSTS = {"127.0.0.1", "localhost", "::1"}
# 8765 is also Loro's `loro web` default, so running both collided. When the default is busy the
# next free port is used; an explicit --port that is busy is an error.
DEFAULT_UI_PORT = 8773
PORT_SEARCH_SPAN = 20
# Host header names accepted on requests. Only literal loopback names: a bare single-label name
# could resolve through a DNS search domain, which is exactly what DNS rebinding needs.
ALLOWED_HOSTS = frozenset(LOOPBACK_HOSTS)
# Uploads are at most 10 MB (about 14 MB of base64); nothing else needs more.
MAX_BODY_BYTES = 16 * 1024 * 1024
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
        # DNS rebinding: a page on attacker.example can resolve its name to 127.0.0.1, and its
        # requests then carry that Host. Only loopback names are served.
        host = (request.headers.get("host") or "").rsplit(":", 1)[0].strip("[]").lower()
        if host not in ALLOWED_HOSTS:
            return PlainTextResponse("Merced AI only answers to loopback host names.", 421)
        declared = request.headers.get("content-length")
        if declared is not None and (not declared.isdigit() or int(declared) > MAX_BODY_BYTES):
            return PlainTextResponse("Request body is too large.", 413)
        if request.headers.get("transfer-encoding", "").lower() == "chunked":
            return PlainTextResponse("Send a Content-Length; chunked bodies are refused.", 411)
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
    for module in (workspace_routes, catalog, conversations, worktrees, inbox, evals, a2a):
        app.include_router(module.router)
    return app


def port_is_free(host: str, port: int) -> bool:
    try:
        infos = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    except OSError:
        return False
    for family, kind, proto, _name, address in infos:
        with socket.socket(family, kind, proto) as probe:
            try:
                probe.bind(address)
            except OSError:
                return False
    return True


def choose_port(host: str, requested: int | None) -> tuple[int, str | None]:
    """The port to serve on, plus a notice when it is not the one the user expects."""
    if requested is not None:
        if not port_is_free(host, requested):
            raise ValueError(
                f"Port {requested} on {host} is already in use (another Merced AI or Loro UI may "
                "be running). Stop it, or choose a different port with --port."
            )
        return requested, None
    for candidate in range(DEFAULT_UI_PORT, DEFAULT_UI_PORT + PORT_SEARCH_SPAN):
        if port_is_free(host, candidate):
            notice = (
                None
                if candidate == DEFAULT_UI_PORT
                else f"Port {DEFAULT_UI_PORT} is in use; serving on {candidate} instead."
            )
            return candidate, notice
    raise ValueError(
        f"Ports {DEFAULT_UI_PORT}-{DEFAULT_UI_PORT + PORT_SEARCH_SPAN - 1} on {host} are all in "
        "use. Choose a free port with --port."
    )


def run_web_ui(
    workspace: Path,
    *,
    host: str = "127.0.0.1",
    port: int | None = None,
    open_browser: bool = True,
) -> None:
    try:
        import uvicorn
    except ImportError as exc:  # pragma: no cover
        raise RuntimeError('Install the UI with: pip install "merced-ai[webui]"') from exc

    if host not in LOOPBACK_HOSTS:
        raise ValueError("The UI is loopback-only; use 127.0.0.1, localhost, or ::1.")
    port, port_notice = choose_port(host, port)
    if port_notice:
        print(port_notice, file=sys.stderr)
    token = secrets.token_urlsafe(24)
    url_host = f"[{host}]" if ":" in host else host
    url = f"http://{url_host}:{port}/#token={token}"
    app = create_web_app(workspace, token)
    for notice in app.state.aais_presenter.notices:
        print(
            f"Warning: {notice['message']} The unreadable file was kept at "
            f"{notice['quarantined_path']}.",
            file=sys.stderr,
        )
    if open_browser:
        threading.Timer(0.8, lambda: webbrowser.open(url)).start()
    print(f"Merced AI UI: {url}", flush=True)
    uvicorn.run(app, host=host, port=port, log_level="warning")
