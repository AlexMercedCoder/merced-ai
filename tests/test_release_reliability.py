"""Regression fixtures for release lifecycle and persistence failures."""

import asyncio
import os
import sys
import threading
from concurrent.futures import ThreadPoolExecutor

import pytest

from merced_ai.harnesses.process import ChildProcessError, run_child
from merced_ai.profiles import create_profile
from merced_ai.run_supervisor import RunSupervisor
from merced_ai.sessions import SessionStore


@pytest.fixture
def storage_workspace(workspace, monkeypatch):
    """A workspace for lock-contention tests that does not depend on disk latency.

    Twelve writers each fsync the file and its directory while holding the lock. On a loaded
    machine that queue took up to 28 s of the 30 s lock timeout, so the test failed with "Storage
    is busy" although the locking was correct. Use tmpfs when there is one and give the test a
    longer timeout; production keeps 30 s.
    """
    import shutil
    import tempfile
    from pathlib import Path

    from merced_ai import storage

    monkeypatch.setattr(storage, "LOCK_TIMEOUT_SECONDS", 180.0)
    shm = Path("/dev/shm")
    if not (shm.is_dir() and os.access(shm, os.W_OK)):
        yield workspace
        return
    root = Path(tempfile.mkdtemp(prefix="merced-ai-test-", dir=shm))
    try:
        monkeypatch.setenv("MERCED_AI_HOME", str(root / "merced-home"))
        path = root / "workspace"
        path.mkdir()
        yield path
    finally:
        shutil.rmtree(root, ignore_errors=True)


def test_parallel_appends_and_stale_save(storage_workspace):
    workspace = storage_workspace
    profile = create_profile("reviewer", "Reviews code", "Review the code.", workspace)
    store = SessionStore(workspace)
    session = store.create("reviewer", "codex", profile)
    stale = store.load(session.id)

    def append(index):
        other = SessionStore(workspace)
        other.append(
            other.load(session.id), "assistant", str(index), turn_id=str(index), profile=profile
        )

    with ThreadPoolExecutor(max_workers=4) as workers:
        list(workers.map(append, range(12)))
    append(0)
    saved = store.load(session.id)
    assert len(saved.turns) == 12
    assert {turn.content for turn in saved.turns} == {str(i) for i in range(12)}
    assert all(turn.spec_digest == profile.spec_digest for turn in saved.turns)
    with pytest.raises(ValueError, match="changed"):
        store.rename(stale, "Stale rename")
    store.delete(session.id)
    with pytest.raises(ValueError, match="deleted"):
        store.save(saved)


def child(workspace, source, **kwargs):
    return run_child(
        [sys.executable, "-c", source],
        workspace=workspace,
        env=dict(os.environ),
        timeout=kwargs.pop("timeout", 5),
        cancellation=kwargs.pop("cancellation", None),
        limit=4096,
        **kwargs,
    )


def test_output_flood_bounded_while_draining_both_pipes(workspace):
    import tracemalloc

    tracemalloc.start()
    try:
        result = child(workspace, "import os; os.write(1,b'x'*8000000); os.write(2,b'y'*8000000)")
        _, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()
    assert peak < 4_000_000, "Broker capture exceeded its memory budget"

    assert len(result.stdout) == len(result.stderr) == 4096
    assert result.truncated and result.returncode == 0


def test_control_frame_limit(workspace):
    with pytest.raises(ChildProcessError, match="maximum size"):
        child(
            workspace,
            "import os,time; os.write(1,b'x'*2000000); time.sleep(60)",
            control=lambda event, stopped: None,
        )


@pytest.mark.skipif(os.name == "nt", reason="POSIX process-group fixture")
def test_timeout_stops_descendant(workspace):
    pidfile = workspace / "descendant.pid"
    code = (
        "import subprocess,sys,time; "
        "p=subprocess.Popen([sys.executable,'-c','import time; time.sleep(60)']); "
        f"open({str(pidfile)!r},'w').write(str(p.pid)); time.sleep(60)"
    )
    with pytest.raises(ChildProcessError, match="timed out"):
        child(workspace, code, timeout=0.5)
    pid = int(pidfile.read_text())
    import time

    for _ in range(50):
        status = __import__("pathlib").Path(f"/proc/{pid}/stat")
        if not status.exists() or status.read_text().split()[2] == "Z":
            break
        time.sleep(0.02)
    else:
        pytest.fail("Owned descendant survived timeout")


@pytest.mark.asyncio
async def test_disconnect_does_not_cancel_owned_run_and_replay_is_sequenced(workspace):
    supervisor = RunSupervisor(workspace)
    proceed = asyncio.Event()

    async def source():
        yield "event: run_started\ndata: {}\n\n"
        await proceed.wait()
        yield "event: run_finished\ndata: {}\n\n"

    supervisor.start("run-test", source(), threading.Event())
    stream = supervisor.stream("run-test")
    first = await anext(stream)
    assert "id: 1" in first
    await stream.aclose()
    proceed.set()
    replay = [event async for event in supervisor.stream("run-test", after=1)]
    assert len(replay) == 1 and "id: 2" in replay[0]
    assert supervisor.snapshot("run-test")["complete"]
    await supervisor.close()


def test_profile_spec_change_blocks_dispatch_but_state_change_does_not(workspace, monkeypatch):
    from types import SimpleNamespace

    from merced_ai.application import RoutingError, prepare_group_turn

    profile = create_profile("reviewer", "Reviews code", "Review the code.", workspace)
    session = SessionStore(workspace).create("reviewer", "codex", profile)
    prepared = SimpleNamespace(profile=profile, request=SimpleNamespace(harness_id="codex"))
    monkeypatch.setattr("merced_ai.application.prepare_run", lambda *args, **kwargs: prepared)
    prepared.profile = profile.model_copy(
        update={"revision": profile.revision + 1, "profile_digest": "state-only"}
    )
    assert prepare_group_turn(session, "Review", workspace) == (prepared,)
    prepared.profile = profile.model_copy(update={"spec_digest": "changed-authority"})
    with pytest.raises(RoutingError, match="changed"):
        prepare_group_turn(session, "Review", workspace)


def test_child_does_not_inherit_test_coverage_settings(workspace, monkeypatch):
    monkeypatch.setenv("COV_CORE_SOURCE", "unrelated-harness")
    monkeypatch.setenv("COVERAGE_PROCESS_START", "/missing/coverage-config")
    result = child(
        workspace,
        "import os; print([k for k in os.environ "
        "if k.startswith('COV_CORE_') or k == 'COVERAGE_PROCESS_START'])",
    )
    assert result.returncode == 0
    assert result.stdout.strip() == "[]"


@pytest.mark.skipif(
    os.name != "nt",
    reason=(
        "Windows process-tree cancellation (taskkill /T); runs in the Windows CI job. POSIX "
        "process-group termination is covered by test_timeout_stops_descendant."
    ),
)
def test_windows_cancel_terminates_grandchild(workspace):  # pragma: no cover - Windows CI
    import time

    from merced_ai.process_liveness import process_alive

    pidfile = workspace / "grandchild.pid"
    code = (
        "import subprocess,sys,time; "
        "p=subprocess.Popen([sys.executable,'-c','import time; time.sleep(120)']); "
        f"open({str(pidfile)!r},'w').write(str(p.pid)); time.sleep(120)"
    )
    cancellation = threading.Event()

    def cancel_when_grandchild_exists() -> None:
        deadline = time.monotonic() + 60
        while not pidfile.exists() or not pidfile.read_text().strip():
            if time.monotonic() > deadline:
                break
            time.sleep(0.05)
        cancellation.set()

    threading.Thread(target=cancel_when_grandchild_exists, daemon=True).start()
    with pytest.raises(ChildProcessError, match="cancelled"):
        run_child(
            [sys.executable, "-c", code],
            workspace=workspace,
            env=dict(os.environ),
            timeout=90,
            cancellation=cancellation,
            limit=10_000,
        )
    pid = int(pidfile.read_text())
    deadline = time.monotonic() + 15
    while process_alive(pid) and time.monotonic() < deadline:
        time.sleep(0.1)
    assert not process_alive(pid), "grandchild survived cancellation"
