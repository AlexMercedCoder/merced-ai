"""Cross-harness eval: deterministic checks, optional judge, ranking, CLI, and web jobs."""

from __future__ import annotations

import asyncio
import json
import threading
import time
from pathlib import Path

import httpx
import pytest
from typer.testing import CliRunner

from merced_ai.cli import app
from merced_ai.evals import EvalCheck, EvalRunner, EvalSpec, parse_judgement
from merced_ai.harnesses import default_registry
from merced_ai.harnesses.adapters.command import CommandHarnessAdapter, HarnessRunError
from merced_ai.models import HarnessProbe, HarnessStatus, RunResult
from merced_ai.profiles import create_profile

runner = CliRunner()
REPLIES = {"codex": "OK, done.", "claude": "OK", "gemini": "Sorry, no."}


@pytest.fixture(autouse=True)
def fake_harnesses(monkeypatch: pytest.MonkeyPatch) -> dict[str, list[float]]:
    windows: dict[str, list[float]] = {}
    lock = threading.Lock()

    def probe(adapter: CommandHarnessAdapter, workspace: Path | None = None) -> HarnessProbe:
        missing = adapter.descriptor.id == "kimi"
        return HarnessProbe(
            harness_id=adapter.descriptor.id,
            status=HarnessStatus.NOT_INSTALLED if missing else HarnessStatus.READY,
            path=None if missing else Path("/bin/true"),
            broker_implements=adapter.descriptor.broker_implements,
        )

    def run(adapter: CommandHarnessAdapter, request, *_args, **_kwargs) -> RunResult:  # type: ignore[no-untyped-def]
        harness = adapter.descriptor.id
        started = time.monotonic()
        if harness == "pi":
            raise HarnessRunError("pi is not logged in")
        if "Reply to grade" in request.prompt:  # judge turn
            score = 9 if ">>>" in request.prompt and "\nOK\n" in request.prompt else 3
            return RunResult(
                harness_id=harness,
                output=f'{{"score": {score}, "reason": "graded"}}',
                exit_code=0,
                duration_ms=1,
            )
        time.sleep(0.2)
        with lock:
            windows[harness] = [started, time.monotonic()]
        return RunResult(
            harness_id=harness, output=REPLIES.get(harness, "?"), exit_code=0, duration_ms=5
        )

    monkeypatch.setattr(CommandHarnessAdapter, "probe", probe)
    monkeypatch.setattr(CommandHarnessAdapter, "run_cancellable", run)
    return windows


def _profile(workspace: Path, *, read_only: bool = True) -> None:
    create_profile(
        "tester",
        "Answers test prompts.",
        "Answer briefly.",
        workspace,
        edit_permission="deny" if read_only else None,
        shell_permission="deny" if read_only else None,
    )


def test_checks_are_deterministic() -> None:
    assert EvalCheck(type="contains", value="ok").run("OK!")
    assert not EvalCheck(type="contains", value="ok", case_sensitive=True).run("OK!")
    assert EvalCheck(type="not_contains", value="sorry").run("fine")
    assert EvalCheck(type="regex", value=r"^ok\b").run("OK then")
    assert not EvalCheck(type="regex", value="(").run("x")  # a bad pattern fails, never raises
    assert EvalCheck(type="exact", value="ok").run("  OK ")
    assert EvalCheck(type="max_chars", value=3).run("abc") and not EvalCheck(
        type="max_chars", value=2
    ).run("abc")
    assert EvalCheck(type="json").run('{"a": 1}') and not EvalCheck(type="json").run("nope")
    assert parse_judgement('Sure: {"score": 7.5, "reason": "fine"}') == (7.5, "fine")
    assert parse_judgement('{"score": 42}')[0] is None
    assert parse_judgement("no json")[0] is None


def test_run_scores_ranks_and_isolates_failures(workspace: Path, fake_harnesses) -> None:  # type: ignore[no-untyped-def]
    _profile(workspace)
    spec = EvalSpec(
        profile="tester",
        prompt="Reply with OK",
        harnesses=["codex", "claude", "gemini", "pi", "kimi"],
        checks=[EvalCheck(type="contains", value="OK"), EvalCheck(type="max_chars", value=5)],
        judge={"harness": "codex", "rubric": "Exactly OK is best."},
    )

    record = EvalRunner(workspace, default_registry()).run(spec)

    results = {item["harness"]: item for item in record["results"]}
    assert results["claude"]["check_score"] == 1.0
    assert results["codex"]["check_score"] == 0.5
    assert results["gemini"]["check_score"] == 0.0
    assert results["pi"]["status"] == "error" and "not logged in" in results["pi"]["error"]
    assert results["kimi"]["status"] == "skipped"
    assert results["claude"]["judge_score"] == 9.0 and results["codex"]["judge_score"] == 3.0
    assert record["ranking"] == ["claude", "codex", "gemini"]
    assert record["sequential"] is False
    windows = fake_harnesses
    assert windows["codex"][0] < windows["claude"][1] and windows["claude"][0] < windows["codex"][1]
    assert EvalRunner(workspace, default_registry()).get(record["id"])["id"] == record["id"]


def test_write_capable_profiles_run_one_harness_at_a_time(workspace: Path, fake_harnesses) -> None:  # type: ignore[no-untyped-def]
    _profile(workspace, read_only=False)
    spec = EvalSpec(profile="tester", prompt="Edit", harnesses=["codex", "claude"])

    record = EvalRunner(workspace, default_registry()).run(spec)

    windows = fake_harnesses
    assert record["sequential"] is True
    assert (
        windows["codex"][1] <= windows["claude"][0] or windows["claude"][1] <= windows["codex"][0]
    )


def test_spec_validation() -> None:
    with pytest.raises(ValueError, match="once"):
        EvalSpec(profile="p", prompt="x", harnesses=["codex", "codex"])
    with pytest.raises(ValueError):
        EvalSpec(profile="p", prompt="x", harnesses=[])


def test_cli_eval_run_list_show(workspace: Path, tmp_path: Path) -> None:
    _profile(workspace)
    result = runner.invoke(
        app,
        [
            "eval",
            "run",
            "-p",
            "tester",
            "--prompt",
            "Reply with OK",
            "-H",
            "codex",
            "-H",
            "claude",
            "--contains",
            "OK",
            "--max-chars",
            "5",
            "--json",
            "-C",
            str(workspace),
        ],
    )
    assert result.exit_code == 0, result.output
    record = json.loads(result.stdout)
    assert record["ranking"][0] == "claude"

    shown = runner.invoke(
        app, ["eval", "show", record["id"], "-C", str(workspace)], env={"COLUMNS": "160"}
    )
    assert "Results (best first)" in shown.stdout and "pass" in shown.stdout
    listed = runner.invoke(app, ["eval", "list", "-C", str(workspace)], env={"COLUMNS": "200"})
    assert record["id"] in listed.stdout

    spec_file = tmp_path / "spec.yaml"
    spec_file.write_text(
        "profile: tester\nprompt: Reply with OK\nharnesses: [claude]\n"
        "checks:\n  - type: exact\n    value: ok\n",
        encoding="utf-8",
    )
    from_file = runner.invoke(
        app, ["eval", "run", "-f", str(spec_file), "--json", "-C", str(workspace)]
    )
    assert json.loads(from_file.stdout)["results"][0]["check_score"] == 1.0

    missing = runner.invoke(app, ["eval", "run", "--prompt", "x", "-C", str(workspace)])
    assert missing.exit_code == 2 and "--profile" in " ".join(missing.stderr.split())
    unknown = runner.invoke(
        app, ["eval", "run", "-p", "tester", "--prompt", "x", "-H", "nope", "-C", str(workspace)]
    )
    assert unknown.exit_code == 2


@pytest.mark.anyio
async def test_web_eval_job_runs_in_background(workspace: Path) -> None:
    from merced_ai.webui_server import create_web_app

    _profile(workspace)
    transport = httpx.ASGITransport(app=create_web_app(workspace, "token"))
    headers = {"x-merced-ai-token": "token"}
    async with httpx.AsyncClient(
        transport=transport, base_url="http://test", headers=headers
    ) as client:
        started = await client.post(
            "/api/evals",
            json={
                "profile": "tester",
                "prompt": "Reply with OK",
                "harnesses": ["codex", "claude"],
                "checks": [{"type": "contains", "value": "OK"}],
            },
        )
        assert started.status_code == 202
        job = started.json()
        for _ in range(200):
            job = (await client.get(f"/api/evals/jobs/{job['id']}")).json()
            if job["status"] != "running":
                break
            await asyncio.sleep(0.05)
        listed = (await client.get("/api/evals")).json()
        bad = await client.post(
            "/api/evals", json={"profile": "ghost", "prompt": "x", "harnesses": ["codex"]}
        )

    assert job["status"] == "completed"
    assert job["harnesses"] == {"codex": "ok", "claude": "ok"}
    assert listed["evals"][0]["id"] == job["record"]["id"]
    assert bad.status_code == 409
