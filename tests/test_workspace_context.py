from __future__ import annotations

import base64
from pathlib import Path

import pytest

from merced_ai.workspace_context import (
    ContextReference,
    RunStore,
    UploadInput,
    build_context_prompt,
    list_workspace_files,
    save_upload,
)


def test_workspace_context_lists_and_inlines_bounded_text(workspace: Path) -> None:
    source = workspace / "notes.md"
    source.write_text("release evidence", encoding="utf-8")
    (workspace / ".git").mkdir()
    (workspace / ".git" / "secret").write_text("ignored", encoding="utf-8")

    files = list_workspace_files(workspace)
    prompt, manifest = build_context_prompt(workspace, [ContextReference(path="notes.md")])

    assert [item["path"] for item in files] == ["notes.md"]
    assert "release evidence" in prompt
    assert manifest == [
        {"path": "notes.md", "size": 16, "media_type": "text/markdown", "delivery": "inline"}
    ]


def test_workspace_context_rejects_escape_and_persists_upload(workspace: Path) -> None:
    with pytest.raises(ValueError, match="inside the active workspace"):
        build_context_prompt(workspace, [ContextReference(path="../outside.txt")])

    upload = save_upload(
        workspace,
        "session-test",
        UploadInput(
            name="../picture.png",
            media_type="image/png",
            content_base64=base64.b64encode(b"png-bytes").decode(),
        ),
    )

    assert upload["name"] == "picture.png"
    assert upload["path"].startswith(".merced-ai/attachments/session-test/")
    assert (workspace / upload["path"]).read_bytes() == b"png-bytes"


def test_run_store_records_durable_lifecycle(workspace: Path) -> None:
    store = RunStore(workspace)
    record = store.start(
        "run-test",
        "session-test",
        "Inspect the release",
        [{"bot_name": "reviewer", "harness_id": "codex"}],
        [{"path": "README.md"}],
    )
    record.events.append({"type": "tool_event", "event": {"name": "read"}})
    store.finish(record, completed=1, failed=0, duration_ms=1250)

    restored = store.list()
    assert len(restored) == 1
    assert restored[0].status == "completed"
    assert restored[0].duration_ms == 1250
    assert restored[0].events[0]["event"]["name"] == "read"
