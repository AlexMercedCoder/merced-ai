"""Filesystem locations with an environment override for tests and automation."""

from __future__ import annotations

import os
from pathlib import Path


def user_root() -> Path:
    override = os.environ.get("MERCED_AI_HOME")
    if override:
        return Path(override).expanduser().resolve()
    if os.name == "nt":
        base = Path(os.environ.get("APPDATA", Path.home() / "AppData" / "Roaming"))
        return base / "merced-ai"
    return Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config")) / "merced-ai"


def project_root(workspace: Path) -> Path:
    return workspace.resolve() / ".merced-ai"


def ensure_project_layout(workspace: Path) -> Path:
    root = project_root(workspace)
    for child in (root, root / "bots", root / "sessions", workspace.resolve() / ".agents"):
        child.mkdir(parents=True, exist_ok=True)
    return root


def ensure_user_layout() -> Path:
    root = user_root()
    for child in (root, root / "bots", root / "agents", root / "sessions"):
        child.mkdir(parents=True, exist_ok=True)
    return root
