"""Bounded executable discovery that never invokes a shell or touches credentials."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

from merced_ai.models import HarnessDescriptor, HarnessProbe, HarnessStatus

PROBE_TIMEOUT_SECONDS = 3.0
MAX_VERSION_LENGTH = 500


def locate_executable(descriptor: HarnessDescriptor) -> Path | None:
    override = os.environ.get(_override_variable(descriptor.id))
    if override:
        candidate = shutil.which(str(Path(override).expanduser()))
        if candidate:
            return Path(candidate).resolve()

    for executable_name in descriptor.executable_names:
        candidate = shutil.which(executable_name)
        if candidate:
            return Path(candidate).resolve()
        for bin_dir in _fallback_bin_dirs(executable_name):
            # Supplying an explicit search path preserves PATHEXT handling for
            # Windows .exe/.cmd launchers while retaining executable checks on Unix.
            fallback = shutil.which(executable_name, path=str(bin_dir))
            if fallback:
                return Path(fallback).resolve()
    return None


def _override_variable(harness_id: str) -> str:
    normalized = "".join(character if character.isalnum() else "_" for character in harness_id)
    return f"MERCED_AI_{normalized.upper()}_PATH"


def _fallback_bin_dirs(executable_name: str) -> tuple[Path, ...]:
    candidates: list[Path] = []

    configured = os.environ.get("MERCED_AI_HARNESS_PATHS", "")
    candidates.extend(Path(value).expanduser() for value in configured.split(os.pathsep) if value)

    for variable in ("UV_TOOL_BIN_DIR", "NPM_CONFIG_PREFIX"):
        if value := os.environ.get(variable):
            root = Path(value).expanduser()
            candidates.extend((root, root / "bin"))

    try:
        home = Path.home()
    except (RuntimeError, KeyError):
        home = None
    if home is not None:
        candidates.extend(
            (
                home / ".local" / "bin",
                home / ".cargo" / "bin",
                home / ".npm-global" / "bin",
                home / f".{executable_name}" / "bin",
            )
        )

    if app_data := os.environ.get("APPDATA"):
        candidates.append(Path(app_data) / "npm")
    if scoop := os.environ.get("SCOOP"):
        candidates.append(Path(scoop) / "shims")
    if chocolatey := os.environ.get("ChocolateyInstall"):
        candidates.append(Path(chocolatey) / "bin")
    if sys.platform == "darwin":
        candidates.extend((Path("/opt/homebrew/bin"), Path("/usr/local/bin")))

    return tuple(dict.fromkeys(candidates))


def probe_executable(descriptor: HarnessDescriptor, workspace: Path | None = None) -> HarnessProbe:
    started = time.monotonic()
    executable = locate_executable(descriptor)
    if executable is None:
        return HarnessProbe(
            harness_id=descriptor.id,
            status=HarnessStatus.NOT_INSTALLED,
            capabilities=descriptor.capabilities,
            duration_ms=_elapsed_ms(started),
        )

    try:
        completed = subprocess.run(  # noqa: S603 - executable is resolved from a fixed descriptor
            [str(executable), *descriptor.version_args],
            capture_output=True,
            check=False,
            shell=False,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=PROBE_TIMEOUT_SECONDS,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return HarnessProbe(
            harness_id=descriptor.id,
            status=HarnessStatus.PROBE_FAILED,
            path=executable,
            capabilities=descriptor.capabilities,
            warnings=(f"Version probe failed: {type(exc).__name__}",),
            duration_ms=_elapsed_ms(started),
        )

    output = (completed.stdout or completed.stderr).strip()
    output = output[:MAX_VERSION_LENGTH] or None
    if completed.returncode != 0:
        return HarnessProbe(
            harness_id=descriptor.id,
            status=HarnessStatus.PROBE_FAILED,
            path=executable,
            version=output,
            capabilities=descriptor.capabilities,
            warnings=(f"Version probe exited with status {completed.returncode}.",),
            duration_ms=_elapsed_ms(started),
        )

    capabilities = descriptor.capabilities
    verified = False
    warnings = ["Authentication and protocol readiness have not been checked yet."]
    if descriptor.id in {"loro", "magagent"}:
        capabilities = capabilities.model_copy(update={"webmcp": False})
        try:
            from merced_ai.harnesses.process import run_child

            result = run_child(
                [str(executable), "capabilities", "--json"],
                workspace=workspace or Path.cwd(),
                env=dict(os.environ),
                timeout=5,
                cancellation=None,
                limit=32_000,
            )
            report = json.loads(result.stdout)
            if not isinstance(report, dict):
                raise ValueError("Invalid capability report")
            verified = (
                result.returncode == 0
                and not result.truncated
                and report.get("schema") == "agent-runtime.capabilities.v1"
                and report.get("harness") == descriptor.id
            )
            if verified:
                capabilities = capabilities.model_copy(
                    update={
                        "webmcp": report.get("supported", {}).get("webmcp") is True
                        and report.get("ready", {}).get("webmcp") is True
                    }
                )
                warnings.extend(report.get("webmcp", {}).get("reasons", []))
        except (OSError, ValueError, RuntimeError, TypeError, AttributeError):
            pass
        if not verified:
            warnings.append("Installed harness did not negotiate capability readiness; upgrade it.")

    return HarnessProbe(
        harness_id=descriptor.id,
        status=HarnessStatus.INSTALLED,
        path=executable,
        version=output,
        transport=descriptor.transports[0] if descriptor.transports else None,
        capabilities=capabilities,
        capabilities_verified=verified,
        warnings=tuple(warnings),
        duration_ms=_elapsed_ms(started),
    )


def _elapsed_ms(started: float) -> int:
    return round((time.monotonic() - started) * 1000)
