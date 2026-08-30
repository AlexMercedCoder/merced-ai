from __future__ import annotations

import os
from pathlib import Path

import pytest
import yaml

from merced_ai.profiles import (
    ProfileError,
    assemble_system_prompt,
    create_profile,
    delete_profile,
    discover_profiles,
    update_profile,
    validate_profile,
)

OAP_REPO = Path(
    os.environ.get(
        "OAP_FIXTURE_REPO",
        str(Path(__file__).resolve().parents[4] / "open-agent-profile"),
    )
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
    assert record.revision == 1
    assert record.profile_digest.startswith("sha256:")
    assert "<open-agent-profile>" in prompt
    assert "Report defects" in prompt
    assert prompt.count("Instructions:") == 1
    delete_profile("reviewer", workspace)
    assert discover_profiles(workspace) == ()


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
    assert discovered[0].document["metadata"]["trust"] == "project"
    assert any("discovery collision" in item for item in discovered[0].warnings)
    assert discovered[0].document["spec"]["role"]["instructions"] == "Project instructions.\n"


def test_profile_editor_preserves_oap_fields_and_increments_revision(workspace: Path) -> None:
    created = create_profile(
        "builder",
        "Builds approved changes.",
        "Implement requested changes.",
        workspace,
        model_provider="openai",
        model_id="gpt-5.4",
        edit_permission="ask",
        shell_permission="deny",
    )
    created.document["spec"]["role"]["constraints"] = ["Keep changes scoped."]
    created.path.write_text(yaml.safe_dump(created.document, sort_keys=False), encoding="utf-8")

    updated = update_profile(
        "builder",
        "Builds and validates approved changes.",
        "Implement and test requested changes.",
        workspace,
        model_provider="anthropic",
        model_id="claude-sonnet-4-5",
        edit_permission="deny",
        shell_permission="deny",
    )

    assert updated.revision == 2
    assert updated.document["spec"]["role"]["constraints"] == ["Keep changes scoped."]
    assert updated.document["spec"]["model"]["provider"] == "anthropic"
    assert updated.document["spec"]["permissions"] == {"edit": "deny", "shell": "deny"}


def test_profile_editor_does_not_replace_valid_profile_with_invalid_candidate(
    workspace: Path,
) -> None:
    created = create_profile(
        "reviewer",
        "Reviews changes safely.",
        "Report concrete defects.",
        workspace,
    )

    with pytest.raises(ProfileError):
        update_profile(
            "reviewer",
            "Reviews changes safely.",
            "Report concrete defects.",
            workspace,
            edit_permission="invalid",
        )

    preserved = validate_profile(created.path, "project")
    assert preserved.revision == 1
    assert preserved.document["spec"]["role"]["instructions"] == "Report concrete defects.\n"


@pytest.mark.parametrize(
    "path",
    sorted((OAP_REPO / "examples").glob("*.agent.*")),
    ids=lambda path: path.name,
)
def test_all_immutable_upstream_profiles_load(path: Path) -> None:
    assert validate_profile(path).profile_digest.startswith("sha256:")


@pytest.mark.parametrize(
    "path",
    sorted((OAP_REPO / "examples" / "invalid").glob("*.agent.*")),
    ids=lambda path: path.name,
)
def test_all_immutable_upstream_invalid_profiles_are_rejected(path: Path) -> None:
    with pytest.raises(ProfileError):
        validate_profile(path)
