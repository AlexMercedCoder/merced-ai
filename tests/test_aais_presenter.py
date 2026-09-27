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
