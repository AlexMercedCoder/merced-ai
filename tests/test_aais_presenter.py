from __future__ import annotations

import threading
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


def test_presenter_relays_valid_decision_and_persists_pending(tmp_path: Path) -> None:
    presenter = AAISPresenter(tmp_path)
    result: list[dict] = []
    worker = threading.Thread(target=lambda: result.append(presenter.present(request())))
    worker.start()
    for _ in range(100):
        pending = presenter.snapshot()["snapshot"]["pending"]
        if pending:
            break
        worker.join(0.01)
    assert pending
    assert presenter.path.exists()
    request_id = pending[0]["id"]
    presenter.decide(request_id, "approve", "once", decision_id="decision-test")
    worker.join(2)
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
