"""Run records identify their owner with aais.liveness OwnerIdentity (I-25)."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

from aais.liveness import OwnerIdentity, current_host_id, process_start_time

from merced_ai.process_liveness import process_alive
from merced_ai.workspace_context import RunStore


def _running(store: RunStore, run_id: str, **owner: object):  # type: ignore[no-untyped-def]
    record = store.start(run_id, "session-" + "a" * 32, "hello", [], [])
    record.owner_pid = owner.get("owner_pid")  # type: ignore[assignment]
    record.owner = owner.get("owner")  # type: ignore[assignment]
    store.save(record)
    return record


def _status(store: RunStore, run_id: str) -> str:
    return next(item for item in store.list() if item.id == run_id).status


def test_new_runs_record_a_full_owner_identity(workspace: Path) -> None:
    record = RunStore(workspace).start("run-new", "session-" + "b" * 32, "hi", [], [])
    assert record.owner == OwnerIdentity.current().to_dict()
    assert record.owner_pid == os.getpid()


def test_a_reused_pid_does_not_keep_a_run_alive(workspace: Path) -> None:
    store = RunStore(workspace)
    started = process_start_time(os.getpid())
    assert started is not None
    # Our PID, our host, but a process that started an hour earlier: the PID was reused.
    ghost = OwnerIdentity(os.getpid(), started - 3600, current_host_id()).to_dict()
    _running(store, "run-ghost", owner_pid=os.getpid(), owner=ghost)
    _running(store, "run-live", owner_pid=os.getpid(), owner=OwnerIdentity.current().to_dict())

    store.recover_interrupted()

    assert _status(store, "run-ghost") == "interrupted"
    assert _status(store, "run-live") == "running"


def test_an_owner_on_another_host_is_never_treated_as_dead(workspace: Path) -> None:
    store = RunStore(workspace)
    elsewhere = OwnerIdentity(999_999, 1.0, "host-somewhere-else").to_dict()
    _running(store, "run-remote", owner_pid=999_999, owner=elsewhere)

    store.recover_interrupted()

    assert _status(store, "run-remote") == "running"


def test_legacy_pid_only_records_are_checked_and_upgraded(workspace: Path) -> None:
    store = RunStore(workspace)
    child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
    try:
        _running(store, "run-legacy-live", owner_pid=child.pid, owner=None)
        store.recover_interrupted()
        upgraded = next(item for item in store.list() if item.id == "run-legacy-live")
        assert upgraded.status == "running"
        assert upgraded.owner is not None and upgraded.owner["pid"] == child.pid
        assert upgraded.owner["process_start_time"] == process_start_time(child.pid)
    finally:
        child.kill()
        child.wait()
    _running(store, "run-legacy-dead", owner_pid=child.pid, owner=None)
    _running(store, "run-no-owner", owner_pid=None, owner=None)

    store.recover_interrupted()

    assert _status(store, "run-legacy-live") == "interrupted"  # its process has exited
    assert _status(store, "run-legacy-dead") == "interrupted"
    assert _status(store, "run-no-owner") == "interrupted"


def test_process_alive_is_pid_only_and_never_signals() -> None:
    assert process_alive(os.getpid())
    assert not process_alive(None) and not process_alive(0) and not process_alive(True)
