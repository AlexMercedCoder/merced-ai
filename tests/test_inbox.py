"""OAP Level 2 applicator behavior through the reviewed state-delta inbox."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import httpx
import pytest
import yaml
from typer.testing import CliRunner

from merced_ai.cli import app
from merced_ai.inbox import DeltaInbox, InboxError

runner = CliRunner()


def _profile(workspace: Path, *, writeback: str = "propose", max_facts: int = 50) -> Path:
    document = {
        "oap": "1.0",
        "kind": "AgentProfile",
        "metadata": {"name": "analyst", "description": "Analyses data carefully.", "revision": 1},
        "spec": {
            "role": {"instructions": "Analyse the data.\n"},
            "tools": {"allow": ["read"]},
            "lifecycle": {
                "writeback": writeback,
                "retention": {"max_facts": max_facts, "eviction": "oldest", "max_history": 10},
            },
        },
        "state": {
            "facts": [
                {"id": "fact-old", "text": "The oldest fact.", "pinned": True},
                {"id": "fact-mid", "text": "A middle fact."},
            ]
        },
    }
    path = workspace / ".agents" / "analyst.agent.yaml"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.safe_dump(document, sort_keys=False), encoding="utf-8")
    return path


def _delta(revision: int = 1, **extra: Any) -> dict[str, Any]:
    return {
        "oap": "1.0",
        "kind": "AgentStateDelta",
        "target": {"name": "analyst", "revision": revision},
        "session": {"id": "sess-1", "harness": "magagent"},
        "summary": "Learned the team's conventions.",
        "operations": [
            {
                "op": "add",
                "path": "/state/facts/-",
                "value": {"id": "fact-new", "text": "Partitions are daily."},
                "reason": "The user said so.",
            }
        ],
        **extra,
    }


def _load(path: Path) -> dict[str, Any]:
    return yaml.safe_load(path.read_text(encoding="utf-8"))


def test_nothing_is_applied_until_approved_then_history_records_the_approver(
    workspace: Path,
) -> None:
    path = _profile(workspace)
    inbox = DeltaInbox(workspace)

    item = inbox.add(_delta(), source="test")
    assert item["status"] == "pending"
    assert _load(path)["metadata"]["revision"] == 1

    applied = inbox.approve(item["id"], actor="alex")

    profile = _load(path)
    assert applied["status"] == "applied" and applied["applied"]["revision"] == 2
    assert profile["metadata"]["revision"] == 2
    assert [fact["id"] for fact in profile["state"]["facts"]][-1] == "fact-new"
    assert profile["history"][-1]["approved_by"] == "alex"
    assert profile["history"][-1]["session_id"] == "sess-1"
    with pytest.raises(InboxError, match="already applied"):
        inbox.approve(item["id"], actor="alex")


def test_writeback_off_refuses_and_leaves_the_profile(workspace: Path) -> None:
    path = _profile(workspace, writeback="off")
    before = path.read_bytes()
    inbox = DeltaInbox(workspace)
    item = inbox.add(_delta(), source="test")

    with pytest.raises(InboxError, match="writeback is 'off'"):
        inbox.approve(item["id"], actor="alex")
    assert path.read_bytes() == before


def test_proposal_gate_even_with_writeback_auto(workspace: Path) -> None:
    """Behavioral test 9: a proposal to add shell is never applied with the state operations."""
    path = _profile(workspace, writeback="auto")
    proposal = {
        "path": "/spec/tools/allow",
        "op": "replace",
        "value": ["read", "shell"],
        "rationale": "I need the shell to run tests.",
        "risk": "low",
    }
    inbox = DeltaInbox(workspace)
    item = inbox.add(_delta(proposals=[proposal]), source="test")

    assert item["proposals"][0]["risk"] == "high"  # computed, not trusted
    inbox.approve(item["id"], actor="alex")
    assert _load(path)["spec"]["tools"]["allow"] == ["read"]

    declined = inbox.decide_proposal(item["id"], 0, approve=False, actor="alex")
    assert declined["proposals"][0]["status"] == "rejected"
    assert _load(path)["spec"]["tools"]["allow"] == ["read"]


def test_an_approved_proposal_is_applied_separately_and_recorded(workspace: Path) -> None:
    path = _profile(workspace)
    proposal = {
        "path": "/spec/role/instructions",
        "value": "Analyse the data and cite sources.\n",
        "rationale": "Users keep asking for sources.",
    }
    inbox = DeltaInbox(workspace)
    item = inbox.add(_delta(proposals=[proposal], operations=[]), source="test")

    decided = inbox.decide_proposal(item["id"], 0, approve=True, actor="alex")

    profile = _load(path)
    assert decided["proposals"][0]["status"] == "applied"
    assert profile["spec"]["role"]["instructions"].startswith("Analyse the data and cite")
    assert profile["metadata"]["revision"] == 2
    assert profile["history"][-1]["approved_by"] == "alex"

    bad = inbox.add(
        _delta(
            2,
            operations=[],
            proposals=[{"path": "/metadata/revision", "value": 9, "rationale": "x"}],
        ),
        source="test",
    )
    with pytest.raises(InboxError, match="owned by the applicator"):
        inbox.decide_proposal(bad["id"], 0, approve=True, actor="alex")


def test_revision_conflict_never_blind_writes_and_can_rebase(workspace: Path) -> None:
    """Behavioral test 8, plus rebase for id-addressed/append operations."""
    path = _profile(workspace)
    inbox = DeltaInbox(workspace)
    first = inbox.add(_delta(), source="test")
    second = _delta()
    second["operations"][0]["value"] = {"id": "fact-other", "text": "Another fact."}
    stale = inbox.add(second, source="test")
    inbox.approve(first["id"], actor="alex")
    before = path.read_bytes()

    conflicted = inbox.approve(stale["id"], actor="alex")

    assert conflicted["status"] == "conflict" and conflicted["conflict"]["rebaseable"] is True
    assert path.read_bytes() == before
    rebased = inbox.approve(stale["id"], actor="alex", rebase=True)
    assert rebased["status"] == "applied" and rebased["applied"]["rebased"] is True

    positional = inbox.add(
        _delta(1, operations=[{"op": "remove", "path": "/state/facts/0"}]), source="test"
    )
    assert inbox.approve(positional["id"], actor="alex")["conflict"]["rebaseable"] is False
    with pytest.raises(InboxError, match="cannot be rebased"):
        inbox.approve(positional["id"], actor="alex", rebase=True)


def test_failed_operation_leaves_profile_unchanged(workspace: Path) -> None:
    """Behavioral test 7: a multi-operation delta applies all or nothing."""
    path = _profile(workspace)
    before = path.read_bytes()
    operations = [
        {"op": "add", "path": "/state/facts/-", "value": {"id": "fact-a", "text": "A."}},
        {
            "op": "replace",
            "path": "/state/facts/id:no-such-fact",
            "value": {"id": "x", "text": "x"},
        },
    ]
    inbox = DeltaInbox(workspace)
    item = inbox.add(_delta(operations=operations), source="test")

    with pytest.raises(InboxError, match="operation 1"):
        inbox.approve(item["id"], actor="alex")
    assert path.read_bytes() == before


def test_a_delta_that_would_invalidate_the_profile_is_refused(workspace: Path) -> None:
    path = _profile(workspace)
    inbox = DeltaInbox(workspace)
    duplicate = _delta()
    duplicate["operations"][0]["value"] = {"id": "fact-mid", "text": "Same id again."}
    item = inbox.add(duplicate, source="test")
    before = path.read_bytes()

    with pytest.raises(InboxError, match="invalid"):
        inbox.approve(item["id"], actor="alex")
    assert path.read_bytes() == before


def test_retention_keeps_pinned_entries(workspace: Path) -> None:
    """Behavioral test 10: the cap is honored and the pinned oldest fact survives."""
    path = _profile(workspace, max_facts=2)
    inbox = DeltaInbox(workspace)
    item = inbox.add(_delta(), source="test")

    inbox.approve(item["id"], actor="alex")

    facts = [fact["id"] for fact in _load(path)["state"]["facts"]]
    assert len(facts) == 2 and "fact-old" in facts


def test_intake_validates_scope_and_unwraps_proposal_records(
    workspace: Path, tmp_path: Path
) -> None:
    _profile(workspace)
    inbox = DeltaInbox(workspace)
    with pytest.raises(InboxError, match="outside /state"):
        inbox.add(
            _delta(operations=[{"op": "add", "path": "/spec/tools/allow/-", "value": "shell"}]),
            source="t",
        )
    with pytest.raises(InboxError, match="unknown profile"):
        inbox.add({**_delta(), "target": {"name": "ghost", "revision": 1}}, source="t")
    wrapped = tmp_path / "proposal.json"
    wrapped.write_text(json.dumps({"id": "p1", "delta": _delta()}), encoding="utf-8")
    assert inbox.add_file(wrapped)["source"] == "file:proposal.json"


def test_remember_queues_an_explicit_statement(workspace: Path) -> None:
    path = _profile(workspace)
    inbox = DeltaInbox(workspace)

    item = inbox.remember("analyst", "Use pytest, not unittest", kind="preference", actor="alex")
    inbox.approve(item["id"], actor="alex")

    assert _load(path)["state"]["preferences"][0]["text"] == "Use pytest, not unittest"
    with pytest.raises(InboxError, match="kind must be"):
        inbox.remember("analyst", "x", kind="secret", actor="alex")


def test_cli_inbox_review_flow(workspace: Path) -> None:
    path = _profile(workspace)
    queued = runner.invoke(
        app, ["inbox", "remember", "analyst", "Daily partitions", "-C", str(workspace)]
    )
    assert queued.exit_code == 0, queued.output
    listed = json.loads(
        runner.invoke(app, ["inbox", "list", "--json", "-C", str(workspace)]).stdout
    )
    item_id = listed[0]["id"]
    shown = runner.invoke(
        app, ["inbox", "show", item_id, "-C", str(workspace)], env={"COLUMNS": "200"}
    )
    assert "add /state/facts/-" in shown.stdout

    declined = runner.invoke(app, ["inbox", "approve", item_id, "-C", str(workspace)], input="n\n")
    assert declined.exit_code == 1 and _load(path)["metadata"]["revision"] == 1
    approved = runner.invoke(app, ["inbox", "approve", item_id, "--yes", "-C", str(workspace)])
    assert approved.exit_code == 0 and "revision 2" in approved.stdout
    empty = runner.invoke(app, ["inbox", "list", "--status", "pending", "-C", str(workspace)])
    assert "The inbox is empty" in empty.stdout
    missing = runner.invoke(app, ["inbox", "show", "delta-nope", "-C", str(workspace)])
    assert missing.exit_code == 2


@pytest.mark.anyio
async def test_web_inbox_endpoints(workspace: Path) -> None:
    from merced_ai.webui_server import create_web_app

    path = _profile(workspace)
    transport = httpx.ASGITransport(app=create_web_app(workspace, "token"))
    async with httpx.AsyncClient(
        transport=transport, base_url="http://test", headers={"x-merced-ai-token": "token"}
    ) as client:
        uploaded = (await client.post("/api/inbox", json={"document": _delta()})).json()
        bootstrap = (await client.get("/api/bootstrap")).json()
        listed = (await client.get("/api/inbox")).json()
        applied = (await client.post(f"/api/inbox/{uploaded['id']}/approve", json={})).json()
        again = await client.post(f"/api/inbox/{uploaded['id']}/approve", json={})
        remembered = (
            await client.post("/api/inbox/remember", json={"profile": "analyst", "text": "Hi"})
        ).json()
        rejected = (await client.post(f"/api/inbox/{remembered['id']}/reject", json={})).json()

    assert bootstrap["inbox_pending"] == 1 and listed["pending"] == 1
    assert applied["status"] == "applied" and _load(path)["metadata"]["revision"] == 2
    assert again.status_code == 409
    assert rejected["status"] == "rejected"
