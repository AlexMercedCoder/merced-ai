"""Opt-in live smoke tests against the harnesses installed on this machine.

These tests run real harness executables and, for the run test, make one tiny model call per
harness. They are skipped unless ``MERCED_AI_LIVE_SMOKE=1``.

- ``test_adapter_flags_appear_in_harness_help`` (no model call) builds each adapter's exact argv
  and checks that every flag appears in the installed harness's own ``--help`` output.
- ``test_harness_answers_through_the_adapter`` sends "Reply with OK" through the real adapter,
  with edit and shell denied, and checks the reply. The harness's accepting the exact flags,
  prompt channel, and output format is what this verifies.

Environment:

- ``MERCED_AI_LIVE_SMOKE_HARNESSES``: comma-separated harness IDs to run (default: every installed
  runnable harness). The help check always covers every installed harness.
- ``MERCED_AI_LIVE_SMOKE_NOUS=1``: run Loro and MagAgent against Nous Portal
  (``deepseek/deepseek-v4-flash``, key from ``NOUS_API_KEY``) using a throwaway ``HOME`` holding a
  minimal config (and, for MagAgent, a throwaway user), so the user's real Loro and MagAgent
  configuration is never read or changed. Point ``MERCED_AI_LORO_PATH`` or
  ``MERCED_AI_MAGAGENT_PATH`` at a specific build to test it instead of the one on ``PATH``.
- ``MERCED_AI_LIVE_SMOKE_GEMINI_API_KEY=1``: run Gemini CLI with a throwaway ``HOME`` so it
  authenticates with ``GEMINI_API_KEY`` instead of whatever login the user's Gemini config holds.
- ``MERCED_AI_LIVE_SMOKE_REPORT``: optional path; one JSON line per run is appended (no prompts,
  no secrets, errors truncated).
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import time
from pathlib import Path

import pytest

from merced_ai.harnesses import default_registry
from merced_ai.harnesses.adapters.command import CommandHarnessAdapter, HarnessRunError
from merced_ai.harnesses.builtin import ACP_LAUNCHES
from merced_ai.harnesses.detection import locate_executable
from merced_ai.models import RunRequest
from merced_ai.profiles import create_profile, validate_profile

pytestmark = pytest.mark.skipif(
    os.environ.get("MERCED_AI_LIVE_SMOKE") != "1",
    reason="live harness smoke tests are opt-in: set MERCED_AI_LIVE_SMOKE=1 (makes model calls)",
)

NOUS_MODEL = "deepseek/deepseek-v4-flash"
SMOKE_PROMPT = "Reply with OK"
HELP_ARGS = {
    "codex": ("exec", "--help"),
    "goose": ("run", "--help"),
    "opencode": ("run", "--help"),
    "magagent": ("ask", "--help"),
    "loro": ("run", "--help"),
    "openclaw": ("agent", "--help"),
}
ALL_IDS = sorted(item.id for item in default_registry().descriptors())


def _selected() -> set[str]:
    raw = os.environ.get("MERCED_AI_LIVE_SMOKE_HARNESSES", "")
    return {item.strip() for item in raw.split(",") if item.strip()} or set(ALL_IDS)


def _adapter(harness_id: str) -> CommandHarnessAdapter:
    adapter = default_registry().get(harness_id)
    assert isinstance(adapter, CommandHarnessAdapter)
    if locate_executable(adapter.descriptor) is None:
        pytest.skip(f"{harness_id} is not installed")
    probe = adapter.probe()
    if probe.status.value == "probe_failed":
        pytest.skip(f"{harness_id} is installed but its version probe fails; repair it first")
    return adapter


def _request(adapter: CommandHarnessAdapter, workspace: Path) -> RunRequest:
    existing = workspace / ".agents" / "smoke.agent.yaml"
    if existing.exists():
        profile = validate_profile(existing, "project")
        return RunRequest(
            harness_id=adapter.descriptor.id,
            prompt=SMOKE_PROMPT,
            workspace=workspace,
            profile=profile,
            projection=adapter.project_profile(profile),
            timeout_seconds=240,
        )
    profile = create_profile(
        "smoke",
        "Answers a single connectivity check without using tools.",
        "Reply with exactly the word OK and nothing else. Do not use any tools.",
        workspace,
        edit_permission="deny",
        shell_permission="deny",
    )
    return RunRequest(
        harness_id=adapter.descriptor.id,
        prompt=SMOKE_PROMPT,
        workspace=workspace,
        profile=profile,
        projection=adapter.project_profile(profile),
        timeout_seconds=240,
    )


def _help_flags(text: str) -> set[str]:
    flags = set(re.findall(r"--[A-Za-z0-9][\w-]*", text))
    # Claude Code documents "--system-prompt[-file]" as one entry.
    for base, suffix in re.findall(r"(--[\w-]+)\[(-[\w-]+)\]", text):
        flags.update({base, base + suffix})
    return flags


@pytest.mark.parametrize("harness_id", ALL_IDS)
def test_adapter_flags_appear_in_harness_help(harness_id: str, workspace: Path) -> None:
    adapter = _adapter(harness_id)
    executable = locate_executable(adapter.descriptor)
    assert executable is not None
    invocation = adapter.build_invocation(_request(adapter, workspace), workspace)
    used = {item.split("=", 1)[0] for item in invocation.argv[1:] if item.startswith("--")}
    completed = subprocess.run(
        [str(executable), *HELP_ARGS.get(harness_id, ("--help",))],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=180,
        env={**os.environ, "COLUMNS": "300", "NO_COLOR": "1"},
        check=False,
    )
    documented = _help_flags(completed.stdout + completed.stderr)
    missing = sorted(used - documented)
    assert not missing, f"{harness_id} help does not document {missing}"


def _isolated_nous_home(
    adapter: CommandHarnessAdapter, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    harness_id = adapter.descriptor.id
    if not os.environ.get("NOUS_API_KEY"):
        pytest.skip("NOUS_API_KEY is not set")
    real_home = Path.home()
    home = tmp_path / "home"
    if harness_id == "loro":
        config = home / ".config" / "loro" / "config.toml"
        config.parent.mkdir(parents=True)
        config.write_text(
            f'[model]\nprovider = "nous"\nmodel = "{NOUS_MODEL}"\n'
            'api_key_env = "NOUS_API_KEY"\n',  # pragma: allowlist secret
            encoding="utf-8",
        )
    else:
        config = home / ".config" / "magent" / "config.toml"
        config.parent.mkdir(parents=True)
        config.write_text(
            f'[defaults]\nprovider = "nous-portal"\nmodel = "{NOUS_MODEL}"\n', encoding="utf-8"
        )
    # Keep pyenv shims working after HOME moves.
    monkeypatch.setenv("PYENV_ROOT", os.environ.get("PYENV_ROOT", str(real_home / ".pyenv")))
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(home / ".config"))
    monkeypatch.setenv("XDG_DATA_HOME", str(home / ".local" / "share"))
    monkeypatch.setenv("XDG_STATE_HOME", str(home / ".local" / "state"))
    monkeypatch.setenv("XDG_CACHE_HOME", str(home / ".cache"))
    if harness_id == "magagent":
        # MagAgent needs an active user profile; create a throwaway one in the isolated HOME.
        executable = locate_executable(adapter.descriptor)
        assert executable is not None
        subprocess.run(
            [str(executable), "user", "create", "smoke"],
            capture_output=True,
            check=True,
            timeout=120,
            env=dict(os.environ),
        )


SECRET_RE = re.compile(r"(sk|key|token)-[A-Za-z0-9_*.-]{4,}", re.IGNORECASE)


def _report(entry: dict[str, object]) -> None:
    target = os.environ.get("MERCED_AI_LIVE_SMOKE_REPORT")
    if target:
        # Provider errors can echo a masked key fragment; never persist any of it.
        line = SECRET_RE.sub("[redacted]", json.dumps(entry))
        with open(target, "a", encoding="utf-8") as handle:
            handle.write(line + "\n")


@pytest.mark.parametrize("harness_id", ALL_IDS)
def test_harness_answers_through_the_adapter(
    harness_id: str, workspace: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    if harness_id not in _selected():
        pytest.skip("not selected by MERCED_AI_LIVE_SMOKE_HARNESSES")
    adapter = _adapter(harness_id)
    probe = adapter.probe(workspace)
    if harness_id in {"loro", "magagent"}:
        if os.environ.get("MERCED_AI_LIVE_SMOKE_NOUS") != "1":
            pytest.skip("set MERCED_AI_LIVE_SMOKE_NOUS=1 to run Loro/MagAgent against Nous")
        _isolated_nous_home(adapter, tmp_path, monkeypatch)
    if harness_id == "gemini":
        # Gemini refuses headless runs in untrusted folders; the smoke workspace is throwaway.
        monkeypatch.setenv("GEMINI_CLI_TRUST_WORKSPACE", "true")
        if os.environ.get("MERCED_AI_LIVE_SMOKE_GEMINI_API_KEY") == "1":
            if not os.environ.get("GEMINI_API_KEY"):
                pytest.skip("GEMINI_API_KEY is not set")
            monkeypatch.setenv("HOME", str(tmp_path / "gemini-home"))
    request = _request(adapter, workspace)
    started = time.monotonic()
    entry: dict[str, object] = {
        "harness": harness_id,
        "version": (probe.version or "").splitlines()[0][:80],
        "prompt_delivery": adapter.descriptor.prompt_delivery.value,
        "model": NOUS_MODEL if harness_id in {"loro", "magagent"} else "harness default",
    }
    try:
        result = adapter.run(request)
    except HarnessRunError as error:
        entry.update(ok=False, error=str(error)[:300], seconds=round(time.monotonic() - started))
        _report(entry)
        raise
    reply = result.output.strip()
    # Exact match (ignoring case and punctuation), so an echoed prompt such as "Reply with OK"
    # cannot pass as an answer.
    ok = re.sub(r"[^A-Za-z]", "", reply).upper() == "OK"
    entry.update(ok=ok, reply=reply[:40], seconds=round(time.monotonic() - started))
    _report(entry)
    assert ok, reply[:200]


ACP_IDS = sorted(key for key, (_, verified) in ACP_LAUNCHES.items() if verified)


@pytest.mark.parametrize("harness_id", ACP_IDS)
def test_acp_streams_and_resumes_natively(
    harness_id: str, workspace: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Two turns over ACP: the second loads the first session and sends only the new message."""
    if harness_id not in _selected():
        pytest.skip("not selected by MERCED_AI_LIVE_SMOKE_HARNESSES")
    monkeypatch.setenv("MERCED_AI_ACP", "1")
    adapter = _adapter(harness_id)
    if not getattr(adapter, "acp_available", lambda: False)():
        pytest.skip(f"{harness_id} has no ACP launcher installed")
    if harness_id == "gemini":
        monkeypatch.setenv("GEMINI_CLI_TRUST_WORKSPACE", "true")
        if os.environ.get("MERCED_AI_LIVE_SMOKE_GEMINI_API_KEY") == "1":
            monkeypatch.setenv("HOME", str(tmp_path / "gemini-home"))
    deltas: list[str] = []
    first = adapter.run_cancellable(
        _request(adapter, workspace),
        None,
        on_event=lambda event: (
            deltas.append(event.get("text", "")) if event.get("type") == "assistant_delta" else None
        ),
    )
    second_request = _request(adapter, workspace).model_copy(
        update={"native_session_id": first.native_session_id, "turn_prompt": "Reply with OK again"}
    )
    second = adapter.run_cancellable(second_request, None)
    entry = {
        "harness": harness_id,
        "transport": "acp",
        "streamed_chunks": len(deltas),
        "first": first.output[:40],
        "second": second.output[:40],
        "resumed": bool(second.raw and second.raw.get("resumed")),
        "load_session": bool(second.raw and second.raw.get("load_session")),
    }
    _report(entry)
    assert re.sub(r"[^A-Za-z]", "", first.output).upper() == "OK"
    assert deltas, "no streamed chunks"
    assert "OK" in second.output.upper()
    # Native resume is claimed only for agents whose session/load works across processes.
    assert entry["resumed"] is ACP_LAUNCHES[harness_id][0].resumes
