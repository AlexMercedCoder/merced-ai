"""Locate the pinned upstream OAP and AGS fixture checkouts used by conformance tests.

CI sets ``OAP_FIXTURE_REPO`` and ``AGS_FIXTURE_REPO`` explicitly. A developer checkout usually has
the spec repositories cloned next to this one, so the sibling directory is used when the variable
is unset. When neither exists, the fixture-driven tests are skipped with a reason that says how to
enable them, instead of failing with a missing-file error on a fresh clone.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[1]


def locate_fixture_repo(env_var: str, sibling: str, marker: str) -> Path | None:
    """Return the fixture checkout, or None when it is not available locally.

    An explicitly configured path that does not contain ``marker`` is a configuration error, so it
    raises instead of silently skipping the conformance suite.
    """
    configured = os.environ.get(env_var)
    if configured:
        path = Path(configured).expanduser()
        if not (path / marker).exists():
            raise RuntimeError(
                f"{env_var}={configured!r} does not contain {marker!r}; point it at a checkout of "
                f"{sibling} or unset it to use the sibling clone."
            )
        return path
    candidate = _REPO_ROOT.parent / sibling
    return candidate if (candidate / marker).exists() else None


def missing_reason(env_var: str, sibling: str) -> str:
    return (
        f"{sibling} fixtures not found: set {env_var} to a checkout of {sibling} "
        f"or clone it next to this repository ({_REPO_ROOT.parent / sibling})."
    )


def fixture_params(
    repo: Path | None, relative: str, pattern: str, *, env_var: str, sibling: str
) -> list[Any]:
    """Parametrize over fixture files, or yield one explicit skip when the repo is missing."""
    if repo is None:
        return [
            pytest.param(
                None,
                id="fixtures-unavailable",
                marks=pytest.mark.skip(reason=missing_reason(env_var, sibling)),
            )
        ]
    paths = sorted((repo / relative).glob(pattern))
    if not paths:
        raise RuntimeError(f"No fixtures match {relative}/{pattern} in {repo}")
    return [pytest.param(path, id=path.name) for path in paths]
