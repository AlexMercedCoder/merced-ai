"""Adapter registry and built-in harness metadata."""

from __future__ import annotations

import os
import threading
import time
from collections.abc import Iterable
from pathlib import Path

from merced_ai.harnesses.adapters.command import CommandHarnessAdapter
from merced_ai.harnesses.adapters.executable import ExecutableProbeAdapter
from merced_ai.harnesses.base import HarnessAdapter
from merced_ai.models import (
    HarnessCapabilities,
    HarnessDescriptor,
    HarnessProbe,
    PromptDelivery,
    TransportKind,
)

DEFAULT_PROBE_TTL_SECONDS = 30.0


def probe_ttl_seconds() -> float:
    """Routing probe cache lifetime; ``MERCED_AI_PROBE_TTL_SECONDS=0`` disables the cache."""
    try:
        return max(0.0, float(os.environ.get("MERCED_AI_PROBE_TTL_SECONDS", "")))
    except ValueError:
        return DEFAULT_PROBE_TTL_SECONDS


class HarnessRegistry:
    def __init__(self, adapters: Iterable[HarnessAdapter] = ()) -> None:
        self._adapters: dict[str, HarnessAdapter] = {}
        self._probe_cache: dict[tuple[str, str], tuple[float, HarnessProbe]] = {}
        self._probe_lock = threading.Lock()
        for adapter in adapters:
            self.register(adapter)

    def cached_probe(self, harness_id: str, workspace: Path | None = None) -> HarnessProbe:
        """Probe a harness, reusing a recent result for the same workspace.

        Routing probes on every turn; spawning each harness's version command (and, for Loro and
        MagAgent, a capability report) each time added seconds per turn. Results are reused for
        a short TTL, so a newly installed or removed harness is noticed within that window.
        """
        ttl = probe_ttl_seconds()
        key = (harness_id, str(workspace or ""))
        now = time.monotonic()
        if ttl > 0:
            with self._probe_lock:
                cached = self._probe_cache.get(key)
            if cached and now - cached[0] < ttl:
                return cached[1]
        adapter = self.get(harness_id)
        probe = (
            adapter.probe(workspace=workspace)
            if isinstance(adapter, CommandHarnessAdapter)
            else adapter.probe()
        )
        if ttl > 0:
            with self._probe_lock:
                self._probe_cache[key] = (now, probe)
        return probe

    def clear_probe_cache(self) -> None:
        with self._probe_lock:
            self._probe_cache.clear()

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
    runnable = {
        "codex",
        "claude",
        "gemini",
        "opencode",
        "goose",
        "loro",
        "magagent",
        "anton",
        "dsh",
        "agy",
        "pi",
        "prime-agent",
        "openclaw",
        "kimi",
    }
    return HarnessRegistry(
        CommandHarnessAdapter(item) if item.id in runnable else ExecutableProbeAdapter(item)
        for item in _BUILTIN_DESCRIPTORS
    )


# What the harnesses document for their own interactive or protocol surfaces. Merced AI records
# these for reference only; it does not use them yet.
_NATIVE_SESSION = HarnessCapabilities(
    streaming=True,
    resume=True,
    approvals=True,
    attachments=True,
    model_listing=True,
)

# What Merced AI delivers end to end through a noninteractive subprocess adapter: output arrives
# when the run completes, each turn starts a fresh harness process with a bounded transcript, and
# selected workspace files are inlined into the prompt as context.
_BROKER_SUBPROCESS = HarnessCapabilities(attachments=True)

# MagAgent and Loro additionally relay AAIS approvals over stdio, receive project OAP profiles
# natively, and can satisfy WebMCP routing once their readiness report is verified.
_BROKER_AAIS_NATIVE = _BROKER_SUBPROCESS.model_copy(
    update={"approvals": True, "native_oap": True, "webmcp": True}
)


def _descriptor(
    harness_id: str,
    name: str,
    executable: str,
    *,
    transports: tuple[TransportKind, ...] = (TransportKind.STRUCTURED_SUBPROCESS,),
    harness_supports: HarnessCapabilities = _NATIVE_SESSION,
    broker_implements: HarnessCapabilities = _BROKER_SUBPROCESS,
    version_args: tuple[str, ...] = ("--version",),
    prompt_delivery: PromptDelivery = PromptDelivery.STDIN,
) -> HarnessDescriptor:
    return HarnessDescriptor(
        id=harness_id,
        name=name,
        executable_names=(executable,),
        transports=transports,
        version_args=version_args,
        harness_supports=harness_supports,
        broker_implements=broker_implements,
        prompt_delivery=prompt_delivery,
    )


_NATIVE_OAP = _NATIVE_SESSION.model_copy(update={"native_oap": True, "webmcp": True})

_BUILTIN_DESCRIPTORS = (
    _descriptor("codex", "Codex", "codex"),
    _descriptor("claude", "Claude Code", "claude"),
    _descriptor("gemini", "Gemini CLI", "gemini"),
    _descriptor("opencode", "OpenCode", "opencode"),
    _descriptor("goose", "Goose", "goose"),
    _descriptor(
        "loro",
        "Loro",
        "loro",
        harness_supports=_NATIVE_OAP,
        broker_implements=_BROKER_AAIS_NATIVE,
        # stdin carries the AAIS approval channel, and neither CLI reads a prompt file yet.
        prompt_delivery=PromptDelivery.ARGV,
    ),
    _descriptor(
        "magagent",
        "MagAgent",
        "magent",
        harness_supports=_NATIVE_OAP,
        broker_implements=_BROKER_AAIS_NATIVE,
        # stdin carries the AAIS approval channel, and neither CLI reads a prompt file yet.
        prompt_delivery=PromptDelivery.ARGV,
    ),
    _descriptor(
        "anton",
        "Anton",
        "anton",
        transports=(TransportKind.TEXT_SUBPROCESS,),
        version_args=("version",),
        harness_supports=_NATIVE_SESSION.model_copy(update={"model_listing": False}),
    ),
    _descriptor(
        "dsh",
        "DeepSeek Harness",
        "dsh",
        harness_supports=_NATIVE_SESSION.model_copy(update={"attachments": False}),
        # The headless profile takes its task only from the command line.
        prompt_delivery=PromptDelivery.ARGV,
    ),
    # Print mode reads stdin only as stream-json; plain-text stdin is unconfirmed, so argv stays.
    _descriptor("agy", "Antigravity CLI", "agy", prompt_delivery=PromptDelivery.ARGV),
    _descriptor("pi", "Pi Coding Agent", "pi"),
    _descriptor("prime-agent", "Prime Agent", "prime-agent"),
    _descriptor("openclaw", "OpenClaw", "openclaw", prompt_delivery=PromptDelivery.FILE),
    _descriptor("kimi", "Kimi Code CLI", "kimi"),
)
