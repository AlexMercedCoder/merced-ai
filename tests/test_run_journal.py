"""The run journal is append-only JSON Lines with bounded replay and a legacy migration."""

from __future__ import annotations

import asyncio
import json
import threading
import time
from pathlib import Path

import pytest

from merced_ai import run_supervisor
from merced_ai.run_supervisor import RunJournal, RunSupervisor, read_journal


def _event(index: int) -> str:
    return f'event: tool_event\ndata: {{"index": {index}}}\n\n'


@pytest.mark.asyncio
async def test_journal_is_append_only_jsonl_and_replays(workspace: Path) -> None:
    supervisor = RunSupervisor(workspace)

    async def source():
        for index in range(3):
            yield _event(index)

    supervisor.start("run-abc", source(), threading.Event())
    await supervisor.live["run-abc"].task
    path = workspace / ".merced-ai" / "run-events" / "run-abc.jsonl"
    records = [json.loads(line) for line in path.read_text().splitlines()]

    assert [item["kind"] for item in records] == ["header", "event", "event", "event", "complete"]
    assert [item["sequence"] for item in records[1:4]] == [1, 2, 3]
    replay = [event async for event in supervisor.stream("run-abc", after=1)]
    assert [line.split("\n")[0] for line in replay] == ["id: 2", "id: 3"]
    assert supervisor.snapshot("run-abc")["complete"] is True


def test_torn_final_line_is_ignored(tmp_path: Path) -> None:
    path = tmp_path / "run-x.jsonl"
    journal = RunJournal(path, "run-x")
    journal.append(_event(1))
    path.write_text(path.read_text() + '{"kind": "event", "seq')

    state = read_journal(path)

    assert state["sequence"] == 1 and state["complete"] is False


def test_legacy_json_journal_is_migrated_on_first_read(workspace: Path) -> None:
    root = workspace / ".merced-ai" / "run-events"
    root.mkdir(parents=True)
    legacy = {
        "run_id": "run-old",
        "sequence": 2,
        "complete": True,
        "events": [{"sequence": 1, "sse": _event(1)}, {"sequence": 2, "sse": _event(2)}],
    }
    (root / "run-old.json").write_text(json.dumps(legacy))

    state = RunSupervisor(workspace).snapshot("run-old")

    assert state["events"] == legacy["events"] and state["complete"] is True
    assert not (root / "run-old.json").exists()
    assert (root / "run-old.jsonl").exists()


def test_replay_window_is_bounded_and_reports_gaps(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(run_supervisor, "MAX_REPLAY_EVENTS", 5)
    journal = RunJournal(tmp_path / "run-w.jsonl", "run-w")
    for index in range(12):
        journal.append(_event(index))
    journal.finish()

    assert [item["sequence"] for item in journal.state()["events"]] == [8, 9, 10, 11, 12]
    assert [item["sequence"] for item in read_journal(tmp_path / "run-w.jsonl")["events"]] == [
        8,
        9,
        10,
        11,
        12,
    ]


def test_per_event_append_cost_stays_flat(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Benchmark: appending event 5,000 costs about the same as appending event 1.

    The 0.7.0 journal rewrote the whole history per event, so late events cost O(n).
    fsync batching is disabled here so disk latency does not swamp the measurement.
    """
    monkeypatch.setattr(run_supervisor, "FSYNC_INTERVAL_SECONDS", 3600)
    journal = RunJournal(tmp_path / "run-bench.jsonl", "run-bench")
    payload = _event(0) + "x" * 2_000
    batch = 250

    def timed() -> float:
        started = time.perf_counter()
        for _ in range(batch):
            journal.append(payload)
        return (time.perf_counter() - started) / batch

    early = min(timed() for _ in range(3))
    for _ in range(4_000 // batch):
        timed()
    late = min(timed() for _ in range(3))
    journal.finish()

    assert late < early * 4, f"per-event cost grew from {early * 1e6:.1f}us to {late * 1e6:.1f}us"


@pytest.mark.asyncio
async def test_live_stream_reads_memory_not_disk(workspace: Path) -> None:
    supervisor = RunSupervisor(workspace)
    proceed = asyncio.Event()

    async def source():
        yield _event(1)
        await proceed.wait()
        yield _event(2)

    supervisor.start("run-live", source(), threading.Event())
    stream = supervisor.stream("run-live")
    first = await anext(stream)
    assert first.startswith("id: 1\n")
    proceed.set()
    rest = [item async for item in stream]
    assert rest and rest[0].startswith("id: 2\n")
