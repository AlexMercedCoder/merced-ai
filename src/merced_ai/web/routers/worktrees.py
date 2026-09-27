"""Compare and apply the changes bots made in their own git worktrees."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException

from merced_ai.sessions import SessionStore
from merced_ai.web.context import ReadContext, WebContext, WriteContext
from merced_ai.worktrees import WorktreeError, WorktreeManager

router = APIRouter()


def _manager(context: WebContext, session_id: str) -> WorktreeManager:
    try:
        session = SessionStore(context.workspace).load(session_id)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    if session.isolation != "worktree":
        raise HTTPException(status_code=409, detail="This conversation does not use worktrees")
    try:
        return WorktreeManager(context.workspace, session_id)
    except WorktreeError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@router.get("/api/sessions/{session_id}/worktrees")
async def worktree_summary(session_id: str, context: ReadContext) -> dict[str, Any]:
    manager = _manager(context, session_id)
    bots = []
    for worktree in manager.worktrees():
        try:
            diff = manager.diff(worktree.bot_name).as_dict()
        except WorktreeError as exc:
            bots.append({**worktree.as_dict(), "error": str(exc)})
            continue
        diff.pop("patch")
        bots.append({**worktree.as_dict(), **diff})
    return {"session_id": session_id, "repository": str(manager.repository), "bots": bots}


@router.get("/api/sessions/{session_id}/worktrees/{bot_name}/diff")
async def worktree_diff(session_id: str, bot_name: str, context: ReadContext) -> dict[str, Any]:
    try:
        return _manager(context, session_id).diff(bot_name).as_dict()
    except WorktreeError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.post("/api/sessions/{session_id}/worktrees/{bot_name}/apply")
async def worktree_apply(session_id: str, bot_name: str, context: WriteContext) -> dict[str, Any]:
    try:
        return _manager(context, session_id).apply(bot_name)
    except WorktreeError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@router.delete("/api/sessions/{session_id}/worktrees")
async def worktree_cleanup(session_id: str, context: WriteContext) -> dict[str, Any]:
    return {"removed": _manager(context, session_id).remove_all()}
