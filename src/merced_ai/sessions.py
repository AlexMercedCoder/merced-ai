"""Durable, atomic local conversation records."""

from __future__ import annotations

import re
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal
from uuid import uuid4

from merced_ai.models import (
    ConversationTurn,
    ProfileRecord,
    SessionParticipant,
    SessionRecord,
)
from merced_ai.paths import ensure_project_layout
from merced_ai.storage import atomic_write, file_lock


class SessionStore:
    def __init__(self, workspace: Path) -> None:
        self.root = ensure_project_layout(workspace) / "sessions"

    def create(self, bot_name: str, harness_id: str, profile: ProfileRecord) -> SessionRecord:
        participant = SessionParticipant(
            bot_name=bot_name,
            harness_id=harness_id,
            profile_name=profile.name,
            profile_revision=profile.revision,
            profile_digest=profile.profile_digest,
            spec_digest=profile.spec_digest,
        )
        return self.create_group((participant,), mode="mentions")

    def create_group(
        self,
        participants: tuple[SessionParticipant, ...],
        *,
        mode: Literal["mentions", "all", "round_robin"] = "mentions",
        title: str | None = None,
        derived_from: str | None = None,
    ) -> SessionRecord:
        if not participants:
            raise ValueError("a session requires at least one participant")
        if len(participants) > 12:
            raise ValueError("a session supports at most twelve participants")
        names = [item.bot_name for item in participants]
        if len(names) != len(set(names)):
            raise ValueError("session participant names must be unique")
        now = datetime.now(UTC).isoformat()
        primary = participants[0]
        session = SessionRecord(
            id=f"session-{uuid4().hex}",
            title=title,
            derived_from=derived_from,
            bot_name=primary.bot_name,
            harness_id=primary.harness_id,
            workspace=self.root.parent.parent,
            profile_name=primary.profile_name,
            profile_revision=primary.profile_revision,
            profile_digest=primary.profile_digest,
            spec_digest=primary.spec_digest,
            created_at=now,
            updated_at=now,
            kind="group" if len(participants) > 1 else "single",
            mode=mode,
            participants=list(participants),
        )
        self.save(session)
        return session

    def save(self, session: SessionRecord) -> None:
        path = self._path(session.id)
        with file_lock(path):
            if path.exists():
                current = self.load(session.id)
                if current.storage_revision != session.storage_revision:
                    raise ValueError("Conversation changed in another client; reload before saving")
            elif session.storage_revision:
                raise ValueError("Conversation was deleted; it cannot be saved again")
            self._write(session)

    def _path(self, session_id: str) -> Path:
        if not re.fullmatch(r"session-[A-Za-z0-9-]+", session_id):
            raise ValueError("Invalid conversation identifier")
        return self.root / f"{session_id}.json"

    def _write(self, session: SessionRecord) -> None:
        revision = session.storage_revision
        session.updated_at = datetime.now(UTC).isoformat()
        session.storage_revision += 1
        try:
            atomic_write(self._path(session.id), session.model_dump_json(indent=2))
        except BaseException:
            session.storage_revision = revision
            raise

    def load(self, session_id: str) -> SessionRecord:
        path = self._path(session_id)
        if not path.is_file():
            raise ValueError(f"session {session_id!r} was not found")
        return SessionRecord.model_validate_json(path.read_text(encoding="utf-8"))

    def list(self) -> tuple[SessionRecord, ...]:
        records = [
            SessionRecord.model_validate_json(path.read_text(encoding="utf-8"))
            for path in self.root.glob("session-*.json")
        ]
        return tuple(sorted(records, key=lambda item: item.updated_at, reverse=True))

    def append(
        self,
        session: SessionRecord,
        role: str,
        content: str,
        *,
        bot_name: str | None = None,
        harness_id: str | None = None,
        turn_id: str | None = None,
        profile: ProfileRecord | None = None,
    ) -> None:
        turn = ConversationTurn(
            id=turn_id or str(uuid4()),
            role=role,
            content=content,
            bot_name=bot_name,
            harness_id=harness_id,
            profile_revision=profile.revision if profile else None,
            spec_digest=profile.spec_digest if profile else None,
            profile_digest=profile.profile_digest if profile else None,
        )
        with file_lock(self._path(session.id)):
            current = self.load(session.id)
            prior = next((item for item in current.turns if item.id == turn.id), None)
            if prior is not None and prior != turn:
                raise ValueError("Turn identifier was already used for different content")
            if prior is None:
                if role == "user" and not current.title:
                    current.title = " ".join(content.split())[:80]
                current.turns.append(turn)
                self._write(current)
            for field in SessionRecord.model_fields:
                setattr(session, field, getattr(current, field))

    def rename(self, session: SessionRecord, title: str) -> None:
        normalized = " ".join(title.split())
        if not normalized:
            raise ValueError("conversation title cannot be empty")
        session.title = normalized[:120]
        self.save(session)

    def delete(self, session_id: str) -> None:
        path = self._path(session_id)
        with file_lock(path):
            if not path.is_file():
                raise ValueError(f"session {session_id!r} was not found")
            path.unlink()


def transcript_prompt(
    session: SessionRecord,
    prompt: str,
    limit: int = 20,
    *,
    recipient: str | None = None,
) -> str:
    if not session.turns:
        if recipient:
            return f"You are {recipient}; respond only as {recipient}.\n\nUser: {prompt}"
        return prompt

    def label(turn: ConversationTurn) -> str:
        if turn.role == "user":
            return "User"
        return turn.bot_name or "Assistant"

    history = "\n\n".join(f"{label(turn)}: {turn.content}" for turn in session.turns[-limit:])
    audience = f" You are {recipient}; respond only as {recipient}." if recipient else ""
    return f"Continue this conversation consistently.{audience}\n\n{history}\n\nUser: {prompt}"


def select_participants(
    session: SessionRecord,
    prompt: str,
    *,
    dispatch: str | None = None,
) -> tuple[SessionParticipant, ...]:
    """Select recipients without allowing recursive autonomous bot loops."""
    participants = tuple(session.participants)
    by_name = {item.bot_name.casefold(): item for item in participants}
    folded_prompt = prompt.casefold()
    mentions = [
        item
        for item in participants
        if re.search(rf"@{re.escape(item.bot_name.casefold())}(?![\w-])", folded_prompt)
    ]
    if dispatch and dispatch not in {"mentions", "all", "round_robin"}:
        selected = by_name.get(dispatch.casefold())
        if selected is None:
            raise ValueError(f"bot {dispatch!r} is not a participant in this session")
        return (selected,)
    effective = dispatch or session.mode
    if effective == "all":
        return participants
    if effective == "round_robin":
        assistant_turns = sum(turn.role == "assistant" for turn in session.turns)
        return (participants[assistant_turns % len(participants)],)
    if mentions:
        return tuple(mentions)
    return (participants[0],)
