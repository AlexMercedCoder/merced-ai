"""Prompts travel over stdin or a private file; argv is bounded by a per-OS guard."""

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
    ARGV_LIMIT_WINDOWS,
    PROMPT_TOO_LARGE_EXIT,
    CommandHarnessAdapter,
    HarnessRunError,
    argv_size,
    check_argv_size,
)
from merced_ai.harnesses.process import run_child
from merced_ai.models import PromptDelivery, RunRequest
from merced_ai.profiles import create_profile

ONE_MB = 1024 * 1024
MARKER = "END-OF-LARGE-PROMPT"

# Echoes back what arrived on each channel so the test can assert the full prompt got through.
FAKE_HARNESS = r"""
import hashlib, json, sys
data = sys.stdin.buffer.read().decode("utf-8")
files = {}
argv = sys.argv[1:]
for flag in ("--system-prompt-file", "--append-system-prompt", "--message-file"):
    if flag in argv:
        with open(argv[argv.index(flag) + 1], encoding="utf-8") as handle:
            files[flag] = handle.read()
channels = {"stdin": data, **files}
report = {
    name: {"length": len(value), "marker": value.rstrip().endswith("END-OF-LARGE-PROMPT")}
    for name, value in channels.items()
}
report["argv_bytes"] = sum(len(item.encode()) + 1 for item in sys.argv)
print(json.dumps({"result": json.dumps(report)}))
"""


def _fake_executable(directory: Path) -> Path:
    script = directory / "fake_harness.py"
    script.write_text(FAKE_HARNESS, encoding="utf-8")
    if os.name == "nt":  # pragma: no cover - Windows CI
        launcher = directory / "fake_harness.cmd"
        launcher.write_text(f'@"{sys.executable}" "{script}" %*\r\n', encoding="utf-8")
        return launcher
    launcher = directory / "fake_harness"
    launcher.write_text(
        f"#!{sys.executable}\n" + script.read_text(encoding="utf-8"), encoding="utf-8"
    )
    launcher.chmod(launcher.stat().st_mode | stat.S_IEXEC)
    return launcher


def _request(adapter: CommandHarnessAdapter, workspace: Path, prompt: str) -> RunRequest:
    profile = create_profile(
        "reviewer",
        "Reviews code for correctness without modifying the workspace.",
        "Review code and report defects.",
        workspace,
        edit_permission="deny",
        shell_permission="deny",
    )
    return RunRequest(
        harness_id=adapter.descriptor.id,
        prompt=prompt,
        workspace=workspace,
        profile=profile,
        projection=adapter.project_profile(profile),
    )


def _registry_adapter(harness_id: str) -> CommandHarnessAdapter:
    adapter = default_registry().get(harness_id)
    assert isinstance(adapter, CommandHarnessAdapter)
    return adapter


ALL_HARNESSES = sorted(item.id for item in default_registry().descriptors())


@pytest.mark.parametrize("harness_id", ALL_HARNESSES)
def test_descriptor_prompt_delivery_matches_the_adapter(
    harness_id: str, workspace: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(command_module, "locate_executable", lambda _d: Path("/bin/harness"))
    adapter = _registry_adapter(harness_id)

    invocation = adapter.build_invocation(_request(adapter, workspace, "hello"), tmp_path)

    assert invocation.prompt_delivery == adapter.descriptor.prompt_delivery
    if invocation.prompt_delivery is PromptDelivery.STDIN:
        assert invocation.stdin
    else:
        assert invocation.stdin is None


STREAMING_INPUT = [
    item.id
    for item in default_registry().descriptors()
    if item.prompt_delivery is not PromptDelivery.ARGV and item.id != "anton"
]


@pytest.mark.parametrize("harness_id", STREAMING_INPUT)
def test_one_megabyte_prompt_reaches_a_fake_harness_intact(
    harness_id: str, workspace: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake = _fake_executable(tmp_path)
    monkeypatch.setattr(command_module, "locate_executable", lambda _d: fake)
    adapter = _registry_adapter(harness_id)
    prompt = "x" * ONE_MB + "\n" + MARKER

    result = adapter.run(_request(adapter, workspace, prompt))

    payload = json.loads(result.output)
    # JSON-normalizing adapters return the "result" text; text adapters return raw stdout.
    report = json.loads(payload["result"]) if "result" in payload else payload
    channel = "--message-file" if harness_id == "openclaw" else "stdin"
    assert report[channel]["length"] >= ONE_MB
    assert report[channel]["marker"] is True
    assert report["argv_bytes"] < ARGV_LIMIT_POSIX


def test_prompt_files_are_private_and_removed_after_the_run(
    workspace: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    seen: dict[str, object] = {}

    def capture(command: list[str], **kwargs: object) -> object:
        path = Path(command[command.index("--system-prompt-file") + 1])
        seen["path"] = path
        seen["mode"] = stat.S_IMODE(path.stat().st_mode)
        seen["stdin"] = kwargs["stdin_payload"]
        from merced_ai.harnesses.process import ChildResult

        return ChildResult('{"result": "ok"}', "", 0, False)

    monkeypatch.setattr(command_module, "locate_executable", lambda _d: Path("/bin/claude"))
    monkeypatch.setattr(command_module, "run_child", capture)
    adapter = _registry_adapter("claude")

    assert adapter.run(_request(adapter, workspace, "hello")).output == "ok"
    assert seen["stdin"] == "hello"
    assert not Path(str(seen["path"])).exists()
    if os.name != "nt":
        assert seen["mode"] == 0o600


def test_argv_size_counts_bytes_and_separators(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(command_module.sys, "platform", "linux")
    assert argv_size(["ab", "é"]) == 3 + 3
    monkeypatch.setattr(command_module.sys, "platform", "win32")
    assert argv_size(["a b", "c"]) == len('"a b" c')


@pytest.mark.parametrize(
    ("platform", "limit"), [("linux", ARGV_LIMIT_POSIX), ("win32", ARGV_LIMIT_WINDOWS)]
)
def test_argv_guard_refuses_oversized_command_lines(
    platform: str, limit: int, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(command_module.sys, "platform", platform)
    check_argv_size(["tool", "x" * (limit - 100)], "Tool")
    with pytest.raises(HarnessRunError, match="too large to pass to Tool") as error:
        check_argv_size(["tool", "x" * (limit + 1)], "Tool")
    assert error.value.exit_code == PROMPT_TOO_LARGE_EXIT
    assert f"{limit // 1024} KB" in str(error.value)


@pytest.mark.parametrize("harness_id", ["loro", "magagent", "dsh", "agy"])
def test_argv_only_harness_refuses_a_huge_prompt_before_spawning(
    harness_id: str, workspace: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(command_module, "locate_executable", lambda _d: Path("/bin/harness"))

    def never(*_args: object, **_kwargs: object) -> object:
        raise AssertionError("the guard must stop the run before spawning")

    monkeypatch.setattr(command_module, "run_child", never)
    adapter = _registry_adapter(harness_id)

    with pytest.raises(HarnessRunError, match="command line") as error:
        adapter.run(_request(adapter, workspace, "y" * (ARGV_LIMIT_POSIX + 1)))
    assert error.value.exit_code == PROMPT_TOO_LARGE_EXIT


def test_child_that_ignores_stdin_is_not_a_transport_failure(workspace: Path) -> None:
    code = "import sys; sys.stdout.write('done')"

    result = run_child(
        [sys.executable, "-c", code],
        workspace=workspace,
        env=dict(os.environ),
        timeout=60,
        cancellation=None,
        limit=10_000,
        stdin_payload="z" * (4 * ONE_MB),
    )

    assert result.returncode == 0
    assert result.stdout == "done"
