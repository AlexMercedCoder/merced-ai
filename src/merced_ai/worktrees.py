"""Worktree-per-bot isolation for group rooms.

In a room created with worktree isolation, every write-capable bot works in its own
``git worktree`` of the project, on its own branch, created from the commit that was checked out
when the bot first ran. Bots can then work concurrently without touching each other's files or
the user's working tree. The user compares their changes and applies one bot's diff to the real
workspace, or discards the worktrees.

Worktrees live outside the project (under the Merced AI user directory) so they never show up as
untracked files in the user's repository. Non-git workspaces cannot be isolated; those rooms fall
back to running write-capable bots one at a time.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from merced_ai.paths import user_root
from merced_ai.storage import atomic_write, file_lock

MAX_PATCH_BYTES = 800_000
GIT_TIMEOUT_SECONDS = 60
SAFE_NAME = re.compile(r"^[a-z][a-z0-9-]{0,62}$")


class WorktreeError(RuntimeError):
    pass


def _git(cwd: Path, *args: str, check: bool = True, input_text: str | None = None) -> str:
    env = {**os.environ, "GIT_TERMINAL_PROMPT": "0", "GIT_OPTIONAL_LOCKS": "0"}
    try:
        completed = subprocess.run(
            ["git", *args],
            cwd=cwd,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=GIT_TIMEOUT_SECONDS,
            env=env,
            input=input_text,
            check=False,
        )
    except FileNotFoundError as error:
        raise WorktreeError("git is not installed or not on PATH") from error
    except subprocess.TimeoutExpired as error:
        raise WorktreeError(f"git {args[0]} timed out") from error
    if check and completed.returncode != 0:
        message = (completed.stderr or completed.stdout).strip().splitlines()
        raise WorktreeError(f"git {args[0]} failed: {message[-1] if message else 'error'}")
    return completed.stdout


def git_toplevel(path: Path) -> Path | None:
    try:
        output = _git(path, "rev-parse", "--show-toplevel", check=False).strip()
    except WorktreeError:
        return None
    return Path(output).resolve() if output else None


@dataclass(frozen=True)
class Worktree:
    bot_name: str
    path: Path
    branch: str
    base: str
    created_at: str

    @classmethod
    def from_dict(cls, item: dict[str, str]) -> Worktree:
        return cls(
            item["bot_name"], Path(item["path"]), item["branch"], item["base"], item["created_at"]
        )

    def as_dict(self) -> dict[str, str]:
        return {
            "bot_name": self.bot_name,
            "path": str(self.path),
            "branch": self.branch,
            "base": self.base,
            "created_at": self.created_at,
        }


@dataclass
class WorktreeDiff:
    bot_name: str
    branch: str
    base: str
    files: list[dict[str, Any]]
    patch: str
    truncated: bool

    def as_dict(self) -> dict[str, Any]:
        return {
            "bot_name": self.bot_name,
            "branch": self.branch,
            "base": self.base,
            "files": self.files,
            "insertions": sum(item["insertions"] for item in self.files),
            "deletions": sum(item["deletions"] for item in self.files),
            "patch": self.patch,
            "truncated": self.truncated,
        }


class WorktreeManager:
    """Creates, inspects, applies, and removes the worktrees for one room."""

    def __init__(self, workspace: Path, session_id: str) -> None:
        self.workspace = workspace.resolve()
        if not re.fullmatch(r"session-[0-9a-f]{32}", session_id):
            raise WorktreeError("invalid session identifier")
        self.session_id = session_id
        toplevel = git_toplevel(self.workspace)
        if toplevel is None:
            raise WorktreeError(
                f"{self.workspace} is not inside a git repository, so bots cannot get isolated "
                "worktrees; their turns run one at a time instead."
            )
        self.repository = toplevel
        # Keep the bot on the same subdirectory of the repo as the workspace.
        self.subpath = self.workspace.relative_to(toplevel)
        digest = hashlib.sha256(str(toplevel).encode()).hexdigest()[:12]
        self.root = user_root() / "worktrees" / digest / session_id
        self.index_path = self.root / "worktrees.json"

    def _load(self) -> dict[str, dict[str, str]]:
        if not self.index_path.exists():
            return {}
        return dict(json.loads(self.index_path.read_text(encoding="utf-8")))

    def _save(self, index: dict[str, dict[str, str]]) -> None:
        atomic_write(self.index_path, json.dumps(index, indent=2, sort_keys=True))

    def worktrees(self) -> list[Worktree]:
        return [Worktree.from_dict(item) for item in self._load().values()]

    def get(self, bot_name: str) -> Worktree | None:
        item = self._load().get(bot_name)
        return Worktree.from_dict(item) if item else None

    def branch_name(self, bot_name: str) -> str:
        return f"merced/{self.session_id.removeprefix('session-')[:12]}/{bot_name}"

    def ensure(self, bot_name: str) -> Worktree:
        """The bot's worktree, created from the current HEAD on first use."""
        if not SAFE_NAME.fullmatch(bot_name):
            raise WorktreeError(f"invalid bot name {bot_name!r}")
        with file_lock(self.index_path):
            index = self._load()
            existing = index.get(bot_name)
            if existing and (Path(existing["path"]) / ".git").exists():
                return Worktree.from_dict(existing)
            base = _git(self.repository, "rev-parse", "HEAD").strip()
            path = self.root / bot_name
            branch = self.branch_name(bot_name)
            if path.exists():
                shutil.rmtree(path)
            path.parent.mkdir(parents=True, exist_ok=True)
            _git(self.repository, "worktree", "add", "-B", branch, str(path), base)
            worktree = Worktree(
                bot_name, path, branch, base, datetime.now(UTC).isoformat(timespec="seconds")
            )
            index[bot_name] = worktree.as_dict()
            self._save(index)
            return worktree

    def workspace_for(self, bot_name: str) -> Path:
        """Where the bot should run: the same project subdirectory inside its worktree."""
        return self.ensure(bot_name).path / self.subpath

    def diff(self, bot_name: str) -> WorktreeDiff:
        worktree = self.get(bot_name)
        if worktree is None:
            raise WorktreeError(f"{bot_name} has no worktree in this conversation yet")
        # Stage everything (including new files) in the bot's own index so one diff covers it.
        _git(worktree.path, "add", "-A")
        numstat = _git(worktree.path, "diff", "--cached", "--numstat", worktree.base)
        files = []
        for line in numstat.splitlines():
            added, removed, name = (line.split("\t", 2) + ["", ""])[:3]
            files.append(
                {
                    "path": name,
                    "insertions": int(added) if added.isdigit() else 0,
                    "deletions": int(removed) if removed.isdigit() else 0,
                    "binary": added == "-",
                }
            )
        patch = _git(worktree.path, "diff", "--cached", "--binary", worktree.base)
        truncated = len(patch.encode()) > MAX_PATCH_BYTES
        return WorktreeDiff(
            bot_name,
            worktree.branch,
            worktree.base,
            files,
            patch[:MAX_PATCH_BYTES] if truncated else patch,
            truncated,
        )

    def apply(self, bot_name: str) -> dict[str, Any]:
        """Apply the bot's changes to the real workspace.

        The patch must apply cleanly to the files as they are now (``git apply --check``);
        otherwise nothing is written, so the user's own edits are never overwritten and no
        conflict markers are left behind. ``git apply`` is all-or-nothing.
        """
        worktree = self.get(bot_name)
        if worktree is None:
            raise WorktreeError(f"{bot_name} has no worktree in this conversation yet")
        _git(worktree.path, "add", "-A")
        patch = _git(worktree.path, "diff", "--cached", "--binary", worktree.base)
        if not patch.strip():
            return {"applied": False, "files": [], "message": f"{bot_name} made no changes."}
        check = subprocess.run(
            ["git", "apply", "--check", "-"],
            cwd=self.repository,
            input=patch,
            capture_output=True,
            text=True,
            timeout=GIT_TIMEOUT_SECONDS,
            check=False,
        )
        if check.returncode != 0:
            detail = (check.stderr or check.stdout).strip().splitlines()
            raise WorktreeError(
                f"{bot_name}'s changes do not apply cleanly to your workspace"
                + (f": {detail[-1]}" if detail else "")
                + ". Commit or stash your own edits to those files, or apply by hand from "
                f"branch {worktree.branch}."
            )
        _git(self.repository, "apply", "-", input_text=patch)
        names = _git(worktree.path, "diff", "--cached", "--name-only", worktree.base).split()
        return {
            "applied": True,
            "files": names,
            "message": f"Applied {len(names)} file(s) from {bot_name} to your workspace.",
        }

    def remove(self, bot_name: str) -> None:
        with file_lock(self.index_path):
            index = self._load()
            item = index.pop(bot_name, None)
            if item is None:
                return
            _git(self.repository, "worktree", "remove", "--force", item["path"], check=False)
            _git(self.repository, "branch", "-D", item["branch"], check=False)
            if Path(item["path"]).exists():
                shutil.rmtree(item["path"], ignore_errors=True)
            self._save(index)

    def remove_all(self) -> list[str]:
        removed = [item.bot_name for item in self.worktrees()]
        for name in removed:
            self.remove(name)
        _git(self.repository, "worktree", "prune", check=False)
        shutil.rmtree(self.root, ignore_errors=True)
        return removed
