"""Reviewed inbox for OAP 1.0 state deltas (the Level 2 applicator side).

Harness sessions propose changes to a profile's learned state as ``AgentStateDelta`` documents.
Merced AI collects them in ``.merced-ai/inbox/`` and applies nothing until a person reviews it:

- **State operations** are applied with the OAP reference applicator (``oap.apply``): schema and
  scope validation, revision check, digest check, atomic all-or-nothing apply, retention, revision
  bump, and a history entry naming the approver, written with a same-directory temp file, fsync,
  and rename. A revision mismatch never blind-writes; id-addressed deltas can be rebased.
- **Proposals** (changes to ``/metadata`` or ``/spec``) are never applied with the state
  operations. Each one needs its own explicit approval, and any proposal touching tools,
  permissions, memory, or subagents is shown as high risk whatever the document claims.

Inbox items are JSON files with a status of ``pending``, ``conflict``, ``applied``, or
``rejected``. Deltas can be imported from a file (including a Loro proposal JSON that wraps one)
or recorded from an explicit user statement with :func:`remember`.
"""

from __future__ import annotations

import copy
import json
import re
import tempfile
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import uuid4

import yaml
from oap.apply import ApplyError, Conflict, apply_delta, dump
from oap.validate import load_schema, validate_file

from merced_ai.profiles import ProfileError, resolve_profile
from merced_ai.storage import atomic_write, file_lock

HIGH_RISK_PREFIXES = ("/spec/tools", "/spec/permissions", "/spec/memory", "/spec/runtime/subagents")
PROTECTED_METADATA = (
    "/metadata/name",
    "/metadata/revision",
    "/metadata/updated_at",
    "/metadata/trust",
)
ITEM_ID = re.compile(r"^delta-[0-9a-f]{32}$")
STATE_KINDS = {"fact": "facts", "preference": "preferences", "thread": "open_threads"}


class InboxError(ValueError):
    pass


MAX_DELTA_BYTES = 1_000_000
MAX_DEPTH = 64


class _NoAliasLoader(yaml.SafeLoader):
    """Safe YAML without anchors/aliases, which can expand a small file into a huge document."""

    def compose_node(self, parent: Any, index: Any) -> Any:
        if self.check_event(yaml.AliasEvent):
            raise InboxError("YAML aliases are not accepted in deltas")
        return super().compose_node(parent, index)


def _check_shape(value: Any) -> None:
    """Refuse documents nested deeper than any real delta, before anything recurses into them."""
    stack: list[tuple[Any, int]] = [(value, 0)]
    while stack:
        item, level = stack.pop()
        if level > MAX_DEPTH:
            raise InboxError(f"the delta is nested too deeply (max {MAX_DEPTH} levels)")
        if isinstance(item, dict):
            stack.extend((child, level + 1) for child in item.values())
        elif isinstance(item, list):
            stack.extend((child, level + 1) for child in item)


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds").replace("+00:00", "Z")


def validate_delta(document: dict[str, Any]) -> list[str]:
    """Validate a delta with the OAP reference validator; returns warnings or raises."""
    with tempfile.TemporaryDirectory(prefix="merced-ai-delta-") as scratch:
        path = Path(scratch) / "incoming.delta.json"
        path.write_text(json.dumps(document), encoding="utf-8")
        report = validate_file(
            path,
            load_schema("agent-profile.schema.json"),
            load_schema("agent-state-delta.schema.json"),
        )
    if report.kind != "AgentStateDelta":
        raise InboxError(f"expected an AgentStateDelta, got {report.kind!r}")
    if report.errors:
        raise InboxError("invalid delta: " + "; ".join(report.errors[:5]))
    return list(report.warnings)


def _proposal_risk(proposal: dict[str, Any]) -> str:
    path = str(proposal.get("path", ""))
    if path.startswith(HIGH_RISK_PREFIXES):
        return "high"  # L2-A8: computed here, never trusted from the document.
    declared = proposal.get("risk")
    return declared if declared in {"low", "medium", "high"} else "medium"


def _recheck_risk(item: dict[str, Any]) -> dict[str, Any]:
    """Recompute proposal risk on every read: items live in the workspace, where a harness can
    edit them, so a stored "low" on a tools or permissions proposal is never believed."""
    for proposal in item.get("proposals") or []:
        if isinstance(proposal, dict):
            proposal["risk"] = _proposal_risk(proposal)
    return item


class DeltaInbox:
    def __init__(self, workspace: Path) -> None:
        self.workspace = workspace.resolve()
        self.root = self.workspace / ".merced-ai" / "inbox"

    # ---- storage -------------------------------------------------------------------------

    def _path(self, item_id: str) -> Path:
        if not ITEM_ID.fullmatch(item_id):
            raise InboxError("invalid inbox item identifier")
        return self.root / f"{item_id}.json"

    def get(self, item_id: str) -> dict[str, Any]:
        path = self._path(item_id)
        if not path.exists():
            raise InboxError(f"inbox item {item_id!r} was not found")
        return _recheck_risk(dict(json.loads(path.read_text(encoding="utf-8"))))

    def _save(self, item: dict[str, Any]) -> None:
        item["updated_at"] = _now()
        atomic_write(self._path(item["id"]), json.dumps(item, indent=2, ensure_ascii=False))

    def items(self, status: str | None = None) -> list[dict[str, Any]]:
        if not self.root.exists():
            return []
        items = []
        for path in self.root.glob("delta-*.json"):
            try:
                loaded = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue  # A torn or foreign file must not hide the rest of the inbox.
            if isinstance(loaded, dict) and {"id", "status", "created_at"} <= loaded.keys():
                items.append(_recheck_risk(loaded))
        items = [item for item in items if status is None or item["status"] == status]
        return sorted(items, key=lambda item: item["created_at"], reverse=True)

    # ---- intake --------------------------------------------------------------------------

    def add(self, document: dict[str, Any], *, source: str) -> dict[str, Any]:
        _check_shape(document)
        if document.get("kind") != "AgentStateDelta" and isinstance(document.get("delta"), dict):
            document = document["delta"]  # A Loro proposal record wraps the delta.
        warnings = validate_delta(document)
        target = document["target"]
        try:
            profile = resolve_profile(str(target["name"]), self.workspace)
        except ProfileError as error:
            raise InboxError(
                f"delta targets unknown profile {target['name']!r}: {error}"
            ) from error
        item_id = f"delta-{uuid4().hex}"
        item = {
            "id": item_id,
            "status": "pending",
            "created_at": _now(),
            "source": source,
            "profile": profile.name,
            "profile_path": str(profile.path),
            "profile_revision_at_intake": profile.revision,
            "delta": document,
            "validation_warnings": warnings,
            "proposals": [
                {
                    "index": index,
                    "path": proposal.get("path"),
                    "op": proposal.get("op", "replace"),
                    "value": proposal.get("value"),
                    "rationale": proposal.get("rationale"),
                    "risk": _proposal_risk(proposal),
                    "status": "pending",
                }
                for index, proposal in enumerate(document.get("proposals") or [])
            ],
            "history": [{"at": _now(), "event": "received", "by": source}],
        }
        self.root.mkdir(parents=True, exist_ok=True)
        self._save(item)
        return item

    def add_file(self, path: Path) -> dict[str, Any]:
        if path.stat().st_size > MAX_DELTA_BYTES:
            raise InboxError(f"{path.name} is too large for a delta (max {MAX_DELTA_BYTES} bytes)")
        text = path.read_text(encoding="utf-8")
        try:
            loaded = json.loads(text) if path.suffix == ".json" else yaml.load(text, _NoAliasLoader)
        except RecursionError as error:
            raise InboxError("the document is nested too deeply") from error
        if not isinstance(loaded, dict):
            raise InboxError(f"{path} does not contain a delta document")
        return self.add(loaded, source=f"file:{path.name}")

    def remember(
        self, profile_name: str, text: str, *, kind: str = "fact", actor: str
    ) -> dict[str, Any]:
        """Record an explicit user statement as a one-operation delta (SPEC 5.5 evidence)."""
        if kind not in STATE_KINDS:
            raise InboxError("kind must be fact, preference, or thread")
        if not text.strip():
            raise InboxError("say what should be remembered")
        profile = resolve_profile(profile_name, self.workspace)
        entry_id = f"{kind[:4]}-{uuid4().hex[:8]}"
        entry: dict[str, Any] = {"id": entry_id, "text": text.strip()[:2000]}
        if kind == "thread":
            entry = {"id": entry_id, "title": text.strip()[:200], "status": "open"}
        collection = STATE_KINDS[kind]
        existing = (profile.document.get("state") or {}).get(collection)
        # Append to an existing collection; create it (as a one-entry list) when it is missing,
        # because appending with "-" to a missing collection is not a list operation.
        operation: dict[str, Any] = (
            {"op": "add", "path": f"/state/{collection}/-", "value": entry}
            if isinstance(existing, list)
            else {"op": "add", "path": f"/state/{collection}", "value": [entry]}
        )
        delta = {
            "oap": "1.0",
            "kind": "AgentStateDelta",
            "target": {"name": profile.name, "revision": profile.revision},
            "session": {"id": f"merced-remember-{uuid4().hex[:12]}", "harness": "merced-ai"},
            "summary": f"Remember a {kind} the user stated.",
            "operations": [
                {**operation, "reason": "Explicit user statement recorded through Merced AI."}
            ],
        }
        return self.add(delta, source=f"remember:{actor}")

    # ---- review --------------------------------------------------------------------------

    def _profile_document(self, item: dict[str, Any]) -> tuple[Path, dict[str, Any]]:
        path = Path(item["profile_path"])
        if path.suffix not in {".yaml", ".yml", ".json"}:
            raise InboxError(
                f"{path.name} is a Markdown profile; apply this delta with the owning harness"
            )
        text = path.read_text(encoding="utf-8")
        document = json.loads(text) if path.suffix == ".json" else yaml.safe_load(text)
        if not isinstance(document, dict):
            raise InboxError(f"{path} is not a profile document")
        return path, document

    @staticmethod
    def _rebaseable(delta: dict[str, Any]) -> bool:
        """Id-addressed and append operations still mean the same thing on a newer revision."""
        for op in delta.get("operations") or []:
            tokens = str(op.get("path", "")).split("/")
            if not (tokens[-1] == "-" or any(token.startswith("id:") for token in tokens)):
                return False
        return True

    def approve(self, item_id: str, *, actor: str, rebase: bool = False) -> dict[str, Any]:
        """Apply the state operations (never the proposals) after human review."""
        path = self._path(item_id)
        with file_lock(path):
            item = self.get(item_id)
            if item["status"] not in {"pending", "conflict"}:
                raise InboxError(f"inbox item is already {item['status']}")
            profile_path, profile = self._profile_document(item)
            delta = copy.deepcopy(item["delta"])
            current = (profile.get("metadata") or {}).get("revision", 1)
            if rebase:
                if not self._rebaseable(delta):
                    raise InboxError(
                        "this delta addresses entries by position, so it cannot be rebased safely; "
                        "reject it and let the harness produce a new one"
                    )
                delta["target"]["revision"] = current
                delta["target"].pop("digest", None)
            try:
                updated, warnings, _pending = apply_delta(
                    profile, delta, approved=True, actor=actor
                )
            except Conflict as error:
                item["status"] = "conflict"
                item["conflict"] = {
                    "message": str(error),
                    "profile_revision": current,
                    "rebaseable": self._rebaseable(delta),
                }
                item["history"].append({"at": _now(), "event": "conflict", "by": actor})
                self._save(item)
                return item
            except ApplyError as error:
                raise InboxError(str(error)) from error
            if not delta.get("operations"):
                warnings.append("the delta has no state operations; only its proposals remain")
            else:
                _validate_profile(updated, profile_path)
                atomic_write(profile_path, dump(updated, profile_path))
            item.update(
                status="applied",
                applied={
                    "revision": updated["metadata"]["revision"],
                    "warnings": warnings,
                    "approved_by": actor,
                    "rebased": rebase,
                },
            )
            item.pop("conflict", None)
            item["history"].append({"at": _now(), "event": "applied", "by": actor})
            self._save(item)
            return item

    def reject(self, item_id: str, *, actor: str, reason: str = "") -> dict[str, Any]:
        path = self._path(item_id)
        with file_lock(path):
            item = self.get(item_id)
            if item["status"] in {"applied", "rejected"}:
                raise InboxError(f"inbox item is already {item['status']}")
            item["status"] = "rejected"
            for proposal in item["proposals"]:
                if proposal["status"] == "pending":
                    proposal["status"] = "rejected"
            item["history"].append(
                {"at": _now(), "event": "rejected", "by": actor, "reason": reason}
            )
            self._save(item)
            return item

    def decide_proposal(
        self, item_id: str, index: int, *, approve: bool, actor: str
    ) -> dict[str, Any]:
        """Apply or decline one proposal. Each approval is separate and explicit (L2-A7)."""
        path = self._path(item_id)
        with file_lock(path):
            item = self.get(item_id)
            proposal = next((p for p in item["proposals"] if p["index"] == index), None)
            if proposal is None:
                raise InboxError(f"proposal {index} was not found")
            if proposal["status"] != "pending":
                raise InboxError(f"proposal {index} is already {proposal['status']}")
            if not approve:
                proposal["status"] = "rejected"
            else:
                pointer = str(proposal["path"])
                if pointer.startswith(PROTECTED_METADATA):
                    raise InboxError(f"{pointer} is owned by the applicator and cannot be proposed")
                profile_path, profile = self._profile_document(item)
                updated = copy.deepcopy(profile)
                _apply_pointer(updated, pointer, proposal["op"], proposal.get("value"))
                metadata = updated.setdefault("metadata", {})
                revision = int(metadata.get("revision", 1)) + 1
                metadata["revision"] = revision
                metadata["updated_at"] = _now()
                updated.setdefault("history", []).append(
                    {
                        "revision": revision,
                        "at": metadata["updated_at"],
                        "by": actor,
                        "change": f"Approved proposal: {proposal['op']} {pointer}",
                        "sections": [pointer.split("/")[1]],
                        "approved_by": actor,
                    }
                )
                _validate_profile(updated, profile_path)
                atomic_write(profile_path, dump(updated, profile_path))
                proposal.update(status="applied", revision=revision, approved_by=actor)
            item["history"].append(
                {
                    "at": _now(),
                    "event": f"proposal {index} {'applied' if approve else 'rejected'}",
                    "by": actor,
                }
            )
            self._save(item)
            return item


def _apply_pointer(document: dict[str, Any], pointer: str, op: str, value: Any) -> None:
    tokens = [part.replace("~1", "/").replace("~0", "~") for part in pointer.split("/")[1:]]
    if len(tokens) < 2:
        raise InboxError("a proposal must name a field inside /metadata or /spec")
    parent: Any = document
    for token in tokens[:-1]:
        if isinstance(parent, dict):
            parent = parent.setdefault(token, {})
        elif isinstance(parent, list) and token.isdigit() and int(token) < len(parent):
            parent = parent[int(token)]
        else:
            raise InboxError(f"{pointer} does not resolve in the profile")
    last = tokens[-1]
    if isinstance(parent, list):
        if op == "add":
            parent.append(value) if last == "-" else parent.insert(int(last), value)
        elif op == "replace" and last.isdigit() and int(last) < len(parent):
            parent[int(last)] = value
        elif op == "remove" and last.isdigit() and int(last) < len(parent):
            parent.pop(int(last))
        else:
            raise InboxError(f"{pointer} does not resolve in the profile")
    elif isinstance(parent, dict):
        if op == "remove":
            parent.pop(last, None)
        else:
            parent[last] = value
    else:
        raise InboxError(f"{pointer} does not resolve in the profile")


def _validate_profile(document: dict[str, Any], original: Path) -> None:
    with tempfile.TemporaryDirectory(prefix="merced-ai-profile-") as scratch:
        path = Path(scratch) / original.name
        path.write_text(dump(document, path), encoding="utf-8")
        report = validate_file(
            path,
            load_schema("agent-profile.schema.json"),
            load_schema("agent-state-delta.schema.json"),
        )
    if report.errors:
        raise InboxError(
            "the change would make the profile invalid: " + "; ".join(report.errors[:3])
        )


__all__ = ["DeltaInbox", "InboxError", "validate_delta"]
