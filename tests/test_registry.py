from __future__ import annotations

from pathlib import Path

import pytest

from merced_ai.harnesses.adapters.executable import ExecutableProbeAdapter
from merced_ai.harnesses.detection import locate_executable
from merced_ai.harnesses.registry import HarnessRegistry, default_registry
from merced_ai.models import HarnessDescriptor, HarnessStatus, TransportKind


def test_default_registry_has_initial_harnesses() -> None:
    ids = {descriptor.id for descriptor in default_registry().descriptors()}
    assert {
        "codex",
        "claude",
        "gemini",
        "opencode",
        "goose",
        "loro",
        "magagent",
        "anton",
        "dsh",
        "agy",
        "pi",
        "prime-agent",
        "openclaw",
        "kimi",
    } <= ids


def test_registry_rejects_duplicate_ids() -> None:
    descriptor = HarnessDescriptor(
        id="fixture",
        name="Fixture",
        executable_names=("fixture",),
        transports=(TransportKind.STRUCTURED_SUBPROCESS,),
    )
    adapter = ExecutableProbeAdapter(descriptor)
    registry = HarnessRegistry((adapter,))
    with pytest.raises(ValueError, match="already registered"):
        registry.register(adapter)


def test_missing_executable_is_reported_without_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("merced_ai.harnesses.detection.shutil.which", lambda _name: None)
    descriptor = HarnessDescriptor(
        id="missing",
        name="Missing",
        executable_names=("does-not-exist",),
        transports=(TransportKind.TEXT_SUBPROCESS,),
    )

    probe = ExecutableProbeAdapter(descriptor).probe()

    assert probe.status is HarnessStatus.NOT_INSTALLED
    assert probe.path is None
    assert probe.capabilities_verified is False


def test_probe_uses_resolved_executable_without_shell(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    executable = tmp_path / "fixture"
    executable.touch()
    observed: dict[str, object] = {}

    class Result:
        returncode = 0
        stdout = "fixture 1.2.3\n"
        stderr = ""

    def fake_run(command: list[str], **kwargs: object) -> Result:
        observed["command"] = command
        observed.update(kwargs)
        return Result()

    monkeypatch.setattr("merced_ai.harnesses.detection.shutil.which", lambda _name: str(executable))
    monkeypatch.setattr("merced_ai.harnesses.detection.subprocess.run", fake_run)
    descriptor = HarnessDescriptor(
        id="fixture",
        name="Fixture",
        executable_names=("fixture",),
        transports=(TransportKind.STRUCTURED_SUBPROCESS,),
    )

    probe = ExecutableProbeAdapter(descriptor).probe()

    assert probe.status is HarnessStatus.INSTALLED
    assert probe.version == "fixture 1.2.3"
    assert probe.capabilities_verified is False
    assert observed["command"] == [str(executable.resolve()), "--version"]
    assert observed["shell"] is False


def test_locate_executable_finds_rootless_private_prefix(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    executable = tmp_path / ".openclaw" / "bin" / "openclaw"
    executable.parent.mkdir(parents=True)
    executable.touch(mode=0o755)
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setattr("merced_ai.harnesses.detection.shutil.which", lambda _name: None)
    descriptor = HarnessDescriptor(
        id="openclaw",
        name="OpenClaw",
        executable_names=("openclaw",),
        transports=(TransportKind.STRUCTURED_SUBPROCESS,),
    )

    assert locate_executable(descriptor) == executable.resolve()
