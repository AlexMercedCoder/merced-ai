"""Routing reuses recent harness probes instead of spawning each harness every turn."""

from __future__ import annotations

from pathlib import Path

import pytest

from merced_ai.application import prepare_run
from merced_ai.bots import create_bot
from merced_ai.harnesses import default_registry
from merced_ai.harnesses.adapters.command import CommandHarnessAdapter
from merced_ai.models import HarnessProbe, HarnessStatus
from merced_ai.profiles import create_profile


@pytest.fixture
def probe_calls(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    calls: list[str] = []

    def probe(adapter: CommandHarnessAdapter, workspace: Path | None = None) -> HarnessProbe:
        calls.append(adapter.descriptor.id)
        return HarnessProbe(
            harness_id=adapter.descriptor.id,
            status=HarnessStatus.INSTALLED,
            path=Path("/bin/true"),
            broker_implements=adapter.descriptor.broker_implements,
        )

    monkeypatch.setattr(CommandHarnessAdapter, "probe", probe)
    return calls


def _bot(workspace: Path) -> None:
    create_profile("reviewer", "Reviews code.", "Review.", workspace)
    create_bot("reviewer", "reviewer", "codex", (), workspace)


def test_three_turns_probe_once_with_a_shared_registry(
    workspace: Path, probe_calls: list[str]
) -> None:
    _bot(workspace)
    registry = default_registry()

    for _ in range(3):
        prepare_run("reviewer", "hi", workspace, registry=registry)

    assert probe_calls == ["codex"]


def test_ttl_expiry_and_opt_out(
    workspace: Path, probe_calls: list[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    _bot(workspace)
    monkeypatch.setenv("MERCED_AI_PROBE_TTL_SECONDS", "0")
    registry = default_registry()
    prepare_run("reviewer", "hi", workspace, registry=registry)
    prepare_run("reviewer", "hi", workspace, registry=registry)
    assert probe_calls == ["codex", "codex"]

    monkeypatch.setenv("MERCED_AI_PROBE_TTL_SECONDS", "30")
    clock = iter([100.0, 110.0, 200.0])
    monkeypatch.setattr("merced_ai.harnesses.registry.time.monotonic", lambda: next(clock))
    probe_calls.clear()
    registry = default_registry()
    for _ in range(3):
        registry.cached_probe("codex", workspace)
    assert probe_calls == ["codex", "codex"]  # t=100 miss, t=110 hit, t=200 expired


def test_invalid_ttl_falls_back_to_default(monkeypatch: pytest.MonkeyPatch) -> None:
    from merced_ai.harnesses.registry import DEFAULT_PROBE_TTL_SECONDS, probe_ttl_seconds

    monkeypatch.setenv("MERCED_AI_PROBE_TTL_SECONDS", "soon")
    assert probe_ttl_seconds() == DEFAULT_PROBE_TTL_SECONDS


def test_web_probes_check_loro_and_magagent_first(tmp_path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    from merced_ai.harnesses import default_registry
    from merced_ai.harnesses.adapters.command import CommandHarnessAdapter
    from merced_ai.models import HarnessProbe, HarnessStatus
    from merced_ai.web import probes as probes_module

    started: list[str] = []

    def probe(adapter, workspace=None):  # type: ignore[no-untyped-def]
        started.append(adapter.descriptor.id)
        return HarnessProbe(harness_id=adapter.descriptor.id, status=HarnessStatus.NOT_INSTALLED)

    monkeypatch.setattr(CommandHarnessAdapter, "probe", probe)
    monkeypatch.setattr(probes_module, "PROBE_WORKERS", 1)  # deterministic start order
    cache = probes_module.HarnessProbeCache(default_registry(), tmp_path / "probes.json")
    cache.start_refresh()
    deadline = __import__("time").monotonic() + 10
    while cache.payload()["refreshing"] and __import__("time").monotonic() < deadline:
        __import__("time").sleep(0.02)

    assert started[:2] == ["loro", "magagent"]
    assert {item["status"] for item in cache.payload()["harnesses"]} == {"not_installed"}


def test_a_slow_version_check_says_so(tmp_path, monkeypatch) -> None:  # type: ignore[no-untyped-def]

    from merced_ai.harnesses import default_registry, detection

    slow = tmp_path / "loro"
    slow.write_text("#!/bin/sh\nsleep 5\n", encoding="utf-8")
    slow.chmod(0o755)
    monkeypatch.setenv("MERCED_AI_LORO_PATH", str(slow))
    assert detection.PROBE_TIMEOUT_SECONDS >= 10
    monkeypatch.setattr(detection, "PROBE_TIMEOUT_SECONDS", 0.2)

    probe = detection.probe_executable(default_registry().get("loro").descriptor)

    assert probe.status.value == "probe_failed"
    assert "did not answer within" in probe.warnings[0] and "refresh detection" in probe.warnings[0]
