"""Use-case layer shared by the CLI and future local web UI."""

from __future__ import annotations

from pathlib import Path

from merced_ai.bots import resolve_bot
from merced_ai.harnesses.registry import HarnessRegistry, default_registry
from merced_ai.models import BotBinding, ProfileProjection, ProfileRecord, RunRequest, RunResult
from merced_ai.profiles import resolve_profile


class RoutingError(RuntimeError):
    pass


class PreparedRun:
    def __init__(
        self,
        bot: BotBinding,
        profile: ProfileRecord,
        projection: ProfileProjection,
        request: RunRequest,
    ) -> None:
        self.bot = bot
        self.profile = profile
        self.projection = projection
        self.request = request


def prepare_run(
    bot_name: str,
    prompt: str,
    workspace: Path,
    *,
    harness_override: str | None = None,
    registry: HarnessRegistry | None = None,
) -> PreparedRun:
    workspace = workspace.resolve()
    registry = registry or default_registry()
    bot = resolve_bot(bot_name, workspace)
    profile = resolve_profile(bot.profile, workspace)
    harness_id = _route_harness(bot, profile, registry, harness_override)
    adapter = registry.get(harness_id)
    projection = adapter.project_profile(profile)
    request = RunRequest(
        harness_id=harness_id,
        prompt=prompt,
        workspace=workspace,
        profile=profile,
        projection=projection,
    )
    return PreparedRun(bot, profile, projection, request)


def execute(prepared: PreparedRun, registry: HarnessRegistry | None = None) -> RunResult:
    registry = registry or default_registry()
    return registry.get(prepared.request.harness_id).run(prepared.request)


def _route_harness(
    bot: BotBinding,
    profile: ProfileRecord,
    registry: HarnessRegistry,
    override: str | None,
) -> str:
    candidates = (override,) if override else (bot.harness.preferred, *bot.harness.fallbacks)
    failures: list[str] = []
    for harness_id in candidates:
        if harness_id is None:
            continue
        try:
            adapter = registry.get(harness_id)
        except KeyError:
            failures.append(f"{harness_id}: unknown")
            continue
        probe = adapter.probe()
        if probe.path is not None and probe.status.value != "probe_failed":
            try:
                adapter.project_profile(profile)
            except (NotImplementedError, ValueError):
                failures.append(f"{harness_id}: execution unsupported")
                continue
            return harness_id
        failures.append(f"{harness_id}: {probe.status.value}")
    raise RoutingError("no usable harness: " + ", ".join(failures))
