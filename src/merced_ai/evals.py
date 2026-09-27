"""Cross-harness eval: run one profile and prompt on several harnesses and score the replies.

Scoring is deterministic first: each check (contains, not_contains, regex, exact, max_chars,
json) passes or fails, and a harness's check score is the fraction that passed. An optional
judge harness can add a 0-10 rubric score; the judge is just another harness, so its score is an
opinion and is reported separately from the deterministic score, never blended into it.

Runs use the same adapters, projection, argv guard, and approvals as any other turn. A profile
that may edit files or run commands runs one harness at a time so the runs cannot collide in the
workspace; read-only profiles run concurrently.
"""

from __future__ import annotations

import json
import re
import threading
import time
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal
from uuid import uuid4

from pydantic import BaseModel, Field, field_validator

from merced_ai.application import is_write_capable
from merced_ai.harnesses.registry import HarnessRegistry
from merced_ai.models import ProfileRecord, RunRequest
from merced_ai.profiles import resolve_profile
from merced_ai.storage import atomic_write

MAX_OUTPUT_CHARS = 20_000
EVAL_ID = re.compile(r"^eval-[0-9a-f]{32}$")
CheckKind = Literal["contains", "not_contains", "regex", "exact", "max_chars", "json"]


class EvalCheck(BaseModel):
    type: CheckKind
    value: str | int | None = None
    case_sensitive: bool = False

    @field_validator("value")
    @classmethod
    def _bounded(cls, value: str | int | None) -> str | int | None:
        if isinstance(value, str) and len(value) > 2000:
            raise ValueError("check values are limited to 2,000 characters")
        return value

    def label(self) -> str:
        if self.type == "json":
            return "reply is valid JSON"
        if self.type == "max_chars":
            return f"at most {self.value} characters"
        readable = {
            "contains": "contains",
            "not_contains": "does not contain",
            "regex": "matches",
            "exact": "equals",
        }[self.type]
        return f"{readable} {self.value!r}"

    def run(self, output: str) -> bool:
        text = output.strip()
        if self.type == "json":
            try:
                json.loads(text)
                return True
            except ValueError:
                return False
        if self.type == "max_chars":
            return len(text) <= int(self.value or 0)
        needle = str(self.value or "")
        haystack, wanted = (text, needle) if self.case_sensitive else (text.lower(), needle.lower())
        if self.type == "contains":
            return wanted in haystack
        if self.type == "not_contains":
            return wanted not in haystack
        if self.type == "exact":
            return haystack == wanted
        flags = 0 if self.case_sensitive else re.IGNORECASE
        try:
            return re.search(needle, text, flags) is not None
        except re.error:
            return False


class EvalJudge(BaseModel):
    harness: str = Field(min_length=1, max_length=60)
    rubric: str = Field(min_length=1, max_length=4000)


class EvalSpec(BaseModel):
    profile: str = Field(min_length=1, max_length=200)
    prompt: str = Field(min_length=1, max_length=20_000)
    harnesses: list[str] = Field(min_length=1, max_length=14)
    checks: list[EvalCheck] = Field(default_factory=list, max_length=20)
    judge: EvalJudge | None = None
    timeout_seconds: int = Field(default=600, ge=5, le=3600)

    @field_validator("harnesses")
    @classmethod
    def _unique(cls, value: list[str]) -> list[str]:
        if len(set(value)) != len(value):
            raise ValueError("list each harness once")
        return value


@dataclass
class HarnessOutcome:
    harness: str
    status: Literal["ok", "error", "skipped"]
    output: str = ""
    error: str | None = None
    duration_ms: int = 0
    checks: list[dict[str, Any]] = field(default_factory=list)
    check_score: float | None = None
    judge_score: float | None = None
    judge_reason: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "harness": self.harness,
            "status": self.status,
            "output": self.output,
            "error": self.error,
            "duration_ms": self.duration_ms,
            "checks": self.checks,
            "check_score": self.check_score,
            "judge_score": self.judge_score,
            "judge_reason": self.judge_reason,
        }


def judge_prompt(spec: EvalSpec, output: str) -> str:
    return (
        "You are grading one assistant reply. Do not use any tools.\n\n"
        f"Task given to the assistant:\n{spec.prompt}\n\n"
        f"Rubric:\n{spec.judge.rubric if spec.judge else ''}\n\n"
        f"Reply to grade:\n<<<\n{output[:8000]}\n>>>\n\n"
        'Answer with only a JSON object: {"score": <number 0-10>, "reason": "<one sentence>"}'
    )


def parse_judgement(text: str) -> tuple[float | None, str | None]:
    match = re.search(r"\{.*\}", text, re.DOTALL)
    if not match:
        return None, "the judge did not return a JSON score"
    try:
        payload = json.loads(match.group(0))
        score = float(payload["score"])
    except (ValueError, KeyError, TypeError):
        return None, "the judge's score could not be read"
    if not 0 <= score <= 10:
        return None, "the judge's score was outside 0-10"
    return score, str(payload.get("reason", ""))[:500]


class EvalRunner:
    def __init__(self, workspace: Path, registry: HarnessRegistry) -> None:
        self.workspace = workspace.resolve()
        self.registry = registry
        self.root = self.workspace / ".merced-ai" / "evals"

    def _request(
        self, profile: ProfileRecord, harness: str, prompt: str, timeout: int
    ) -> RunRequest:
        adapter = self.registry.get(harness)
        return RunRequest(
            harness_id=harness,
            prompt=prompt,
            workspace=self.workspace,
            profile=profile,
            projection=adapter.project_profile(profile),
            timeout_seconds=timeout,
        )

    def _run_one(
        self,
        profile: ProfileRecord,
        spec: EvalSpec,
        harness: str,
        cancellation: threading.Event | None,
    ) -> HarnessOutcome:
        started = time.monotonic()
        try:
            adapter = self.registry.get(harness)
            probe = adapter.probe()
            if probe.path is None or probe.status.value == "probe_failed":
                return HarnessOutcome(
                    harness, "skipped", error=f"{harness} is {probe.status.value}"
                )
            request = self._request(profile, harness, spec.prompt, spec.timeout_seconds)
            runner = getattr(adapter, "run_cancellable", None)
            result = runner(request, cancellation) if runner else adapter.run(request)
        except Exception as error:  # Each harness fails on its own.
            return HarnessOutcome(
                harness,
                "error",
                error=str(error)[:500],
                duration_ms=round((time.monotonic() - started) * 1000),
            )
        output = result.output[:MAX_OUTPUT_CHARS]
        passed = [check.run(output) for check in spec.checks]
        checks: list[dict[str, Any]] = [
            {"label": check.label(), "type": check.type, "passed": ok}
            for check, ok in zip(spec.checks, passed, strict=True)
        ]
        score = sum(passed) / len(passed) if passed else None
        return HarnessOutcome(
            harness,
            "ok",
            output=output,
            duration_ms=result.duration_ms,
            checks=checks,
            check_score=score,
        )

    def run(
        self,
        spec: EvalSpec,
        *,
        cancellation: threading.Event | None = None,
        on_progress: Callable[[HarnessOutcome], None] | None = None,
    ) -> dict[str, Any]:
        profile = resolve_profile(spec.profile, self.workspace)
        for harness in [*spec.harnesses, *([spec.judge.harness] if spec.judge else [])]:
            self.registry.get(harness)  # Unknown harnesses fail before anything runs.
        sequential = is_write_capable(profile)
        outcomes: dict[str, HarnessOutcome] = {}

        def task(harness: str) -> None:
            outcome = self._run_one(profile, spec, harness, cancellation)
            outcomes[harness] = outcome
            if on_progress is not None:
                on_progress(outcome)

        started = time.monotonic()
        with ThreadPoolExecutor(max_workers=1 if sequential else len(spec.harnesses)) as pool:
            list(pool.map(task, spec.harnesses))
        if spec.judge is not None:
            self._judge(spec, profile, [outcomes[h] for h in spec.harnesses], cancellation)
        ordered = [outcomes[harness] for harness in spec.harnesses]
        record = {
            "id": f"eval-{uuid4().hex}",
            "created_at": datetime.now(UTC).isoformat(timespec="seconds"),
            "spec": spec.model_dump(mode="json"),
            "profile_revision": profile.revision,
            "profile_digest": profile.profile_digest,
            "sequential": sequential,
            "duration_ms": round((time.monotonic() - started) * 1000),
            "results": [item.as_dict() for item in ordered],
            "ranking": rank(ordered),
        }
        self.root.mkdir(parents=True, exist_ok=True)
        atomic_write(self.root / f"{record['id']}.json", json.dumps(record, indent=2))
        return record

    def _judge(
        self,
        spec: EvalSpec,
        profile: ProfileRecord,
        outcomes: list[HarnessOutcome],
        cancellation: threading.Event | None,
    ) -> None:
        assert spec.judge is not None
        adapter = self.registry.get(spec.judge.harness)
        judge_profile = profile.model_copy(
            update={
                "document": {
                    **profile.document,
                    "spec": {
                        **profile.document["spec"],
                        "role": {"instructions": "Grade replies strictly against the rubric.\n"},
                        "permissions": {"edit": "deny", "shell": "deny"},
                    },
                }
            }
        )
        for outcome in outcomes:
            if outcome.status != "ok":
                continue
            request = self._request(
                judge_profile, spec.judge.harness, judge_prompt(spec, outcome.output), 300
            )
            try:
                runner = getattr(adapter, "run_cancellable", None)
                reply = runner(request, cancellation) if runner else adapter.run(request)
                outcome.judge_score, outcome.judge_reason = parse_judgement(reply.output)
            except Exception as error:
                outcome.judge_reason = f"judge failed: {str(error)[:300]}"

    def items(self) -> list[dict[str, Any]]:
        if not self.root.exists():
            return []
        records = [
            json.loads(path.read_text(encoding="utf-8")) for path in self.root.glob("eval-*.json")
        ]
        return sorted(records, key=lambda item: item["created_at"], reverse=True)

    def get(self, eval_id: str) -> dict[str, Any]:
        if not EVAL_ID.fullmatch(eval_id):
            raise ValueError("invalid eval identifier")
        path = self.root / f"{eval_id}.json"
        if not path.exists():
            raise ValueError(f"eval {eval_id!r} was not found")
        return dict(json.loads(path.read_text(encoding="utf-8")))


def rank(outcomes: list[HarnessOutcome]) -> list[str]:
    """Harnesses that answered, best first: check score, then judge score, then speed."""
    answered = [item for item in outcomes if item.status == "ok"]
    return [
        item.harness
        for item in sorted(
            answered,
            key=lambda item: (
                -(item.check_score if item.check_score is not None else -1),
                -(item.judge_score if item.judge_score is not None else -1),
                item.duration_ms,
            ),
        )
    ]
