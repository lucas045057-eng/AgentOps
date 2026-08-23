"""Durable workflow orchestration layered on top of the legacy script runner."""

from __future__ import annotations

import asyncio
import json
import logging
import time
from datetime import datetime
from typing import Any, Dict, Optional

from app.database.database import (
    add_artifact,
    create_attempt,
    create_run,
    get_attempts,
    get_workflow,
    get_workflow_steps,
    update_attempt,
    update_run,
)
from app.services.execution_service import execute_script


logger = logging.getLogger(__name__)
SUCCESS_STATUSES = {"success", "already_done", "skipped", "no_work"}


async def run_workflow(
    workflow_id: int,
    *,
    trigger: str = "manual",
    idempotency_key: Optional[str] = None,
    max_retries: int = 1,
) -> Dict[str, Any]:
    workflow = get_workflow(workflow_id)
    if not workflow:
        raise ValueError(f"Workflow {workflow_id} not found")

    run = create_run(workflow_id, trigger=trigger, idempotency_key=idempotency_key)
    # An idempotency key represents one logical trigger, including a failed
    # attempt.  Reusing it must never create a second set of attempts.  A
    # caller that wants to retry deliberately can submit a new key.
    if run["status"] in {"running", "success", "already_done", "skipped", "failed"}:
        return {"run": run, "attempts": get_attempts(run["id"]), "reused": True}

    started_at = datetime.utcnow()
    update_run(run["id"], status="running", started_at=started_at, error=None)
    steps = get_workflow_steps(workflow_id)
    if not steps:
        update_run(
            run["id"],
            status="failed",
            finished_at=datetime.utcnow(),
            summary="Workflow has no active steps",
            error="Workflow has no active steps",
        )
        return {"run": get_workflow_run(run["id"]), "attempts": []}

    completed = 0
    final_error = None
    for step in steps:
        step_done = False
        for attempt_no in range(1, max_retries + 2):
            attempt_started = time.monotonic()
            attempt_id = create_attempt(
                run_id=run["id"],
                step_id=step["id"],
                script_id=step["script_id"],
                attempt_no=attempt_no,
                started_at=datetime.utcnow(),
            )
            try:
                result = await execute_script(step["script_id"], max_retries=0)
            except Exception as exc:
                result = {
                    "status": "failed",
                    "error": str(exc),
                    "output": "",
                    "retryable": True,
                    "execution_id": None,
                }
            status = str(result.get("status", "unknown"))
            update_attempt(
                attempt_id,
                status=status,
                finished_at=datetime.utcnow(),
                duration=round(time.monotonic() - attempt_started, 3),
                output=str(result.get("output", "")),
                error=result.get("error"),
                result_json=json.dumps(result, ensure_ascii=False, default=str),
            )
            if result.get("artifact_dir"):
                add_artifact(
                    kind="execution_dir",
                    path=str(result["artifact_dir"]),
                    run_id=run["id"],
                    attempt_id=attempt_id,
                    execution_id=result.get("execution_id"),
                    metadata={"step_id": step["id"], "status": status},
                )

            if status == "cancelled":
                finished_at = datetime.utcnow()
                update_run(
                    run["id"],
                    status="cancelled",
                    finished_at=finished_at,
                    duration=(finished_at - started_at).total_seconds(),
                    summary=f"{completed}/{len(steps)} steps completed before cancellation",
                    error=result.get("error") or "Workflow cancelled",
                )
                return {"run": get_workflow_run(run["id"]), "attempts": get_attempts(run["id"])}

            if status in SUCCESS_STATUSES:
                step_done = True
                completed += 1
                break
            final_error = result.get("error") or result.get("output") or f"Step {step['name']} returned {status}"
            if not result.get("retryable", False) or attempt_no > max_retries:
                break
            await asyncio.sleep(min(5 * attempt_no, 30))

        if not step_done and step.get("required", 1):
            finished_at = datetime.utcnow()
            update_run(
                run["id"],
                status="failed",
                finished_at=finished_at,
                duration=(finished_at - started_at).total_seconds(),
                summary=f"{completed}/{len(steps)} steps completed",
                error=str(final_error)[:2000],
            )
            return {"run": get_workflow_run(run["id"]), "attempts": get_attempts(run["id"])}

    finished_at = datetime.utcnow()
    update_run(
        run["id"],
        status="success",
        finished_at=finished_at,
        duration=(finished_at - started_at).total_seconds(),
        summary=f"{completed}/{len(steps)} steps completed",
        error=None,
    )
    return {"run": get_workflow_run(run["id"]), "attempts": get_attempts(run["id"])}


def get_workflow_run(run_id: int) -> Dict[str, Any]:
    from app.database.database import get_run

    return get_run(run_id) or {"id": run_id}


async def run_all_workflows(
    trigger: str = "schedule",
    idempotency_prefix: Optional[str] = None,
    workflow_ids: Optional[set[int]] = None,
) -> list[Dict[str, Any]]:
    from app.database.database import get_workflows

    results = []
    for workflow in get_workflows():
        if not workflow.get("enabled", 1):
            continue
        if workflow_ids is not None and workflow["id"] not in workflow_ids:
            continue
        key = None
        if idempotency_prefix:
            day = datetime.utcnow().strftime("%Y-%m-%d")
            key = f"{idempotency_prefix}:{workflow['id']}:{day}"
        results.append(
            await run_workflow(
                workflow["id"],
                trigger=trigger,
                idempotency_key=key,
            )
        )
    return results
