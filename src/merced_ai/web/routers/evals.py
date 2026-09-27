"""Cross-harness eval runs from the web UI (started in the background, polled for progress)."""

from __future__ import annotations

import threading
from typing import Any
from uuid import uuid4

from fastapi import APIRouter, HTTPException

from merced_ai.evals import EvalRunner, EvalSpec, HarnessOutcome
from merced_ai.profiles import ProfileError, resolve_profile
from merced_ai.web.context import ReadContext, WriteContext

router = APIRouter()


@router.get("/api/evals")
async def eval_list(context: ReadContext) -> dict[str, Any]:
    return {"evals": EvalRunner(context.workspace, context.registry).items()[:50]}


@router.get("/api/evals/jobs/{job_id}")
async def eval_job(job_id: str, context: ReadContext) -> dict[str, Any]:
    job = context.eval_jobs.get(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="Eval job not found")
    return job


@router.get("/api/evals/{eval_id}")
async def eval_get(eval_id: str, context: ReadContext) -> dict[str, Any]:
    try:
        return EvalRunner(context.workspace, context.registry).get(eval_id)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.post("/api/evals", status_code=202)
async def eval_start(spec: EvalSpec, context: WriteContext) -> dict[str, Any]:
    try:
        resolve_profile(spec.profile, context.workspace)
        for harness in [*spec.harnesses, *([spec.judge.harness] if spec.judge else [])]:
            context.registry.get(harness)
    except (ProfileError, KeyError) as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    if sum(1 for job in context.eval_jobs.values() if job["status"] == "running") >= 3:
        raise HTTPException(status_code=429, detail="Three evals are already running; wait for one")
    job_id = f"job-{uuid4().hex}"
    job: dict[str, Any] = {
        "id": job_id,
        "status": "running",
        "harnesses": {harness: "running" for harness in spec.harnesses},
        "record": None,
        "error": None,
    }
    context.eval_jobs[job_id] = job
    runner = EvalRunner(context.workspace, context.registry)

    def progress(outcome: HarnessOutcome) -> None:
        job["harnesses"][outcome.harness] = outcome.status

    def work() -> None:
        try:
            job["record"] = runner.run(spec, on_progress=progress)
            job["status"] = "completed"
        except Exception as error:  # Reported to the page instead of lost in a thread.
            job["status"], job["error"] = "failed", str(error)[:500]

    threading.Thread(target=work, daemon=True, name=job_id).start()
    return job
