"""Optional loopback-first web UI over Merced AI's application services."""

from __future__ import annotations

import asyncio
import json
import secrets
import threading
import webbrowser
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit
from uuid import uuid4

from pydantic import BaseModel, Field

from merced_ai.application import RoutingError, prepare_run
from merced_ai.bots import BotError, create_bot, discover_bots
from merced_ai.harnesses import default_registry
from merced_ai.harnesses.adapters.command import HarnessRunError
from merced_ai.models import ProfileRecord, RunResult
from merced_ai.profiles import (
    ProfileError,
    create_profile,
    discover_profiles,
    resolve_profile,
    update_profile,
)
from merced_ai.sessions import SessionStore, transcript_prompt

try:  # Optional dependency boundary.
    from fastapi import FastAPI, HTTPException, Request
    from fastapi.responses import HTMLResponse, PlainTextResponse, Response, StreamingResponse
    from fastapi.staticfiles import StaticFiles
except ImportError:  # pragma: no cover
    FastAPI = HTTPException = Request = None  # type: ignore[assignment,misc]
    HTMLResponse = PlainTextResponse = Response = StreamingResponse = StaticFiles = None  # type: ignore[assignment,misc]


COOKIE_NAME = "merced_ai_ui"
MAX_MESSAGE_CHARS = 100_000


class AuthInput(BaseModel):
    token: str


class ProfileInput(BaseModel):
    name: str
    description: str = Field(min_length=1, max_length=500)
    instructions: str = Field(min_length=1, max_length=100_000)
    model_provider: str | None = Field(default=None, max_length=60)
    model_id: str | None = Field(default=None, max_length=200)
    edit_permission: str | None = None
    shell_permission: str | None = None


class ProfileUpdateInput(BaseModel):
    description: str = Field(min_length=1, max_length=500)
    instructions: str = Field(min_length=1, max_length=100_000)
    model_provider: str | None = Field(default=None, max_length=60)
    model_id: str | None = Field(default=None, max_length=200)
    edit_permission: str | None = None
    shell_permission: str | None = None


class BotInput(BaseModel):
    name: str
    profile: str
    harness: str
    fallbacks: list[str] = Field(default_factory=list, max_length=14)


class SessionInput(BaseModel):
    bot_name: str
    harness: str | None = None


class MessageInput(BaseModel):
    content: str = Field(min_length=1, max_length=MAX_MESSAGE_CHARS)
    approved: bool = False


def _sse(event: str, payload: dict[str, Any]) -> str:
    return f"event: {event}\ndata: {json.dumps(payload, ensure_ascii=False)}\n\n"


def _profile_payload(record: ProfileRecord, workspace: Path) -> dict[str, Any]:
    payload = record.model_dump(mode="json", exclude={"document"})
    payload["editable"] = (
        record.source == "project"
        and record.path.parent.resolve() == (workspace.resolve() / ".agents").resolve()
    )
    payload["instructions"] = record.document["spec"]["role"]["instructions"]
    payload["model"] = record.document["spec"].get("model", {})
    payload["permissions"] = record.document["spec"].get("permissions", {})
    return payload


def _approval_needed(profile: ProfileRecord) -> bool:
    permissions = profile.document.get("spec", {}).get("permissions", {})
    return permissions.get("edit") != "deny" or permissions.get("shell") != "deny"


def _raw_events(result: RunResult) -> list[dict[str, Any]]:
    events = (result.raw or {}).get("events")
    if not isinstance(events, list):
        return []
    return [item for item in events if isinstance(item, dict)][-100:]


def create_web_app(workspace: Path, access_token: str | None = None) -> Any:
    if FastAPI is None:
        raise RuntimeError('Install the UI with: pip install "merced-ai[webui]"')

    workspace = workspace.resolve()
    static_root = Path(__file__).resolve().parent / "webui"
    app = FastAPI(title="Merced AI", docs_url=None, redoc_url=None, openapi_url=None)
    app.mount("/assets", StaticFiles(directory=static_root), name="assets")
    cancellations: dict[str, threading.Event] = {}
    cancellation_lock = threading.Lock()

    @app.middleware("http")
    async def security_headers(request: Request, call_next: Any) -> Any:
        response = await call_next(request)
        response.headers["Cache-Control"] = "no-store"
        response.headers["Content-Security-Policy"] = (
            "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; "
            "connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'"
        )
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["Permissions-Policy"] = "camera=(), microphone=(), geolocation=()"
        return response

    def authorize(request: Request, *, mutation: bool = False) -> None:
        supplied = request.cookies.get(COOKIE_NAME) or request.headers.get("x-merced-ai-token")
        if access_token and not secrets.compare_digest(supplied or "", access_token):
            raise HTTPException(status_code=401, detail="Invalid local UI token")
        if mutation:
            origin = request.headers.get("origin")
            if origin and urlsplit(origin).netloc != request.headers.get("host"):
                raise HTTPException(status_code=403, detail="Cross-origin mutation rejected")

    def bootstrap_payload() -> dict[str, Any]:
        profiles = discover_profiles(workspace)
        return {
            "workspace": str(workspace),
            "profiles": [_profile_payload(item, workspace) for item in profiles],
            "bots": [item.model_dump(mode="json") for item in discover_bots(workspace)],
            "sessions": [item.model_dump(mode="json") for item in SessionStore(workspace).list()],
            "harnesses": [item.model_dump(mode="json") for item in default_registry().probe_all()],
        }

    @app.get("/", response_class=HTMLResponse)
    async def index() -> HTMLResponse:
        return HTMLResponse((static_root / "index.html").read_text(encoding="utf-8"))

    @app.post("/api/auth")
    async def authenticate(payload: AuthInput, response: Response) -> dict[str, bool]:
        if access_token and not secrets.compare_digest(payload.token, access_token):
            raise HTTPException(status_code=401, detail="Invalid local UI token")
        response.set_cookie(
            COOKIE_NAME,
            access_token or payload.token,
            httponly=True,
            samesite="strict",
            secure=False,
            path="/",
        )
        return {"authenticated": True}

    @app.post("/api/logout")
    async def logout(request: Request, response: Response) -> dict[str, bool]:
        authorize(request, mutation=True)
        response.delete_cookie(COOKIE_NAME, path="/")
        return {"authenticated": False}

    @app.get("/api/bootstrap")
    async def bootstrap(request: Request) -> dict[str, Any]:
        authorize(request)
        try:
            return bootstrap_payload()
        except (BotError, ProfileError, ValueError) as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc

    @app.get("/api/projection/{bot_name}")
    async def projection(
        bot_name: str, request: Request, harness: str | None = None
    ) -> dict[str, Any]:
        authorize(request)
        bot = next((item for item in discover_bots(workspace) if item.name == bot_name), None)
        if bot is None:
            raise HTTPException(status_code=404, detail="Bot not found")
        profile = resolve_profile(bot.profile, workspace)
        try:
            report = (
                default_registry().get(harness or bot.harness.preferred).project_profile(profile)
            )
        except (KeyError, NotImplementedError) as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        payload = report.model_dump(mode="json", exclude={"system_prompt"})
        payload["approval_required"] = _approval_needed(profile)
        return payload

    @app.post("/api/profiles", status_code=201)
    async def profile_create(payload: ProfileInput, request: Request) -> dict[str, Any]:
        authorize(request, mutation=True)
        try:
            record = create_profile(
                payload.name,
                payload.description,
                payload.instructions,
                workspace,
                model_provider=payload.model_provider,
                model_id=payload.model_id,
                edit_permission=payload.edit_permission,
                shell_permission=payload.shell_permission,
            )
        except ProfileError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        return _profile_payload(record, workspace)

    @app.put("/api/profiles/{name}")
    async def profile_update(
        name: str, payload: ProfileUpdateInput, request: Request
    ) -> dict[str, Any]:
        authorize(request, mutation=True)
        try:
            record = update_profile(
                name,
                payload.description,
                payload.instructions,
                workspace,
                model_provider=payload.model_provider,
                model_id=payload.model_id,
                edit_permission=payload.edit_permission,
                shell_permission=payload.shell_permission,
            )
        except ProfileError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        return _profile_payload(record, workspace)

    @app.post("/api/bots", status_code=201)
    async def bot_create(payload: BotInput, request: Request) -> dict[str, Any]:
        authorize(request, mutation=True)
        registry = default_registry()
        try:
            registry.get(payload.harness)
            for fallback in payload.fallbacks:
                registry.get(fallback)
            binding = create_bot(
                payload.name, payload.profile, payload.harness, tuple(payload.fallbacks), workspace
            )
        except (BotError, ProfileError, KeyError) as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        return binding.model_dump(mode="json")

    @app.post("/api/sessions", status_code=201)
    async def session_create(payload: SessionInput, request: Request) -> dict[str, Any]:
        authorize(request, mutation=True)
        try:
            prepared = prepare_run(
                payload.bot_name,
                "Start the conversation.",
                workspace,
                harness_override=payload.harness,
            )
        except (BotError, ProfileError, RoutingError) as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        session = SessionStore(workspace).create(
            payload.bot_name, prepared.request.harness_id, prepared.profile
        )
        return session.model_dump(mode="json")

    @app.get("/api/sessions/{session_id}/export", response_class=PlainTextResponse)
    async def session_export(session_id: str, request: Request) -> PlainTextResponse:
        authorize(request)
        try:
            session = SessionStore(workspace).load(session_id)
        except ValueError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        lines = [f"# {session.bot_name} conversation", ""]
        for turn in session.turns:
            lines.extend((f"## {turn.role.title()}", "", turn.content, ""))
        return PlainTextResponse(
            "\n".join(lines),
            headers={"Content-Disposition": f'attachment; filename="{session.id}.md"'},
            media_type="text/markdown",
        )

    @app.post("/api/sessions/{session_id}/messages")
    async def session_message(
        session_id: str, payload: MessageInput, request: Request
    ) -> StreamingResponse:
        authorize(request, mutation=True)
        store = SessionStore(workspace)
        try:
            session = store.load(session_id)
            expanded = transcript_prompt(session, payload.content.strip())
            prepared = prepare_run(
                session.bot_name, expanded, workspace, harness_override=session.harness_id
            )
        except (ValueError, BotError, ProfileError, RoutingError) as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        if _approval_needed(prepared.profile) and not payload.approved:

            async def approval_stream() -> Any:
                yield _sse(
                    "approval_required",
                    {
                        "message": "This profile may allow workspace edits or shell commands.",
                        "harness_id": prepared.request.harness_id,
                        "authority": "Harness policy remains authoritative.",
                    },
                )

            return StreamingResponse(approval_stream(), media_type="text/event-stream")

        run_id = f"run-{uuid4().hex}"
        cancellation = threading.Event()
        with cancellation_lock:
            cancellations[run_id] = cancellation

        async def event_stream() -> Any:
            yield _sse("run_started", {"run_id": run_id, "harness_id": prepared.request.harness_id})
            store.append(session, "user", payload.content.strip())
            try:
                adapter = default_registry().get(prepared.request.harness_id)
                if hasattr(adapter, "run_cancellable"):
                    result = await asyncio.to_thread(
                        adapter.run_cancellable,
                        prepared.request,
                        cancellation,  # type: ignore[attr-defined]
                    )
                else:  # pragma: no cover
                    result = await asyncio.to_thread(adapter.run, prepared.request)
            except HarnessRunError as exc:
                event = "run_cancelled" if exc.exit_code == 130 else "run_error"
                yield _sse(event, {"run_id": run_id, "message": str(exc)})
                return
            finally:
                with cancellation_lock:
                    cancellations.pop(run_id, None)
            for native_event in _raw_events(result):
                yield _sse("tool_event", {"run_id": run_id, "event": native_event})
            store.append(session, "assistant", result.output)
            yield _sse(
                "assistant_message",
                {
                    "run_id": run_id,
                    "content": result.output,
                    "duration_ms": result.duration_ms,
                    "harness_id": result.harness_id,
                },
            )
            yield _sse("run_finished", {"run_id": run_id})

        return StreamingResponse(
            event_stream(), media_type="text/event-stream", headers={"X-Accel-Buffering": "no"}
        )

    @app.post("/api/runs/{run_id}/cancel")
    async def cancel_run(run_id: str, request: Request) -> dict[str, bool]:
        authorize(request, mutation=True)
        with cancellation_lock:
            cancellation = cancellations.get(run_id)
        if cancellation is None:
            raise HTTPException(status_code=404, detail="Active run not found")
        cancellation.set()
        return {"cancelled": True}

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
        raise ValueError("The UI is loopback-only; use 127.0.0.1, localhost, or ::1.")
    token = secrets.token_urlsafe(24)
    url = f"http://{host}:{port}/#token={token}"
    if open_browser:
        threading.Timer(0.8, lambda: webbrowser.open(url)).start()
    print(f"Merced AI UI: {url}")
    uvicorn.run(create_web_app(workspace, token), host=host, port=port, log_level="warning")
