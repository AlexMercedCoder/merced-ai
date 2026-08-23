from __future__ import annotations

from pathlib import Path

import pytest

from merced_ai.harnesses.adapters.command import CommandHarnessAdapter, HarnessRunError
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


@pytest.mark.parametrize("harness_id", ["codex", "claude", "gemini", "magagent", "loro"])
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
    assert "Review README.md" in " ".join(command)


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

        def communicate(self, timeout: int | None = None) -> tuple[str, str]:
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

        def communicate(self, timeout: int | None = None) -> tuple[str, str]:
            return "", "authentication required\n"

    monkeypatch.setattr(
        "merced_ai.harnesses.adapters.command.subprocess.Popen", lambda *_args, **_kwargs: Process()
    )

    with pytest.raises(HarnessRunError, match="authentication required") as error:
        _adapter("gemini").run(_request("gemini", workspace))
    assert error.value.exit_code == 7


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

        def communicate(self, timeout: int | None = None) -> tuple[str, str]:
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
