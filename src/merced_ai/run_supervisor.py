"""Own broker runs independently of HTTP connections, with bounded durable event replay."""

from __future__ import annotations

import asyncio
import json
import threading
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from pathlib import Path

from merced_ai.storage import atomic_write

MAX_REPLAY_EVENTS = 1000
MAX_REPLAY_BYTES = 24_000_000


@dataclass
class LiveRun:
    cancellation: threading.Event
    changed: asyncio.Event = field(default_factory=asyncio.Event)
    task: asyncio.Task | None = None


class RunSupervisor:
    def __init__(self, workspace: Path):
        self.root = workspace / ".merced-ai" / "run-events"
        self.live: dict[str, LiveRun] = {}

    def _path(self, run_id: str) -> Path:
        if not run_id.startswith("run-") or not run_id[4:].isalnum():
            raise ValueError("Invalid run identifier")
        return self.root / f"{run_id}.json"

    def snapshot(self, run_id: str) -> dict:
        path = self._path(run_id)
        if not path.exists():
            raise ValueError("Run event history was not found")
        return json.loads(path.read_text())

    def start(self, run_id: str, source: AsyncIterator[str], cancellation: threading.Event) -> None:
        if len(self.live) >= 12:
            raise ValueError("Twelve runs are already active; wait or cancel a run")
        run = LiveRun(cancellation)
        self.live[run_id] = run
        path = self._path(run_id)
        state = {"run_id": run_id, "sequence": 0, "complete": False, "events": []}
        atomic_write(path, json.dumps(state))

        async def execute() -> None:
            try:
                async for event in source:
                    state["sequence"] += 1
                    state["events"].append({"sequence": state["sequence"], "sse": event})
                    state["events"] = state["events"][-MAX_REPLAY_EVENTS:]
                    while (
                        len(state["events"]) > 1
                        and sum(len(item["sse"].encode()) for item in state["events"])
                        > MAX_REPLAY_BYTES
                    ):
                        state["events"].pop(0)
                    await asyncio.to_thread(atomic_write, path, json.dumps(state))
                    run.changed.set()
            except Exception as error:
                state["sequence"] += 1
                payload = json.dumps({"run_id": run_id, "message": str(error)})
                state["events"].append(
                    {"sequence": state["sequence"], "sse": f"event: run_error\ndata: {payload}\n\n"}
                )
            finally:
                state["complete"] = True
                await asyncio.to_thread(atomic_write, path, json.dumps(state))
                run.changed.set()
                self.live.pop(run_id, None)

        run.task = asyncio.create_task(execute())

    async def stream(self, run_id: str, after: int = 0) -> AsyncIterator[str]:
        while True:
            run = self.live.get(run_id)
            if run:
                run.changed.clear()
            state = self.snapshot(run_id)
            events = state["events"]
            if events and after and after < events[0]["sequence"] - 1:
                yield (
                    'event: replay_gap\ndata: {"message":"Earlier events were compacted; '
                    'inspect the durable run record."}\n\n'
                )
            for item in events:
                if item["sequence"] > after:
                    after = item["sequence"]
                    yield f"id: {after}\n{item['sse']}"
            if state["complete"] or not run:
                return
            try:
                await asyncio.wait_for(run.changed.wait(), timeout=0.25)
            except TimeoutError:
                continue

    async def close(self) -> None:
        runs = list(self.live.values())
        for run in runs:
            run.cancellation.set()
        if runs:
            await asyncio.gather(*(run.task for run in runs if run.task), return_exceptions=True)
