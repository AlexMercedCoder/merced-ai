"""Contract implemented by all harness adapters."""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from merced_ai.models import (
    HarnessDescriptor,
    HarnessProbe,
    ProfileProjection,
    ProfileRecord,
    RunRequest,
    RunResult,
)


@runtime_checkable
class HarnessAdapter(Protocol):
    @property
    def descriptor(self) -> HarnessDescriptor: ...

    def probe(self) -> HarnessProbe: ...

    def project_profile(self, profile: ProfileRecord) -> ProfileProjection: ...

    def run(self, request: RunRequest) -> RunResult: ...
