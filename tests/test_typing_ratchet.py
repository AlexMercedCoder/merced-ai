"""The mypy ignore list is a backlog that may only shrink."""

from __future__ import annotations

import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

# Modules allowed to skip type checking. Remove entries as they are fixed; never add one.
BASELINE: set[str] = set()


def _ignored_modules() -> set[str]:
    config = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    ignored: set[str] = set()
    for override in config["tool"]["mypy"].get("overrides", []):
        if override.get("ignore_errors"):
            modules = override["module"]
            ignored.update([modules] if isinstance(modules, str) else modules)
    return ignored


def test_mypy_ignore_list_only_shrinks() -> None:
    grown = _ignored_modules() - BASELINE
    assert not grown, f"Do not add modules to the mypy ignore list: {sorted(grown)}"


def test_mypy_ignore_list_names_real_modules() -> None:
    for module in _ignored_modules():
        relative = Path("src", *module.split("."))
        assert (ROOT / relative.with_suffix(".py")).exists() or (ROOT / relative).is_dir(), module
