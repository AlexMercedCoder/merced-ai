"""Own broker runs independently of HTTP connections, with bounded durable event replay.

Each run has an append-only JSON Lines journal in ``.merced-ai/run-events/<run-id>.jsonl``:

- a ``header`` line when the run starts;
- one ``event`` line per server-sent event, with a monotonically increasing ``sequence``;
- a ``complete`` line when the run ends.

Appending an event costs the same no matter how long the run is (the previous format rewrote
and fsynced the whole history on every event). Lines are flushed immediately so any reader sees
them; ``fsync`` is batched to at most once per ``FSYNC_INTERVAL_SECONDS`` and always happens at
completion, so a crash can lose at most the last fraction of a second of events, and the run is
then marked interrupted on the next start. Journals written by 0.7.0 (one ``<run-id>.json``
document) are converted on first read.
"""

from __future__ import annotations

import asyncio
import json
import os
import threading
import time
from collections import deque
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from merced_ai.storage import atomic_write

MAX_REPLAY_EVENTS = 1000
MAX_REPLAY_BYTES = 24_000_000
FSYNC_INTERVAL_SECONDS = 0.25
JOURNAL_VERSION = 1
REPLAY_GAP_EVENT = (
    'event: replay_gap\ndata: {"message":"Earlier events were compacted; '
    'inspect the durable run record."}\n\n'
)


class RunJournal:
    """Append-only journal for one run plus a bounded in-memory replay window."""

    def __init__(self, path: Path, run_id: str) -> None:
        self.path = path
        self.run_id = run_id
        self.sequence = 0
        self.complete = False
        self.window: deque[dict[str, Any]] = deque()
        self._window_bytes = 0
        self._last_sync = 0.0
        path.parent.mkdir(parents=True, exist_ok=True)
        self._handle = open(path, "a", encoding="utf-8")  # noqa: SIM115 - held for the run
        self._write({"kind": "header", "run_id": run_id, "version": JOURNAL_VERSION}, sync=True)

    def _write(self, record: dict[str, Any], *, sync: bool = False) -> None:
        self._handle.write(json.dumps(record, ensure_ascii=False) + "\n")
        self._handle.flush()
        now = time.monotonic()
        if sync or now - self._last_sync >= FSYNC_INTERVAL_SECONDS:
            os.fsync(self._handle.fileno())
            self._last_sync = now

    def append(self, sse: str) -> int:
        self.sequence += 1
        item = {"sequence": self.sequence, "sse": sse}
        self._write({"kind": "event", **item})
        self.window.append(item)
        self._window_bytes += len(sse.encode())
        while len(self.window) > MAX_REPLAY_EVENTS or (
            len(self.window) > 1 and self._window_bytes > MAX_REPLAY_BYTES
        ):
            dropped = self.window.popleft()
            self._window_bytes -= len(dropped["sse"].encode())
        return self.sequence

    def finish(self) -> None:
        if self.complete:
            return
        self.complete = True
        self._write({"kind": "complete", "sequence": self.sequence}, sync=True)
        self._handle.close()

    def state(self) -> dict[str, Any]:
        return {
            "run_id": self.run_id,
            "sequence": self.sequence,
            "complete": self.complete,
            "events": list(self.window),
        }


def read_journal(path: Path) -> dict[str, Any]:
    """Load a journal file into the replay shape, keeping only the bounded tail."""
    events: deque[dict[str, Any]] = deque(maxlen=MAX_REPLAY_EVENTS)
    run_id, sequence, complete = path.stem, 0, False
    with open(path, encoding="utf-8") as handle:
        for line in handle:
            try:
                record = json.loads(line)
            except json.JSONDecodeError:
                continue  # A torn final line after a crash.
            kind = record.get("kind")
            if kind == "header":
                run_id = str(record.get("run_id", run_id))
            elif kind == "event":
                sequence = int(record["sequence"])
                events.append({"sequence": sequence, "sse": record["sse"]})
            elif kind == "complete":
                complete = True
    total = sum(len(item["sse"].encode()) for item in events)
    while len(events) > 1 and total > MAX_REPLAY_BYTES:
        total -= len(events.popleft()["sse"].encode())
    return {"run_id": run_id, "sequence": sequence, "complete": complete, "events": list(events)}


def migrate_legacy_journal(legacy: Path, target: Path) -> None:
    """Convert a 0.7.0 whole-document journal into the JSON Lines format."""
    state = json.loads(legacy.read_text(encoding="utf-8"))
    lines = [{"kind": "header", "run_id": state.get("run_id"), "version": JOURNAL_VERSION}]
    lines += [{"kind": "event", **item} for item in state.get("events", [])]
    if state.get("complete"):
        lines.append({"kind": "complete", "sequence": state.get("sequence", 0)})
    atomic_write(target, "".join(json.dumps(line, ensure_ascii=False) + "\n" for line in lines))
    legacy.unlink()


@dataclass
class LiveRun:
    cancellation: threading.Event
    journal: RunJournal
    changed: asyncio.Event = field(default_factory=asyncio.Event)
    task: asyncio.Task[None] | None = None


class RunSupervisor:
    def __init__(self, workspace: Path):
        self.root = workspace / ".merced-ai" / "run-events"
        self.live: dict[str, LiveRun] = {}

    def _path(self, run_id: str) -> Path:
        if not run_id.startswith("run-") or not run_id[4:].isalnum():
            raise ValueError("Invalid run identifier")
        return self.root / f"{run_id}.jsonl"

    def snapshot(self, run_id: str) -> dict[str, Any]:
        path = self._path(run_id)
        run = self.live.get(run_id)
        if run is not None:
            return run.journal.state()
        legacy = path.with_suffix(".json")
        if not path.exists() and legacy.exists():
            migrate_legacy_journal(legacy, path)
        if not path.exists():
            raise ValueError("Run event history was not found")
        return read_journal(path)

    def start(self, run_id: str, source: AsyncIterator[str], cancellation: threading.Event) -> None:
        if len(self.live) >= 12:
            raise ValueError("Twelve runs are already active; wait or cancel a run")
        path = self._path(run_id)
        run = LiveRun(cancellation, RunJournal(path, run_id))
        self.live[run_id] = run

        async def execute() -> None:
            journal = run.journal
            try:
                async for event in source:
                    journal.append(event)
                    run.changed.set()
            except Exception as error:
                payload = json.dumps({"run_id": run_id, "message": str(error)})
                journal.append(f"event: run_error\ndata: {payload}\n\n")
            finally:
                await asyncio.to_thread(journal.finish)
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
                yield REPLAY_GAP_EVENT
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
