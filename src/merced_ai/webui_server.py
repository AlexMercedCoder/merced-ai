"""Compatibility entry point for the web UI; the implementation lives in ``merced_ai.web``.

Importing this module never requires the ``webui`` extra; calling into it does.
"""

from __future__ import annotations

import asyncio  # noqa: F401 - kept importable for callers that patch asyncio through this module
from pathlib import Path
from typing import Any

COOKIE_NAME = "merced_ai_ui"
MAX_MESSAGE_CHARS = 100_000
_INSTALL_HINT = 'Install the UI with: pip install "merced-ai[webui]"'


def create_web_app(workspace: Path, access_token: str | None = None, **kwargs: Any) -> Any:
    try:
        from merced_ai.web.app import create_web_app as implementation
    except ImportError as exc:  # pragma: no cover - FastAPI missing
        raise RuntimeError(_INSTALL_HINT) from exc
    return implementation(workspace, access_token, **kwargs)


def run_web_ui(workspace: Path, **kwargs: Any) -> None:
    try:
        from merced_ai.web.app import run_web_ui as implementation
    except ImportError as exc:  # pragma: no cover - FastAPI missing
        raise RuntimeError(_INSTALL_HINT) from exc
    implementation(workspace, **kwargs)


__all__ = ["COOKIE_NAME", "MAX_MESSAGE_CHARS", "create_web_app", "run_web_ui"]
