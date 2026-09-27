"""Run service: turns one user message into supervised harness runs and an SSE event stream."""

from __future__ import annotations

import asyncio
import json
import time
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass, field
from typing import Any
from uuid import uuid4

from fastapi import HTTPException
from fastapi.responses import StreamingResponse

from merced_ai.application import (
    PreparedRun,
    RoutingError,
    is_write_capable,
    isolate_group_turn,
    prepare_group_turn,
    shared_workspace_writers,
    write_serialization_message,
)
from merced_ai.bots import BotError
from merced_ai.harnesses.adapters.command import HarnessRunError
from merced_ai.models import RunResult, SessionRecord
from merced_ai.profiles import ProfileError
from merced_ai.sessions import SessionStore
from merced_ai.web.context import WebContext
from merced_ai.web.models import MessageInput
from merced_ai.workspace_context import RunStore, build_context_prompt

AAIS_HARNESSES = {"magagent", "loro"}


def sse(event: str, payload: dict[str, Any]) -> str:
    return f"event: {event}\ndata: {json.dumps(payload, ensure_ascii=False)}\n\n"


def raw_events(result: RunResult) -> list[dict[str, Any]]:
    events = (result.raw or {}).get("events")
    if not isinstance(events, list):
        return []
    return [item for item in events if isinstance(item, dict)][-100:]


@dataclass
class TurnPlan:
    session: SessionRecord
    payload: MessageInput
    prepared: tuple[PreparedRun, ...]
    context_manifest: list[dict[str, Any]]
    writers: tuple[str, ...]
    worktrees: dict[str, str] = field(default_factory=dict)
    fallback_reason: str | None = None

    @property
    def serialize_writers(self) -> bool:
        return bool(self.writers) and not self.payload.allow_concurrent_writes


class RunService:
    """Owns the lifecycle of web-initiated runs; HTTP streams only observe them."""

    def __init__(self, context: WebContext) -> None:
        self.context = context

    def plan(self, session_id: str, payload: MessageInput) -> TurnPlan:
        workspace = self.context.workspace
        try:
            session = SessionStore(workspace).load(session_id)
            context_prompt, manifest = build_context_prompt(workspace, payload.context)
            prepared = prepare_group_turn(
                session,
                payload.content.strip() + context_prompt,
                workspace,
                dispatch=payload.dispatch,
                registry=self.context.registry,
            )
        except (ValueError, BotError, ProfileError, RoutingError) as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        isolation = isolate_group_turn(
            session,
            prepared,
            workspace,
            allow_concurrent_writes=payload.allow_concurrent_writes,
        )
        writers = () if isolation.worktrees else shared_workspace_writers(isolation.prepared)
        return TurnPlan(
            session,
            payload,
            isolation.prepared,
            manifest,
            writers,
            isolation.worktrees,
            isolation.fallback_reason,
        )

    def approval_response(self, plan: TurnPlan) -> StreamingResponse | None:
        """Launch consent for profiles that may edit or run commands, unless already given."""
        approvals = [item for item in plan.prepared if is_write_capable(item.profile)]
        if not approvals or plan.payload.approved:
            return None

        async def stream() -> AsyncIterator[str]:
            yield sse(
                "approval_required",
                {
                    "message": "This profile may allow workspace edits or shell commands.",
                    "participants": [
                        {"bot_name": item.bot.name, "harness_id": item.request.harness_id}
                        for item in approvals
                    ],
                    "authority": "Harness policy remains authoritative.",
                },
            )

        return StreamingResponse(stream(), media_type="text/event-stream")

    def start(self, plan: TurnPlan) -> StreamingResponse:
        run_id = f"run-{uuid4().hex}"
        cancellation = self.context.register_cancellation(run_id)
        try:
            self.context.supervisor.start(run_id, self._events(run_id, plan), cancellation)
        except ValueError as exc:
            self.context.release_cancellation(run_id)
            raise HTTPException(status_code=429, detail=str(exc)) from exc
        return StreamingResponse(
            self.context.supervisor.stream(run_id),
            media_type="text/event-stream",
            headers={"X-Accel-Buffering": "no"},
        )

    async def _run_adapter(
        self,
        prepared: PreparedRun,
        run_id: str,
        emit: Callable[[str, dict[str, Any]], None],
    ) -> RunResult:
        adapter = self.context.registry.get(prepared.request.harness_id)
        cancellation = self.context.cancellation(run_id)
        runner = getattr(adapter, "run_cancellable", None)
        if runner is None:  # pragma: no cover - every built-in adapter is cancellable
            return await asyncio.to_thread(adapter.run, prepared.request)
        kwargs: dict[str, Any] = {}
        if getattr(adapter, "streams_output", False):
            kwargs["on_event"] = lambda event: emit(prepared.bot.name, event)
        if prepared.request.harness_id in AAIS_HARNESSES or getattr(
            adapter, "relays_approvals", False
        ):
            presenter = self.context.presenter
            return await asyncio.to_thread(
                runner,
                prepared.request,
                cancellation,
                presenter.present,
                presenter.record_event,
                **kwargs,
            )
        return await asyncio.to_thread(runner, prepared.request, cancellation, **kwargs)

    async def _events(self, run_id: str, plan: TurnPlan) -> AsyncIterator[str]:
        workspace = self.context.workspace
        store = SessionStore(workspace)
        started = time.monotonic()
        routes = [
            {"bot_name": item.bot.name, "harness_id": item.request.harness_id}
            for item in plan.prepared
        ]
        run_store = RunStore(workspace)
        record = run_store.start(
            run_id, plan.session.id, plan.payload.content, routes, plan.context_manifest
        )
        yield sse(
            "run_started",
            {
                "run_id": run_id,
                "harness_id": plan.prepared[0].request.harness_id,
                "participants": routes,
            },
        )
        store.append(plan.session, "user", plan.payload.content.strip())

        loop = asyncio.get_running_loop()
        queue: asyncio.Queue[tuple[str, Any]] = asyncio.Queue()
        # asyncio.Lock wakes waiters first-in, first-out, and tasks start in participant order,
        # so serialized writers run in participant order.
        write_lock = asyncio.Lock()

        def emit(bot_name: str, event: dict[str, Any]) -> None:
            loop.call_soon_threadsafe(queue.put_nowait, ("stream", (bot_name, event)))

        async def run_one(index: int, prepared: PreparedRun) -> None:
            try:
                if plan.serialize_writers and prepared.bot.name in plan.writers:
                    async with write_lock:
                        result: Any = await self._run_adapter(prepared, run_id, emit)
                else:
                    result = await self._run_adapter(prepared, run_id, emit)
            except BaseException as exc:  # Reported per participant below.
                result = exc
            await queue.put(("done", (index, prepared, result)))

        if plan.worktrees:
            isolation_notice: dict[str, Any] = {
                "run_id": run_id,
                "bots": list(plan.worktrees),
                "branches": plan.worktrees,
                "message": (
                    f"{', '.join(plan.worktrees)} each work in their own git worktree; "
                    "compare and apply their changes when they finish."
                ),
            }
            record.events.append({"type": "worktree_isolation", **isolation_notice})
            yield sse("worktree_isolation", isolation_notice)
        writers = plan.writers
        if writers:
            notice: dict[str, Any] = {
                "run_id": run_id,
                "bots": list(writers),
                "serialized": plan.serialize_writers,
                "fallback_reason": plan.fallback_reason,
                "message": (
                    write_serialization_message(writers)
                    if plan.serialize_writers
                    else "Concurrent writes allowed: "
                    + ", ".join(writers)
                    + " may edit this workspace at the same time."
                ),
            }
            record.events.append({"type": "write_serialization", **notice})
            yield sse("write_serialization", notice)
        for route in routes:
            name = route["bot_name"]
            if plan.serialize_writers and name in writers[1:]:
                ahead = writers[: writers.index(name)]
                yield sse(
                    "participant_queued", {"run_id": run_id, **route, "waiting_for": list(ahead)}
                )
            else:
                yield sse("participant_started", {"run_id": run_id, **route})

        tasks = [
            asyncio.create_task(run_one(index, prepared))
            for index, prepared in enumerate(plan.prepared)
        ]
        remaining = len(tasks)
        completed = failed = 0
        finished_cleanly = False
        by_bot = {item.bot.name: item for item in plan.prepared}
        try:
            while remaining:
                kind, value = await queue.get()
                if kind == "stream":
                    bot_name, event = value
                    prepared = by_bot[bot_name]
                    yield sse(
                        str(event.get("type", "assistant_delta")),
                        {
                            "run_id": run_id,
                            "bot_name": bot_name,
                            "harness_id": prepared.request.harness_id,
                            **{key: item for key, item in event.items() if key != "type"},
                        },
                    )
                    continue
                remaining -= 1
                index, prepared, result = value
                identity = {
                    "run_id": run_id,
                    "bot_name": prepared.bot.name,
                    "harness_id": prepared.request.harness_id,
                }
                if isinstance(result, BaseException):
                    failed += 1
                    cancelled = isinstance(result, HarnessRunError) and result.exit_code == 130
                    if len(plan.prepared) == 1:
                        event_name = "run_cancelled" if cancelled else "run_error"
                    else:
                        event_name = "participant_cancelled" if cancelled else "participant_error"
                    yield sse(event_name, {**identity, "message": str(result)})
                    continue
                completed += 1
                store.append(
                    plan.session,
                    "assistant",
                    result.output,
                    bot_name=prepared.bot.name,
                    harness_id=result.harness_id,
                    profile=prepared.profile,
                    turn_id=f"{run_id}:{index}",
                    native_session_id=result.native_session_id,
                )
                for native_event in raw_events(result):
                    record.events.append(
                        {"type": "tool_event", "bot_name": prepared.bot.name, "event": native_event}
                    )
                    yield sse("tool_event", {**identity, "event": native_event})
                yield sse(
                    "assistant_message",
                    {**identity, "content": result.output, "duration_ms": result.duration_ms},
                )
            finished_cleanly = True
        finally:
            cancellation = self.context.cancellation(run_id)
            pending = [task for task in tasks if not task.done()]
            if pending and cancellation is not None:
                cancellation.set()
            if pending:
                await asyncio.gather(*pending, return_exceptions=True)
            duration_ms = int((time.monotonic() - started) * 1000)
            run_store.finish(record, completed, failed, duration_ms)
            if not finished_cleanly:
                record.status = "interrupted"
            elif cancellation is not None and cancellation.is_set():
                record.status = "cancelled"
            run_store.save(record)
            self.context.release_cancellation(run_id)
        yield sse(
            "run_finished",
            {
                "run_id": run_id,
                "completed": completed,
                "failed": failed,
                "duration_ms": duration_ms,
                "context": plan.context_manifest,
            },
        )
