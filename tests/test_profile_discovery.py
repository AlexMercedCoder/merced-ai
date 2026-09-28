"""Profile discovery across .agents/, .loro/agents/, and .magent/agents/ (I-20)."""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest
from typer.testing import CliRunner

from merced_ai.bots import create_bot
from merced_ai.cli import app
from merced_ai.harnesses.api import native_profile_visible
from merced_ai.profiles import ProfileError, create_profile, discover_profiles, resolve_profile

runner = CliRunner()


def _copy_to(workspace: Path, name: str, directory: str) -> Path:
    source = workspace / ".agents" / f"{name}.agent.yaml"
    target = workspace / directory / source.name
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(source, target)
    source.unlink()
    return target


def test_harness_directories_are_discovered_with_their_origin(workspace: Path) -> None:
    create_profile("portable", "Portable.", "Help.", workspace)
    create_profile("parrot", "From Loro.", "Help.", workspace)
    create_profile("mag", "From MagAgent.", "Help.", workspace)
    _copy_to(workspace, "parrot", ".loro/agents")
    _copy_to(workspace, "mag", ".magent/agents")

    found = {item.name: item for item in discover_profiles(workspace)}

    assert {name: item.origin for name, item in found.items()} == {
        "portable": ".agents",
        "parrot": ".loro/agents",
        "mag": ".magent/agents",
    }
    assert all(item.source == "project" and item.conflict is None for item in found.values())
    # Each native harness is only handed profiles it discovers itself.
    assert native_profile_visible(found["parrot"], "loro")
    assert not native_profile_visible(found["parrot"], "magagent")
    assert native_profile_visible(found["mag"], "magagent")
    assert not native_profile_visible(found["mag"], "loro")
    assert native_profile_visible(found["portable"], "loro")
    assert native_profile_visible(found["portable"], "magagent")


def test_identical_copies_are_not_a_conflict(workspace: Path) -> None:
    create_profile("shared", "Shared.", "Help.", workspace)
    copy = workspace / ".magent" / "agents" / "shared.agent.yaml"
    copy.parent.mkdir(parents=True)
    shutil.copyfile(workspace / ".agents" / "shared.agent.yaml", copy)

    record = resolve_profile("shared", workspace)

    assert record.origin == ".magent/agents"  # the harness directory has precedence
    assert record.also_in == (".agents",)
    assert record.conflict is None


def test_different_definitions_are_reported_and_never_picked(workspace: Path) -> None:
    create_profile("twin", "Portable twin.", "One way.", workspace)
    other = workspace / ".loro" / "agents" / "twin.agent.yaml"
    other.parent.mkdir(parents=True)
    other.write_text(
        (workspace / ".agents" / "twin.agent.yaml")
        .read_text(encoding="utf-8")
        .replace("One way.", "Another way."),
        encoding="utf-8",
    )

    listed = {item.name: item for item in discover_profiles(workspace)}

    assert listed["twin"].conflict is not None
    assert ".agents/twin.agent.yaml" in listed["twin"].conflict
    assert ".loro/agents/twin.agent.yaml" in listed["twin"].conflict
    with pytest.raises(ProfileError, match="different definitions"):
        resolve_profile("twin", workspace)
    assert not native_profile_visible(listed["twin"], "loro")


def test_cli_lists_the_source_and_the_conflict(workspace: Path) -> None:
    create_profile("helper", "Helps.", "Help.", workspace)
    create_bot("helper", "helper", "codex", (), workspace)
    create_profile("twin", "Twin.", "One way.", workspace)
    other = workspace / ".magent" / "agents" / "twin.agent.yaml"
    other.parent.mkdir(parents=True)
    other.write_text(
        (workspace / ".agents" / "twin.agent.yaml")
        .read_text(encoding="utf-8")
        .replace("One way.", "Another way."),
        encoding="utf-8",
    )

    profiles = runner.invoke(app, ["profile", "list", "-C", str(workspace)], env={"COLUMNS": "200"})
    bots = runner.invoke(app, ["bot", "list", "-C", str(workspace)], env={"COLUMNS": "200"})

    assert profiles.exit_code == 0, profiles.output
    assert ".magent/agents" in profiles.output and "conflict" in profiles.output.lower()
    assert bots.exit_code == 0, bots.output
    assert ".agents" in bots.output


@pytest.mark.anyio
async def test_web_reports_a_conflicted_profile_instead_of_failing(workspace: Path) -> None:
    import httpx

    from merced_ai.webui_server import create_web_app

    create_profile("twin", "Twin.", "One way.", workspace)
    create_bot("twin", "twin", "magagent", (), workspace)
    other = workspace / ".magent" / "agents" / "twin.agent.yaml"
    other.parent.mkdir(parents=True)
    other.write_text(
        (workspace / ".agents" / "twin.agent.yaml")
        .read_text(encoding="utf-8")
        .replace("One way.", "Another way."),
        encoding="utf-8",
    )
    transport = httpx.ASGITransport(app=create_web_app(workspace, "token"))
    async with httpx.AsyncClient(
        transport=transport, base_url="http://127.0.0.1", headers={"x-merced-ai-token": "token"}
    ) as client:
        projection = await client.get("/api/projection/twin")
        boot = (await client.get("/api/bootstrap")).json()

    assert projection.status_code == 409 and "different definitions" in projection.text
    profile = next(item for item in boot["profiles"] if item["name"] == "twin")
    assert profile["origin"] == ".magent/agents" and profile["conflict"]
    bot = next(item for item in boot["bots"] if item["name"] == "twin")
    assert bot["profile_origin"] is None and "different definitions" in bot["profile_problem"]
