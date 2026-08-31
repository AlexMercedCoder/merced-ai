from __future__ import annotations

import json
import subprocess
import threading
from pathlib import Path

import pytest
from aais import create_request, validate

from merced_ai.harnesses.adapters.command import (
    CommandHarnessAdapter,
    HarnessRunError,
    _normalize_output,
    _stdin_payload,
)
from merced_ai.models import HarnessDescriptor, RunRequest, TransportKind
from merced_ai.profiles import create_profile


def _adapter(harness_id: str) -> CommandHarnessAdapter:
    return CommandHarnessAdapter(
        HarnessDescriptor(
            id=harness_id,
            name=harness_id.title(),
            executable_names=(harness_id,),
            transports=(TransportKind.STRUCTURED_SUBPROCESS,),
        )
    )


def _request(harness_id: str, workspace: Path) -> RunRequest:
    profile = create_profile(
        "reviewer",
        "Reviews code for correctness without modifying the workspace.",
        "Review code and report defects.",
        workspace,
    )
    adapter = _adapter(harness_id)
    return RunRequest(
        harness_id=harness_id,
        prompt="Review README.md",
        workspace=workspace,
        profile=profile,
        projection=adapter.project_profile(profile),
    )


@pytest.mark.parametrize(
    "harness_id",
    [
        "codex",
        "claude",
        "gemini",
        "magagent",
        "loro",
        "opencode",
        "goose",
        "dsh",
        "agy",
        "pi",
        "prime-agent",
        "openclaw",
        "kimi",
        "anton",
    ],
)
def test_qualified_adapter_builds_argv_without_shell_text(
    harness_id: str, workspace: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    executable = workspace / harness_id
    executable.touch()
    monkeypatch.setattr(
        "merced_ai.harnesses.adapters.command.locate_executable", lambda _descriptor: executable
    )
    request = _request(harness_id, workspace)

    command = _adapter(harness_id).build_command(request)

    assert command[0] == str(executable)
    invocation = " ".join(command)
    if harness_id == "anton":
        invocation += _stdin_payload(harness_id, request) or ""
    assert "Review README.md" in invocation


def test_anton_repl_receives_profile_and_request_as_one_turn(workspace: Path) -> None:
    request = _request("anton", workspace)

    payload = _stdin_payload("anton", request)

    assert payload is not None
    assert payload.endswith("\nexit\n")
    assert payload.count("\n") == 2
    assert "<open-agent-profile>" in payload.splitlines()[0]
    assert "Review README.md" in payload.splitlines()[0]


def test_anton_output_extracts_last_assistant_turn() -> None:
    stdout = (
        "\x1b[1manton>\x1b[0m Welcome\n"
        "you> atomic request\n"
        "anton> First line\nsecond line\n"
        "you> exit\nSee you."
    )

    output, raw, session_id = _normalize_output("anton", stdout)

    assert output == "First line\nsecond line"
    assert raw is None
    assert session_id is None


def test_command_adapter_normalizes_json_response(
    workspace: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    executable = workspace / "claude"
    executable.touch()
    monkeypatch.setattr(
        "merced_ai.harnesses.adapters.command.locate_executable", lambda _descriptor: executable
    )

    class Process:
        returncode = 0

        def communicate(
            self, input: str | None = None, timeout: int | None = None
        ) -> tuple[str, str]:
            return '{"result":"Reviewed successfully","session_id":"native-1"}', ""

    observed: dict[str, object] = {}

    def fake_popen(command: list[str], **kwargs: object) -> Process:
        observed["command"] = command
        observed.update(kwargs)
        return Process()

    monkeypatch.setattr("merced_ai.harnesses.adapters.command.subprocess.Popen", fake_popen)

    result = _adapter("claude").run(_request("claude", workspace))

    assert result.output == "Reviewed successfully"
    assert result.native_session_id == "native-1"
    assert observed["shell"] is False
    assert observed["stdin"] is not None


def test_aais_child_request_is_decided_over_its_stdin(
    workspace: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    requested = create_request(
        action={
            "kind": "tool.call",
            "name": "shell.exec",
            "summary": "Check syntax",
            "arguments": {"command": "node --check app.js"},
        },
        origin={"harness": "magagent", "session_id": "session-1"},
        risk={"level": "medium", "reasons": ["Runs a local process"]},
        choices=[
            {"decision": "approve", "scope": "once", "label": "Allow once"},
            {"decision": "deny", "scope": "once", "label": "Deny"},
        ],
        sequence=1,
        stream="child",
    )
    executable = workspace / "magagent"
    executable.write_text(
        "#!/usr/bin/env python3\n"
        "import json, sys\n"
        f"print({json.dumps(json.dumps(requested))}, flush=True)\n"
        "decision = json.loads(sys.stdin.readline())\n"
        "assert decision['type'] == 'approval.decided'\n"
        "assert decision['decision']['request_id'] == "
        f"{json.dumps(requested['request']['id'])}\n"
        "print(json.dumps({'response': 'approved child completed'}), flush=True)\n",
        encoding="utf-8",
    )
    executable.chmod(0o755)
    monkeypatch.setattr(
        "merced_ai.harnesses.adapters.command.locate_executable", lambda _descriptor: executable
    )
    observed: list[dict] = []

    def approve(envelope: dict, _cancellation: threading.Event | None) -> dict:
        observed.append(validate(envelope))
        from aais import create_decision

        return create_decision(
            envelope,
            decision="approve",
            scope="once",
            actor={
                "id": "test-user",
                "type": "human",
                "authenticated_by": "test",
            },
            sequence=1,
            stream="test-presenter",
        )

    result = _adapter("magagent").run_cancellable(_request("magagent", workspace), None, approve)

    assert result.output == "approved child completed"
    assert observed[0]["request"]["action_digest"] == requested["request"]["action_digest"]


def test_command_adapter_contains_failure_output(
    workspace: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    executable = workspace / "gemini"
    executable.touch()
    monkeypatch.setattr(
        "merced_ai.harnesses.adapters.command.locate_executable", lambda _descriptor: executable
    )

    class Process:
        returncode = 7

        def communicate(
            self, input: str | None = None, timeout: int | None = None
        ) -> tuple[str, str]:
            return "", "authentication required\n"

    monkeypatch.setattr(
        "merced_ai.harnesses.adapters.command.subprocess.Popen", lambda *_args, **_kwargs: Process()
    )

    with pytest.raises(HarnessRunError, match="authentication required") as error:
        _adapter("gemini").run(_request("gemini", workspace))
    assert error.value.exit_code == 7


def test_command_adapter_rejects_embedded_jsonl_error(
    workspace: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    executable = workspace / "pi"
    executable.touch()
    monkeypatch.setattr(
        "merced_ai.harnesses.adapters.command.locate_executable", lambda _descriptor: executable
    )

    class Process:
        returncode = 0

        def communicate(
            self, input: str | None = None, timeout: int | None = None
        ) -> tuple[str, str]:
            return (
                '{"type":"message_end","message":{"role":"user",'
                '"content":[{"type":"text","text":"hello"}]}}\n'
                '{"type":"message_end","message":{"role":"assistant",'
                '"content":[],"stopReason":"error","errorMessage":"fetch failed"}}',
                "",
            )

    monkeypatch.setattr(
        "merced_ai.harnesses.adapters.command.subprocess.Popen", lambda *_args, **_kwargs: Process()
    )

    with pytest.raises(HarnessRunError, match="fetch failed"):
        _adapter("pi").run(_request("pi", workspace))


def test_command_adapter_extracts_last_assistant_jsonl_message(
    workspace: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    executable = workspace / "pi"
    executable.touch()
    monkeypatch.setattr(
        "merced_ai.harnesses.adapters.command.locate_executable", lambda _descriptor: executable
    )

    class Process:
        returncode = 0

        def communicate(
            self, input: str | None = None, timeout: int | None = None
        ) -> tuple[str, str]:
            return (
                '{"message":{"role":"user","content":[{"text":"hello"}]}}\n'
                '{"message":{"role":"assistant","content":[{"text":"done"}]}}',
                "",
            )

    monkeypatch.setattr(
        "merced_ai.harnesses.adapters.command.subprocess.Popen", lambda *_args, **_kwargs: Process()
    )

    assert _adapter("pi").run(_request("pi", workspace)).output == "done"


def test_command_adapter_terminates_cancelled_child(
    workspace: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    executable = workspace / "codex"
    executable.touch()
    monkeypatch.setattr(
        "merced_ai.harnesses.adapters.command.locate_executable", lambda _descriptor: executable
    )

    class Process:
        returncode = None
        terminated = False

        def communicate(
            self, input: str | None = None, timeout: int | None = None
        ) -> tuple[str, str]:
            raise KeyboardInterrupt

        def terminate(self) -> None:
            self.terminated = True

        def wait(self, timeout: int | None = None) -> int:
            self.returncode = 130
            return 130

    process = Process()
    monkeypatch.setattr(
        "merced_ai.harnesses.adapters.command.subprocess.Popen", lambda *_args, **_kwargs: process
    )

    with pytest.raises(HarnessRunError, match="cancelled") as error:
        _adapter("codex").run(_request("codex", workspace))
    assert error.value.exit_code == 130
    assert process.terminated is True


def test_command_adapter_honors_external_cancellation(
    workspace: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    executable = workspace / "codex"
    executable.touch()
    monkeypatch.setattr(
        "merced_ai.harnesses.adapters.command.locate_executable", lambda _descriptor: executable
    )

    class Process:
        returncode = None
        terminated = False

        def communicate(
            self, input: str | None = None, timeout: float | None = None
        ) -> tuple[str, str]:
            raise subprocess.TimeoutExpired("codex", timeout or 0)

        def terminate(self) -> None:
            self.terminated = True

        def wait(self, timeout: int | None = None) -> int:
            self.returncode = 130
            return 130

    process = Process()
    monkeypatch.setattr(
        "merced_ai.harnesses.adapters.command.subprocess.Popen", lambda *_args, **_kwargs: process
    )
    cancellation = threading.Event()
    cancellation.set()

    with pytest.raises(HarnessRunError, match="cancelled") as error:
        _adapter("codex").run_cancellable(_request("codex", workspace), cancellation)

    assert error.value.exit_code == 130
    assert process.terminated is True


def test_projection_does_not_send_anthropic_model_to_codex(workspace: Path) -> None:
    profile = create_profile(
        "reviewer",
        "Reviews code for correctness without modifying the workspace.",
        "Review code and report defects.",
        workspace,
    )
    profile.document["spec"]["model"] = {"provider": "anthropic", "id": "claude-sonnet"}

    projection = _adapter("codex").project_profile(profile)

    assert projection.model is None
    assert any(item.field == "spec.model" for item in projection.adjustments)


@pytest.mark.parametrize("harness_id", ["opencode", "pi", "prime-agent"])
def test_multi_provider_adapters_qualify_model_id(
    harness_id: str, workspace: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    executable = workspace / harness_id
    executable.touch()
    monkeypatch.setattr(
        "merced_ai.harnesses.adapters.command.locate_executable", lambda _descriptor: executable
    )
    request = _request(harness_id, workspace)
    request.profile.document["spec"]["model"] = {
        "provider": "google",
        "id": "gemini-2.5-flash",
    }
    adapter = _adapter(harness_id)
    request.projection = adapter.project_profile(request.profile)

    command = adapter.build_command(request)

    assert "google/gemini-2.5-flash" in command


def test_goose_maps_provider_and_model_separately(
    workspace: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    executable = workspace / "goose"
    executable.touch()
    monkeypatch.setattr(
        "merced_ai.harnesses.adapters.command.locate_executable", lambda _descriptor: executable
    )
    request = _request("goose", workspace)
    request.profile.document["spec"]["model"] = {
        "provider": "google",
        "id": "gemini-2.5-flash",
    }
    adapter = _adapter("goose")
    request.projection = adapter.project_profile(request.profile)

    command = adapter.build_command(request)

    assert command[command.index("--provider") + 1] == "google"
    assert command[command.index("--model") + 1] == "gemini-2.5-flash"


def test_openclaw_uses_current_local_agent_interface(
    workspace: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    executable = workspace / "openclaw"
    executable.touch()
    monkeypatch.setattr(
        "merced_ai.harnesses.adapters.command.locate_executable", lambda _descriptor: executable
    )

    command = _adapter("openclaw").build_command(_request("openclaw", workspace))

    assert command[1:6] == ["agent", "--local", "--agent", "main", "--json"]
    assert "exec" not in command
    assert "--message" in command


def test_agy_passes_prompt_as_flag_value(workspace: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    executable = workspace / "agy"
    executable.touch()
    monkeypatch.setattr(
        "merced_ai.harnesses.adapters.command.locate_executable", lambda _descriptor: executable
    )

    command = _adapter("agy").build_command(_request("agy", workspace))

    assert any(value.startswith("--print=") for value in command)
    assert command[-1] != "Review README.md"


def test_kimi_accepts_merced_config_file_override(
    workspace: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    executable = workspace / "kimi"
    executable.touch()
    config_file = workspace / "kimi.toml"
    monkeypatch.setenv("MERCED_AI_KIMI_CONFIG_FILE", str(config_file))
    monkeypatch.setattr(
        "merced_ai.harnesses.adapters.command.locate_executable", lambda _descriptor: executable
    )

    command = _adapter("kimi").build_command(_request("kimi", workspace))

    assert command[command.index("--config-file") + 1] == str(config_file)
