"""Bounded workspace context and durable broker run records for the local UI."""

from __future__ import annotations

import base64
import binascii
import mimetypes
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import uuid4

from pydantic import BaseModel, Field

from merced_ai.paths import ensure_project_layout

MAX_CONTEXT_FILE_BYTES = 256_000
MAX_CONTEXT_TOTAL_BYTES = 750_000
MAX_UPLOAD_BYTES = 10_000_000
IGNORED_PARTS = {".git", ".venv", "node_modules", "dist", "build", "__pycache__"}


class ContextReference(BaseModel):
    path: str = Field(min_length=1, max_length=1_000)


class UploadInput(BaseModel):
    name: str = Field(min_length=1, max_length=255)
    media_type: str = Field(default="application/octet-stream", max_length=200)
    content_base64: str = Field(min_length=1, max_length=14_000_000)


class RunRecord(BaseModel):
    id: str
    session_id: str
    status: str
    prompt_preview: str
    participants: list[dict[str, str]] = Field(default_factory=list)
    context: list[dict[str, Any]] = Field(default_factory=list)
    events: list[dict[str, Any]] = Field(default_factory=list)
    started_at: str
    finished_at: str | None = None
    completed: int = 0
    failed: int = 0
    duration_ms: int = 0


def _safe_relative(workspace: Path, value: str) -> tuple[Path, str]:
    root = workspace.resolve()
    candidate = (root / value).resolve()
    try:
        relative = candidate.relative_to(root)
    except ValueError as exc:
        raise ValueError("context paths must stay inside the active workspace") from exc
    if any(part in IGNORED_PARTS for part in relative.parts):
        raise ValueError("that path is excluded from workspace context")
    if relative.parts[:1] == (".merced-ai",) and relative.parts[1:2] != ("attachments",):
        raise ValueError("internal Merced AI state cannot be attached as workspace context")
    return candidate, relative.as_posix()


def list_workspace_files(
    workspace: Path, query: str = "", limit: int = 200
) -> list[dict[str, Any]]:
    needle = query.casefold().strip()
    rows: list[dict[str, Any]] = []
    for path in workspace.resolve().rglob("*"):
        if not path.is_file():
            continue
        relative = path.relative_to(workspace.resolve())
        if any(part in IGNORED_PARTS for part in relative.parts):
            continue
        if relative.parts[:1] == (".merced-ai",) and relative.parts[1:2] != ("attachments",):
            continue
        display = relative.as_posix()
        if needle and needle not in display.casefold():
            continue
        try:
            size = path.stat().st_size
        except OSError:
            continue
        rows.append(
            {
                "path": display,
                "size": size,
                "media_type": mimetypes.guess_type(path.name)[0] or "application/octet-stream",
                "readable": size <= MAX_CONTEXT_FILE_BYTES,
            }
        )
        if len(rows) >= max(1, min(limit, 500)):
            break
    return sorted(rows, key=lambda item: item["path"].casefold())


def save_upload(workspace: Path, session_id: str, payload: UploadInput) -> dict[str, Any]:
    name = Path(payload.name).name
    if not name or name in {".", ".."}:
        raise ValueError("upload name is invalid")
    try:
        content = base64.b64decode(payload.content_base64, validate=True)
    except (ValueError, binascii.Error) as exc:
        raise ValueError("upload content is not valid base64") from exc
    if len(content) > MAX_UPLOAD_BYTES:
        raise ValueError("attachments must be 10 MB or smaller")
    root = ensure_project_layout(workspace) / "attachments" / session_id
    root.mkdir(parents=True, exist_ok=True)
    destination = root / f"{uuid4().hex[:10]}-{name}"
    destination.write_bytes(content)
    return {
        "path": destination.relative_to(workspace.resolve()).as_posix(),
        "name": name,
        "size": len(content),
        "media_type": payload.media_type,
        "uploaded": True,
    }


def build_context_prompt(
    workspace: Path, references: list[ContextReference]
) -> tuple[str, list[dict[str, Any]]]:
    blocks: list[str] = []
    manifest: list[dict[str, Any]] = []
    total = 0
    for reference in references:
        path, relative = _safe_relative(workspace, reference.path)
        if not path.is_file():
            raise ValueError(f"context file {relative!r} was not found")
        size = path.stat().st_size
        media_type = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
        item = {"path": relative, "size": size, "media_type": media_type}
        text_like = media_type.startswith("text/") or media_type in {
            "application/json",
            "application/yaml",
            "application/xml",
        }
        if text_like and size <= MAX_CONTEXT_FILE_BYTES and total + size <= MAX_CONTEXT_TOTAL_BYTES:
            try:
                body = path.read_text(encoding="utf-8")
            except UnicodeDecodeError:
                item["delivery"] = "workspace_path"
                blocks.append(
                    f"- Binary attachment available in the workspace at `{relative}` "
                    f"({media_type})."
                )
            else:
                item["delivery"] = "inline"
                total += size
                blocks.append(f"### {relative}\n```\n{body}\n```")
        else:
            item["delivery"] = "workspace_path"
            blocks.append(
                f"- Attachment available in the workspace at `{relative}` "
                f"({size} bytes, {media_type})."
            )
        manifest.append(item)
    if not blocks:
        return "", manifest
    return "\n\nWorkspace context supplied by the operator:\n\n" + "\n\n".join(blocks), manifest


class RunStore:
    def __init__(self, workspace: Path) -> None:
        self.root = ensure_project_layout(workspace) / "runs"
        self.root.mkdir(parents=True, exist_ok=True)

    def save(self, record: RunRecord) -> None:
        path = self.root / f"{record.id}.json"
        temporary = path.with_name(f".{path.name}.tmp")
        temporary.write_text(record.model_dump_json(indent=2), encoding="utf-8")
        temporary.replace(path)

    def start(
        self,
        run_id: str,
        session_id: str,
        prompt: str,
        participants: list[dict[str, str]],
        context: list[dict[str, Any]],
    ) -> RunRecord:
        record = RunRecord(
            id=run_id,
            session_id=session_id,
            status="running",
            prompt_preview=" ".join(prompt.split())[:240],
            participants=participants,
            context=context,
            started_at=datetime.now(UTC).isoformat(),
        )
        self.save(record)
        return record

    def finish(self, record: RunRecord, completed: int, failed: int, duration_ms: int) -> None:
        record.completed = completed
        record.failed = failed
        record.duration_ms = duration_ms
        record.status = (
            "failed" if failed and not completed else "partial" if failed else "completed"
        )
        record.finished_at = datetime.now(UTC).isoformat()
        self.save(record)

    def list(self, limit: int = 100) -> list[RunRecord]:
        records: list[RunRecord] = []
        for path in self.root.glob("run-*.json"):
            try:
                records.append(RunRecord.model_validate_json(path.read_text(encoding="utf-8")))
            except (OSError, ValueError):
                continue
        return sorted(records, key=lambda item: item.started_at, reverse=True)[:limit]
