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

from aais import ApprovalError, ApprovalStore, ConflictError, create_decision, validate

from merced_ai.storage import atomic_write, file_lock


class StateCorruptError(ValueError):
    """The presenter state file exists but cannot be parsed or validated."""


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
        self._decisions: dict[str, dict[str, Any]] = {}
        self._receipts: dict[str, dict[str, Any]] = {}
        self._owners: dict[str, int] = {}
        # Recovery notices for the UI, for example a quarantined state file.
        self.notices: list[dict[str, Any]] = []
        with self._lock, file_lock(self.path):
            self._load()

    def _load(self) -> None:
        """Reload shared state; quarantine an unreadable file instead of failing."""
        try:
            self._read_state()
        except StateCorruptError as error:
            self._quarantine(error)

    def _quarantine(self, error: StateCorruptError) -> None:
        stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%S%fZ")
        target = self.path.with_name(f"{self.path.stem}.corrupt-{stamp}{self.path.suffix}")
        try:
            os.replace(self.path, target)
        except OSError as move_error:
            raise ValueError(
                f"Approval presenter state is unreadable and could not be moved aside: {self.path}"
            ) from move_error
        # Start clean. Requests this process is still presenting stay in memory and are written
        # back on the next change; requests owned by other processes must be issued again.
        self._decisions = {}
        self._receipts = {}
        self._owners = {key: value for key, value in self._owners.items() if key in self._pending}
        self.notices.append(
            {
                "id": f"approval-state-{stamp}",
                "kind": "approval_state_quarantined",
                "level": "warning",
                "title": "Approval state was reset",
                "message": (
                    "The saved approval state could not be read, so Merced AI moved it aside and "
                    "started fresh. Any approval that was waiting will be asked again by its "
                    "harness, or has timed out."
                ),
                "reason": str(error.__cause__ or error),
                "quarantined_path": str(target),
                "detected_at": datetime.now(UTC).isoformat().replace("+00:00", "Z"),
            }
        )

    def _read_state(self) -> None:
        if not self.path.exists():
            return
        try:
            text = self.path.read_text(encoding="utf-8")
        except UnicodeDecodeError as error:
            raise StateCorruptError(
                f"Approval presenter state is not UTF-8: {self.path}"
            ) from error
        except OSError as error:
            # Unreadable for another reason (permissions, I/O): not safe to move aside.
            raise ValueError(f"Approval presenter state cannot be read: {self.path}") from error
        try:
            payload = json.loads(text)
            if not isinstance(payload, dict):
                raise ValueError("Presenter storage must be a JSON object")
            if payload.get("schema") != "merced-ai.aais-presenter.v1":
                raise ValueError("Unsupported presenter storage")
            sequence = int(payload.get("sequence", 0))
            decisions = dict(payload.get("decisions", {}))
            receipts = dict(payload.get("receipts", {}))
            owners = dict(payload.get("owners", {}))
            envelopes = payload.get("envelopes")
            if envelopes is None:
                envelopes = [
                    {
                        "aais": "1.0",
                        "type": "approval.requested",
                        "id": "restore_" + request["id"],
                        "occurred_at": request["created_at"],
                        "sequence": 0,
                        "stream": "merced-ai.presenter",
                        "request": request,
                    }
                    for request in payload.get("pending", [])
                ]
            validated = [validate(envelope) for envelope in envelopes]
        except (ValueError, KeyError, TypeError, AttributeError, ApprovalError) as error:
            raise StateCorruptError(
                f"Approval presenter state requires recovery: {self.path}"
            ) from error
        # Apply only after the whole file parsed, so a failure never leaves partial state.
        self._sequence, self._decisions, self._receipts, self._owners = (
            sequence,
            decisions,
            receipts,
            owners,
        )
        existing = self._pending
        self._pending = {}
        for envelope in validated:
            request_id = envelope["request"]["id"]
            item = existing.get(request_id) or _Pending(envelope, threading.Event())
            item.decision = self._decisions.get(request_id)
            if item.decision:
                item.decided.set()
            self._pending[request_id] = item

    def _persist(self) -> None:
        # Keep replay receipts bounded independently of active requests.
        for mapping in (self._decisions, self._receipts):
            removable = [key for key in mapping if key not in self._pending]
            for key in removable[:-1000]:
                mapping.pop(key, None)
        atomic_write(
            self.path,
            json.dumps(
                {
                    "schema": "merced-ai.aais-presenter.v1",
                    "sequence": self._sequence,
                    "pending": [item.envelope["request"] for item in self._pending.values()],
                    "envelopes": [item.envelope for item in self._pending.values()],
                    "decisions": self._decisions,
                    "receipts": self._receipts,
                    "owners": self._owners,
                },
                sort_keys=True,
                separators=(",", ":"),
            ),
        )

    @staticmethod
    def _owner_alive(pid: int | None) -> bool:
        from merced_ai.process_liveness import process_alive

        return process_alive(pid)

    def record_event(self, envelope: dict[str, Any]) -> None:
        event = validate(envelope)
        if event["type"] != "approval.resolved":
            return
        with self._lock, file_lock(self.path):
            self._load()
            request_id = event["resolution"]["request_id"]
            pending = self._pending.get(request_id)
            decision = self._decisions.get(request_id)
            expected = (
                pending.envelope["request"]["action_digest"]
                if pending
                else (decision["decision"]["action_digest"] if decision else None)
            )
            if expected != event["resolution"]["action_digest"]:
                raise ValueError("Authority receipt does not match a presented action")
            self._receipts[request_id] = event
            self._pending.pop(request_id, None)
            self._owners.pop(request_id, None)
            self._persist()

    def present(
        self,
        envelope: dict[str, Any],
        cancellation: threading.Event | None = None,
    ) -> dict[str, Any]:
        requested = validate(envelope)
        if requested["type"] != "approval.requested":
            raise ValueError("AAIS presenter requires approval.requested")
        request_id = str(requested["request"]["id"])
        with self._lock, file_lock(self.path):
            self._load()
            prior = self._decisions.get(request_id)
            if (
                prior
                and prior["decision"]["action_digest"] != requested["request"]["action_digest"]
            ):
                raise ConflictError("Approval identifier was reused for a different action")
            pending = self._pending.get(request_id)
            if pending is None:
                pending = _Pending(requested, threading.Event())
                self._pending[request_id] = pending
            elif (
                pending.envelope["request"]["action_digest"]
                != requested["request"]["action_digest"]
            ):
                raise ConflictError("Approval identifier was reused for a different action")
            self._owners[request_id] = os.getpid()
            self._persist()
        while not pending.decided.wait(0.1):
            with self._lock, file_lock(self.path):
                self._load()
                if request_id in self._decisions:
                    pending.decision = self._decisions[request_id]
                    break
            expires = requested["request"].get("expires_at")
            expired = bool(
                expires
                and datetime.now(UTC) >= datetime.fromisoformat(expires.replace("Z", "+00:00"))
            )
            if expired or (cancellation is not None and cancellation.is_set()):
                self.decide(request_id, "cancel", "once", actor_id="merced-ai.cancel")
                break
        assert pending.decision is not None
        with self._lock, file_lock(self.path):
            self._load()
            self._pending.pop(request_id, None)
            self._owners.pop(request_id, None)
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
        authenticated_by: str = "merced-ai-web-session",
    ) -> dict[str, Any]:
        with self._lock, file_lock(self.path):
            self._load()
            prior = self._decisions.get(request_id)
            if prior is not None:
                body = prior["decision"]
                if (body["decision"], body["scope"]) == (decision, scope):
                    return copy.deepcopy(prior)
                raise ConflictError(f"request {request_id} was already decided")
            pending = self._pending.get(request_id)
            if pending is None:
                raise ValueError(f"unknown pending approval: {request_id}")
            if pending.decision is not None:
                body = pending.decision["decision"]
                if (body["decision"], body["scope"]) == (decision, scope):
                    return copy.deepcopy(pending.decision)
                raise ConflictError(f"request {request_id} was already decided")
            if self.path.exists() and not self._owner_alive(self._owners.get(request_id)):
                raise ConflictError("The issuing process stopped; inspect approval recovery")
            expires = pending.envelope["request"].get("expires_at")
            if (
                decision != "cancel"
                and expires
                and datetime.now(UTC) >= datetime.fromisoformat(expires.replace("Z", "+00:00"))
            ):
                raise ConflictError("Approval expired; wait for the harness to issue a new request")
            self._sequence += 1
            decided = create_decision(
                pending.envelope,
                decision=decision,
                scope=scope,
                actor={
                    "id": actor_id.replace(" ", "."),
                    "type": "policy" if actor_id.startswith("merced-ai.") else "human",
                    "authenticated_by": authenticated_by,
                },
                sequence=self._sequence,
                stream="merced-ai.presenter",
                decision_id=decision_id,
            )
            self._decisions[request_id] = decided
            pending.decision = decided
            self._persist()
            pending.decided.set()
            return copy.deepcopy(decided)

    def snapshot(self) -> dict[str, Any]:
        with self._lock, file_lock(self.path):
            self._load()
            machine = ApprovalStore(last_sequence=self._sequence)
            for request_id, pending in self._pending.items():
                if request_id not in self._decisions and self._owner_alive(
                    self._owners.get(request_id)
                ):
                    machine.add(pending.envelope)
            return machine.snapshot(stream="merced-ai.presenter")

    def recovery(self) -> dict[str, Any]:
        with self._lock, file_lock(self.path):
            self._load()
            return {
                "notices": list(self.notices),
                "orphaned": [
                    key for key in self._pending if not self._owner_alive(self._owners.get(key))
                ],
                "decisions_sent": len(self._decisions),
                "receipts": list(self._receipts.values())[-100:],
            }
