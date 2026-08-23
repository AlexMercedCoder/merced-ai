"""Durable, atomic local conversation records."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

from merced_ai.models import ConversationTurn, ProfileRecord, SessionRecord
from merced_ai.paths import ensure_project_layout


class SessionStore:
    def __init__(self, workspace: Path) -> None:
        self.root = ensure_project_layout(workspace) / "sessions"

    def create(self, bot_name: str, harness_id: str, profile: ProfileRecord) -> SessionRecord:
        now = datetime.now(UTC).isoformat()
        session = SessionRecord(
            id=f"session-{uuid4().hex}",
            bot_name=bot_name,
            harness_id=harness_id,
            workspace=self.root.parent.parent,
            profile_name=profile.name,
            profile_revision=profile.revision,
            profile_digest=profile.profile_digest,
            spec_digest=profile.spec_digest,
            created_at=now,
            updated_at=now,
        )
        self.save(session)
        return session

    def save(self, session: SessionRecord) -> None:
        session.updated_at = datetime.now(UTC).isoformat()
        path = self.root / f"{session.id}.json"
        temporary = path.with_name(f".{path.name}.tmp")
        temporary.write_text(session.model_dump_json(indent=2), encoding="utf-8")
        temporary.replace(path)

    def load(self, session_id: str) -> SessionRecord:
        path = self.root / f"{session_id}.json"
        if not path.is_file():
            raise ValueError(f"session {session_id!r} was not found")
        return SessionRecord.model_validate_json(path.read_text(encoding="utf-8"))

    def list(self) -> tuple[SessionRecord, ...]:
        records = [
            SessionRecord.model_validate_json(path.read_text(encoding="utf-8"))
            for path in self.root.glob("session-*.json")
        ]
        return tuple(sorted(records, key=lambda item: item.updated_at, reverse=True))

    def append(self, session: SessionRecord, role: str, content: str) -> None:
        session.turns.append(ConversationTurn(role=role, content=content))  # type: ignore[arg-type]
        self.save(session)


def transcript_prompt(session: SessionRecord, prompt: str, limit: int = 20) -> str:
    if not session.turns:
        return prompt
    history = "\n\n".join(f"{turn.role.title()}: {turn.content}" for turn in session.turns[-limit:])
    return f"Continue this conversation consistently.\n\n{history}\n\nUser: {prompt}"
