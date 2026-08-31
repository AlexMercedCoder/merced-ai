"""Durable AAIS presenter used to relay child-harness decisions."""

from __future__ import annotations

import copy
import json
import os
import threading
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from aais import ConflictError, create_decision, validate


@dataclass
class _Pending:
    envelope: dict[str, Any]
    decided: threading.Event
    decision: dict[str, Any] | None = None


class AAISPresenter:
    """Presents authority-owned requests without becoming their authority."""

    def __init__(self, workspace: Path) -> None:
        self.path = workspace.resolve() / ".merced-ai" / "aais-presenter.json"
        self._lock = threading.RLock()
        self._pending: dict[str, _Pending] = {}
        self._sequence = 0

    def _persist(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "schema": "merced-ai.aais-presenter.v1",
            "pending": [item.envelope["request"] for item in self._pending.values()],
        }
        temporary = self.path.with_name(f".{self.path.name}.{os.getpid()}.tmp")
        descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
                json.dump(payload, handle, sort_keys=True, separators=(",", ":"))
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, self.path)
        finally:
            temporary.unlink(missing_ok=True)

    def present(
        self,
        envelope: dict[str, Any],
        cancellation: threading.Event | None = None,
    ) -> dict[str, Any]:
        requested = validate(envelope)
        if requested["type"] != "approval.requested":
            raise ValueError("AAIS presenter requires approval.requested")
        request_id = str(requested["request"]["id"])
        with self._lock:
            pending = self._pending.get(request_id)
            if pending is None:
                pending = _Pending(requested, threading.Event())
                self._pending[request_id] = pending
                self._persist()
        while not pending.decided.wait(0.25):
            if cancellation is not None and cancellation.is_set():
                self.decide(request_id, "cancel", "once", actor_id="merced-ai.cancel")
                break
        assert pending.decision is not None
        with self._lock:
            self._pending.pop(request_id, None)
            self._persist()
        return copy.deepcopy(pending.decision)

    def decide(
        self,
        request_id: str,
        decision: str,
        scope: str,
        *,
        actor_id: str = "local-user",
        decision_id: str | None = None,
    ) -> dict[str, Any]:
        with self._lock:
            pending = self._pending.get(request_id)
            if pending is None:
                raise ValueError(f"unknown pending approval: {request_id}")
            if pending.decision is not None:
                body = pending.decision["decision"]
                if (body["decision"], body["scope"]) == (decision, scope):
                    return copy.deepcopy(pending.decision)
                raise ConflictError(f"request {request_id} was already decided")
            self._sequence += 1
            decided = create_decision(
                pending.envelope,
                decision=decision,
                scope=scope,
                actor={
                    "id": actor_id.replace(" ", "."),
                    "type": "policy" if actor_id.startswith("merced-ai.") else "human",
                    "authenticated_by": "merced-ai-web-session",
                },
                sequence=self._sequence,
                stream="merced-ai.presenter",
                decision_id=decision_id,
            )
            pending.decision = decided
            pending.decided.set()
            return copy.deepcopy(decided)

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            return validate(
                {
                    "aais": "1.0",
                    "type": "approval.snapshot",
                    "id": "evt_merced_ai_snapshot",
                    "occurred_at": datetime.now(UTC).isoformat().replace("+00:00", "Z"),
                    "sequence": self._sequence,
                    "stream": "merced-ai.presenter",
                    "snapshot": {
                        "as_of_sequence": self._sequence,
                        "pending": [
                            copy.deepcopy(item.envelope["request"])
                            for item in self._pending.values()
                        ],
                    },
                }
            )
