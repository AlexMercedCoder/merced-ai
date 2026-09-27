"""Review OAP state deltas in the browser."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from merced_ai.inbox import DeltaInbox, InboxError
from merced_ai.profiles import ProfileError
from merced_ai.web.context import ReadContext, WriteContext

router = APIRouter()
ACTOR = "local-user"


class DeltaUpload(BaseModel):
    document: dict[str, Any]


class RememberInput(BaseModel):
    profile: str = Field(min_length=1, max_length=63)
    text: str = Field(min_length=1, max_length=2000)
    kind: str = Field(default="fact", pattern="^(fact|preference|thread)$")


class ApproveInput(BaseModel):
    rebase: bool = False


class RejectInput(BaseModel):
    reason: str = Field(default="", max_length=500)


class ProposalInput(BaseModel):
    approve: bool


def _call(action: Any) -> dict[str, Any]:
    try:
        result: dict[str, Any] = action()
        return result
    except (InboxError, ProfileError) as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@router.get("/api/inbox")
async def inbox_list(context: ReadContext) -> dict[str, Any]:
    items = DeltaInbox(context.workspace).items()
    return {
        "items": items,
        "pending": sum(item["status"] in {"pending", "conflict"} for item in items),
    }


@router.post("/api/inbox", status_code=201)
async def inbox_add(payload: DeltaUpload, context: WriteContext) -> dict[str, Any]:
    return _call(lambda: DeltaInbox(context.workspace).add(payload.document, source="web upload"))


@router.post("/api/inbox/remember", status_code=201)
async def inbox_remember(payload: RememberInput, context: WriteContext) -> dict[str, Any]:
    return _call(
        lambda: DeltaInbox(context.workspace).remember(
            payload.profile, payload.text, kind=payload.kind, actor=ACTOR
        )
    )


@router.post("/api/inbox/{item_id}/approve")
async def inbox_approve(
    item_id: str, payload: ApproveInput, context: WriteContext
) -> dict[str, Any]:
    return _call(
        lambda: DeltaInbox(context.workspace).approve(item_id, actor=ACTOR, rebase=payload.rebase)
    )


@router.post("/api/inbox/{item_id}/reject")
async def inbox_reject(item_id: str, payload: RejectInput, context: WriteContext) -> dict[str, Any]:
    return _call(
        lambda: DeltaInbox(context.workspace).reject(item_id, actor=ACTOR, reason=payload.reason)
    )


@router.post("/api/inbox/{item_id}/proposals/{index}")
async def inbox_proposal(
    item_id: str, index: int, payload: ProposalInput, context: WriteContext
) -> dict[str, Any]:
    return _call(
        lambda: DeltaInbox(context.workspace).decide_proposal(
            item_id, index, approve=payload.approve, actor=ACTOR
        )
    )
