"""Authentication, bootstrap, harness detection, context files, and run history."""

from __future__ import annotations

import secrets
from pathlib import Path
from typing import Any

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import Response

from merced_ai.application import is_write_capable
from merced_ai.bots import BotError, discover_bots
from merced_ai.inbox import DeltaInbox
from merced_ai.models import BotBinding, ProfileRecord
from merced_ai.paths import ensure_user_layout
from merced_ai.profiles import ProfileError, discover_profiles, resolve_profile
from merced_ai.sessions import SessionStore
from merced_ai.web.context import COOKIE_NAME, ReadContext, WebContext, WriteContext, get_context
from merced_ai.web.models import AuthInput
from merced_ai.workspace_context import (
    RunStore,
    UploadInput,
    list_workspace_files,
    save_upload,
)

router = APIRouter()


def profile_payload(record: ProfileRecord, workspace: Path) -> dict[str, Any]:
    payload = record.model_dump(mode="json", exclude={"document"})
    resolved_parent = record.path.parent.resolve()
    user_root = ensure_user_layout() / "agents"
    universal_root = Path("~/.agentprofiles").expanduser().resolve()
    project_root = (workspace.resolve() / ".agents").resolve()
    payload["editable"] = resolved_parent in {project_root, user_root.resolve(), universal_root}
    if resolved_parent == project_root:
        payload["source_scope"] = "portable"
    elif record.origin in {".loro/agents", ".magent/agents"}:
        # Owned by Loro or MagAgent; edit it with that tool.
        payload["source_scope"] = "loro" if record.origin.startswith(".loro") else "magagent"
    elif resolved_parent == universal_root:
        payload["source_scope"] = "universal"
    else:
        payload["source_scope"] = "user"
    # The UI presents provenance, not the resolver's internal trust bucket.
    payload["source"] = payload["source_scope"]
    payload["instructions"] = record.document["spec"]["role"]["instructions"]
    payload["model"] = record.document["spec"].get("model", {})
    payload["permissions"] = record.document["spec"].get("permissions", {})
    return payload


def bot_payload(binding: BotBinding, workspace: Path) -> dict[str, Any]:
    payload = binding.model_dump(mode="json")
    payload["profile_origin"] = None
    payload["profile_problem"] = None
    try:
        profile = resolve_profile(binding.profile, workspace)
        payload["write_capable"] = is_write_capable(profile)
        payload["profile_origin"] = profile.origin or profile.source
    except (ProfileError, ValueError) as error:
        payload["write_capable"] = None  # Unknown until the profile resolves again.
        payload["profile_problem"] = str(error)
    return payload


def bootstrap_payload(context: WebContext) -> dict[str, Any]:
    workspace = context.workspace
    detection = context.probes.payload()
    return {
        "workspace": str(workspace),
        "profiles": [profile_payload(item, workspace) for item in discover_profiles(workspace)],
        "bots": [bot_payload(item, workspace) for item in discover_bots(workspace)],
        "sessions": [item.model_dump(mode="json") for item in SessionStore(workspace).list()],
        "harnesses": detection.pop("harnesses"),
        "harness_detection": detection,
        "recent_runs": [item.model_dump(mode="json") for item in RunStore(workspace).list(20)],
        "notices": list(context.presenter.notices),
        "inbox_pending": sum(
            item["status"] in {"pending", "conflict"}
            for item in DeltaInbox(context.workspace).items()
        ),
        "webmcp": {
            "supported_harnesses": [
                item.id for item in context.probes.descriptors if item.broker_implements.webmcp
            ],
            "setup": {"magagent": "magent webmcp origins", "loro": "loro setup webmcp"},
            "note": "WebMCP execution remains inside the selected harness and its approval policy.",
        },
    }


@router.post("/api/auth")
async def authenticate(payload: AuthInput, request: Request, response: Response) -> dict[str, bool]:
    token = get_context(request).access_token
    if token and not secrets.compare_digest(payload.token, token):
        raise HTTPException(status_code=401, detail="Invalid local UI token")
    response.set_cookie(
        COOKIE_NAME,
        token or payload.token,
        httponly=True,
        samesite="strict",
        secure=False,
        path="/",
    )
    return {"authenticated": True}


@router.post("/api/logout")
async def logout(response: Response, _: WriteContext) -> dict[str, bool]:
    response.delete_cookie(COOKIE_NAME, path="/")
    return {"authenticated": False}


@router.get("/api/bootstrap")
async def bootstrap(context: ReadContext) -> dict[str, Any]:
    try:
        return bootstrap_payload(context)
    except (BotError, ProfileError, ValueError) as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@router.get("/api/harnesses")
async def harnesses(context: ReadContext) -> dict[str, Any]:
    return context.probes.payload()


@router.post("/api/harnesses/refresh", status_code=202)
async def harnesses_refresh(context: WriteContext) -> dict[str, Any]:
    context.probes.start_refresh()
    return context.probes.payload()


@router.get("/api/handoff/{harness_id}")
async def harness_handoff(harness_id: str, context: ReadContext) -> dict[str, Any]:
    try:
        descriptor = context.registry.get(harness_id).descriptor
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="Harness not found") from exc
    executable = context.probes.path_for(harness_id) or descriptor.executable_names[0]
    return {
        "harness_id": harness_id,
        "workspace": str(context.workspace),
        "argv": [executable],
        "note": "Open the harness in this workspace; its own CLI remains authoritative.",
    }


@router.get("/api/context")
async def context_files(context: ReadContext, query: str = "") -> dict[str, Any]:
    return {
        "workspace": str(context.workspace),
        "files": list_workspace_files(context.workspace, query),
    }


@router.post("/api/sessions/{session_id}/attachments", status_code=201)
async def attachment_create(
    session_id: str, payload: UploadInput, context: WriteContext
) -> dict[str, Any]:
    try:
        SessionStore(context.workspace).load(session_id)
        return save_upload(context.workspace, session_id, payload)
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@router.get("/api/runs")
async def run_history(context: ReadContext, session_id: str | None = None) -> dict[str, Any]:
    records = RunStore(context.workspace).list()
    if session_id:
        records = [item for item in records if item.session_id == session_id]
    return {"runs": [item.model_dump(mode="json") for item in records]}
