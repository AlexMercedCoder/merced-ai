from __future__ import annotations

import json
from pathlib import Path

from merced_ai.profile_generation import generate_profile_proposal, store_profile_proposal
from merced_ai.profiles import create_profile_document, discover_profiles


def _draft(**updates: object) -> str:
    payload = {
        "name": "release-reviewer",
        "description": "Reviews releases for evidence and bounded risk.",
        "instructions": "Review the release, cite evidence, and do not modify files.",
        "objectives": ["Find release-blocking defects."],
        "constraints": ["Do not edit files."],
        "default_permission": "ask",
        "shell_permission": "deny",
        "edit_permission": "deny",
        "network_permission": "deny",
    }
    payload.update(updates)
    return json.dumps(payload)


def test_generation_compiles_and_validates_a_reviewable_profile(workspace: Path) -> None:
    proposal = generate_profile_proposal(
        "Create a cautious release reviewer.",
        workspace,
        author=lambda _prompt: _draft(),
    )

    assert proposal["status"] == "proposed"
    assert proposal["requires_review"] is True
    document = proposal["document"]
    assert document["spec"]["tools"] == {"policy": "inherit"}
    assert document["spec"]["permissions"]["edit"] == "deny"
    assert document["state"] == {"revision": 1, "facts": [], "preferences": []}

    record = create_profile_document(document, workspace)
    assert record.name == "release-reviewer"
    assert [item.name for item in discover_profiles(workspace)] == ["release-reviewer"]


def test_generation_repairs_invalid_output_and_stores_autonomous_proposal(
    workspace: Path,
) -> None:
    responses = iter(["not json", _draft(name="subagent-researcher")])
    proposal = generate_profile_proposal(
        "Create a research subagent.",
        workspace,
        author=lambda _prompt: next(responses),
        autonomous=True,
    )
    path = store_profile_proposal(proposal, workspace)

    assert proposal["autonomous"] is True
    assert path.parent == workspace / ".merced-ai" / "profile-proposals"
    assert json.loads(path.read_text(encoding="utf-8"))["status"] == "proposed"
