"""Adapters are specs on a public plugin API; third-party adapters load from entry points."""

from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path

import pytest
from typer.testing import CliRunner

from merced_ai.cli import app
from merced_ai.harnesses import default_registry
from merced_ai.harnesses.api import (
    ENTRY_POINT_GROUP,
    HarnessInvocation,
    HarnessSpec,
    InvocationContext,
)
from merced_ai.harnesses.builtin import BUILTIN_SPECS, descriptor
from merced_ai.harnesses.registry import adapter_from_plugin
from merced_ai.models import PromptDelivery
from merced_ai.testing.contract import check_harness_spec, run_harness_contract


def build_echo(ctx: InvocationContext) -> HarnessInvocation:
    return HarnessInvocation(
        [str(ctx.executable), "--json", "--stdin"], PromptDelivery.STDIN, stdin=ctx.prefixed_prompt
    )


ECHO_SPEC = HarnessSpec(
    descriptor("echo-agent", "Echo Agent", "echo-agent"), build_echo, output="json"
)


@pytest.mark.parametrize("spec", BUILTIN_SPECS, ids=lambda spec: spec.descriptor.id)
def test_every_builtin_adapter_passes_the_contract_kit(spec: HarnessSpec, tmp_path: Path) -> None:
    report = check_harness_spec(spec, tmp_path)
    assert len(report.checked) > 15


def test_contract_kit_catches_a_dishonest_adapter(tmp_path: Path) -> None:
    def leaky(ctx: InvocationContext) -> HarnessInvocation:
        # Claims stdin but puts the prompt on the command line.
        return HarnessInvocation([str(ctx.executable), ctx.prompt], PromptDelivery.STDIN)

    report = run_harness_contract(replace(ECHO_SPEC, build=leaky), tmp_path)

    joined = "\n".join(report.failures)
    assert "prompt-arrives" in joined
    assert "prompt-off-argv" in joined
    assert "large-prompt-off-argv" in joined
    with pytest.raises(AssertionError, match="fails the Merced AI adapter contract"):
        check_harness_spec(replace(ECHO_SPEC, build=leaky), tmp_path / "again")


class FakeEntryPoint:
    def __init__(self, name: str, value: object, dist: str = "merced-echo") -> None:
        self.name = name
        self.value = f"{dist}:SPEC"
        self._value = value
        self.dist = type("Dist", (), {"name": dist})()

    def load(self) -> object:
        if isinstance(self._value, Exception):
            raise self._value
        return self._value


def _entry_points(monkeypatch: pytest.MonkeyPatch, *points: FakeEntryPoint) -> None:
    def fake(*, group: str) -> tuple[FakeEntryPoint, ...]:
        assert group == ENTRY_POINT_GROUP
        return points

    monkeypatch.setattr("merced_ai.harnesses.registry.metadata.entry_points", fake)


def test_entry_point_plugins_register_alongside_builtins(monkeypatch: pytest.MonkeyPatch) -> None:
    _entry_points(
        monkeypatch,
        FakeEntryPoint("echo", ECHO_SPEC),
        FakeEntryPoint(
            "factory",
            lambda: replace(ECHO_SPEC, descriptor=descriptor("echo-two", "Echo Two", "echo-two")),
        ),
        FakeEntryPoint("dupe", replace(ECHO_SPEC, descriptor=descriptor("codex", "Fake", "codex"))),
        FakeEntryPoint("broken", ImportError("no module named echo")),
        FakeEntryPoint("future", replace(ECHO_SPEC, api_version=99)),
        FakeEntryPoint("junk", 42),
    )

    registry = default_registry()

    ids = {item.id for item in registry.descriptors()}
    assert {"echo-agent", "echo-two", "codex"} <= ids
    assert registry.get("echo-agent").spec.origin == "merced-echo"  # type: ignore[attr-defined]
    assert registry.get("codex").spec.origin == "builtin"  # type: ignore[attr-defined]
    errors = dict(registry.plugin_errors)
    assert "already registered" in errors["dupe"]
    assert "ImportError" in errors["broken"]
    assert "version 99" in errors["future"]
    assert "HarnessSpec" in errors["junk"]


def test_plugins_can_be_disabled(monkeypatch: pytest.MonkeyPatch) -> None:
    _entry_points(monkeypatch, FakeEntryPoint("echo", ECHO_SPEC))
    monkeypatch.setenv("MERCED_AI_DISABLE_PLUGINS", "1")
    assert "echo-agent" not in {item.id for item in default_registry().descriptors()}


def test_cli_reports_plugin_origin_and_load_errors(monkeypatch: pytest.MonkeyPatch) -> None:
    _entry_points(
        monkeypatch,
        FakeEntryPoint("echo", ECHO_SPEC),
        FakeEntryPoint("broken", ImportError("boom")),
    )
    monkeypatch.setattr("merced_ai.harnesses.detection.locate_executable", lambda _d: None)
    runner = CliRunner()

    shown = runner.invoke(app, ["harness", "show", "echo-agent"], env={"COLUMNS": "200"})
    listed = runner.invoke(app, ["harness", "list"], env={"COLUMNS": "200"})
    as_json = runner.invoke(app, ["harness", "list", "--json"])

    assert shown.exit_code == 0, shown.output
    assert "plugin from merced-echo" in shown.stdout
    assert "harness plugin 'broken' was not loaded" in " ".join(listed.stderr.split())
    assert "echo-agent" in {item["harness_id"] for item in json.loads(as_json.stdout)}


def test_full_adapter_objects_are_accepted_as_plugins() -> None:
    class Minimal:
        descriptor = ECHO_SPEC.descriptor

        def probe(self):  # pragma: no cover - protocol shape only
            raise NotImplementedError

        def project_profile(self, profile):  # pragma: no cover
            raise NotImplementedError

        def run(self, request):  # pragma: no cover
            raise NotImplementedError

    adapter = Minimal()
    assert adapter_from_plugin(adapter, "dist") is adapter
