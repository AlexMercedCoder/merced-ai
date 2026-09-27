"""MagAgent adapter: --prompt-file on newer MagAgent, argv fallback on older MagAgent."""

from __future__ import annotations

import json
import os
import stat
import sys
from pathlib import Path

import pytest

from merced_ai.harnesses import default_registry
from merced_ai.harnesses.adapters import command as command_module
from merced_ai.harnesses.adapters.command import (
    ARGV_LIMIT_POSIX,
    CommandHarnessAdapter,
    HarnessRunError,
)
from merced_ai.models import PromptDelivery, RunRequest
from merced_ai.profiles import create_profile, validate_profile

pytestmark = pytest.mark.skipif(os.name == "nt", reason="POSIX fake MagAgent script")

FAKE_MAGENT = r"""
import json, os, stat, sys
args = sys.argv[1:]
NEW = os.environ.get("FAKE_MAGENT_NEW") == "1"
if args[:2] == ["ask", "--help"]:
    print("Usage: magent ask [OPTIONS] [task]")
    if NEW:
        print("  --prompt-file <path>  Read the task from this UTF-8 file instead of argv.")
    sys.exit(0)
if args == ["--version"]:
    print("MagAgent 1.4.0" if NEW else "MagAgent 1.3.0"); sys.exit(0)
if args[:1] == ["capabilities"]:
    sys.exit(1)
if args[:1] == ["ask"]:
    if "--prompt-file" in args:
        path = args[args.index("--prompt-file") + 1]
        record = {"path": path, "mode": oct(stat.S_IMODE(os.stat(path).st_mode))}
        task = open(path, encoding="utf-8").read()
    else:
        task = args[1]
        record = {"argv_len": len(task)}
    record["argv"] = args
    with open(os.environ["FAKE_MAGENT_LOG"], "w") as log:
        json.dump(record, log)
    print(json.dumps({"ok": True, "response": f"got {len(task)} chars", "session_id": "mag-1"}))
"""


@pytest.fixture
def fake_magent(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    path = tmp_path / "magent"
    path.write_text(f"#!{sys.executable}\n{FAKE_MAGENT}", encoding="utf-8")
    path.chmod(path.stat().st_mode | stat.S_IEXEC)
    monkeypatch.setenv("MERCED_AI_MAGAGENT_PATH", str(path))
    monkeypatch.setenv("FAKE_MAGENT_LOG", str(tmp_path / "magent-log.json"))
    command_module._cached_features.cache_clear()
    return tmp_path / "magent-log.json"


def _adapter() -> CommandHarnessAdapter:
    adapter = default_registry().get("magagent")
    assert isinstance(adapter, CommandHarnessAdapter)
    return adapter


def _request(workspace: Path, prompt: str) -> RunRequest:
    existing = workspace / ".agents" / "helper.agent.yaml"
    profile = (
        validate_profile(existing, "project")
        if existing.exists()
        else create_profile(
            "helper", "Helps.", "Help.", workspace, edit_permission="deny", shell_permission="deny"
        )
    )
    return RunRequest(
        harness_id="magagent",
        prompt=prompt,
        workspace=workspace,
        profile=profile,
        projection=_adapter().project_profile(profile),
        timeout_seconds=60,
    )


def test_new_magagent_gets_a_private_prompt_file(
    fake_magent: Path, workspace: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("FAKE_MAGENT_NEW", "1")
    adapter = _adapter()
    large = "x" * (ARGV_LIMIT_POSIX * 3)

    result = adapter.run(_request(workspace, large))

    log = json.loads(fake_magent.read_text())
    assert log["mode"] == "0o600"
    assert not Path(log["path"]).exists()
    assert "--approval-stdio" in log["argv"] and "--json" in log["argv"]
    assert log["argv"][log["argv"].index("--agent") + 1] == "helper"
    assert int(result.output.split()[1]) >= len(large)
    assert result.native_session_id == "mag-1"
    probe = adapter.probe()
    assert probe.prompt_delivery == PromptDelivery.FILE
    assert probe.features == ("--prompt-file",)


def test_older_magagent_falls_back_to_argv_behind_the_guard(
    fake_magent: Path, workspace: Path
) -> None:
    adapter = _adapter()
    assert adapter.run(_request(workspace, "small task")).output == "got 10 chars"
    assert json.loads(fake_magent.read_text())["argv_len"] == 10
    assert adapter.probe().prompt_delivery == PromptDelivery.ARGV
    with pytest.raises(HarnessRunError, match="command line"):
        adapter.run(_request(workspace, "y" * (ARGV_LIMIT_POSIX + 1)))
