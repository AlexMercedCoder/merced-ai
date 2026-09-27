from __future__ import annotations

import json
import sys
import threading
from pathlib import Path

import pytest
from aais import create_request, validate

from merced_ai.harnesses.adapters.command import (
    CommandHarnessAdapter,
    HarnessInvocation,
    HarnessRunError,
    _normalize_output,
    _stdin_payload,
    _subprocess_env,
)
from merced_ai.models import HarnessDescriptor, PromptDelivery, RunRequest, TransportKind
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


def _build(adapter: CommandHarnessAdapter, request: RunRequest) -> list[str]:
    scratch = request.workspace / ".scratch"
    scratch.mkdir(exist_ok=True)
    return adapter.build_invocation(request, scratch).argv


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
    scratch = workspace / ".scratch"
    scratch.mkdir()

    invocation = _adapter(harness_id).build_invocation(request, scratch)

    assert invocation.argv[0] == str(executable)
    files = "".join(path.read_text(encoding="utf-8") for path in scratch.iterdir())
    if invocation.prompt_delivery is PromptDelivery.ARGV:
        assert "Review README.md" in " ".join(invocation.argv)
        assert invocation.stdin is None
    elif invocation.prompt_delivery is PromptDelivery.STDIN:
        assert "Review README.md" in (invocation.stdin or "")
        assert "Review README.md" not in " ".join(invocation.argv)
    else:
        assert "Review README.md" in files
        assert "Review README.md" not in " ".join(invocation.argv)
    # The profile reaches the harness natively by name or as instructions on some channel.
    everything = " ".join(invocation.argv) + (invocation.stdin or "") + files
    if harness_id in {"magagent", "loro"}:
        assert invocation.argv[invocation.argv.index("--agent") + 1] == "reviewer"
    else:
        assert "Review code and report defects." in everything


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

    from merced_ai.harnesses.process import ChildResult

    observed = {}

    def capture(command, **kwargs):
        observed.update(kwargs)
        observed["command"] = command
        return ChildResult(
            '{"result":"Reviewed successfully","session_id":"native-1"}', "", 0, False
        )

    monkeypatch.setattr("merced_ai.harnesses.adapters.command.run_child", capture)
    result = _adapter("claude").run(_request("claude", workspace))
    assert result.output == "Reviewed successfully"
    assert result.native_session_id == "native-1"
    assert observed["workspace"] == workspace
    assert observed["command"][0] == str(executable)


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
    child = workspace / "magagent_child.py"
    child.write_text(
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
    adapter = _adapter("magagent")
    monkeypatch.setattr(
        adapter,
        "build_invocation",
        lambda _request, _scratch: HarnessInvocation(
            [sys.executable, str(child)], PromptDelivery.ARGV
        ),
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

    result = adapter.run_cancellable(_request("magagent", workspace), None, approve)

    assert result.output == "approved child completed"
    assert observed[0]["request"]["action_digest"] == requested["request"]["action_digest"]


def test_child_environment_does_not_inherit_parent_coverage_hooks(
    workspace: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("COVERAGE_PROCESS_START", "pyproject.toml")
    monkeypatch.setenv("COV_CORE_SOURCE", "merced_ai")
    monkeypatch.setenv("COV_CORE_DATAFILE", ".coverage")

    env = _subprocess_env("magagent", _request("magagent", workspace))

    assert "COVERAGE_PROCESS_START" not in env
    assert not any(key.startswith("COV_CORE_") for key in env)


def test_command_adapter_contains_failure_output(
    workspace: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    executable = workspace / "gemini"
    executable.touch()
    monkeypatch.setattr(
        "merced_ai.harnesses.adapters.command.locate_executable", lambda _descriptor: executable
    )

    from merced_ai.harnesses.process import ChildResult

    def capture(*args, **kwargs):
        def output():
            return "", "authentication required\n"

        stdout, stderr = output()
        return ChildResult(stdout, stderr, 7, False)

    monkeypatch.setattr("merced_ai.harnesses.adapters.command.run_child", capture)

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

    from merced_ai.harnesses.process import ChildResult

    def capture(*args, **kwargs):
        def output():
            return (
                '{"type":"message_end","message":{"role":"user",'
                '"content":[{"type":"text","text":"hello"}]}}\n'
                '{"type":"message_end","message":{"role":"assistant",'
                '"content":[],"stopReason":"error","errorMessage":"fetch failed"}}',
                "",
            )

        stdout, stderr = output()
        return ChildResult(stdout, stderr, 0, False)

    monkeypatch.setattr("merced_ai.harnesses.adapters.command.run_child", capture)

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

    from merced_ai.harnesses.process import ChildResult

    def capture(*args, **kwargs):
        def output():
            return (
                '{"message":{"role":"user","content":[{"text":"hello"}]}}\n'
                '{"message":{"role":"assistant","content":[{"text":"done"}]}}',
                "",
            )

        stdout, stderr = output()
        return ChildResult(stdout, stderr, 0, False)

    monkeypatch.setattr("merced_ai.harnesses.adapters.command.run_child", capture)

    assert _adapter("pi").run(_request("pi", workspace)).output == "done"


def test_command_adapter_honors_external_cancellation(workspace, monkeypatch):
    adapter = _adapter("codex")
    monkeypatch.setattr(
        adapter,
        "build_invocation",
        lambda request, scratch: HarnessInvocation(
            [sys.executable, "-c", "import time; time.sleep(60)"], PromptDelivery.ARGV
        ),
    )
    cancellation = threading.Event()
    cancellation.set()
    with pytest.raises(HarnessRunError, match="cancelled") as error:
        adapter.run_cancellable(_request("codex", workspace), cancellation)
    assert error.value.exit_code == 130


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

    command = _build(adapter, request)

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

    command = _build(adapter, request)

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

    command = _build(_adapter("openclaw"), _request("openclaw", workspace))

    assert command[1:6] == ["agent", "--local", "--agent", "main", "--json"]
    assert "exec" not in command
    message_file = Path(command[command.index("--message-file") + 1])
    assert "Review README.md" in message_file.read_text(encoding="utf-8")
    assert "--message" not in command


def test_agy_passes_prompt_as_flag_value(workspace: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    executable = workspace / "agy"
    executable.touch()
    monkeypatch.setattr(
        "merced_ai.harnesses.adapters.command.locate_executable", lambda _descriptor: executable
    )

    command = _build(_adapter("agy"), _request("agy", workspace))

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

    command = _build(_adapter("kimi"), _request("kimi", workspace))

    assert command[command.index("--config-file") + 1] == str(config_file)


def test_goose_transcript_returns_the_assistant_reply_not_the_prompt() -> None:
    # Shape observed from `goose run --output-format json` 1.48 in the M-5 live smoke run.
    stdout = json.dumps(
        {
            "messages": [
                {"role": "user", "content": [{"type": "text", "text": "Reply with OK"}]},
                {
                    "role": "assistant",
                    "content": [
                        {"type": "thinking", "thinking": "The user wants OK."},
                        {"type": "text", "text": "OK"},
                    ],
                },
            ],
            "metadata": {"status": "completed"},
        }
    )

    output, raw, _ = _normalize_output("goose", stdout)

    assert output == "OK"
    assert raw is not None


def test_opencode_json_parts_return_the_final_message_text() -> None:
    # Shape observed from `opencode run --format json` 1.18 in the M-5 live smoke run.
    events = [
        {"type": "step_start", "part": {"type": "step-start", "messageID": "m1"}},
        {"type": "text", "part": {"type": "text", "messageID": "m1", "text": "Looking."}},
        {"type": "tool_use", "part": {"type": "tool", "messageID": "m1"}},
        {"type": "text", "part": {"type": "text", "messageID": "m2", "text": "First part."}},
        {"type": "text", "part": {"type": "text", "messageID": "m2", "text": "OK"}},
        {"type": "step_finish", "part": {"type": "step-finish", "messageID": "m2"}},
    ]
    stdout = "\n".join(json.dumps(item) for item in events)

    output, raw, _ = _normalize_output("opencode", stdout)

    assert output == "First part.\n\nOK"
    assert raw is not None and len(raw["events"]) == len(events)


def test_json_document_after_status_lines_is_parsed() -> None:
    # MagAgent 1.3 prints a status line before its pretty-printed --json document.
    stdout = "Loaded 3 skills\n" + json.dumps(
        {"ok": True, "response": "OK", "session_id": "s-1"}, indent=2
    )

    output, raw, session_id = _normalize_output("magagent", stdout)

    assert output == "OK"
    assert raw is not None and raw["ok"] is True
    assert session_id == "s-1"


LORO_SUMMARY = (
    "Loro run mode completed.\n\nProvider: nous / deepseek/deepseek-v4-flash\n\n"
    "Stop reason: {stop}\nSteps: 1\n\nPrompt: Reply with OK\n\nModel response: {response}\n\n"
    "Run d2005c43-3414-4806-a619-6cc5d4aadb94 (export: loro run export "
    "d2005c43-3414-4806-a619-6cc5d4aadb94 --out run.zip)\n"
)


def test_loro_summary_returns_only_the_model_response() -> None:
    output, raw, _ = _normalize_output(
        "loro", LORO_SUMMARY.format(stop="completed", response="OK\nsecond line")
    )

    assert output == "OK\nsecond line"
    assert raw == {
        "stop_reason": "completed",
        "provider": "nous / deepseek/deepseek-v4-flash",
        "run_id": "d2005c43-3414-4806-a619-6cc5d4aadb94",
    }


def test_loro_provider_error_is_a_failed_run(workspace, monkeypatch) -> None:
    from merced_ai.harnesses.process import ChildResult

    executable = workspace / "loro"
    executable.touch()
    monkeypatch.setattr(
        "merced_ai.harnesses.adapters.command.locate_executable", lambda _descriptor: executable
    )
    summary = LORO_SUMMARY.format(
        stop="provider_error", response="Provider error: nous returned HTTP 401"
    )
    monkeypatch.setattr(
        "merced_ai.harnesses.adapters.command.run_child",
        lambda *_args, **_kwargs: ChildResult(summary, "", 0, False),
    )

    with pytest.raises(HarnessRunError, match="HTTP 401"):
        _adapter("loro").run(_request("loro", workspace))


def test_magagent_events_document_returns_the_response() -> None:
    stdout = "Loaded 3 skills\n" + json.dumps(
        {
            "ok": True,
            "events": [
                {"type": "user_message", "content": "Reply with OK"},
                {"type": "assistant_message", "content": "OK"},
            ],
        },
        indent=2,
    )

    assert _normalize_output("magagent", stdout)[0] == "OK"
