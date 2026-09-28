from __future__ import annotations

from pathlib import Path

import pytest
import typer.rich_utils


@pytest.fixture(autouse=True)
def isolated_home(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("MERCED_AI_HOME", str(tmp_path / "merced-home"))
    # Never start a real ACP agent installed on the developer machine; ACP tests opt back in.
    monkeypatch.setenv("MERCED_AI_ACP", "0")
    # Typer reads GITHUB_ACTIONS, FORCE_COLOR, PY_COLORS, and TERMINAL_WIDTH once at import and
    # then forces a colored, fixed-width terminal. Help output in tests must depend only on the
    # environment each test passes (COLUMNS, NO_COLOR), not on the CI runner.
    monkeypatch.setattr(typer.rich_utils, "FORCE_TERMINAL", None)
    monkeypatch.setattr(typer.rich_utils, "MAX_WIDTH", None)
    for name in ("FORCE_COLOR", "PY_COLORS", "TERMINAL_WIDTH"):  # Rich reads these at runtime.
        monkeypatch.delenv(name, raising=False)


@pytest.fixture
def workspace(tmp_path: Path) -> Path:
    path = tmp_path / "workspace"
    path.mkdir()
    return path


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"
