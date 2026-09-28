from __future__ import annotations

import importlib.util
import json
import shutil
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
_spec = importlib.util.spec_from_file_location(
    "check_release_metadata", ROOT / "scripts" / "check_release_metadata.py"
)
assert _spec and _spec.loader
checker = importlib.util.module_from_spec(_spec)
sys.modules[_spec.name] = checker
_spec.loader.exec_module(checker)


def _copy_metadata(target: Path) -> Path:
    for relative in ("pyproject.toml", "README.md", "CHANGELOG.md", "src/merced_ai/__init__.py"):
        (target / relative).parent.mkdir(parents=True, exist_ok=True)
        shutil.copy(ROOT / relative, target / relative)
    (target / "docs").mkdir(exist_ok=True)
    for path in (ROOT / "docs").iterdir():
        if path.suffix in {".md", ".json"}:
            shutil.copy(path, target / "docs" / path.name)
    return target


def test_repository_release_metadata_agrees() -> None:
    report = checker.check(ROOT)
    assert report.issues == []
    assert len(report.checked) >= 9


def test_detects_the_drift_fixed_for_0_7_0(tmp_path: Path) -> None:
    root = _copy_metadata(tmp_path)
    version = checker.check(root).version
    conformance = root / "docs" / "oap-conformance.json"
    payload = json.loads(conformance.read_text(encoding="utf-8"))
    payload["implementation_version"] = "0.4.0"
    conformance.write_text(json.dumps(payload), encoding="utf-8")
    readme = root / "README.md"
    readme.write_text(
        readme.read_text(encoding="utf-8").replace(f"Current release: {version}", "Release:")
        + "\nMerced AI `0.4.0` uses the OAP support library.\n",
        encoding="utf-8",
    )
    changelog = root / "CHANGELOG.md"
    text = changelog.read_text(encoding="utf-8")
    changelog.write_text(
        text.replace(f"## {version} — ", f"## {version} — Unreleased\n\n## Old ", 1)
        # Two headings, so the repeat is detected whether or not the real changelog still has
        # an Unreleased section (it does not right after a release).
        + "\n## Unreleased\n\n## Unreleased\n",
        encoding="utf-8",
    )
    index = root / "docs" / "README.md"
    index.write_text(
        index.read_text(encoding="utf-8").replace(f"(RELEASE_NOTES_{version}.md)", "(missing.md)"),
        encoding="utf-8",
    )

    issues = "\n".join(checker.check(root).issues)

    assert "oap-conformance.json implementation_version: found '0.4.0'" in issues
    assert "current-release line" in issues
    assert "names Merced AI 0.4.0" in issues
    assert "instead of a release date" in issues
    assert "repeats the heading 'Unreleased'" in issues
    assert f"does not link RELEASE_NOTES_{version}.md" in issues


def test_strict_on_tags_and_advisory_on_branches(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    root = _copy_metadata(tmp_path)
    monkeypatch.delenv("GITHUB_REF", raising=False)
    assert checker.main(["--root", str(root)]) == 0
    assert "Release metadata OK" in capsys.readouterr().out
    monkeypatch.setenv("GITHUB_REF", "refs/tags/v9.9.9")
    assert checker.main(["--root", str(root), "--json"]) == 1
    report = json.loads(capsys.readouterr().out)
    assert report["strict"] is True and report["ok"] is False
    assert checker.main(["--root", str(root)]) == 1
    assert "git tag: found '9.9.9'" in capsys.readouterr().out
