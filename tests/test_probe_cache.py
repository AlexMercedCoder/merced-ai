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
