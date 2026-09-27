"""Transport-neutral execution of one conversation turn (used by the ACP and A2A servers).

It applies the same rules as the CLI and web UI: routing and profile-drift checks, worktree
isolation or write serialization for write-capable bots, per-participant failure containment,
durable attributed turns, and harness session IDs for native resume.
"""

from __future__ import annotations

import threading
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from merced_ai.application import IsolationPlan, PreparedRun, isolate_group_turn, prepare_group_turn
from merced_ai.harnesses.registry import HarnessRegistry
from merced_ai.models import RunResult, SessionRecord
from merced_ai.sessions import SessionStore

ApprovalHandler = Callable[[dict[str, Any], threading.Event | None], dict[str, Any]]
BotEventHandler = Callable[[str, dict[str, Any]], None]


@dataclass
class TurnOutcome:
    plan: IsolationPlan
    results: list[tuple[PreparedRun, RunResult | Exception]] = field(default_factory=list)

    @property
    def replies(self) -> list[tuple[str, str]]:
        return [
            (prepared.bot.name, result.output)
            for prepared, result in self.results
            if isinstance(result, RunResult)
        ]

    @property
    def failures(self) -> list[tuple[str, str]]:
        return [
            (prepared.bot.name, str(result))
            for prepared, result in self.results
            if isinstance(result, Exception)
        ]


def plan_turn(
    session: SessionRecord,
    prompt: str,
    workspace: Path,
    *,
    registry: HarnessRegistry,
    dispatch: str | None = None,
    allow_concurrent_writes: bool = False,
) -> IsolationPlan:
    prepared = prepare_group_turn(session, prompt, workspace, dispatch=dispatch, registry=registry)
    return isolate_group_turn(
        session, prepared, workspace, allow_concurrent_writes=allow_concurrent_writes
    )


def execute_turn(
    session: SessionRecord,
    prompt: str,
    plan: IsolationPlan,
    workspace: Path,
    *,
    registry: HarnessRegistry,
    cancellation: threading.Event | None = None,
    approval_handler: ApprovalHandler | None = None,
    on_event: BotEventHandler | None = None,
) -> TurnOutcome:
    store = SessionStore(workspace)
    store.append(session, "user", prompt)
    outcome = TurnOutcome(plan)
    serial = [item for item in plan.prepared if item.bot.name in plan.serialized]
    parallel = [item for item in plan.prepared if item not in serial]
    results: dict[str, RunResult | Exception] = {}

    def run(prepared: PreparedRun) -> None:
        adapter = registry.get(prepared.request.harness_id)
        runner = getattr(adapter, "run_cancellable", None)
        try:
            if runner is None:  # pragma: no cover - every built-in adapter is cancellable
                results[prepared.bot.name] = adapter.run(prepared.request)
                return
            kwargs: dict[str, Any] = {}
            if getattr(adapter, "streams_output", False) and on_event is not None:
                name = prepared.bot.name
                kwargs["on_event"] = lambda event: on_event(name, event)
            relays = getattr(adapter, "relays_approvals", False)
            results[prepared.bot.name] = runner(
                prepared.request,
                cancellation,
                approval_handler if relays else None,
                None,
                **kwargs,
            )
        except Exception as error:  # Contained per participant.
            results[prepared.bot.name] = error

    def run_serially() -> None:
        for prepared in serial:
            run(prepared)

    with ThreadPoolExecutor(max_workers=max(1, len(parallel) + (1 if serial else 0))) as pool:
        futures = [pool.submit(run, item) for item in parallel]
        if serial:
            futures.append(pool.submit(run_serially))
        for future in futures:
            future.result()
    for prepared in plan.prepared:
        result = results[prepared.bot.name]
        if isinstance(result, RunResult):
            store.append(
                session,
                "assistant",
                result.output,
                bot_name=prepared.bot.name,
                harness_id=result.harness_id,
                profile=prepared.profile,
                native_session_id=result.native_session_id,
            )
        outcome.results.append((prepared, result))
    return outcome
