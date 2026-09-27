"""Profile and bot lifecycle endpoints, plus projection previews."""

from __future__ import annotations

import asyncio
from typing import Any

from fastapi import APIRouter, HTTPException
from fastapi.responses import Response

from merced_ai.application import is_write_capable
from merced_ai.bots import BotError, create_bot, delete_bot, discover_bots, update_bot
from merced_ai.profile_generation import generate_profile_proposal
from merced_ai.profiles import (
    ProfileError,
    create_profile,
    create_profile_document,
    delete_profile,
    resolve_profile,
    update_profile,
)
from merced_ai.web.context import ReadContext, WebContext, WriteContext
from merced_ai.web.models import (
    BotInput,
    ProfileDocumentInput,
    ProfileGenerateInput,
    ProfileInput,
    ProfileUpdateInput,
)
from merced_ai.web.routers.workspace import profile_payload

router = APIRouter()


@router.get("/api/projection/{bot_name}")
async def projection(
    bot_name: str, context: ReadContext, harness: str | None = None
) -> dict[str, Any]:
    workspace = context.workspace
    bot = next((item for item in discover_bots(workspace) if item.name == bot_name), None)
    if bot is None:
        raise HTTPException(status_code=404, detail="Bot not found")
    profile = resolve_profile(bot.profile, workspace)
    try:
        report = context.registry.get(harness or bot.harness.preferred).project_profile(profile)
    except (KeyError, NotImplementedError) as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    payload = report.model_dump(mode="json", exclude={"system_prompt"})
    payload["approval_required"] = is_write_capable(profile)
    return payload


@router.post("/api/profiles", status_code=201)
async def profile_create(payload: ProfileInput, context: WriteContext) -> Any:
    try:
        record = create_profile(
            payload.name,
            payload.description,
            payload.instructions,
            context.workspace,
            model_provider=payload.model_provider,
            model_id=payload.model_id,
            edit_permission=payload.edit_permission,
            shell_permission=payload.shell_permission,
            scope=payload.scope,
        )
    except ProfileError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return profile_payload(record, context.workspace)


@router.post("/api/profiles/generate")
async def profile_generate(payload: ProfileGenerateInput, context: WriteContext) -> dict[str, Any]:
    try:
        return await asyncio.to_thread(
            generate_profile_proposal,
            payload.prompt,
            context.workspace,
            preferred_name=payload.name,
            harness_id=payload.harness,
            registry=context.registry,
            autonomous=False,
        )
    except (ProfileError, KeyError) as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@router.post("/api/profiles/document", status_code=201)
async def profile_create_document(
    payload: ProfileDocumentInput, context: WriteContext
) -> dict[str, Any]:
    try:
        record = create_profile_document(payload.document, context.workspace, scope=payload.scope)
    except ProfileError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return profile_payload(record, context.workspace)


@router.put("/api/profiles/{name}")
async def profile_update(
    name: str, payload: ProfileUpdateInput, context: WriteContext
) -> dict[str, Any]:
    try:
        record = update_profile(
            name,
            payload.description,
            payload.instructions,
            context.workspace,
            model_provider=payload.model_provider,
            model_id=payload.model_id,
            edit_permission=payload.edit_permission,
            shell_permission=payload.shell_permission,
        )
    except ProfileError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return profile_payload(record, context.workspace)


@router.delete("/api/profiles/{name}", status_code=204, response_model=None)
async def profile_delete(name: str, context: WriteContext) -> Response:
    workspace = context.workspace
    if any(
        resolve_profile(item.profile, workspace).name == name for item in discover_bots(workspace)
    ):
        raise HTTPException(
            status_code=409, detail="Delete or rebind bots that use this profile first"
        )
    try:
        delete_profile(name, workspace)
    except ProfileError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return Response(status_code=204)


def _check_harnesses(context: WebContext, payload: BotInput) -> None:
    context.registry.get(payload.harness)
    for fallback in payload.fallbacks:
        context.registry.get(fallback)


@router.post("/api/bots", status_code=201)
async def bot_create(payload: BotInput, context: WriteContext) -> dict[str, Any]:
    try:
        _check_harnesses(context, payload)
        binding = create_bot(
            payload.name,
            payload.profile,
            payload.harness,
            tuple(payload.fallbacks),
            context.workspace,
            requires_webmcp=payload.requires_webmcp,
        )
    except (BotError, ProfileError, KeyError) as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return binding.model_dump(mode="json")


@router.put("/api/bots/{name}")
async def bot_update(name: str, payload: BotInput, context: WriteContext) -> dict[str, Any]:
    try:
        if payload.name != name:
            raise BotError("bot names cannot be changed; create a new binding instead")
        _check_harnesses(context, payload)
        binding = update_bot(
            name,
            payload.profile,
            payload.harness,
            tuple(payload.fallbacks),
            context.workspace,
            requires_webmcp=payload.requires_webmcp,
        )
    except (BotError, ProfileError, KeyError) as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return binding.model_dump(mode="json")


@router.delete("/api/bots/{name}", status_code=204, response_model=None)
async def bot_delete(name: str, context: WriteContext) -> Response:
    try:
        delete_bot(name, context.workspace)
    except BotError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return Response(status_code=204)
