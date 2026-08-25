"""Harness-neutral domain records used by the CLI and future application service."""

from __future__ import annotations

from enum import StrEnum
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


class HarnessStatus(StrEnum):
    NOT_INSTALLED = "not_installed"
    INSTALLED = "installed"
    INCOMPATIBLE = "incompatible"
    NEEDS_AUTH = "needs_auth"
    READY = "ready"
    DEGRADED = "degraded"
    PROBE_FAILED = "probe_failed"


class TransportKind(StrEnum):
    NATIVE = "native"
    ACP_STDIO = "acp_stdio"
    STRUCTURED_SUBPROCESS = "structured_subprocess"
    TEXT_SUBPROCESS = "text_subprocess"


class HarnessCapabilities(BaseModel):
    model_config = ConfigDict(frozen=True)

    streaming: bool = False
    resume: bool = False
    approvals: bool = False
    attachments: bool = False
    model_listing: bool = False
    native_oap: bool = False


class HarnessDescriptor(BaseModel):
    model_config = ConfigDict(frozen=True)

    id: str
    name: str
    executable_names: tuple[str, ...]
    transports: tuple[TransportKind, ...]
    version_args: tuple[str, ...] = ("--version",)
    capabilities: HarnessCapabilities = Field(default_factory=HarnessCapabilities)


class HarnessProbe(BaseModel):
    model_config = ConfigDict(frozen=True)

    harness_id: str
    status: HarnessStatus
    path: Path | None = None
    version: str | None = None
    transport: TransportKind | None = None
    capabilities: HarnessCapabilities = Field(default_factory=HarnessCapabilities)
    capabilities_verified: bool = False
    warnings: tuple[str, ...] = ()
    duration_ms: int = 0


class SessionEvent(BaseModel):
    """Small stable event envelope; adapter-native detail belongs in metadata."""

    type: str
    session_id: str
    sequence: int = Field(ge=0)
    payload: dict[str, Any] = Field(default_factory=dict)
    metadata: dict[str, Any] = Field(default_factory=dict)


class ProfileRecord(BaseModel):
    model_config = ConfigDict(frozen=True)

    name: str
    path: Path
    source: Literal["user", "project", "portable"]
    description: str
    revision: int = 0
    profile_digest: str
    spec_digest: str
    document: dict[str, Any]
    warnings: tuple[str, ...] = ()


class BotHarnessPreference(BaseModel):
    preferred: str
    fallbacks: tuple[str, ...] = ()


class BotSessionPreference(BaseModel):
    resume: bool = True


class BotBinding(BaseModel):
    api_version: Literal["merced.ai/v1alpha1"] = "merced.ai/v1alpha1"
    kind: Literal["BotBinding"] = "BotBinding"
    name: str
    profile: str
    harness: BotHarnessPreference
    workspace: str = "."
    session: BotSessionPreference = Field(default_factory=BotSessionPreference)
    source: Literal["user", "project"] = "project"
    path: Path | None = None


class ProjectionAdjustment(BaseModel):
    field: str
    action: Literal["mapped", "narrowed", "substituted", "dropped"]
    reason: str


class ProfileProjection(BaseModel):
    harness_id: str
    support_level: Literal["native", "projected", "degraded", "unsupported"]
    provisional: bool = True
    system_prompt: str
    model: str | None = None
    adjustments: tuple[ProjectionAdjustment, ...] = ()


class RunRequest(BaseModel):
    harness_id: str
    prompt: str
    workspace: Path
    profile: ProfileRecord
    projection: ProfileProjection
    timeout_seconds: int = Field(default=1800, ge=1, le=7200)


class RunResult(BaseModel):
    harness_id: str
    output: str
    exit_code: int
    native_session_id: str | None = None
    raw: dict[str, Any] | None = None
    duration_ms: int


class ConversationTurn(BaseModel):
    role: Literal["user", "assistant"]
    content: str
    bot_name: str | None = None
    harness_id: str | None = None


class SessionParticipant(BaseModel):
    """A bot and its immutable route/profile snapshot within a conversation."""

    bot_name: str
    harness_id: str
    profile_name: str
    profile_revision: int
    profile_digest: str
    spec_digest: str


class SessionRecord(BaseModel):
    id: str
    title: str | None = None
    derived_from: str | None = None
    bot_name: str
    harness_id: str
    workspace: Path
    profile_name: str
    profile_revision: int
    profile_digest: str
    spec_digest: str
    created_at: str
    updated_at: str
    kind: Literal["single", "group"] = "single"
    mode: Literal["mentions", "all", "round_robin"] = "mentions"
    participants: list[SessionParticipant] = Field(default_factory=list)
    turns: list[ConversationTurn] = Field(default_factory=list)

    @model_validator(mode="after")
    def populate_legacy_participant(self) -> SessionRecord:
        """Make pre-group-chat session JSON usable without an on-disk migration."""
        if not self.participants:
            self.participants.append(
                SessionParticipant(
                    bot_name=self.bot_name,
                    harness_id=self.harness_id,
                    profile_name=self.profile_name,
                    profile_revision=self.profile_revision,
                    profile_digest=self.profile_digest,
                    spec_digest=self.spec_digest,
                )
            )
        if len(self.participants) > 1:
            self.kind = "group"
        return self
