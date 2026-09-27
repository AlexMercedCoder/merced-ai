"""Use-case layer shared by the CLI and future local web UI."""

from __future__ import annotations

from pathlib import Path

from merced_ai.bots import resolve_bot
from merced_ai.harnesses.registry import HarnessRegistry, default_registry
from merced_ai.models import (
    BotBinding,
    ProfileProjection,
    ProfileRecord,
    RunRequest,
    RunResult,
    SessionParticipant,
    SessionRecord,
)
from merced_ai.profiles import resolve_profile
from merced_ai.sessions import select_participants, transcript_prompt


class RoutingError(RuntimeError):
    pass


def is_write_capable(profile: ProfileRecord) -> bool:
    """A profile that does not deny both editing and shell access may change the workspace."""
    permissions = profile.document.get("spec", {}).get("permissions", {})
    return permissions.get("edit") != "deny" or permissions.get("shell") != "deny"


def shared_workspace_writers(prepared: tuple[PreparedRun, ...]) -> tuple[str, ...]:
    """Bots that could write to a workspace another selected write-capable bot also uses.

    Returns their names in participant order, or an empty tuple when no two write-capable
    participants share a workspace. Group turns serialize these bots by default.
    """
    by_workspace: dict[Path, list[str]] = {}
    for item in prepared:
        if is_write_capable(item.profile):
            by_workspace.setdefault(item.request.workspace, []).append(item.bot.name)
    shared = {name for names in by_workspace.values() if len(names) > 1 for name in names}
    return tuple(item.bot.name for item in prepared if item.bot.name in shared)


def write_serialization_message(writers: tuple[str, ...]) -> str:
    names = ", ".join(writers[:-1]) + f" and {writers[-1]}" if len(writers) > 1 else writers[0]
    quantifier = "both" if len(writers) == 2 else "all"
    return (
        f"{names} can {quantifier} change this workspace, so their turns run one at a time. "
        "Allow concurrent writes only if they will not edit the same files."
    )


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
    harness_id = _route_harness(bot, profile, registry, harness_override, workspace)
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


def participant_from_run(prepared: PreparedRun) -> SessionParticipant:
    return SessionParticipant(
        bot_name=prepared.bot.name,
        harness_id=prepared.request.harness_id,
        profile_name=prepared.profile.name,
        profile_revision=prepared.profile.revision,
        profile_digest=prepared.profile.profile_digest,
        spec_digest=prepared.profile.spec_digest,
    )


def prepare_group(
    bot_names: tuple[str, ...],
    workspace: Path,
    *,
    registry: HarnessRegistry | None = None,
) -> tuple[PreparedRun, ...]:
    """Resolve every participant before a group session is written."""
    if len(bot_names) < 2:
        raise ValueError("a group conversation requires at least two bots")
    if len(bot_names) > 12:
        raise ValueError("a group conversation supports at most twelve bots")
    if len(set(bot_names)) != len(bot_names):
        raise ValueError("group bot names must be unique")
    registry = registry or default_registry()
    return tuple(
        prepare_run(name, "Start the group conversation.", workspace, registry=registry)
        for name in bot_names
    )


def prepare_group_turn(
    session: SessionRecord,
    prompt: str,
    workspace: Path,
    *,
    dispatch: str | None = None,
    registry: HarnessRegistry | None = None,
) -> tuple[PreparedRun, ...]:
    """Prepare isolated prompts for the selected group participants."""
    registry = registry or default_registry()
    selected = select_participants(session, prompt, dispatch=dispatch)
    prepared = []
    for item in selected:
        run = prepare_run(
            item.bot_name,
            transcript_prompt(session, prompt, recipient=item.bot_name),
            workspace,
            harness_override=item.harness_id,
            registry=registry,
        )
        if run.profile.spec_digest != item.spec_digest or run.profile.name != item.profile_name:
            raise RoutingError(
                f"Profile {item.profile_name!r} changed since this conversation was pinned. "
                "Review the updated profile and start or derive a new conversation to use it."
            )
        prepared.append(run)
    return tuple(prepared)


def _route_harness(
    bot: BotBinding,
    profile: ProfileRecord,
    registry: HarnessRegistry,
    override: str | None,
    workspace: Path,
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
        probe = registry.cached_probe(harness_id, workspace)
        if probe.path is not None and probe.status.value != "probe_failed":
            if bot.harness.requires_webmcp and not (
                probe.broker_implements.webmcp and probe.capabilities_verified
            ):
                failures.append(f"{harness_id}: WebMCP unsupported or readiness unverified")
                continue
            try:
                adapter.project_profile(profile)
            except (NotImplementedError, ValueError):
                failures.append(f"{harness_id}: execution unsupported")
                continue
            return harness_id
        failures.append(f"{harness_id}: {probe.status.value}")
    raise RoutingError("no usable harness: " + ", ".join(failures))
