"""Merced AI command-line interface.

Commands live in one module per family (``workspace``, ``catalog``, ``chat``, ``group``,
``inbox``, ``evals``); importing them registers the commands on the Typer applications in
``apps``. Import order is registration order, which is the order ``--help`` lists commands.
"""

# isort: skip_file
from __future__ import annotations

from merced_ai.application import execute
from merced_ai.cli import common as _common  # noqa: F401 - registers the root callback
from merced_ai.cli import workspace as _workspace  # noqa: F401 - init, status, doctor, ui, acp
from merced_ai.cli import catalog as _catalog  # noqa: F401 - profile, bot
from merced_ai.cli import chat as _chat  # noqa: F401 - ask, chat, session
from merced_ai.cli import group as _group  # noqa: F401
from merced_ai.cli import inbox as _inbox  # noqa: F401
from merced_ai.cli import evals as _evals  # noqa: F401
from merced_ai.cli.apps import app

__all__ = ["app", "execute"]
