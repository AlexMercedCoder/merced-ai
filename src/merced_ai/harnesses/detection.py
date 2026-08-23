"""Bounded executable discovery that never invokes a shell or touches credentials."""

from __future__ import annotations

import os
import shutil
import subprocess
import time
from pathlib import Path

from merced_ai.models import HarnessDescriptor, HarnessProbe, HarnessStatus

PROBE_TIMEOUT_SECONDS = 3.0
MAX_VERSION_LENGTH = 500


def locate_executable(descriptor: HarnessDescriptor) -> Path | None:
    for executable_name in descriptor.executable_names:
        candidate = shutil.which(executable_name)
        if candidate:
            return Path(candidate).resolve()
        # Rootless installers commonly place binaries in a private user prefix
        # without updating the environment of an already-running parent process.
        home_value = os.environ.get("HOME")
        if not home_value:
            continue
        home = Path(home_value).expanduser()
        for bin_dir in (home / ".local" / "bin", home / f".{executable_name}" / "bin"):
            fallback = bin_dir / executable_name
            if fallback.is_file() and os.access(fallback, os.X_OK):
                return fallback.resolve()
    return None


def probe_executable(descriptor: HarnessDescriptor) -> HarnessProbe:
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

    return HarnessProbe(
        harness_id=descriptor.id,
        status=HarnessStatus.INSTALLED,
        path=executable,
        version=output,
        transport=descriptor.transports[0] if descriptor.transports else None,
        capabilities=descriptor.capabilities,
        warnings=("Authentication and protocol readiness have not been checked yet.",),
        duration_ms=_elapsed_ms(started),
    )


def _elapsed_ms(started: float) -> int:
    return round((time.monotonic() - started) * 1000)
