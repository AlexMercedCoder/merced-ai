"""Loro adapter: --prompt-file and --json on newer Loro, argv fallback on older Loro."""

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

pytestmark = pytest.mark.skipif(os.name == "nt", reason="POSIX fake Loro script")

FAKE_LORO = r"""
import json, os, stat, sys
args = sys.argv[1:]
NEW = os.environ.get("FAKE_LORO_NEW") == "1"
if args[:2] == ["run", "--help"]:
    print("Usage: loro run [OPTIONS] [prompt]")
    if NEW:
        print("  --prompt-file <path>  Read the task prompt from a UTF-8 file.")
        print("  --json                Print one JSON object.")
    sys.exit(0)
if args == ["--version"]:
    print("loro 0.22.0" if NEW else "loro 0.19.2"); sys.exit(0)
if args[:1] == ["capabilities"]:
    sys.exit(1)
if args[:1] == ["run"]:
    if "--prompt-file" in args:
        path = args[args.index("--prompt-file") + 1]
        mode = oct(stat.S_IMODE(os.stat(path).st_mode))
        task = open(path, encoding="utf-8").read()
        with open(os.environ["FAKE_LORO_LOG"], "w") as log:
            json.dump({"path": path, "mode": mode, "argv": args}, log)
    else:
        task = args[1]
        with open(os.environ["FAKE_LORO_LOG"], "w") as log:
            json.dump({"argv_len": len(task), "argv": args[:1]}, log)
    failing = "FAIL" in task
    if "--json" in args:
        print(json.dumps({
            "ok": not failing, "run_id": "r1", "session_id": "loro-session-1",
            "stop_reason": "provider_error" if failing else "completed", "steps": 1,
            "response": ("Provider error: nous returned HTTP 401" if failing
                         else f"got {len(task)} chars"),
        }))
        sys.exit(1 if failing else 0)
    print("Loro run mode completed.\n\nProvider: x / y\n\nStop reason: completed\nSteps: 1\n\n"
          f"Prompt: ...\n\nModel response: got {len(task)} chars")
"""


@pytest.fixture
def fake_loro(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    path = tmp_path / "loro"
    path.write_text(f"#!{sys.executable}\n{FAKE_LORO}", encoding="utf-8")
    path.chmod(path.stat().st_mode | stat.S_IEXEC)
    monkeypatch.setenv("MERCED_AI_LORO_PATH", str(path))
    monkeypatch.setenv("FAKE_LORO_LOG", str(tmp_path / "loro-log.json"))
    command_module._cached_features.cache_clear()
    return tmp_path / "loro-log.json"


def _adapter() -> CommandHarnessAdapter:
    adapter = default_registry().get("loro")
    assert isinstance(adapter, CommandHarnessAdapter)
    return adapter


def _request(workspace: Path, prompt: str) -> RunRequest:
    adapter = _adapter()
    existing = workspace / ".agents" / "helper.agent.yaml"
    profile = (
        validate_profile(existing, "project")
        if existing.exists()
        else create_profile(
            "helper", "Helps.", "Help.", workspace, edit_permission="deny", shell_permission="deny"
        )
    )
    return RunRequest(
        harness_id="loro",
        prompt=prompt,
        workspace=workspace,
        profile=profile,
        projection=adapter.project_profile(profile),
        timeout_seconds=60,
    )


def test_new_loro_gets_a_private_prompt_file_and_json(
    fake_loro: Path, workspace: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("FAKE_LORO_NEW", "1")
    adapter = _adapter()
    large = "x" * (ARGV_LIMIT_POSIX * 3)

    result = adapter.run(_request(workspace, large))

    log = json.loads(fake_loro.read_text())
    assert log["mode"] == "0o600"
    assert not Path(log["path"]).exists()  # removed after the run
    assert "--json" in log["argv"] and "--approval-stdio" in log["argv"]
    assert result.output.startswith("got ") and int(result.output.split()[1]) >= len(large)
    assert result.native_session_id == "loro-session-1"
    assert result.raw and result.raw["stop_reason"] == "completed"

    probe = adapter.probe()
    assert probe.prompt_delivery == PromptDelivery.FILE
    assert set(probe.features) == {"--prompt-file", "--json"}


def test_provider_error_is_a_failed_run_with_the_reason(
    fake_loro: Path, workspace: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("FAKE_LORO_NEW", "1")
    with pytest.raises(HarnessRunError, match="HTTP 401") as error:
        _adapter().run(_request(workspace, "FAIL please"))
    assert error.value.exit_code == 1


def test_older_loro_falls_back_to_argv_behind_the_guard(fake_loro: Path, workspace: Path) -> None:
    adapter = _adapter()
    result = adapter.run(_request(workspace, "small task"))
    assert result.output.startswith("got ")
    assert "argv_len" in json.loads(fake_loro.read_text())
    assert adapter.probe().prompt_delivery == PromptDelivery.ARGV

    with pytest.raises(HarnessRunError, match="command line"):
        adapter.run(_request(workspace, "y" * (ARGV_LIMIT_POSIX + 1)))
