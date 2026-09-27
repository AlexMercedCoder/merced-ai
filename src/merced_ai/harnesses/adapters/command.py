"""Generic noninteractive subprocess runner for harness specs.

The per-harness knowledge (argv, prompt channel, projection style, output format) lives in
:class:`~merced_ai.harnesses.api.HarnessSpec`; the built-in specs are in
``merced_ai.harnesses.builtin``. This module owns everything shared: discovery, private temp
files, the argv size guard, bounded capture, cancellation, the AAIS control channel, and errors.
"""

from __future__ import annotations

import functools
import os
import subprocess
import sys
import tempfile
import threading
import time
from collections.abc import Callable
from dataclasses import replace
from pathlib import Path
from typing import Any

from aais import create_decision, validate

from merced_ai.harnesses.api import (
    HarnessInvocation,
    HarnessSpec,
    InvocationContext,
    native_profile_visible,
    prefixed_prompt,
    write_private,
)
from merced_ai.harnesses.detection import locate_executable, probe_executable
from merced_ai.harnesses.output import (
    NormalizedOutput,
    _last_nonempty_line,
    find_error,
    normalize_json,
    normalize_text,
)
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

__all__ = [
    "ARGV_LIMIT_POSIX",
    "ARGV_LIMIT_WINDOWS",
    "CommandHarnessAdapter",
    "HarnessInvocation",
    "HarnessRunError",
    "PROMPT_TOO_LARGE_EXIT",
    "argv_limit",
    "argv_size",
    "check_argv_size",
]

MAX_CAPTURE_CHARS = 10_000_000

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


def _unsupported_build(ctx: InvocationContext) -> HarnessInvocation:
    raise HarnessRunError(
        f"Harness {ctx.request.harness_id!r} is not executable in this MVP.", exit_code=4
    )


def _spec_for(target: HarnessSpec | HarnessDescriptor) -> HarnessSpec:
    if isinstance(target, HarnessSpec):
        return target
    from merced_ai.harnesses.builtin import BUILTIN_BY_ID

    builtin = BUILTIN_BY_ID.get(target.id)
    if builtin is None:
        return HarnessSpec(target, _unsupported_build)
    # A caller-supplied descriptor (tests, overrides) keeps the built-in behavior for its ID.
    return replace(builtin, descriptor=target)


class CommandHarnessAdapter:
    """Runs one harness spec as a bounded, shell-free subprocess per turn."""

    def __init__(self, spec: HarnessSpec | HarnessDescriptor) -> None:
        self.spec = _spec_for(spec)

    @property
    def descriptor(self) -> HarnessDescriptor:
        return self.spec.descriptor

    @property
    def relays_approvals(self) -> bool:
        return self.spec.aais_control

    def probe(self, workspace: Path | None = None) -> HarnessProbe:
        probe = probe_executable(self.descriptor, workspace)
        if probe.path is None or self.spec.features is None:
            return probe
        features = self.features(probe.path)
        update: dict[str, Any] = {"features": tuple(sorted(features))}
        if "--prompt-file" in features and self.descriptor.prompt_delivery.value == "argv":
            update["prompt_delivery"] = PromptDelivery.FILE
        return probe.model_copy(update=update)

    def project_profile(self, profile: ProfileRecord) -> ProfileProjection:
        harness_id = self.descriptor.id
        prompt = assemble_system_prompt(profile)
        model, model_adjustment = _projected_model(
            profile, harness_id, self.spec.compatible_providers
        )
        if self.spec.projection == "native" and native_profile_visible(profile):
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
        if self.spec.projection == "system_prompt":
            adjustments = [
                ProjectionAdjustment(
                    field="spec.role",
                    action="mapped",
                    reason="Role and bounded state are passed through a harness system prompt.",
                ),
                ProjectionAdjustment(
                    field="spec.permissions",
                    action="narrowed",
                    reason=(
                        "Requested permissions map to the harness's coarse permission controls."
                    ),
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

        ``scratch`` is a private temporary directory the caller creates and removes.
        """
        executable = locate_executable(self.descriptor)
        if executable is None:
            raise HarnessRunError(f"Harness {self.descriptor.id!r} is not installed.", exit_code=3)
        return self.spec.build(
            InvocationContext(request, executable, scratch, self.features(executable))
        )

    def features(self, executable: Path | None = None) -> frozenset[str]:
        """Optional CLI features of the installed executable, cached per path and mtime."""
        if self.spec.features is None:
            return frozenset()
        executable = executable or locate_executable(self.descriptor)
        if executable is None:
            return frozenset()
        try:
            stamp = executable.stat().st_mtime_ns
        except OSError:
            return frozenset()
        return _cached_features(self.spec.features, str(executable), stamp)

    def normalize(self, stdout: str) -> NormalizedOutput:
        output = self.spec.output
        if output == "json":
            return normalize_json(stdout)
        if output == "text":
            return normalize_text(stdout)
        return output(stdout)

    def environment(self, request: RunRequest, scratch: Path) -> dict[str, str]:
        env = _clean_environment()
        if self.spec.env is not None:
            executable = locate_executable(self.descriptor) or Path(self.descriptor.id)
            env.update(self.spec.env(InvocationContext(request, executable, scratch)))
        return env

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
                    env=self.environment(request, Path(scratch)),
                    timeout=request.timeout_seconds,
                    cancellation=cancellation,
                    limit=MAX_CAPTURE_CHARS,
                    stdin_payload=invocation.stdin,
                    control=control if self.spec.aais_control else None,
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
            # A harness that reports failures as structured output (for example `loro run
            # --json` on a provider error) says more in its reply than in its last stderr line.
            reply, reply_raw, _ = self.normalize(result.stdout)
            summary = (
                (reply[:500] if reply_raw is not None and reply else None)
                or _last_nonempty_line(result.stderr)
                or _last_nonempty_line(result.stdout)
                or "unknown error"
            )
            raise HarnessRunError(
                f"Harness {self.descriptor.id!r} failed: {summary}",
                exit_code=result.returncode,
                stderr=result.stderr,
            )
        output, raw, native_session_id = self.normalize(result.stdout)
        if raw and raw.get("stop_reason") == "provider_error":
            raise HarnessRunError(
                f"Harness {self.descriptor.id!r} failed: {output[:500] or 'provider error'}",
                exit_code=1,
                stderr=result.stderr,
            )
        embedded_error = find_error(raw) if raw else None
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


@functools.lru_cache(maxsize=64)
def _cached_features(
    detector: Callable[[Path], frozenset[str]], executable: str, _mtime_ns: int
) -> frozenset[str]:
    return detector(Path(executable))


def _clean_environment() -> dict[str, str]:
    env = os.environ.copy()
    # pytest-cov enables subprocess coverage through inherited environment variables. A child
    # harness is a separate product boundary, so allowing those variables through both
    # contaminates this package's coverage data and changes the child's startup behavior.
    env.pop("COVERAGE_PROCESS_START", None)
    for key in tuple(env):
        if key.startswith("COV_CORE_"):
            env.pop(key, None)
    return env


def _projected_model(
    profile: ProfileRecord, harness_id: str, compatible: frozenset[str] | None
) -> tuple[str | None, ProjectionAdjustment | None]:
    model_spec = profile.document.get("spec", {}).get("model", {})
    model_id = model_spec.get("id")
    provider = model_spec.get("provider")
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


# Backward-compatible helpers used by tests and older integrations.
def _builtin_adapter(harness_id: str) -> CommandHarnessAdapter:
    from merced_ai.harnesses.builtin import BUILTIN_BY_ID

    return CommandHarnessAdapter(BUILTIN_BY_ID[harness_id])


def _normalize_output(harness_id: str, stdout: str) -> NormalizedOutput:
    return _builtin_adapter(harness_id).normalize(stdout)


def _stdin_payload(harness_id: str, request: RunRequest) -> str | None:
    if harness_id != "anton":
        return None
    from merced_ai.harnesses.builtin import anton_turn

    return anton_turn(InvocationContext(request, Path("anton"), Path(".")))


def _subprocess_env(harness_id: str, request: RunRequest) -> dict[str, str]:
    return _builtin_adapter(harness_id).environment(request, Path("."))


_prefixed_prompt = prefixed_prompt
_write_private = write_private
