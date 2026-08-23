"""Qualified noninteractive adapters for the first MVP harness set."""

from __future__ import annotations

import json
import subprocess
import time
from typing import Any

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
        if harness_id == "claude":
            adjustments = [
                ProjectionAdjustment(
                    field="spec.role",
                    action="mapped",
                    reason="Role and bounded state are passed through --system-prompt.",
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
            if shell_denied:
                command.append("--sandbox")
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
            return command
        raise HarnessRunError(f"Harness {harness_id!r} is not executable in this MVP.", exit_code=4)

    def run(self, request: RunRequest) -> RunResult:
        command = self.build_command(request)
        started = time.monotonic()
        process: subprocess.Popen[str] | None = None
        try:
            process = subprocess.Popen(  # noqa: S603 - argv is built by a trusted adapter
                command,
                cwd=request.workspace,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                shell=False,
                text=True,
            )
            stdout, stderr = process.communicate(timeout=request.timeout_seconds)
        except subprocess.TimeoutExpired as exc:
            if process is not None:
                process.kill()
                process.communicate()
            raise HarnessRunError(
                f"Harness {self.descriptor.id!r} timed out after {request.timeout_seconds}s.",
                exit_code=5,
            ) from exc
        except KeyboardInterrupt as exc:
            if process is not None:
                process.terminate()
                try:
                    process.wait(timeout=3)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait()
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


def _normalize_output(
    harness_id: str, stdout: str
) -> tuple[str, dict[str, Any] | None, str | None]:
    text = stdout.strip()
    if harness_id in {"claude", "gemini", "magagent"}:
        try:
            payload = json.loads(text)
        except json.JSONDecodeError:
            return text, None, None
        output = _find_text(payload) or text
        session_id = _find_string(payload, ("session_id", "sessionId"))
        return output, payload, session_id
    return text, None, None


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
