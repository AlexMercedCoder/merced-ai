"""Qualified noninteractive adapters for the first MVP harness set."""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import tempfile
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from aais import create_decision, validate

from merced_ai.harnesses.detection import locate_executable, probe_executable
from merced_ai.harnesses.process import ChildProcessError, run_child
from merced_ai.models import (
    HarnessDescriptor,
    HarnessProbe,
    ProfileProjection,
    ProfileRecord,
    ProjectionAdjustment,
    PromptDelivery,
    RunRequest,
    RunResult,
)
from merced_ai.profiles import assemble_system_prompt

MAX_CAPTURE_CHARS = 10_000_000
ANSI_ESCAPE_RE = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]")

# Conservative command-line budgets. Linux caps one argument at 128 KiB and the whole argv plus
# environment near 2 MiB; Windows caps the whole command line at 32,767 UTF-16 characters, and
# npm-style .cmd launchers go through cmd.exe, which allows only 8,191.
ARGV_LIMIT_POSIX = 100 * 1024
ARGV_LIMIT_WINDOWS = 24 * 1024
PROMPT_TOO_LARGE_EXIT = 7


class HarnessRunError(RuntimeError):
    def __init__(self, message: str, *, exit_code: int = 1, stderr: str = "") -> None:
        super().__init__(message)
        self.exit_code = exit_code
        self.stderr = stderr


@dataclass(frozen=True)
class HarnessInvocation:
    """One harness process: its argv, how the prompt travels, and the stdin payload."""

    argv: list[str]
    prompt_delivery: PromptDelivery
    stdin: str | None = None


def argv_limit() -> int:
    return ARGV_LIMIT_WINDOWS if sys.platform == "win32" else ARGV_LIMIT_POSIX


def argv_size(argv: list[str]) -> int:
    """Size of the command line as the operating system will measure it."""
    if sys.platform == "win32":
        return len(subprocess.list2cmdline(argv))
    return sum(len(item.encode("utf-8", errors="surrogatepass")) + 1 for item in argv)


def check_argv_size(argv: list[str], harness_name: str) -> None:
    """Refuse a command line that would exceed the platform budget, before spawning."""
    size, limit = argv_size(argv), argv_limit()
    if size <= limit:
        return
    raise HarnessRunError(
        f"The prompt is too large to pass to {harness_name} on the command line "
        f"({size / 1024:.0f} KB; the limit on this platform is {limit // 1024} KB). "
        f"{harness_name} only accepts this input as a command-line argument. Shorten the "
        "message or the attached context, or route this bot to a harness that reads prompts "
        "from stdin (see `merced-ai harness show`).",
        exit_code=PROMPT_TOO_LARGE_EXIT,
    )


def _write_private(path: Path, content: str) -> Path:
    """Write a prompt file readable only by the current user inside a private temp dir."""
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8", newline="") as handle:
        handle.write(content)
    return path


class CommandHarnessAdapter:
    def __init__(self, descriptor: HarnessDescriptor) -> None:
        self._descriptor = descriptor

    @property
    def descriptor(self) -> HarnessDescriptor:
        return self._descriptor

    def probe(self, workspace: Path | None = None) -> HarnessProbe:
        return probe_executable(self.descriptor, workspace)

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

    def build_invocation(self, request: RunRequest, scratch: Path) -> HarnessInvocation:
        """Build the argv, stdin payload, and private prompt files for one run.

        Prompts go through stdin wherever the harness reads it, or through a file in ``scratch``
        (a private temporary directory the caller creates and removes). Only harnesses that
        accept the prompt solely as an argument receive it on the command line, and those are
        bounded by :func:`check_argv_size`.
        """
        executable = locate_executable(self.descriptor)
        if executable is None:
            raise HarnessRunError(f"Harness {self.descriptor.id!r} is not installed.", exit_code=3)
        profile = request.profile
        projection = request.projection
        prompt = request.prompt
        prefixed = _prefixed_prompt(projection.system_prompt, prompt)
        harness_id = self.descriptor.id
        permissions = profile.document.get("spec", {}).get("permissions", {})
        edit_denied = permissions.get("edit") == "deny"
        shell_denied = permissions.get("shell") == "deny"
        stdin = PromptDelivery.STDIN

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
            # `codex exec -` reads the whole prompt from stdin.
            command.append("-")
            return HarnessInvocation(command, stdin, stdin=prefixed)
        if harness_id == "claude":
            mode = "plan" if edit_denied or shell_denied else "manual"
            system_file = _write_private(scratch / "system-prompt.md", projection.system_prompt)
            command = [
                str(executable),
                "--print",
                "--output-format",
                "json",
                "--permission-mode",
                mode,
                "--system-prompt-file",
                str(system_file),
            ]
            if projection.model:
                command.extend(("--model", projection.model))
            # `claude --print` reads the prompt from stdin when no prompt argument is given.
            return HarnessInvocation(command, stdin, stdin=prompt)
        if harness_id == "gemini":
            command = [str(executable), "--output-format", "json", "--approval-mode", "default"]
            if projection.model:
                command.extend(("--model", projection.model))
            # Gemini runs headless when stdin is not a terminal and uses stdin as the prompt.
            return HarnessInvocation(command, stdin, stdin=prefixed)
        if harness_id == "magagent":
            mode = "paranoid" if edit_denied or shell_denied else "balanced"
            # stdin is the AAIS approval channel, and `magent ask` takes the task only as an
            # argument, so this route stays on argv and is bounded by the argv guard.
            command = [
                str(executable),
                "ask",
                prompt if _native_profile_visible(profile) else prefixed,
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
            return HarnessInvocation(command, PromptDelivery.ARGV)
        if harness_id == "loro":
            # Same constraint as MagAgent: stdin carries AAIS envelopes.
            command = [
                str(executable),
                "run",
                prompt if _native_profile_visible(profile) else prefixed,
            ]
            if _native_profile_visible(profile):
                command.extend(("--agent", profile.name))
            command.append("--approval-stdio")
            return HarnessInvocation(command, PromptDelivery.ARGV)
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
            # `opencode run` uses piped stdin as the message when no positional is given.
            return HarnessInvocation(command, stdin, stdin=prefixed)
        if harness_id == "goose":
            # `--instructions -` reads the request from stdin. The system prompt has no file
            # variant, so it stays on argv and is covered by the argv guard.
            command = [
                str(executable),
                "run",
                "--instructions",
                "-",
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
            return HarnessInvocation(command, stdin, stdin=prompt)
        if harness_id == "dsh":
            # The headless profile reads its task only from the command line.
            return HarnessInvocation(
                [str(executable), "--profile", "headless", prefixed], PromptDelivery.ARGV
            )
        if harness_id == "agy":
            # Print mode reads stdin only as stream-json input, which this adapter does not
            # speak yet, so the prompt stays on argv and is bounded by the argv guard.
            command = [
                str(executable),
                f"--print={prefixed}",
                "--output-format",
                "json",
                "--disable-slash-commands",
            ]
            if edit_denied or shell_denied:
                command.extend(("--mode", "plan"))
            if projection.model:
                command.extend(("--model", projection.model))
            return HarnessInvocation(command, PromptDelivery.ARGV)
        if harness_id in {"pi", "prime-agent"}:
            command = [str(executable), "--print", "--mode", "json", "--no-session"]
            if harness_id == "prime-agent":
                command.extend(("--cwd", str(request.workspace)))
            # `--append-system-prompt` reads the file when the value is an existing path, and
            # print mode uses piped stdin as the initial message.
            system_file = _write_private(scratch / "system-prompt.md", projection.system_prompt)
            command.extend(("--append-system-prompt", str(system_file)))
            if harness_id == "prime-agent" and (edit_denied or shell_denied):
                command.append("--no-tools")
            else:
                excluded: list[str] = []
                if edit_denied:
                    excluded.extend(("edit", "write"))
                if shell_denied:
                    excluded.append("bash")
                if excluded:
                    command.extend(("--exclude-tools", ",".join(excluded)))
            if projection.model:
                command.extend(("--model", _qualified_model(profile, projection.model)))
            return HarnessInvocation(command, stdin, stdin=prompt)
        if harness_id == "openclaw":
            message_file = _write_private(scratch / "message.md", prefixed)
            command = [
                str(executable),
                "agent",
                "--local",
                "--agent",
                "main",
                "--json",
                "--message-file",
                str(message_file),
            ]
            if projection.model:
                command.extend(("--model", _qualified_model(profile, projection.model)))
            return HarnessInvocation(command, PromptDelivery.FILE)
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
            # Print mode reads the command from stdin when `--prompt` is absent.
            return HarnessInvocation(command, stdin, stdin=prefixed)
        if harness_id == "anton":
            return HarnessInvocation(
                [str(executable), "--folder", str(request.workspace), "--no-update"],
                stdin,
                stdin=_stdin_payload(harness_id, request),
            )
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
        approval_event_handler: Callable[[dict[str, Any]], None] | None = None,
    ) -> RunResult:
        started = time.monotonic()

        def control(value: dict[str, Any], stopped: threading.Event) -> dict[str, Any] | None:
            envelope = validate(value)
            if envelope["type"] != "approval.requested":
                if approval_event_handler is not None:
                    approval_event_handler(envelope)
                return None
            if approval_handler is not None:
                return approval_handler(envelope, stopped)
            return create_decision(
                envelope,
                decision="deny",
                scope="once",
                actor={
                    "id": "merced-ai.no-presenter",
                    "type": "policy",
                    "authenticated_by": "adapter",
                },
                sequence=envelope["sequence"],
                stream="merced-ai.presenter",
            )

        try:
            with tempfile.TemporaryDirectory(prefix="merced-ai-prompt-") as scratch:
                invocation = self.build_invocation(request, Path(scratch))
                check_argv_size(invocation.argv, self.descriptor.name)
                result = run_child(
                    invocation.argv,
                    workspace=request.workspace,
                    env=_subprocess_env(self.descriptor.id, request),
                    timeout=request.timeout_seconds,
                    cancellation=cancellation,
                    limit=MAX_CAPTURE_CHARS,
                    stdin_payload=invocation.stdin,
                    control=control if self.descriptor.id in {"magagent", "loro"} else None,
                )
        except ChildProcessError as exc:
            raise HarnessRunError(
                f"Harness {self.descriptor.id!r} {exc}.", exit_code=exc.exit_code
            ) from exc
        except OSError as exc:
            raise HarnessRunError(
                f"Harness {self.descriptor.id!r} could not start: {type(exc).__name__}.",
                exit_code=5,
            ) from exc
        if result.returncode != 0:
            summary = (
                _last_nonempty_line(result.stderr)
                or _last_nonempty_line(result.stdout)
                or "unknown error"
            )
            raise HarnessRunError(
                f"Harness {self.descriptor.id!r} failed: {summary}",
                exit_code=result.returncode,
                stderr=result.stderr,
            )
        output, raw, native_session_id = _normalize_output(self.descriptor.id, result.stdout)
        if raw and raw.get("stop_reason") == "provider_error":
            raise HarnessRunError(
                f"Harness {self.descriptor.id!r} failed: {output[:500] or 'provider error'}",
                exit_code=1,
                stderr=result.stderr,
            )
        embedded_error = _find_error(raw) if raw else None
        if not output and embedded_error:
            raise HarnessRunError(
                f"Harness {self.descriptor.id!r} failed: {embedded_error}",
                exit_code=1,
                stderr=result.stderr,
            )
        if result.truncated:
            raw = {**(raw or {}), "output_truncated": True}
            output += "\n\n[Harness output was truncated at the broker capture limit.]"
        return RunResult(
            harness_id=self.descriptor.id,
            output=output,
            exit_code=result.returncode,
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
    if harness_id == "loro":
        return _normalize_loro(text)
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
        streamed = False
        try:
            payload = json.loads(text)
        except json.JSONDecodeError:
            payload = _parse_json_lines(text)
            streamed = payload is not None
            if payload is None:
                payload = _parse_trailing_object(text)
            if payload is None:
                return text, None, None
        if not isinstance(payload, dict):
            return text, None, None
        # An explicit top-level answer wins. Otherwise use the assistant's turn: transcripts
        # (Goose "messages", JSONL event streams) also contain the user's own message, which
        # must never come back as the reply.
        output = _top_level_text(payload) or _find_assistant_text(payload)
        if output is None and not streamed:
            output = _find_text(payload)
        output = output or ""
        session_id = _find_string(payload, ("session_id", "sessionId"))
        return output, payload, session_id
    return text, None, None


LORO_SUMMARY_RE = re.compile(r"^Loro \w+ mode completed\.", re.MULTILINE)
LORO_RUN_TRAILER_RE = re.compile(r"\n+Run [0-9a-fA-F-]{36} \(export:[^\n]*\)\s*$")


def _normalize_loro(text: str) -> tuple[str, dict[str, Any] | None, str | None]:
    """Return the model's reply from `loro run`'s plain-text summary.

    `loro run` prints a run summary (provider, stop reason, steps, prompt, tools) that ends with
    "Model response: ...". Only the response is the bot's answer; the stop reason is kept so a
    provider error is reported as a failure instead of as the reply.
    """
    if not LORO_SUMMARY_RE.search(text) or "\nModel response: " not in text:
        return text, None, None
    response = text.rsplit("\nModel response: ", 1)[1]
    response = LORO_RUN_TRAILER_RE.sub("", response).strip()
    raw: dict[str, Any] = {}
    if match := re.search(r"^Stop reason: (\S+)", text, re.MULTILINE):
        raw["stop_reason"] = match.group(1)
    if match := re.search(r"^Provider: (.+)$", text, re.MULTILINE):
        raw["provider"] = match.group(1).strip()
    if match := LORO_RUN_TRAILER_RE.search(text):
        raw["run_id"] = match.group(0).split()[1]
    return response, raw, None


def _parse_json_lines(text: str) -> dict[str, Any] | None:
    values: list[Any] = []
    for line in text.splitlines():
        try:
            value = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(value, dict):
            values.append(value)
    return {"events": values} if values else None


def _parse_trailing_object(text: str) -> dict[str, Any] | None:
    """Parse one pretty-printed JSON object that follows status lines (MagAgent prints a
    "Loaded N skills" line before its --json document)."""
    for match in re.finditer(r"^\{", text, re.MULTILINE):
        try:
            value, end = json.JSONDecoder().raw_decode(text, match.start())
        except json.JSONDecodeError:
            continue
        if isinstance(value, dict) and not text[end:].strip():
            return value
    return None


def _stdin_payload(harness_id: str, request: RunRequest) -> str | None:
    if harness_id == "anton":
        prompt = _prefixed_prompt(request.projection.system_prompt, request.prompt)
        atomic_prompt = " ".join(line.strip() for line in prompt.splitlines() if line.strip())
        return atomic_prompt + "\nexit\n"
    return None


def _subprocess_env(harness_id: str, request: RunRequest) -> dict[str, str]:
    env = os.environ.copy()
    # pytest-cov enables subprocess coverage through inherited environment
    # variables. A child harness is a separate product boundary, so allowing
    # those variables through both contaminates this package's coverage data
    # and changes the child's startup behavior.
    env.pop("COVERAGE_PROCESS_START", None)
    for key in tuple(env):
        if key.startswith("COV_CORE_"):
            env.pop(key, None)
    if harness_id == "openclaw":
        env["OPENCLAW_WORKSPACE_DIR"] = str(request.workspace)
    return env


def _profile_provider(profile: ProfileRecord) -> str | None:
    provider = profile.document.get("spec", {}).get("model", {}).get("provider")
    return provider if isinstance(provider, str) and provider else None


def _qualified_model(profile: ProfileRecord, model: str) -> str:
    provider = _profile_provider(profile)
    return f"{provider}/{model}" if provider and "/" not in model else model


def _top_level_text(value: dict[str, Any]) -> str | None:
    for key in ("result", "response", "output", "answer"):
        candidate = value.get(key)
        if isinstance(candidate, str) and candidate.strip():
            return candidate.strip()
    return None


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
    # OpenCode `run --format json` streams parts: {"type": "text", "part": {"type": "text", ...}}.
    part_texts: dict[str, list[str]] = {}
    part_order: list[str] = []

    def visit(item: Any) -> None:
        if isinstance(item, dict):
            part = item.get("part")
            if (
                item.get("type") == "text"
                and isinstance(part, dict)
                and part.get("type") == "text"
                and isinstance(part.get("text"), str)
                and part["text"].strip()
            ):
                message_id = str(part.get("messageID") or "")
                if message_id not in part_texts:
                    part_texts[message_id] = []
                    part_order.append(message_id)
                part_texts[message_id].append(part["text"].strip())
                return
            if item.get("type") == "assistant_message" and isinstance(item.get("content"), str):
                # MagAgent --events records.
                if item["content"].strip():
                    candidates.append(item["content"].strip())
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
    if part_order:
        return "\n\n".join(part_texts[part_order[-1]])
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
