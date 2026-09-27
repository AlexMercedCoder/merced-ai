"""Experimental A2A (Agent2Agent) endpoint: another agent can message a bot or a room.

Implements the JSON-RPC binding's ``message/send``, ``tasks/get``, and ``tasks/cancel`` plus the
agent card. It shares the web UI's loopback binding and token: send
``Authorization: Bearer <token>`` (the token in the URL ``merced-ai ui`` prints). Streaming
(``message/stream``) and push notifications are not implemented and are declared as such.
"""

from __future__ import annotations

import asyncio
import threading
from datetime import UTC, datetime
from typing import Any, Literal, cast
from uuid import uuid4

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from merced_ai import __version__
from merced_ai.application import (
    RoutingError,
    is_write_capable,
    participant_from_run,
    prepare_group,
    prepare_run,
)
from merced_ai.bots import BotError, discover_bots
from merced_ai.profiles import ProfileError
from merced_ai.sessions import SessionStore
from merced_ai.turns import execute_turn, plan_turn
from merced_ai.web.context import ReadContext, WebContext, WriteContext

router = APIRouter()
A2A_PROTOCOL_VERSION = "0.3.0"
INVALID_PARAMS, METHOD_NOT_FOUND, TASK_NOT_FOUND, UNSUPPORTED = -32602, -32601, -32001, -32004
MAX_TEXT_CHARS = 100_000
# Tasks are kept in memory for tasks/get and tasks/cancel; the oldest are forgotten first.
MAX_TASKS = 200


def _now() -> str:
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


@router.get("/.well-known/agent-card.json")
async def agent_card(request: Request, context: ReadContext) -> dict[str, Any]:
    bots = discover_bots(context.workspace)
    base = str(request.base_url).rstrip("/")
    return {
        "protocolVersion": A2A_PROTOCOL_VERSION,
        "name": "Merced AI",
        "description": (
            "Portable OAP bots running on the agent harnesses installed on this machine. "
            "Send to one bot or a room with metadata.bots; experimental."
        ),
        "url": f"{base}/a2a",
        "preferredTransport": "JSONRPC",
        "version": __version__,
        "capabilities": {"streaming": False, "pushNotifications": False},
        "defaultInputModes": ["text/plain"],
        "defaultOutputModes": ["text/plain"],
        "securitySchemes": {"bearer": {"type": "http", "scheme": "bearer"}},
        "security": [{"bearer": []}],
        "skills": [
            {
                "id": bot.name,
                "name": bot.name,
                "description": f"Bot {bot.name} (profile {bot.profile}, harness "
                f"{bot.harness.preferred}).",
                "tags": ["merced-ai", bot.harness.preferred],
            }
            for bot in bots
        ],
    }


def _error(request_id: Any, code: int, message: str) -> JSONResponse:
    return JSONResponse(
        {"jsonrpc": "2.0", "id": request_id, "error": {"code": code, "message": message}}
    )


def _result(request_id: Any, result: dict[str, Any]) -> JSONResponse:
    return JSONResponse({"jsonrpc": "2.0", "id": request_id, "result": result})


@router.post("/a2a")
async def a2a(request: Request, context: WriteContext) -> JSONResponse:
    try:
        body = await request.json()
    except ValueError:
        return _error(None, -32700, "parse error")
    request_id = body.get("id") if isinstance(body, dict) else None
    if not isinstance(body, dict) or body.get("jsonrpc") != "2.0":
        return _error(request_id, -32600, "invalid JSON-RPC request")
    method, params = body.get("method"), body.get("params") or {}
    if method == "message/send":
        return await _message_send(context, request_id, params)
    if method == "tasks/get":
        task = context.a2a_tasks.get(str(params.get("id", "")))
        if task is None:
            return _error(request_id, TASK_NOT_FOUND, "task not found")
        return _result(request_id, {k: v for k, v in task.items() if not k.startswith("_")})
    if method == "tasks/cancel":
        task = context.a2a_tasks.get(str(params.get("id", "")))
        if task is None:
            return _error(request_id, TASK_NOT_FOUND, "task not found")
        cancel = task.get("_cancel")
        if isinstance(cancel, threading.Event):
            cancel.set()
        return _result(request_id, {k: v for k, v in task.items() if not k.startswith("_")})
    if method in {"message/stream", "tasks/resubscribe", "tasks/pushNotificationConfig/set"}:
        return _error(request_id, UNSUPPORTED, f"{method} is not supported by Merced AI yet")
    return _error(request_id, METHOD_NOT_FOUND, f"{method} is not supported")


def _text(message: dict[str, Any]) -> str:
    return "".join(
        str(part.get("text", ""))
        for part in message.get("parts", [])
        if isinstance(part, dict) and part.get("kind") == "text"
    )


def _agent_message(text: str, context_id: str, task_id: str) -> dict[str, Any]:
    return {
        "kind": "message",
        "role": "agent",
        "messageId": uuid4().hex,
        "parts": [{"kind": "text", "text": text}],
        "contextId": context_id,
        "taskId": task_id,
    }


async def _message_send(
    context: WebContext, request_id: Any, params: dict[str, Any]
) -> JSONResponse:
    message = params.get("message") or {}
    text = _text(message)
    if message.get("role") != "user" or not text.strip():
        return _error(request_id, INVALID_PARAMS, "message must be a user message with text parts")
    if len(text) > MAX_TEXT_CHARS:
        return _error(
            request_id, INVALID_PARAMS, f"message text is too long (max {MAX_TEXT_CHARS})"
        )
    metadata = {**(params.get("metadata") or {}), **(message.get("metadata") or {})}
    workspace = context.workspace
    store = SessionStore(workspace)
    context_id = str(message.get("contextId") or "")
    try:
        if context_id:
            session = store.load(context_id)
        else:
            bots = metadata.get("bots") or ([metadata["bot"]] if metadata.get("bot") else [])
            if not bots:
                known = discover_bots(workspace)
                bots = [known[0].name] if known else []
            if not bots:
                return _error(
                    request_id, INVALID_PARAMS, "no bots are configured in this workspace"
                )
            prepared = (
                prepare_group(tuple(bots), workspace, registry=context.registry)
                if len(bots) > 1
                else (prepare_run(bots[0], "Start.", workspace, registry=context.registry),)
            )
            session = store.create_group(
                tuple(participant_from_run(item) for item in prepared),
                mode=cast(Literal["mentions", "all", "round_robin"], metadata.get("mode", "all")),
                title=f"A2A · {', '.join(bots)}",
            )
        plan = plan_turn(session, text, workspace, registry=context.registry)
    except (ValueError, BotError, ProfileError, RoutingError) as error:
        return _error(request_id, INVALID_PARAMS, str(error))

    task_id = uuid4().hex
    task: dict[str, Any] = {
        "kind": "task",
        "id": task_id,
        "contextId": session.id,
        "status": {"state": "working", "timestamp": _now()},
        "history": [{**message, "contextId": session.id, "taskId": task_id}],
        "artifacts": [],
    }
    context.a2a_tasks[task_id] = task
    while len(context.a2a_tasks) > MAX_TASKS:
        context.a2a_tasks.pop(next(iter(context.a2a_tasks)))
    writers = [item.bot.name for item in plan.prepared if is_write_capable(item.profile)]
    if writers and metadata.get("approved") is not True:
        task["status"] = {
            "state": "input-required",
            "timestamp": _now(),
            "message": _agent_message(
                f"{', '.join(writers)} may edit files or run commands in {workspace}. Resend "
                "with metadata.approved = true to allow it for this message.",
                session.id,
                task_id,
            ),
        }
        return _result(request_id, task)

    cancellation = threading.Event()
    task["_cancel"] = cancellation
    outcome = await asyncio.to_thread(
        execute_turn,
        session,
        text,
        plan,
        workspace,
        registry=context.registry,
        cancellation=cancellation,
        approval_handler=context.presenter.present,
    )
    task["artifacts"] = [
        {"artifactId": uuid4().hex, "name": bot, "parts": [{"kind": "text", "text": reply}]}
        for bot, reply in outcome.replies
    ]
    failures = "; ".join(f"{bot}: {reason}" for bot, reason in outcome.failures)
    state = (
        "canceled"
        if cancellation.is_set()
        else "failed"
        if outcome.failures and not outcome.replies
        else "completed"
    )
    summary = "\n\n".join(
        f"{bot}: {reply}" if len(outcome.results) > 1 else reply for bot, reply in outcome.replies
    )
    if failures:
        summary = (summary + "\n\n" if summary else "") + f"Failed: {failures}"
    task["status"] = {
        "state": state,
        "timestamp": _now(),
        "message": _agent_message(summary, session.id, task_id),
    }
    task.pop("_cancel", None)
    return _result(request_id, task)
