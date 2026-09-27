"""Cached, progressively refreshed harness detection for the web UI."""

from __future__ import annotations

import json
import threading
import time
from pathlib import Path
from typing import Any
from uuid import uuid4

from merced_ai.harnesses.registry import HarnessRegistry
from merced_ai.models import HarnessDescriptor, HarnessProbe

# Schema 2 split capabilities into harness_supports and broker_implements; schema 3 added
# prompt_delivery.
HARNESS_CACHE_SCHEMA = 3
HARNESS_CACHE_MAX_AGE_SECONDS = 300


def detecting_probe(
    descriptor: HarnessDescriptor, previous: dict[str, Any] | None = None
) -> dict[str, Any]:
    payload = dict(previous or {})
    payload.update(
        {
            "harness_id": descriptor.id,
            "status": "detecting",
            "harness_supports": descriptor.harness_supports.model_dump(mode="json"),
            "broker_implements": descriptor.broker_implements.model_dump(mode="json"),
            "capabilities": descriptor.broker_implements.model_dump(mode="json"),
            "prompt_delivery": descriptor.prompt_delivery.value,
            "capabilities_verified": False,
            "warnings": ["Bounded executable detection is running in the background."],
            "duration_ms": 0,
        }
    )
    payload.setdefault("path", None)
    payload.setdefault("version", None)
    payload.setdefault("transport", None)
    return payload


class HarnessProbeCache:
    """Serves the last known probe results and refreshes them on a background thread.

    Bootstrap never probes executables. A refresh probes each harness in turn, publishes each
    result as soon as it is known, and writes a complete snapshot for the next launch.
    """

    def __init__(self, registry: HarnessRegistry, cache_path: Path) -> None:
        self.registry = registry
        self.descriptors = registry.descriptors()
        self.cache_path = cache_path
        self._lock = threading.Lock()
        self._refreshing = False
        self._updated_at: float | None = None
        self._cached = False
        self._probes: dict[str, dict[str, Any]] = {
            item.id: detecting_probe(item) for item in self.descriptors
        }
        self._load_snapshot()

    def _load_snapshot(self) -> None:
        try:
            cached = json.loads(self.cache_path.read_text(encoding="utf-8"))
            if cached.get("schema") != HARNESS_CACHE_SCHEMA:
                return
            probes = {
                item.harness_id: item.model_dump(mode="json")
                for item in (HarnessProbe.model_validate(value) for value in cached["harnesses"])
            }
            if set(probes) == {item.id for item in self.descriptors}:
                self._probes = probes
                self._updated_at = float(cached["updated_at"])
                self._cached = True
        except (OSError, ValueError, TypeError, KeyError):
            pass

    def payload(self) -> dict[str, Any]:
        with self._lock:
            updated_at = self._updated_at
            return {
                "harnesses": [dict(self._probes[item.id]) for item in self.descriptors],
                "refreshing": self._refreshing,
                "updated_at": updated_at,
                "cached": self._cached,
                "stale": updated_at is None
                or time.time() - updated_at > HARNESS_CACHE_MAX_AGE_SECONDS,
            }

    def path_for(self, harness_id: str) -> str | None:
        with self._lock:
            value = self._probes.get(harness_id, {}).get("path")
        return str(value) if value else None

    def start_refresh(self) -> bool:
        with self._lock:
            if self._refreshing:
                return False
            self._refreshing = True
            self._cached = False
            for descriptor in self.descriptors:
                self._probes[descriptor.id] = detecting_probe(
                    descriptor, self._probes.get(descriptor.id)
                )
        threading.Thread(target=self._refresh, daemon=True, name="merced-ai-probes").start()
        return True

    def _refresh(self) -> None:
        try:
            for descriptor in self.descriptors:
                result = self.registry.get(descriptor.id).probe().model_dump(mode="json")
                with self._lock:
                    self._probes[descriptor.id] = result
            completed_at = time.time()
            with self._lock:
                self._updated_at = completed_at
                self._cached = False
                snapshot = {
                    "schema": HARNESS_CACHE_SCHEMA,
                    "updated_at": completed_at,
                    "harnesses": [dict(self._probes[item.id]) for item in self.descriptors],
                }
            try:
                temporary = self.cache_path.with_name(f".{self.cache_path.name}.{uuid4().hex}.tmp")
                temporary.write_text(json.dumps(snapshot, indent=2), encoding="utf-8")
                temporary.replace(self.cache_path)
            except OSError:
                pass
        finally:
            with self._lock:
                self._refreshing = False
