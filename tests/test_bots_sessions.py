from __future__ import annotations

from pathlib import Path

import pytest

from merced_ai.bots import create_bot, delete_bot, discover_bots, resolve_bot, update_bot
from merced_ai.models import ConversationTurn, SessionParticipant, SessionRecord
from merced_ai.profiles import create_profile
from merced_ai.sessions import SessionStore, select_participants, transcript_prompt


def test_bot_binding_round_trip(workspace: Path) -> None:
    create_profile(
        "reviewer",
        "Reviews code for correctness before a pull request is opened.",
        "Review code and report defects.",
        workspace,
    )
    created = create_bot("reviewer", "reviewer", "codex", ("claude",), workspace)

    loaded = resolve_bot("reviewer", workspace)

    assert [item.name for item in discover_bots(workspace)] == ["reviewer"]
    assert loaded == created
    assert loaded.harness.fallbacks == ("claude",)

    updated = update_bot("reviewer", "reviewer", "loro", ("codex",), workspace)
    assert updated.harness.preferred == "loro"
    delete_bot("reviewer", workspace)
    assert discover_bots(workspace) == ()


def test_session_store_is_durable_and_builds_bounded_transcript(workspace: Path) -> None:
    profile = create_profile(
        "notes",
        "Maintains concise notes for the current working conversation.",
        "Summarize decisions.",
        workspace,
    )
    store = SessionStore(workspace)
    session = store.create("notes", "codex", profile)
    store.append(session, "user", "First question")
    store.append(session, "assistant", "First answer")

    loaded = store.load(session.id)
    prompt = transcript_prompt(loaded, "Next question")

    assert len(store.list()) == 1
    assert loaded.title == "First question"
    assert [turn.role for turn in loaded.turns] == ["user", "assistant"]
    assert "User: First question" in prompt
    assert prompt.endswith("User: Next question")
    store.rename(loaded, "  Project   notes  ")
    assert store.load(session.id).title == "Project notes"
    store.delete(session.id)
    assert store.list() == ()
    with pytest.raises(ValueError, match="was not found"):
        store.delete(session.id)


def test_legacy_session_migrates_in_memory_without_rewriting() -> None:
    record = SessionRecord.model_validate(
        {
            "id": "session-old",
            "bot_name": "reviewer",
            "harness_id": "codex",
            "workspace": ".",
            "profile_name": "reviewer",
            "profile_revision": 1,
            "profile_digest": "profile",
            "spec_digest": "spec",
            "created_at": "2026-01-01T00:00:00Z",
            "updated_at": "2026-01-01T00:00:00Z",
            "turns": [{"role": "assistant", "content": "Legacy response"}],
        }
    )

    assert record.kind == "single"
    assert [item.bot_name for item in record.participants] == ["reviewer"]
    assert record.turns[0].bot_name is None


def test_group_selection_mentions_all_direct_and_round_robin() -> None:
    participants = [
        SessionParticipant(
            bot_name=name,
            harness_id="codex",
            profile_name=name,
            profile_revision=1,
            profile_digest=name,
            spec_digest=name,
        )
        for name in ("reviewer", "builder", "tester")
    ]
    record = SessionRecord(
        id="session-group",
        bot_name="reviewer",
        harness_id="codex",
        workspace=Path("."),
        profile_name="reviewer",
        profile_revision=1,
        profile_digest="reviewer",
        spec_digest="reviewer",
        created_at="2026-01-01T00:00:00Z",
        updated_at="2026-01-01T00:00:00Z",
        participants=participants,
        mode="mentions",
    )

    assert [item.bot_name for item in select_participants(record, "@tester check this")] == [
        "tester"
    ]
    assert [item.bot_name for item in select_participants(record, "check", dispatch="all")] == [
        "reviewer",
        "builder",
        "tester",
    ]
    assert select_participants(record, "check", dispatch="builder")[0].bot_name == "builder"
    assert transcript_prompt(record, "raw prompt") == "raw prompt"
    with pytest.raises(ValueError, match="not a participant"):
        select_participants(record, "check", dispatch="missing")
    record.turns.append(ConversationTurn(role="assistant", content="done", bot_name="reviewer"))
    assert select_participants(record, "next", dispatch="round_robin")[0].bot_name == "builder"


def test_group_store_rejects_empty_and_duplicate_participants(workspace: Path) -> None:
    store = SessionStore(workspace)
    with pytest.raises(ValueError, match="at least one"):
        store.create_group(())
    participant = SessionParticipant(
        bot_name="same",
        harness_id="codex",
        profile_name="same",
        profile_revision=1,
        profile_digest="same",
        spec_digest="same",
    )
    with pytest.raises(ValueError, match="unique"):
        store.create_group((participant, participant))
    too_many = tuple(
        participant.model_copy(update={"bot_name": f"bot-{index}"}) for index in range(13)
    )
    with pytest.raises(ValueError, match="at most twelve"):
        store.create_group(too_many)
