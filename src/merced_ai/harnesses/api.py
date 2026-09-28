"""Public adapter plugin API (version 1).

A harness adapter is described by a :class:`HarnessSpec`: a descriptor, a function that builds
one noninteractive invocation, how the profile is projected, and how stdout becomes a reply.
Merced AI's generic subprocess runner does the rest: executable discovery, private temp files,
the argv size guard, bounded capture, cancellation, the AAIS approval channel, and error
reporting. All built-in adapters are specs (see ``merced_ai.harnesses.builtin``).

Third-party adapters are installed packages that declare an entry point in the
``merced_ai.harnesses`` group. The entry point may load a ``HarnessSpec``, a zero-argument
callable returning one, or a complete adapter object implementing
:class:`~merced_ai.harnesses.base.HarnessAdapter`::

    [project.entry-points."merced_ai.harnesses"]
    myharness = "my_package.merced:SPEC"

Validate a plugin with ``merced_ai.testing.contract.check_harness_spec``.
"""

from __future__ import annotations

import os
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

from merced_ai.harnesses.output import NormalizedOutput
from merced_ai.models import (
    HarnessDescriptor,
    ProfileRecord,
    ProjectionAdjustment,
    PromptDelivery,
    RunRequest,
)

ADAPTER_API_VERSION = 1
ENTRY_POINT_GROUP = "merced_ai.harnesses"

# How the OAP profile reaches the harness:
# - "native": the harness loads the project profile by name when it is discoverable, otherwise
#   it falls back to "prefixed".
# - "system_prompt": the profile goes through a system-prompt flag or file.
# - "prefixed": the profile is delimited context at the top of the prompt.
ProjectionStyle = Literal["native", "system_prompt", "prefixed"]


@dataclass(frozen=True)
class HarnessInvocation:
    """One harness process: its argv, how the prompt travels, and the stdin payload."""

    argv: list[str]
    prompt_delivery: PromptDelivery
    stdin: str | None = None


def executable_version(argv: list[str], *, timeout: float = 10.0) -> tuple[int, int, int] | None:
    """The first ``X.Y.Z`` in a ``--version`` command's output (never raises)."""
    import re
    import subprocess

    try:
        completed = subprocess.run(
            argv,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
            env={**os.environ, "NO_COLOR": "1", "TERM": "dumb"},
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    match = re.search(r"(\d+)\.(\d+)\.(\d+)", completed.stdout + completed.stderr)
    return (int(match[1]), int(match[2]), int(match[3])) if match else None


def help_flags(
    argv: list[str], wanted: tuple[str, ...], *, timeout: float = 10.0
) -> frozenset[str]:
    """Which of ``wanted`` flags appear in a command's ``--help`` output (never raises)."""
    import subprocess

    try:
        completed = subprocess.run(
            argv,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
            env={**os.environ, "COLUMNS": "400", "NO_COLOR": "1", "TERM": "dumb"},
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return frozenset()
    text = completed.stdout + completed.stderr
    return frozenset(flag for flag in wanted if flag in text)


def write_private(path: Path, content: str) -> Path:
    """Write a file readable only by the current user (mode 0600 on POSIX)."""
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8", newline="") as handle:
        handle.write(content)
    return path


def prefixed_prompt(system_prompt: str, prompt: str) -> str:
    return (
        f"{system_prompt}\n\n"
        "The surrounding harness instructions and permission policy remain authoritative.\n\n"
        f"User request:\n{prompt}"
    )


# Project profile directories each native-OAP harness discovers by itself.
NATIVE_PROFILE_DIRS = {
    "loro": (".agents", ".loro/agents"),
    "magagent": (".agents", ".magent/agents"),
}


def _project_dir(profile: ProfileRecord) -> str | None:
    parent = profile.path.parent
    if parent.name == ".agents":
        return ".agents"
    if parent.name == "agents" and parent.parent.name in {".loro", ".magent"}:
        return f"{parent.parent.name}/agents"
    return None


def native_profile_visible(profile: ProfileRecord, harness_id: str | None = None) -> bool:
    """True when ``harness_id`` discovers this project profile itself, so it can be passed by
    name. Loro reads .agents/ and .loro/agents/; MagAgent reads .agents/ and .magent/agents/."""
    if profile.source != "project" or profile.conflict:
        return False
    location = _project_dir(profile)
    if location is None:
        return False
    if harness_id is None:
        return True
    return location in NATIVE_PROFILE_DIRS.get(harness_id, ())


def profile_provider(profile: ProfileRecord) -> str | None:
    provider = profile.document.get("spec", {}).get("model", {}).get("provider")
    return provider if isinstance(provider, str) and provider else None


def qualified_model(profile: ProfileRecord, model: str) -> str:
    """``provider/model`` for multi-provider harnesses, unless the ID is already qualified."""
    provider = profile_provider(profile)
    return f"{provider}/{model}" if provider and "/" not in model else model


@dataclass(frozen=True)
class InvocationContext:
    """Everything a spec's ``build`` function needs to construct one invocation."""

    request: RunRequest
    executable: Path
    scratch: Path
    # Optional CLI features detected on this installed version (see HarnessSpec.features).
    features: frozenset[str] = frozenset()

    @property
    def profile(self) -> ProfileRecord:
        return self.request.profile

    @property
    def prompt(self) -> str:
        return self.request.prompt

    @property
    def system_prompt(self) -> str:
        return self.request.projection.system_prompt

    @property
    def prefixed_prompt(self) -> str:
        """The profile as delimited context followed by the user request."""
        return prefixed_prompt(self.system_prompt, self.prompt)

    @property
    def model(self) -> str | None:
        return self.request.projection.model

    @property
    def workspace(self) -> Path:
        return self.request.workspace

    @property
    def native_profile(self) -> bool:
        return native_profile_visible(self.profile, self.request.harness_id)

    def permission(self, name: str) -> str | None:
        value = self.profile.document.get("spec", {}).get("permissions", {}).get(name)
        return value if isinstance(value, str) else None

    @property
    def edit_denied(self) -> bool:
        return self.permission("edit") == "deny"

    @property
    def shell_denied(self) -> bool:
        return self.permission("shell") == "deny"

    def private_file(self, name: str, content: str) -> Path:
        """A 0600 file in this run's private temp directory, removed after the run."""
        return write_private(self.scratch / name, content)


@dataclass(frozen=True)
class HarnessSpec:
    """Declarative definition of a subprocess harness adapter."""

    descriptor: HarnessDescriptor
    build: Callable[[InvocationContext], HarnessInvocation]
    projection: ProjectionStyle = "prefixed"
    # Providers whose model IDs the harness accepts; None means it accepts any provider.
    compatible_providers: frozenset[str] | None = None
    # "json", "text", or a function from stdout to (reply, payload, native session ID).
    output: Literal["json", "text"] | Callable[[str], NormalizedOutput] = "text"
    # Extra environment variables for the child process.
    env: Callable[[InvocationContext], dict[str, str]] | None = None
    # The harness speaks AAIS 1.0 envelopes on stdout/stdin (stdin is then not a prompt channel).
    aais_control: bool = False
    # Detects optional CLI features of the installed executable (for example a newer
    # `--prompt-file` flag) so `build` can use them and fall back on older versions. Results are
    # cached per executable path and modification time.
    features: Callable[[Path], frozenset[str]] | None = None
    # Extra projection-report lines when the profile is passed natively (by name), given the
    # profile and the detected features; for example how the harness applies a permission.
    native_adjustments: (
        Callable[[ProfileRecord, frozenset[str]], tuple[ProjectionAdjustment, ...]] | None
    ) = None
    api_version: int = ADAPTER_API_VERSION
    # Where the adapter came from: "builtin" or the distribution that provided the entry point.
    origin: str = field(default="builtin", compare=False)
