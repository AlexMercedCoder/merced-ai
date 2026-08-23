"""Probe-only adapter used while individual session transports are qualified."""

from __future__ import annotations

from typing import NoReturn

from merced_ai.harnesses.detection import probe_executable
from merced_ai.models import (
    HarnessDescriptor,
    HarnessProbe,
    ProfileProjection,
    ProfileRecord,
    RunRequest,
    RunResult,
)


class ExecutableProbeAdapter:
    def __init__(self, descriptor: HarnessDescriptor) -> None:
        self._descriptor = descriptor

    @property
    def descriptor(self) -> HarnessDescriptor:
        return self._descriptor

    def probe(self) -> HarnessProbe:
        return probe_executable(self.descriptor)

    def project_profile(self, profile: ProfileRecord) -> ProfileProjection:
        return self._unsupported()

    def run(self, request: RunRequest) -> RunResult:
        return self._unsupported()

    def _unsupported(self) -> NoReturn:
        raise NotImplementedError(
            f"Harness {self.descriptor.id!r} is registered for discovery only; "
            "its session transport is not implemented yet."
        )
