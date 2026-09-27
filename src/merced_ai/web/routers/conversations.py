"""Conversation lifecycle, messages, run replay/cancel, and runtime approvals."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException
from fastapi.responses import PlainTextResponse, Response, StreamingResponse

from merced_ai.application import (
    PreparedRun,
    RoutingError,
    participant_from_run,
    prepare_group,
    prepare_run,
)
from merced_ai.bots import BotError
from merced_ai.profiles import ProfileError
from merced_ai.sessions import SessionStore
from merced_ai.web.context import ReadContext, WebContext, WriteContext
from merced_ai.web.models import AAISDecisionInput, MessageInput, SessionInput, SessionUpdateInput
from merced_ai.web.runs import RunService
from merced_ai.workspace_context import RunStore
from merced_ai.worktrees import WorktreeError, WorktreeManager

router = APIRouter()


def _prepare_participants(
    context: WebContext, payload: SessionInput, starter: str
) -> tuple[PreparedRun, ...]:
    names = payload.names()
    if len(names) == 1:
        return (
            prepare_run(
                names[0],
                starter,
                context.workspace,
                harness_override=payload.harness,
                registry=context.registry,
            ),
        )
    if payload.harness:
        raise ValueError("group sessions use each bot's pinned harness route")
    return prepare_group(names, context.workspace, registry=context.registry)


@router.post("/api/sessions", status_code=201)
async def session_create(payload: SessionInput, context: WriteContext) -> dict[str, Any]:
    try:
        prepared = _prepare_participants(context, payload, "Start the conversation.")
    except (BotError, ProfileError, RoutingError, ValueError) as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    if payload.isolation == "worktree" and len(prepared) < 2:
        raise HTTPException(status_code=409, detail="worktree isolation is for group rooms")
    session = SessionStore(context.workspace).create_group(
        tuple(participant_from_run(item) for item in prepared),
        mode=payload.mode,
        title=payload.title,
        isolation=payload.isolation,
    )
    return session.model_dump(mode="json")


@router.put("/api/sessions/{session_id}")
async def session_update(
    session_id: str, payload: SessionUpdateInput, context: WriteContext
) -> dict[str, Any]:
    store = SessionStore(context.workspace)
    try:
        session = store.load(session_id)
        store.rename(session, payload.title)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return session.model_dump(mode="json")


@router.delete("/api/sessions/{session_id}", status_code=204, response_model=None)
async def session_delete(session_id: str, context: WriteContext) -> Response:
    if any(
        item.session_id == session_id and item.status == "running"
        for item in RunStore(context.workspace).list()
    ):
        raise HTTPException(
            status_code=409, detail="Cancel the active run before deleting this conversation"
        )
    store = SessionStore(context.workspace)
    try:
        session = store.load(session_id)
        store.delete(session_id)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    if session.isolation == "worktree":
        try:
            WorktreeManager(context.workspace, session_id).remove_all()
        except WorktreeError:
            pass  # The repository is gone or was never a repository; nothing to clean.
    return Response(status_code=204)


@router.post("/api/sessions/{session_id}/derive", status_code=201)
async def session_derive(
    session_id: str, payload: SessionInput, context: WriteContext
) -> dict[str, Any]:
    store = SessionStore(context.workspace)
    try:
        store.load(session_id)
        prepared = _prepare_participants(
            context, payload.model_copy(update={"harness": None}), "Start the derived conversation."
        )
        derived = store.create_group(
            tuple(participant_from_run(item) for item in prepared),
            mode=payload.mode,
            title=payload.title,
            derived_from=session_id,
            isolation=payload.isolation if len(prepared) > 1 else "shared",
        )
    except (ValueError, BotError, ProfileError, RoutingError) as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return derived.model_dump(mode="json")


@router.get("/api/sessions/{session_id}/export", response_class=PlainTextResponse)
async def session_export(session_id: str, context: ReadContext) -> PlainTextResponse:
    try:
        session = SessionStore(context.workspace).load(session_id)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    title = (
        ", ".join(item.bot_name for item in session.participants)
        if session.kind == "group"
        else session.bot_name
    )
    lines = [f"# {title} conversation", ""]
    for turn in session.turns:
        speaker = turn.bot_name or turn.role.title()
        route = f" ({turn.harness_id})" if turn.harness_id else ""
        lines.extend((f"## {speaker}{route}", "", turn.content, ""))
    return PlainTextResponse(
        "\n".join(lines),
        headers={"Content-Disposition": f'attachment; filename="{session.id}.md"'},
        media_type="text/markdown",
    )


@router.post("/api/sessions/{session_id}/messages")
async def session_message(
    session_id: str, payload: MessageInput, context: WriteContext
) -> StreamingResponse:
    service = RunService(context)
    plan = service.plan(session_id, payload)
    return service.approval_response(plan) or service.start(plan)


@router.get("/api/runs/{run_id}/events")
async def replay_run(run_id: str, context: ReadContext, after: int = 0) -> StreamingResponse:
    try:
        context.supervisor.snapshot(run_id)
        if after < 0:
            raise ValueError("Event cursor must be nonnegative")
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return StreamingResponse(
        context.supervisor.stream(run_id, after), media_type="text/event-stream"
    )


@router.post("/api/runs/{run_id}/cancel")
async def cancel_run(run_id: str, context: WriteContext) -> dict[str, bool]:
    cancellation = context.cancellation(run_id)
    if cancellation is None:
        raise HTTPException(status_code=404, detail="Active run not found")
    cancellation.set()
    return {"cancelled": True}


@router.get("/api/approvals/recovery")
async def approval_recovery(context: ReadContext) -> dict[str, Any]:
    return context.presenter.recovery()


@router.get("/api/approvals/snapshot")
async def approval_snapshot(context: ReadContext) -> dict[str, Any]:
    return context.presenter.snapshot()


@router.post("/api/approvals/decisions")
async def approval_decision(payload: AAISDecisionInput, context: WriteContext) -> dict[str, Any]:
    try:
        decision = context.presenter.decide(
            payload.request_id,
            payload.decision,
            payload.scope,
            actor_id="local-user",
            decision_id=payload.decision_id,
        )
    except Exception as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return {"ok": True, "decision": decision}
