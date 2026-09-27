"""Check that every place naming Merced AI's version agrees with the package version.

The check reads the version from ``pyproject.toml`` and compares it with:

- ``src/merced_ai/__init__.py`` ``__version__``;
- the ``implementation_version`` of every ``docs/*-conformance.json``;
- the README "Current release: X.Y.Z" line, and any other "Merced AI `X.Y.Z`" mention;
- the newest released ``CHANGELOG.md`` heading, which must carry a date rather than
  "Unreleased", with no heading repeated;
- ``docs/RELEASE_NOTES_<version>.md``, which must exist and be linked from ``docs/README.md``;
- on tag builds, the tag name itself.

It is strict on tag builds (``GITHUB_REF`` starts with ``refs/tags/``) or with ``--strict``, and
advisory otherwise, so an in-progress version bump on a branch does not block work. ``--json``
prints a machine-readable report.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import tomllib
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
VERSION = re.compile(r"^\d+\.\d+\.\d+$")
CHANGELOG_HEADING = re.compile(r"^## (?P<title>.+?)\s*$", re.MULTILINE)
RELEASED_HEADING = re.compile(r"^(?P<version>\d+\.\d+\.\d+) — (?P<status>.+)$")
DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


@dataclass
class Report:
    version: str = ""
    issues: list[str] = field(default_factory=list)
    checked: list[str] = field(default_factory=list)

    def expect(self, label: str, found: str | None, expected: str) -> None:
        self.checked.append(label)
        if found != expected:
            self.issues.append(f"{label}: found {found!r}, expected {expected!r}")


def check(root: Path = ROOT, *, tag: str | None = None) -> Report:
    report = Report()
    project = tomllib.loads((root / "pyproject.toml").read_text(encoding="utf-8"))
    version = str(project["project"]["version"])
    report.version = version
    if VERSION.match(version) is None:
        report.issues.append(f"pyproject.toml version {version!r} is not X.Y.Z")

    init = (root / "src" / "merced_ai" / "__init__.py").read_text(encoding="utf-8")
    match = re.search(r'^__version__ = "([^"]+)"', init, re.MULTILINE)
    report.expect(
        "src/merced_ai/__init__.py __version__", match.group(1) if match else None, version
    )

    for path in sorted((root / "docs").glob("*-conformance.json")):
        payload = json.loads(path.read_text(encoding="utf-8"))
        report.expect(
            f"docs/{path.name} implementation_version",
            payload.get("implementation_version"),
            version,
        )

    readme = (root / "README.md").read_text(encoding="utf-8")
    current = re.search(r"^Current release: (\d+\.\d+\.\d+)\b", readme, re.MULTILINE)
    report.expect("README.md current-release line", current.group(1) if current else None, version)
    report.checked.append("README.md stale version mentions")
    for stale in re.finditer(r"Merced AI `(\d+\.\d+\.\d+)`", readme):
        if stale.group(1) != version:
            report.issues.append(f"README.md names Merced AI {stale.group(1)}, not {version}")

    changelog = (root / "CHANGELOG.md").read_text(encoding="utf-8")
    headings = [item.group("title") for item in CHANGELOG_HEADING.finditer(changelog)]
    report.checked.append("CHANGELOG.md duplicate headings")
    for title, count in Counter(headings).items():
        if count > 1:
            report.issues.append(f"CHANGELOG.md repeats the heading {title!r} {count} times")
    released = [RELEASED_HEADING.match(title) for title in headings]
    top = next((item for item in released if item is not None), None)
    report.expect(
        "CHANGELOG.md newest released heading", top.group("version") if top else None, version
    )
    if top is not None:
        report.checked.append("CHANGELOG.md newest released heading date")
        if DATE.match(top.group("status")) is None:
            report.issues.append(
                f"CHANGELOG.md heading for {top.group('version')} says "
                f"{top.group('status')!r} instead of a release date"
            )

    notes = f"RELEASE_NOTES_{version}.md"
    report.checked.append(f"docs/{notes} exists")
    if not (root / "docs" / notes).exists():
        report.issues.append(f"docs/{notes} is missing")
    report.checked.append(f"docs/README.md links {notes}")
    if f"({notes})" not in (root / "docs" / "README.md").read_text(encoding="utf-8"):
        report.issues.append(f"docs/README.md does not link {notes}")

    if tag:
        report.expect("git tag", tag.removeprefix("v"), version)
    return report


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Check that release metadata agrees with the package version.",
        epilog=(
            "Examples:\n"
            "  python scripts/check_release_metadata.py\n"
            "  python scripts/check_release_metadata.py --strict --json"
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--strict", action="store_true", help="Exit 1 on any drift.")
    parser.add_argument("--json", action="store_true", help="Print a JSON report.")
    parser.add_argument("--root", type=Path, default=ROOT, help=argparse.SUPPRESS)
    args = parser.parse_args(argv)

    ref = os.environ.get("GITHUB_REF", "")
    tag = ref.removeprefix("refs/tags/") if ref.startswith("refs/tags/") else None
    strict = args.strict or tag is not None
    report = check(args.root, tag=tag)
    failed = bool(report.issues)

    if args.json:
        print(
            json.dumps(
                {
                    "version": report.version,
                    "strict": strict,
                    "ok": not failed,
                    "checked": report.checked,
                    "issues": report.issues,
                },
                indent=2,
            )
        )
    elif failed:
        label = "error" if strict else "warning"
        for issue in report.issues:
            print(f"::{label}::Release metadata drift: {issue}")
        if not strict:
            print("Advisory only on branches; tag builds and --strict fail on this drift.")
    else:
        print(f"Release metadata OK: {len(report.checked)} checks agree on {report.version}.")
    return 1 if failed and strict else 0


if __name__ == "__main__":
    sys.exit(main())
