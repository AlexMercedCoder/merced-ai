"""Qualified noninteractive adapters for the first MVP harness set."""

from __future__ import annotations

import json
import os
import re
import subprocess
import threading
import time
from collections.abc import Callable
from typing import Any

from aais import create_decision, validate

from merced_ai.harnesses.detection import locate_executable, probe_executable
from merced_ai.models import (
    HarnessDescriptor,
    HarnessProbe,
    ProfileProjection,
    ProfileRecord,
    ProjectionAdjustment,
    RunRequest,
    RunResult,
)
from merced_ai.profiles import assemble_system_prompt

MAX_CAPTURE_CHARS = 10_000_000
ANSI_ESCAPE_RE = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]")


class HarnessRunError(RuntimeError):
    def __init__(self, message: str, *, exit_code: int = 1, stderr: str = "") -> None:
        super().__init__(message)
        self.exit_code = exit_code
        self.stderr = stderr


class CommandHarnessAdapter:
    def __init__(self, descriptor: HarnessDescriptor) -> None:
        self._descriptor = descriptor

    @property
    def descriptor(self) -> HarnessDescriptor:
        return self._descriptor

    def probe(self) -> HarnessProbe:
        return probe_executable(self.descriptor)

    def project_profile(self, profile: ProfileRecord) -> ProfileProjection:
        harness_id = self.descriptor.id
        prompt = assemble_system_prompt(profile)
        model, model_adjustment = _projected_model(profile, harness_id)
        if harness_id in {"magagent", "loro"} and _native_profile_visible(profile):
            native_model = profile.document.get("spec", {}).get("model", {}).get("id")
            return ProfileProjection(
                harness_id=harness_id,
                support_level="native",
                system_prompt=prompt,
                model=native_model,
                adjustments=(
                    ProjectionAdjustment(
                        field="profile",
                        action="mapped",
                        reason="The harness receives the discovered OAP profile name natively.",
                    ),
                ),
            )
        if harness_id in {"claude", "goose", "pi", "prime-agent"}:
            adjustments = [
                ProjectionAdjustment(
                    field="spec.role",
                    action="mapped",
                    reason=(
                        "Role and bounded state are passed through a harness system-prompt flag."
                    ),
                ),
                ProjectionAdjustment(
                    field="spec.permissions",
                    action="narrowed",
                    reason="Requested permissions map to Claude's coarse permission modes.",
                ),
            ]
            if model_adjustment:
                adjustments.append(model_adjustment)
            return ProfileProjection(
                harness_id=harness_id,
                support_level="projected",
                system_prompt=prompt,
                model=model,
                adjustments=tuple(adjustments),
            )
        adjustments = [
            ProjectionAdjustment(
                field="spec.role",
                action="substituted",
                reason="This adapter injects the profile as delimited prompt context.",
            ),
            ProjectionAdjustment(
                field="spec.permissions",
                action="narrowed",
                reason="Only supported coarse sandbox controls are mapped; harness policy wins.",
            ),
        ]
        if model_adjustment:
            adjustments.append(model_adjustment)
        return ProfileProjection(
            harness_id=harness_id,
            support_level="degraded",
            system_prompt=prompt,
            model=model,
            adjustments=tuple(adjustments),
        )

    def build_command(self, request: RunRequest) -> list[str]:
        executable = locate_executable(self.descriptor)
        if executable is None:
            raise HarnessRunError(f"Harness {self.descriptor.id!r} is not installed.", exit_code=3)
        profile = request.profile
        projection = request.projection
        prompt = request.prompt
        harness_id = self.descriptor.id
        permissions = profile.document.get("spec", {}).get("permissions", {})
        edit_denied = permissions.get("edit") == "deny"
        shell_denied = permissions.get("shell") == "deny"

        if harness_id == "codex":
            sandbox = "read-only" if edit_denied else "workspace-write"
            command = [
                str(executable),
                "exec",
                "--color",
                "never",
                "--skip-git-repo-check",
                "-C",
                str(request.workspace),
                "-s",
                sandbox,
            ]
            if projection.model:
                command.extend(("--model", projection.model))
            command.append(_prefixed_prompt(projection.system_prompt, prompt))
            return command
        if harness_id == "claude":
            mode = "plan" if edit_denied or shell_denied else "manual"
            command = [
                str(executable),
                "--print",
                "--output-format",
                "json",
                "--permission-mode",
                mode,
                "--system-prompt",
                projection.system_prompt,
            ]
            if projection.model:
                command.extend(("--model", projection.model))
            command.append(prompt)
            return command
        if harness_id == "gemini":
            command = [str(executable), "--output-format", "json", "--approval-mode", "default"]
            if projection.model:
                command.extend(("--model", projection.model))
            command.append(_prefixed_prompt(projection.system_prompt, prompt))
            return command
        if harness_id == "magagent":
            mode = "paranoid" if edit_denied or shell_denied else "balanced"
            command = [
                str(executable),
                "ask",
                prompt
                if _native_profile_visible(profile)
                else _prefixed_prompt(projection.system_prompt, prompt),
                "--project",
                str(request.workspace),
                "--permission-mode",
                mode,
                "--json",
                "--events",
                "--approval-stdio",
            ]
            if _native_profile_visible(profile):
                command.extend(("--agent", profile.name))
            return command
        if harness_id == "loro":
            command = [
                str(executable),
                "run",
                prompt
                if _native_profile_visible(profile)
                else _prefixed_prompt(projection.system_prompt, prompt),
            ]
            if _native_profile_visible(profile):
                command.extend(("--agent", profile.name))
            command.append("--approval-stdio")
            return command
        if harness_id == "opencode":
            command = [
                str(executable),
                "run",
                "--format",
                "json",
                "--dir",
                str(request.workspace),
            ]
            if projection.model:
                command.extend(("--model", _qualified_model(profile, projection.model)))
            command.append(_prefixed_prompt(projection.system_prompt, prompt))
            return command
        if harness_id == "goose":
            command = [
                str(executable),
                "run",
                "--text",
                prompt,
                "--system",
                projection.system_prompt,
                "--quiet",
                "--output-format",
                "json",
                "--no-session",
            ]
            provider = _profile_provider(profile)
            if provider:
                command.extend(("--provider", provider))
            if projection.model:
                command.extend(("--model", projection.model))
            if edit_denied and shell_denied:
                command.append("--no-profile")
            return command
        if harness_id == "dsh":
            return [
                str(executable),
                "--profile",
                "headless",
                _prefixed_prompt(projection.system_prompt, prompt),
            ]
        if harness_id == "agy":
            qualified_prompt = _prefixed_prompt(projection.system_prompt, prompt)
            command = [
                str(executable),
                f"--print={qualified_prompt}",
                "--output-format",
                "json",
                "--disable-slash-commands",
            ]
            if edit_denied or shell_denied:
                command.extend(("--mode", "plan"))
            if projection.model:
                command.extend(("--model", projection.model))
            return command
        if harness_id in {"pi", "prime-agent"}:
            command = [str(executable), "--print", "--mode", "json", "--no-session"]
            if harness_id == "prime-agent":
                command.extend(("--cwd", str(request.workspace)))
            command.extend(("--append-system-prompt", projection.system_prompt))
            if harness_id == "prime-agent" and (edit_denied or shell_denied):
                command.append("--no-tools")
            else:
                excluded = []
                if edit_denied:
                    excluded.extend(("edit", "write"))
                if shell_denied:
                    excluded.append("bash")
                if excluded:
                    command.extend(("--exclude-tools", ",".join(excluded)))
            if projection.model:
                command.extend(("--model", _qualified_model(profile, projection.model)))
            command.append(prompt)
            return command
        if harness_id == "openclaw":
            command = [
                str(executable),
                "agent",
                "--local",
                "--agent",
                "main",
                "--json",
                "--message",
                _prefixed_prompt(projection.system_prompt, prompt),
            ]
            if projection.model:
                command.extend(("--model", _qualified_model(profile, projection.model)))
            return command
        if harness_id == "kimi":
            command = [
                str(executable),
                "--quiet",
                "--plan",
                "--work-dir",
                str(request.workspace),
            ]
            if config_file := os.environ.get("MERCED_AI_KIMI_CONFIG_FILE"):
                command.extend(("--config-file", config_file))
            if projection.model:
                command.extend(("--model", projection.model))
            command.extend(("--prompt", _prefixed_prompt(projection.system_prompt, prompt)))
            return command
        if harness_id == "anton":
            return [
                str(executable),
                "--folder",
                str(request.workspace),
                "--no-update",
            ]
        raise HarnessRunError(f"Harness {harness_id!r} is not executable in this MVP.", exit_code=4)

    def run(self, request: RunRequest) -> RunResult:
        return self.run_cancellable(request, None)

    def run_cancellable(
        self,
        request: RunRequest,
        cancellation: threading.Event | None,
        approval_handler: (
            Callable[[dict[str, Any], threading.Event | None], dict[str, Any]] | None
        ) = None,
    ) -> RunResult:
        command = self.build_command(request)
        if self.descriptor.id in {"magagent", "loro"}:
            return self._run_aais(command, request, cancellation, approval_handler)
        started = time.monotonic()
        process: subprocess.Popen[str] | None = None
        try:
            process = subprocess.Popen(  # noqa: S603 - argv is built by a trusted adapter
                command,
                cwd=request.workspace,
                env=_subprocess_env(self.descriptor.id, request),
                stdin=(subprocess.PIPE if self.descriptor.id == "anton" else subprocess.DEVNULL),
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                shell=False,
                text=True,
            )
            stdin_payload = _stdin_payload(self.descriptor.id, request)
            deadline = started + request.timeout_seconds
            first_poll = True
            while True:
                if cancellation is not None and cancellation.is_set():
                    _stop_process(process)
                    raise HarnessRunError(
                        f"Harness {self.descriptor.id!r} was cancelled.", exit_code=130
                    )
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    _stop_process(process)
                    raise HarnessRunError(
                        f"Harness {self.descriptor.id!r} timed out after "
                        f"{request.timeout_seconds}s.",
                        exit_code=5,
                    )
                try:
                    stdout, stderr = process.communicate(
                        input=stdin_payload if first_poll else None,
                        timeout=min(0.2, remaining),
                    )
                    break
                except subprocess.TimeoutExpired:
                    first_poll = False
        except KeyboardInterrupt as exc:
            if process is not None:
                _stop_process(process)
            raise HarnessRunError(
                f"Harness {self.descriptor.id!r} was cancelled.", exit_code=130
            ) from exc
        except OSError as exc:
            raise HarnessRunError(
                f"Harness {self.descriptor.id!r} could not start: {type(exc).__name__}.",
                exit_code=5,
            ) from exc

        assert process is not None
        stdout = stdout[:MAX_CAPTURE_CHARS]
        stderr = stderr[:MAX_CAPTURE_CHARS]
        if process.returncode != 0:
            summary = _last_nonempty_line(stderr) or _last_nonempty_line(stdout) or "unknown error"
            raise HarnessRunError(
                f"Harness {self.descriptor.id!r} failed: {summary}",
                exit_code=process.returncode,
                stderr=stderr,
            )
        output, raw, native_session_id = _normalize_output(self.descriptor.id, stdout)
        embedded_error = _find_error(raw) if raw else None
        if not output and embedded_error:
            raise HarnessRunError(
                f"Harness {self.descriptor.id!r} failed: {embedded_error}",
                exit_code=1,
                stderr=stderr,
            )
        return RunResult(
            harness_id=self.descriptor.id,
            output=output,
            exit_code=process.returncode,
            raw=raw,
            native_session_id=native_session_id,
            duration_ms=round((time.monotonic() - started) * 1000),
        )

    def _run_aais(
        self,
        command: list[str],
        request: RunRequest,
        cancellation: threading.Event | None,
        approval_handler: Callable[[dict[str, Any], threading.Event | None], dict[str, Any]] | None,
    ) -> RunResult:
        """Run an AAIS-aware child with independent read and write channels."""
        started = time.monotonic()
        try:
            process = subprocess.Popen(  # noqa: S603 - trusted adapter argv
                command,
                cwd=request.workspace,
                env=_subprocess_env(self.descriptor.id, request),
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                shell=False,
                text=True,
                bufsize=1,
            )
        except OSError as exc:
            raise HarnessRunError(
                f"Harness {self.descriptor.id!r} could not start: {type(exc).__name__}.",
                exit_code=5,
            ) from exc
        stdout_lines: list[str] = []
        stderr_lines: list[str] = []
        io_lock = threading.Lock()
        approval_cancelled = threading.Event()

        def read_stdout() -> None:
            assert process.stdout is not None
            for line in process.stdout:
                text = line.rstrip("\r\n")
                try:
                    value = json.loads(text)
                except json.JSONDecodeError:
                    value = None
                if isinstance(value, dict) and value.get("type") == "approval.requested":
                    requested = validate(value)
                    try:
                        if approval_handler is None:
                            raise RuntimeError("no AAIS presenter is attached")
                        decided = approval_handler(requested, approval_cancelled)
                    except Exception as error:
                        with io_lock:
                            stderr_lines.append(f"AAIS presenter denied request: {error}")
                        decided = create_decision(
                            requested,
                            decision="deny",
                            scope="once",
                            actor={
                                "id": "merced-ai.no-presenter",
                                "type": "policy",
                                "authenticated_by": "adapter",
                            },
                            sequence=1,
                            stream="merced-ai.presenter",
                        )
                    if process.stdin is not None:
                        process.stdin.write(json.dumps(decided, separators=(",", ":")) + "\n")
                        process.stdin.flush()
                    continue
                with io_lock:
                    stdout_lines.append(text)

        def read_stderr() -> None:
            assert process.stderr is not None
            for line in process.stderr:
                with io_lock:
                    stderr_lines.append(line.rstrip("\r\n"))

        readers = [
            threading.Thread(target=read_stdout, daemon=True, name="merced-ai-aais-out"),
            threading.Thread(target=read_stderr, daemon=True, name="merced-ai-aais-err"),
        ]
        for reader in readers:
            reader.start()
        deadline = started + request.timeout_seconds
        while process.poll() is None:
            if cancellation is not None and cancellation.is_set():
                approval_cancelled.set()
                _stop_process(process)
                raise HarnessRunError(
                    f"Harness {self.descriptor.id!r} was cancelled.", exit_code=130
                )
            if time.monotonic() >= deadline:
                approval_cancelled.set()
                _stop_process(process)
                raise HarnessRunError(
                    f"Harness {self.descriptor.id!r} timed out after {request.timeout_seconds}s.",
                    exit_code=5,
                )
            time.sleep(0.05)
        approval_cancelled.set()
        for reader in readers:
            reader.join(timeout=2)
        stdout = "\n".join(stdout_lines)[:MAX_CAPTURE_CHARS]
        stderr = "\n".join(stderr_lines)[:MAX_CAPTURE_CHARS]
        if process.returncode != 0:
            summary = _last_nonempty_line(stderr) or _last_nonempty_line(stdout) or "unknown error"
            raise HarnessRunError(
                f"Harness {self.descriptor.id!r} failed: {summary}",
                exit_code=process.returncode or 1,
                stderr=stderr,
            )
        output, raw, native_session_id = _normalize_output(self.descriptor.id, stdout)
        return RunResult(
            harness_id=self.descriptor.id,
            output=output,
            exit_code=process.returncode,
            raw=raw,
            native_session_id=native_session_id,
            duration_ms=round((time.monotonic() - started) * 1000),
        )


def _prefixed_prompt(system_prompt: str, prompt: str) -> str:
    return (
        f"{system_prompt}\n\n"
        "The surrounding harness instructions and permission policy remain authoritative.\n\n"
        f"User request:\n{prompt}"
    )


def _stop_process(process: subprocess.Popen[str]) -> None:
    process.terminate()
    try:
        process.wait(timeout=3)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait()


def _normalize_output(
    harness_id: str, stdout: str
) -> tuple[str, dict[str, Any] | None, str | None]:
    text = stdout.strip()
    if harness_id == "anton":
        clean = ANSI_ESCAPE_RE.sub("", text)
        responses = re.findall(r"(?:^|\n)anton>\s*(.*?)(?=\n(?:you>|anton>)|\Z)", clean, re.DOTALL)
        output = responses[-1].strip() if responses else clean
        return output, None, None
    if harness_id in {
        "claude",
        "gemini",
        "magagent",
        "opencode",
        "goose",
        "agy",
        "pi",
        "prime-agent",
        "openclaw",
    }:
        try:
            payload = json.loads(text)
        except json.JSONDecodeError:
            payload = _parse_json_lines(text)
            if payload is None:
                return text, None, None
        if "events" in payload:
            output = _find_assistant_text(payload)
        else:
            output = _find_text(payload)
        output = output or ""
        session_id = _find_string(payload, ("session_id", "sessionId"))
        return output, payload, session_id
    return text, None, None


def _parse_json_lines(text: str) -> dict[str, Any] | None:
    values: list[Any] = []
    for line in text.splitlines():
        try:
            values.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return {"events": values} if values else None


def _stdin_payload(harness_id: str, request: RunRequest) -> str | None:
    if harness_id == "anton":
        prompt = _prefixed_prompt(request.projection.system_prompt, request.prompt)
        atomic_prompt = " ".join(line.strip() for line in prompt.splitlines() if line.strip())
        return atomic_prompt + "\nexit\n"
    return None


def _subprocess_env(harness_id: str, request: RunRequest) -> dict[str, str]:
    env = os.environ.copy()
    if harness_id == "openclaw":
        env["OPENCLAW_WORKSPACE_DIR"] = str(request.workspace)
    return env


def _profile_provider(profile: ProfileRecord) -> str | None:
    provider = profile.document.get("spec", {}).get("model", {}).get("provider")
    return provider if isinstance(provider, str) and provider else None


def _qualified_model(profile: ProfileRecord, model: str) -> str:
    provider = _profile_provider(profile)
    return f"{provider}/{model}" if provider and "/" not in model else model


def _find_text(value: Any) -> str | None:
    if isinstance(value, dict):
        for key in ("result", "response", "output", "answer", "content", "text"):
            candidate = value.get(key)
            if isinstance(candidate, str) and candidate.strip():
                return candidate.strip()
        for candidate in value.values():
            found = _find_text(candidate)
            if found:
                return found
    elif isinstance(value, list):
        for candidate in value:
            found = _find_text(candidate)
            if found:
                return found
    return None


def _find_assistant_text(value: Any) -> str | None:
    candidates: list[str] = []

    def visit(item: Any) -> None:
        if isinstance(item, dict):
            if item.get("role") == "assistant":
                content = item.get("content")
                if isinstance(content, str) and content.strip():
                    candidates.append(content.strip())
                elif isinstance(content, list):
                    for block in content:
                        if isinstance(block, dict):
                            text = block.get("text")
                            if isinstance(text, str) and text.strip():
                                candidates.append(text.strip())
            for child in item.values():
                visit(child)
        elif isinstance(item, list):
            for child in item:
                visit(child)

    visit(value)
    return candidates[-1] if candidates else None


def _find_error(value: Any) -> str | None:
    if isinstance(value, dict):
        for key in ("errorMessage", "error_message"):
            candidate = value.get(key)
            if isinstance(candidate, str) and candidate.strip():
                return candidate.strip()
        for child in value.values():
            found = _find_error(child)
            if found:
                return found
    elif isinstance(value, list):
        for child in value:
            found = _find_error(child)
            if found:
                return found
    return None


def _find_string(value: dict[str, Any], keys: tuple[str, ...]) -> str | None:
    for key in keys:
        candidate = value.get(key)
        if isinstance(candidate, str):
            return candidate
    return None


def _last_nonempty_line(value: str) -> str | None:
    lines = [line.strip() for line in value.splitlines() if line.strip()]
    return lines[-1][:500] if lines else None


def _native_profile_visible(profile: ProfileRecord) -> bool:
    parent = profile.path.parent
    return profile.source == "project" and (
        parent.name == ".agents" or (parent.name == "agents" and parent.parent.name == ".magent")
    )


def _projected_model(
    profile: ProfileRecord, harness_id: str
) -> tuple[str | None, ProjectionAdjustment | None]:
    model_spec = profile.document.get("spec", {}).get("model", {})
    model_id = model_spec.get("id")
    provider = model_spec.get("provider")
    compatible = {
        "codex": {"openai"},
        "claude": {"anthropic"},
        "gemini": {"google", "gemini"},
        "agy": {"google", "gemini"},
        "dsh": {"deepseek"},
        "kimi": {"moonshot", "kimi", "openai", "anthropic", "google", "gemini"},
    }.get(harness_id)
    if not model_id or compatible is None or provider in compatible:
        return model_id, None
    return None, ProjectionAdjustment(
        field="spec.model",
        action="substituted",
        reason=(
            f"Requested provider {provider!r} does not match harness {harness_id!r}; "
            "the harness default model will be used."
        ),
    )
