"""Optional loopback-first web UI over Merced AI's application services.

Requires the ``webui`` extra (FastAPI and Uvicorn).
"""

from __future__ import annotations

from merced_ai.web.app import create_web_app, run_web_ui

__all__ = ["create_web_app", "run_web_ui"]
