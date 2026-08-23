"""Adapter registry and built-in harness metadata."""

from __future__ import annotations

from collections.abc import Iterable

from merced_ai.harnesses.adapters.command import CommandHarnessAdapter
from merced_ai.harnesses.adapters.executable import ExecutableProbeAdapter
from merced_ai.harnesses.base import HarnessAdapter
from merced_ai.models import HarnessCapabilities, HarnessDescriptor, HarnessProbe, TransportKind


class HarnessRegistry:
    def __init__(self, adapters: Iterable[HarnessAdapter] = ()) -> None:
        self._adapters: dict[str, HarnessAdapter] = {}
        for adapter in adapters:
            self.register(adapter)

    def register(self, adapter: HarnessAdapter) -> None:
        harness_id = adapter.descriptor.id
        if harness_id in self._adapters:
            raise ValueError(f"Harness adapter {harness_id!r} is already registered.")
        self._adapters[harness_id] = adapter

    def get(self, harness_id: str) -> HarnessAdapter:
        try:
            return self._adapters[harness_id]
        except KeyError as exc:
            raise KeyError(f"Unknown harness {harness_id!r}.") from exc

    def descriptors(self) -> tuple[HarnessDescriptor, ...]:
        return tuple(adapter.descriptor for adapter in self._adapters.values())

    def probe_all(self) -> tuple[HarnessProbe, ...]:
        return tuple(adapter.probe() for adapter in self._adapters.values())


def default_registry() -> HarnessRegistry:
    runnable = {"codex", "claude", "gemini", "loro", "magagent"}
    return HarnessRegistry(
        CommandHarnessAdapter(item) if item.id in runnable else ExecutableProbeAdapter(item)
        for item in _BUILTIN_DESCRIPTORS
    )


_RICH_SESSION = HarnessCapabilities(
    streaming=True,
    resume=True,
    approvals=True,
    attachments=True,
    model_listing=True,
)

_BUILTIN_DESCRIPTORS = (
    HarnessDescriptor(
        id="codex",
        name="Codex",
        executable_names=("codex",),
        transports=(TransportKind.NATIVE, TransportKind.STRUCTURED_SUBPROCESS),
        capabilities=_RICH_SESSION,
    ),
    HarnessDescriptor(
        id="claude",
        name="Claude Code",
        executable_names=("claude",),
        transports=(TransportKind.STRUCTURED_SUBPROCESS,),
        capabilities=_RICH_SESSION,
    ),
    HarnessDescriptor(
        id="gemini",
        name="Gemini CLI",
        executable_names=("gemini",),
        transports=(TransportKind.ACP_STDIO, TransportKind.STRUCTURED_SUBPROCESS),
        capabilities=_RICH_SESSION,
    ),
    HarnessDescriptor(
        id="opencode",
        name="OpenCode",
        executable_names=("opencode",),
        transports=(TransportKind.ACP_STDIO, TransportKind.STRUCTURED_SUBPROCESS),
        capabilities=_RICH_SESSION,
    ),
    HarnessDescriptor(
        id="goose",
        name="Goose",
        executable_names=("goose",),
        transports=(TransportKind.ACP_STDIO, TransportKind.STRUCTURED_SUBPROCESS),
        capabilities=_RICH_SESSION,
    ),
    HarnessDescriptor(
        id="loro",
        name="Loro",
        executable_names=("loro",),
        transports=(TransportKind.NATIVE, TransportKind.STRUCTURED_SUBPROCESS),
        capabilities=_RICH_SESSION.model_copy(update={"native_oap": True}),
    ),
    HarnessDescriptor(
        id="magagent",
        name="MagAgent",
        executable_names=("magent",),
        transports=(TransportKind.NATIVE, TransportKind.STRUCTURED_SUBPROCESS),
        capabilities=_RICH_SESSION.model_copy(update={"native_oap": True}),
    ),
)
