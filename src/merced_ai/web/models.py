"""Request bodies accepted by the local web API."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field, model_validator

from merced_ai.workspace_context import ContextReference

MAX_MESSAGE_CHARS = 100_000
DispatchMode = Literal["mentions", "all", "round_robin"]


class AuthInput(BaseModel):
    token: str


class AAISDecisionInput(BaseModel):
    request_id: str = Field(min_length=1, max_length=200)
    decision: str = Field(pattern="^(approve|deny|cancel)$")
    scope: str = Field(pattern="^(once|session|persistent)$")
    decision_id: str | None = Field(default=None, max_length=200)


class ProfileInput(BaseModel):
    name: str
    description: str = Field(min_length=1, max_length=500)
    instructions: str = Field(min_length=1, max_length=100_000)
    model_provider: str | None = Field(default=None, max_length=60)
    model_id: str | None = Field(default=None, max_length=200)
    edit_permission: str | None = None
    shell_permission: str | None = None
    scope: str = "project"


class ProfileUpdateInput(BaseModel):
    description: str = Field(min_length=1, max_length=500)
    instructions: str = Field(min_length=1, max_length=100_000)
    model_provider: str | None = Field(default=None, max_length=60)
    model_id: str | None = Field(default=None, max_length=200)
    edit_permission: str | None = None
    shell_permission: str | None = None


class ProfileGenerateInput(BaseModel):
    prompt: str = Field(min_length=1, max_length=20_000)
    name: str | None = Field(default=None, max_length=63)
    harness: str | None = Field(default=None, max_length=60)


class ProfileDocumentInput(BaseModel):
    document: dict[str, Any]
    scope: str = "project"


class BotInput(BaseModel):
    name: str
    profile: str
    harness: str
    fallbacks: list[str] = Field(default_factory=list, max_length=14)
    requires_webmcp: bool = False


class SessionInput(BaseModel):
    bot_name: str | None = None
    bot_names: list[str] = Field(default_factory=list, max_length=12)
    harness: str | None = None
    mode: DispatchMode = "mentions"
    title: str | None = Field(default=None, max_length=120)
    isolation: Literal["shared", "worktree"] = "shared"

    @model_validator(mode="after")
    def validate_participants(self) -> SessionInput:
        names = self.bot_names or ([self.bot_name] if self.bot_name else [])
        if not names:
            raise ValueError("select at least one bot")
        if len(names) != len(set(names)):
            raise ValueError("bot names must be unique")
        return self

    def names(self) -> tuple[str, ...]:
        return tuple(self.bot_names or ([self.bot_name] if self.bot_name else []))


class MessageInput(BaseModel):
    content: str = Field(min_length=1, max_length=MAX_MESSAGE_CHARS)
    approved: bool = False
    dispatch: str | None = Field(default=None, max_length=100)
    context: list[ContextReference] = Field(default_factory=list, max_length=20)
    # Group turns serialize write-capable bots that share a workspace unless this is set.
    allow_concurrent_writes: bool = False


class SessionUpdateInput(BaseModel):
    title: str = Field(min_length=1, max_length=120)
