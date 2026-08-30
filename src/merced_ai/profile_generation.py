"""Prompt-driven, review-first portable OAP profile generation."""

from __future__ import annotations

import hashlib
import json
import tempfile
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field

from merced_ai.harnesses.registry import HarnessRegistry, default_registry
from merced_ai.models import RunRequest
from merced_ai.profiles import NAME_RE, ProfileError, discover_profiles, validate_profile

GENERATION_CONTRACT = "merced-ai.oap-profile-generation.v1"


class ProfileDraft(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str
    description: str = Field(min_length=1, max_length=500)
    instructions: str = Field(min_length=1, max_length=20_000)
    objectives: list[str] = Field(default_factory=list, max_length=20)
    constraints: list[str] = Field(default_factory=list, max_length=20)
    default_permission: Literal["allow", "ask", "deny"] = "ask"
    shell_permission: Literal["allow", "ask", "deny"] = "ask"
    edit_permission: Literal["allow", "ask", "deny"] = "ask"
    network_permission: Literal["allow", "ask", "deny"] = "deny"


def generation_prompt(
    request: str,
    workspace: Path,
    *,
    preferred_name: str | None = None,
    feedback: str | None = None,
) -> str:
    existing = [item.name for item in discover_profiles(workspace)]
    correction = (
        "\nThe previous draft was rejected. Return a complete corrected replacement. "
        f"Feedback: {feedback[:2000]}"
        if feedback
        else ""
    )
    return (
        "Design one portable Open Agent Profile 1.0 specialist. Return exactly one JSON "
        "object and no markdown, using exactly these fields: name, description, instructions, "
        "objectives, constraints, default_permission, shell_permission, edit_permission, "
        "network_permission. Permission values are allow, ask, or deny. Use least authority, "
        "do not invent tools, skills, MCP servers, credentials, or paths. Merced AI will "
        "compile the result; tool availability will inherit the selected harness and remains "
        "bounded by its policy.\n"
        f"Existing profile names (do not reuse): {json.dumps(existing)}\n"
        f"Preferred name: {preferred_name or 'choose a unique kebab-case name'}\n"
        f"PROFILE REQUEST:\n{request.strip()}" + correction
    )


def generate_profile_proposal(
    request: str,
    workspace: Path,
    *,
    preferred_name: str | None = None,
    harness_id: str | None = None,
    author: Callable[[str], str] | None = None,
    registry: HarnessRegistry | None = None,
    autonomous: bool = False,
) -> dict[str, Any]:
    if not request.strip():
        raise ProfileError("Describe the profile to generate.")
    workspace = workspace.resolve()
    selected_harness = harness_id or ""
    if author is None:
        selected_harness, author = _harness_author(workspace, harness_id, registry)
    feedback: str | None = None
    for _attempt in range(3):
        response = author(
            generation_prompt(
                request,
                workspace,
                preferred_name=preferred_name,
                feedback=feedback,
            )
        )
        try:
            draft = _parse_draft(response)
            if preferred_name:
                draft = draft.model_copy(update={"name": preferred_name})
            document = compile_profile(draft, workspace)
            _validate_document(document)
            return {
                "contract": GENERATION_CONTRACT,
                "status": "proposed",
                "autonomous": autonomous,
                "requires_review": True,
                "harness": selected_harness or "injected-author",
                "document": document,
                "request_digest": "sha256:" + hashlib.sha256(request.encode("utf-8")).hexdigest(),
                "warnings": [],
            }
        except Exception as error:
            feedback = str(error)
    raise ProfileError(
        f"The model could not produce a valid portable profile after two corrections: {feedback}"
    )


def compile_profile(draft: ProfileDraft, workspace: Path) -> dict[str, Any]:
    if not NAME_RE.fullmatch(draft.name):
        raise ProfileError("profile name must match ^[a-z][a-z0-9-]{0,62}$")
    if draft.name in {item.name for item in discover_profiles(workspace)}:
        raise ProfileError(f"profile {draft.name!r} already exists")
    return {
        "oap": "1.0",
        "kind": "AgentProfile",
        "metadata": {
            "name": draft.name,
            "description": draft.description,
            "revision": 1,
        },
        "spec": {
            "role": {
                "instructions": draft.instructions.rstrip() + "\n",
                **({"objectives": draft.objectives} if draft.objectives else {}),
                **({"constraints": draft.constraints} if draft.constraints else {}),
            },
            "tools": {"policy": "inherit"},
            "permissions": {
                "default": draft.default_permission,
                "shell": draft.shell_permission,
                "edit": draft.edit_permission,
                "network": draft.network_permission,
            },
            "lifecycle": {"writeback": "propose"},
        },
        "state": {"revision": 1, "facts": [], "preferences": []},
        "history": [],
    }


def store_profile_proposal(proposal: Mapping[str, Any], workspace: Path) -> Path:
    root = workspace.resolve() / ".merced-ai" / "profile-proposals"
    root.mkdir(parents=True, exist_ok=True)
    digest = str(proposal.get("request_digest", "")).removeprefix("sha256:")[:16]
    if not digest:
        digest = hashlib.sha256(json.dumps(dict(proposal), sort_keys=True).encode()).hexdigest()[
            :16
        ]
    path = root / f"{digest}.json"
    path.write_text(json.dumps(dict(proposal), indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return path


def _parse_draft(content: str) -> ProfileDraft:
    decoder = json.JSONDecoder()
    for index, character in enumerate(content):
        if character != "{":
            continue
        try:
            candidate, _ = decoder.raw_decode(content[index:])
        except json.JSONDecodeError:
            continue
        if isinstance(candidate, dict):
            return ProfileDraft.model_validate(candidate)
    raise ProfileError("The model did not return one JSON profile draft.")


def _validate_document(document: Mapping[str, Any]) -> None:
    with tempfile.TemporaryDirectory(prefix="merced-ai-profile-review-") as directory:
        path = Path(directory) / "candidate.agent.yaml"
        path.write_text(yaml.safe_dump(dict(document), sort_keys=False), encoding="utf-8")
        validate_profile(path, "portable")


def _harness_author(
    workspace: Path,
    harness_id: str | None,
    registry: HarnessRegistry | None,
) -> tuple[str, Callable[[str], str]]:
    registry = registry or default_registry()
    priority = (
        [harness_id]
        if harness_id
        else [
            "magagent",
            "loro",
            "codex",
            "claude",
            "gemini",
            "opencode",
            "pi",
        ]
    )
    selected = None
    failures: list[str] = []
    for candidate in priority:
        if candidate is None:
            continue
        try:
            adapter = registry.get(candidate)
            probe = adapter.probe()
        except Exception as error:
            failures.append(f"{candidate}: {error}")
            continue
        if probe.path is not None and probe.status.value != "probe_failed":
            selected = adapter
            break
        failures.append(f"{candidate}: {probe.status.value}")
    if selected is None:
        raise ProfileError("No usable generation harness: " + ", ".join(failures))

    def author(prompt: str) -> str:
        with tempfile.TemporaryDirectory(prefix="merced-ai-profile-author-") as directory:
            path = Path(directory) / "profile-author.agent.yaml"
            document = {
                "oap": "1.0",
                "kind": "AgentProfile",
                "metadata": {
                    "name": "profile-author",
                    "description": "Authors conservative portable OAP profile drafts.",
                    "revision": 1,
                },
                "spec": {
                    "role": {"instructions": "Return only the requested profile draft JSON."},
                    "tools": {"policy": "allowlist", "allow": []},
                    "permissions": {
                        "default": "deny",
                        "shell": "deny",
                        "edit": "deny",
                        "network": "deny",
                    },
                    "lifecycle": {"writeback": "off"},
                },
            }
            path.write_text(yaml.safe_dump(document, sort_keys=False), encoding="utf-8")
            profile = validate_profile(path, "portable")
            projection = selected.project_profile(profile)
            result = selected.run(
                RunRequest(
                    harness_id=selected.descriptor.id,
                    prompt=prompt,
                    workspace=workspace,
                    profile=profile,
                    projection=projection,
                )
            )
            if result.exit_code:
                raise ProfileError(
                    f"Generation harness {selected.descriptor.id} exited {result.exit_code}."
                )
            return result.output

    return selected.descriptor.id, author
