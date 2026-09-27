"""Shared state for one web UI server and the request-level security checks."""

from __future__ import annotations

import secrets
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Annotated, Any
from urllib.parse import urlsplit

from fastapi import Depends, HTTPException, Request

from merced_ai.aais_presenter import AAISPresenter
from merced_ai.harnesses.registry import HarnessRegistry
from merced_ai.run_supervisor import RunSupervisor
from merced_ai.web.probes import HarnessProbeCache

COOKIE_NAME = "merced_ai_ui"


@dataclass
class WebContext:
    workspace: Path
    access_token: str | None
    registry: HarnessRegistry
    presenter: AAISPresenter
    supervisor: RunSupervisor
    probes: HarnessProbeCache
    cancellations: dict[str, threading.Event] = field(default_factory=dict)
    # A2A tasks served by this process (kept in memory; the conversation itself is durable).
    a2a_tasks: dict[str, dict[str, Any]] = field(default_factory=dict)
    cancellation_lock: threading.Lock = field(default_factory=threading.Lock)

    def register_cancellation(self, run_id: str) -> threading.Event:
        event = threading.Event()
        with self.cancellation_lock:
            self.cancellations[run_id] = event
        return event

    def release_cancellation(self, run_id: str) -> None:
        with self.cancellation_lock:
            self.cancellations.pop(run_id, None)

    def cancellation(self, run_id: str) -> threading.Event | None:
        with self.cancellation_lock:
            return self.cancellations.get(run_id)


def get_context(request: Request) -> WebContext:
    context: WebContext = request.app.state.web
    return context


def authorize(request: Request, *, mutation: bool = False) -> WebContext:
    """Require the session cookie (or header) and, for mutations, a same-origin request."""
    context = get_context(request)
    bearer = request.headers.get("authorization", "")
    supplied = (
        request.cookies.get(COOKIE_NAME)
        or request.headers.get("x-merced-ai-token")
        or (bearer[7:].strip() if bearer.lower().startswith("bearer ") else None)
    )
    if context.access_token and not secrets.compare_digest(supplied or "", context.access_token):
        raise HTTPException(status_code=401, detail="Invalid local UI token")
    if mutation:
        origin = request.headers.get("origin")
        if origin and urlsplit(origin).netloc != request.headers.get("host"):
            raise HTTPException(status_code=403, detail="Cross-origin mutation rejected")
    return context


def reader(request: Request) -> WebContext:
    """FastAPI dependency for authenticated read endpoints."""
    return authorize(request)


def writer(request: Request) -> WebContext:
    """FastAPI dependency for authenticated, same-origin mutations."""
    return authorize(request, mutation=True)


ReadContext = Annotated[WebContext, Depends(reader)]
WriteContext = Annotated[WebContext, Depends(writer)]
