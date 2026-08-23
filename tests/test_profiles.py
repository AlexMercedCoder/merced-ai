from __future__ import annotations

from pathlib import Path

import pytest

from merced_ai.profiles import (
    ProfileError,
    assemble_system_prompt,
    create_profile,
    discover_profiles,
    validate_profile,
)


def test_create_discover_and_assemble_profile(workspace: Path) -> None:
    record = create_profile(
        "reviewer",
        "Reviews code for concrete correctness defects before merge.",
        "Review code. Report defects and do not edit files.",
        workspace,
    )

    discovered = discover_profiles(workspace)
    prompt = assemble_system_prompt(record)

    assert [item.name for item in discovered] == ["reviewer"]
    assert record.path == workspace / ".agents" / "reviewer.agent.yaml"
    assert record.profile_digest.startswith("sha256:")
    assert "<open-agent-profile>" in prompt
    assert "Report defects" in prompt


def test_reference_validator_rejects_literal_secret(workspace: Path) -> None:
    profile = workspace / "unsafe.agent.yaml"
    token = "sk-" + ("a" * 32)
    profile.write_text(
        f"""oap: '1.0'
kind: AgentProfile
metadata:
  name: unsafe
  description: Unsafe test profile with a literal secret.
spec:
  role:
    instructions: Never persist {token}.
""",
        encoding="utf-8",
    )

    with pytest.raises(ProfileError, match="literal OpenAI-style API key"):
        validate_profile(profile)


def test_project_profile_overrides_user_profile_with_same_name(workspace: Path) -> None:
    from merced_ai.paths import ensure_user_layout

    user_agents = ensure_user_layout() / "agents"
    user_profile = user_agents / "reviewer.agent.yaml"
    user_profile.write_text(
        """oap: '1.0'
kind: AgentProfile
metadata:
  name: reviewer
  description: User-level reviewer profile used across projects.
spec:
  role:
    instructions: User instructions.
""",
        encoding="utf-8",
    )
    create_profile(
        "reviewer",
        "Project-level reviewer profile for this workspace.",
        "Project instructions.",
        workspace,
    )

    discovered = discover_profiles(workspace)

    assert len(discovered) == 1
    assert discovered[0].source == "project"
    assert discovered[0].document["spec"]["role"]["instructions"] == "Project instructions.\n"
