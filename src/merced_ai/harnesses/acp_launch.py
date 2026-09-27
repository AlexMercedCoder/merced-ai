"""ACP launch description, kept free of runtime imports so specs can reference it."""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class AcpLaunch:
    """How to start a harness as an ACP agent."""

    # Executable names for a dedicated ACP binary (e.g. ``claude-agent-acp``); empty to reuse
    # the harness's own executable.
    executable_names: tuple[str, ...] = ()
    args: tuple[str, ...] = ()
    env: dict[str, str] = field(default_factory=dict)
    # Whether the agent implements session/load, as verified live; drives the resume claim.
    resumes: bool = False
