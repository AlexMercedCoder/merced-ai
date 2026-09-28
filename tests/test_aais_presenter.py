from __future__ import annotations

import threading
import time
from pathlib import Path

import pytest
from aais import ConflictError, create_request, validate

from merced_ai.aais_presenter import AAISPresenter


def request() -> dict:
    return create_request(
        action={
            "kind": "tool.call",
            "name": "shell.exec",
            "summary": "Check syntax",
            "arguments": {"command": "node --check app.js"},
        },
        origin={"harness": "magagent", "session_id": "session-1"},
        risk={"level": "medium", "reasons": ["Runs a local process"]},
        choices=[
            {"decision": "approve", "scope": "once", "label": "Allow once"},
            {"decision": "deny", "scope": "once", "label": "Deny"},
        ],
        sequence=1,
        stream="test",
    )


# Every presenter step fsyncs its state file. Under heavy disk load one fsync can take close to a
# second, so waits use a generous deadline instead of a fixed number of short polls.
WAIT_SECONDS = 30.0


def _poll(check, *, timeout: float = WAIT_SECONDS):
    deadline = time.monotonic() + timeout
    while True:
        value = check()
        if value or time.monotonic() >= deadline:
            return value
        time.sleep(0.01)


def test_presenter_relays_valid_decision_and_persists_pending(tmp_path: Path) -> None:
    presenter = AAISPresenter(tmp_path)
    result: list[dict] = []
    worker = threading.Thread(target=lambda: result.append(presenter.present(request())))
    worker.start()
    pending = _poll(lambda: presenter.snapshot()["snapshot"]["pending"])
    assert pending
    assert presenter.path.exists()
    request_id = pending[0]["id"]
    presenter.decide(request_id, "approve", "once", decision_id="decision-test")
    worker.join(WAIT_SECONDS)
    assert not worker.is_alive()
    assert validate(result[0])["decision"]["request_id"] == request_id
    assert presenter.snapshot()["snapshot"]["pending"] == []


def test_presenter_decision_is_idempotent_but_conflicts_are_rejected(tmp_path: Path) -> None:
    presenter = AAISPresenter(tmp_path)
    envelope = request()
    pending = presenter._pending  # exercise atomic resolution before the waiter removes it
    from merced_ai.aais_presenter import _Pending

    pending[envelope["request"]["id"]] = _Pending(envelope, threading.Event())
    request_id = envelope["request"]["id"]
    first = presenter.decide(request_id, "approve", "once", decision_id="same-id")
    assert presenter.decide(request_id, "approve", "once") == first
    with pytest.raises(ConflictError):
        presenter.decide(request_id, "deny", "once")


def test_second_presenter_restores_and_resolves_live_owner(tmp_path):
    owner = AAISPresenter(tmp_path)
    result = []
    envelope = request()
    worker = threading.Thread(target=lambda: result.append(owner.present(envelope)))
    worker.start()
    try:
        restored = _poll(
            lambda: (
                (candidate := AAISPresenter(tmp_path)).snapshot()["snapshot"]["pending"]
                and candidate
            )
        )
        assert restored
        first = restored.decide(envelope["request"]["id"], "approve", "once")
        worker.join(WAIT_SECONDS)
        assert result == [first]
        assert AAISPresenter(tmp_path).decide(envelope["request"]["id"], "approve", "once") == first
    finally:
        if worker.is_alive():
            owner.decide(envelope["request"]["id"], "cancel", "once")
            worker.join(WAIT_SECONDS)


def test_expired_request_is_cancelled_without_waiting_for_user(tmp_path):
    envelope = request()
    envelope["occurred_at"] = "2000-01-01T00:00:00Z"
    envelope["request"]["created_at"] = "2000-01-01T00:00:00Z"
    envelope["request"]["expires_at"] = "2000-01-01T00:00:01Z"
    presenter = AAISPresenter(tmp_path)
    decision = presenter.present(envelope)
    assert decision["decision"]["decision"] == "cancel"
    assert presenter.snapshot()["snapshot"]["pending"] == []


@pytest.mark.parametrize(
    "content",
    [
        b"{not json",
        b'{"schema": "something-else"}',
        b'{"schema": "merced-ai.aais-presenter.v1", "envelopes": [{"aais": "0.1"}]}',
        b"\xff\xfe\x00garbage",
        b"[]",
    ],
    ids=["truncated", "wrong-schema", "invalid-envelope", "not-utf8", "not-object"],
)
def test_corrupt_state_is_quarantined_and_presenter_starts_fresh(
    tmp_path: Path, content: bytes
) -> None:
    state = tmp_path / ".merced-ai" / "aais-presenter.json"
    state.parent.mkdir(parents=True)
    state.write_bytes(content)

    presenter = AAISPresenter(tmp_path)

    quarantined = list(state.parent.glob("aais-presenter.corrupt-*.json"))
    assert len(quarantined) == 1
    assert quarantined[0].read_bytes() == content
    assert not state.exists()
    [notice] = presenter.notices
    assert notice["kind"] == "approval_state_quarantined"
    assert notice["quarantined_path"] == str(quarantined[0])
    assert presenter.recovery()["notices"] == [notice]
    assert presenter.snapshot()["snapshot"]["pending"] == []

    # The fresh store works end to end.
    result: list[dict] = []
    worker = threading.Thread(target=lambda: result.append(presenter.present(request())))
    worker.start()
    pending = _poll(lambda: presenter.snapshot()["snapshot"]["pending"])
    assert pending
    presenter.decide(pending[0]["id"], "deny", "once")
    worker.join(WAIT_SECONDS)
    assert result and result[0]["decision"]["decision"] == "deny"
    assert state.exists()


def test_valid_state_is_not_quarantined(tmp_path: Path) -> None:
    AAISPresenter(tmp_path)._persist()
    presenter = AAISPresenter(tmp_path)
    assert presenter.notices == []
    assert not list(presenter.path.parent.glob("*.corrupt-*"))


# ---- owner identity (I-24): pid + process start time + host, via aais.liveness ------------------


def _pending_state(workspace: Path, owner: object) -> tuple[Path, str]:
    """A presenter state file with one pending request owned by ``owner``."""
    import json

    envelope = validate(request())
    path = workspace / ".merced-ai" / "aais-presenter.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "schema": "merced-ai.aais-presenter.v1",
                "sequence": 1,
                "envelopes": [envelope],
                "decisions": {},
                "receipts": {},
                "owners": {envelope["request"]["id"]: owner},
            }
        ),
        encoding="utf-8",
    )
    return path, envelope["request"]["id"]


def test_legacy_bare_pid_owners_are_read_and_upgraded(workspace: Path) -> None:
    import json
    import os

    from aais.liveness import current_host_id

    path, request_id = _pending_state(workspace, os.getpid())

    presenter = AAISPresenter(workspace)

    stored = json.loads(path.read_text(encoding="utf-8"))["owners"][request_id]
    assert stored["pid"] == os.getpid() and stored["host_id"] == current_host_id()
    assert "process_start_time" in stored  # upgraded in place on load
    assert presenter.recovery()["orphaned"] == []  # this process is alive
    assert len(presenter.snapshot()["snapshot"]["pending"]) == 1


def test_a_reused_pid_is_not_mistaken_for_the_owner(workspace: Path) -> None:
    import os

    from aais.liveness import OwnerIdentity, current_host_id, process_start_time

    started = process_start_time(os.getpid())
    assert started is not None
    # Same PID and host, but the recorded process started an hour earlier: the PID was reused.
    ghost = OwnerIdentity(os.getpid(), started - 3600, current_host_id()).to_dict()
    _path, request_id = _pending_state(workspace, ghost)

    presenter = AAISPresenter(workspace)

    assert presenter.recovery()["orphaned"] == [request_id]
    assert presenter.snapshot()["snapshot"]["pending"] == []
    with pytest.raises(ConflictError, match="issuing process stopped"):
        presenter.decide(request_id, "approve", "once")


def test_an_owner_on_another_host_is_unknown_not_dead(workspace: Path) -> None:
    from aais.liveness import OwnerIdentity

    other = OwnerIdentity(999_999, 1.0, "host-somewhere-else").to_dict()
    _path, request_id = _pending_state(workspace, other)

    presenter = AAISPresenter(workspace)

    assert presenter.recovery()["orphaned"] == []  # never treated as dead
    assert len(presenter.snapshot()["snapshot"]["pending"]) == 1
    decided = presenter.decide(request_id, "deny", "once")
    assert decided["decision"]["decision"] == "deny"


def test_an_invalid_owner_record_quarantines_the_state(workspace: Path) -> None:
    path, _request_id = _pending_state(workspace, {"pid": -4, "host_id": "x"})

    presenter = AAISPresenter(workspace)

    assert presenter.notices and presenter.notices[0]["kind"] == "approval_state_quarantined"
    assert not path.exists()
