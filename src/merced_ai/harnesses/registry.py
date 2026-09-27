"""Adapter registry: built-in harness specs plus adapters installed as entry-point plugins."""

from __future__ import annotations

import os
import threading
import time
from collections.abc import Iterable
from dataclasses import replace
from importlib import metadata
from pathlib import Path
from typing import Any

from merced_ai.harnesses.acp import AcpHarnessAdapter
from merced_ai.harnesses.adapters.command import CommandHarnessAdapter
from merced_ai.harnesses.api import ADAPTER_API_VERSION, ENTRY_POINT_GROUP, HarnessSpec
from merced_ai.harnesses.base import HarnessAdapter
from merced_ai.harnesses.builtin import ACP_LAUNCHES, BUILTIN_SPECS, acp_enabled
from merced_ai.models import HarnessDescriptor, HarnessProbe

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
        # Plugins that failed to load, as (entry point name, reason); shown by `harness list`.
        self.plugin_errors: list[tuple[str, str]] = []
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


def adapter_from_plugin(value: Any, origin: str) -> HarnessAdapter:
    """Accept a HarnessSpec, a zero-argument factory returning one, or a full adapter object."""
    if (
        callable(value)
        and not isinstance(value, HarnessSpec)
        and not isinstance(value, HarnessAdapter)
    ):
        value = value()
    if isinstance(value, HarnessSpec):
        if value.api_version != ADAPTER_API_VERSION:
            raise ValueError(
                f"adapter API version {value.api_version} is not supported "
                f"(this Merced AI implements version {ADAPTER_API_VERSION})"
            )
        return CommandHarnessAdapter(replace(value, origin=origin))
    if isinstance(value, HarnessAdapter):
        return value
    raise TypeError("entry point must provide a HarnessSpec or a HarnessAdapter")


def load_plugins(registry: HarnessRegistry) -> None:
    """Register adapters from the ``merced_ai.harnesses`` entry-point group.

    A plugin cannot replace a built-in or an earlier plugin, and a plugin that fails to load is
    recorded in ``registry.plugin_errors`` instead of breaking the CLI.
    """
    if os.environ.get("MERCED_AI_DISABLE_PLUGINS") == "1":
        return
    cwd = Path.cwd().resolve()
    for entry in metadata.entry_points(group=ENTRY_POINT_GROUP):
        origin = entry.dist.name if entry.dist is not None else entry.value
        if _from_working_directory(entry, cwd):
            # `python -m merced_ai` puts the current directory on sys.path, so a project could
            # otherwise ship a *.dist-info that registers code to run inside the broker.
            registry.plugin_errors.append(
                (entry.name, f"{origin} is installed in the working directory {cwd}; ignored")
            )
            continue
        try:
            registry.register(adapter_from_plugin(entry.load(), origin))
        except Exception as error:  # A broken plugin must not take the broker down.
            registry.plugin_errors.append((entry.name, f"{type(error).__name__}: {error}"))


def builtin_adapter(spec: HarnessSpec) -> HarnessAdapter:
    harness_id = spec.descriptor.id
    if acp_enabled(harness_id):
        return AcpHarnessAdapter(spec, ACP_LAUNCHES[harness_id][0])
    return CommandHarnessAdapter(spec)


def _from_working_directory(entry: Any, cwd: Path) -> bool:
    location = getattr(entry.dist, "_path", None)
    if location is None:
        return False
    try:
        return Path(location).resolve().parent == cwd
    except OSError:
        return True


def default_registry() -> HarnessRegistry:
    registry = HarnessRegistry(builtin_adapter(spec) for spec in BUILTIN_SPECS)
    load_plugins(registry)
    return registry
