from __future__ import annotations

from pathlib import Path

from merced_ai.bots import create_bot, discover_bots, resolve_bot
from merced_ai.profiles import create_profile
from merced_ai.sessions import SessionStore, transcript_prompt


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
    assert [turn.role for turn in loaded.turns] == ["user", "assistant"]
    assert "User: First question" in prompt
    assert prompt.endswith("User: Next question")
