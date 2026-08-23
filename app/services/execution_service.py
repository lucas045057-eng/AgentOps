import asyncio
import json
import logging
import os
import signal
import sys
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Optional, Tuple

from app.config import settings
from app.database.database import (
    add_account_results,
    add_execution,
    get_execution,
    get_script,
    update_execution,
)
from app.runners import create_runner
from app.runners.base import KNOWN_STATUSES, redact_sensitive_text
from app.runners.catalog import get_runner_spec, resolve_script_path


logger = logging.getLogger(__name__)

_ACTIVE_EXECUTION_TASKS: dict[int, asyncio.Task[Any]] = {}


def register_execution_task(execution_id: int, task: asyncio.Task[Any]) -> None:
    _ACTIVE_EXECUTION_TASKS[execution_id] = task


def unregister_execution_task(execution_id: int, task: asyncio.Task[Any] | None = None) -> None:
    current = _ACTIVE_EXECUTION_TASKS.get(execution_id)
    if task is None or current is task:
        _ACTIVE_EXECUTION_TASKS.pop(execution_id, None)


def cancel_active_execution(execution_id: int) -> bool:
    task = _ACTIVE_EXECUTION_TASKS.get(execution_id)
    if task is None or task.done():
        return False
    task.cancel()
    return True


def cancel_persisted_process(execution_id: int) -> bool:
    root = Path(settings.execution_log_root)
    if not root.is_absolute():
        root = Path(__file__).resolve().parents[2] / root
    pid_path = root / str(execution_id) / "process.pid"
    try:
        pid = int(pid_path.read_text(encoding="ascii").strip())
    except (FileNotFoundError, OSError, ValueError):
        return False

    try:
        if os.name == "posix":
            os.killpg(pid, signal.SIGKILL)
        else:
            os.kill(pid, signal.SIGTERM)
        return True
    except (ProcessLookupError, PermissionError, OSError):
        return False


def _retryable_for_status(status: str) -> bool:
    return status in {"failed", "timeout", "crashed"}


async def execute_script(
    script_id: int,
    max_retries: int = 2,
    retry_delay: int = 5,
    execution_id: Optional[int] = None,
) -> Dict[str, Any]:
    last_result: Optional[Dict[str, Any]] = None
    precreated_execution_id = execution_id

    for attempt in range(max_retries + 1):
        try:
            if attempt > 0:
                logger.warning("Script %s retry %s", script_id, attempt)
                await asyncio.sleep(retry_delay)

            result = await _execute_script_once(
                script_id,
                execution_id=precreated_execution_id,
            )
            precreated_execution_id = None
            if result["status"] in {"success", "already_done", "skipped", "no_work", "cancelled"}:
                return result

            last_result = result
            if result.get("retryable") is False:
                return result
            logger.warning("Script %s attempt %s finished with %s", script_id, attempt + 1, result["status"])
        except Exception as exc:
            logger.error("Script %s execution exception: %s", script_id, exc, exc_info=True)
            last_result = {
                "execution_id": None,
                "status": "failed",
                "output": "",
                "error": str(exc),
                "duration": 0.0,
                "retryable": True,
            }

    return last_result or {
        "execution_id": None,
        "status": "failed",
        "output": "",
        "error": "All attempts failed",
        "duration": 0.0,
        "retryable": True,
    }


async def _execute_script_once(
    script_id: int,
    *,
    execution_id: Optional[int] = None,
) -> Dict[str, Any]:
    script_info = get_script(script_id)
    if not script_info:
        raise ValueError(f"Script {script_id} not found")

    script_path = resolve_script_path(script_info["path"])
    spec = get_runner_spec(script_info["path"])
    runner = create_runner(spec)

    started_at = datetime.utcnow()
    execution_id = execution_id or add_execution(script_id, "running", "", started_at)
    update_execution(
        execution_id,
        status="running",
        started_at=started_at,
        error=None,
    )
    current_task = asyncio.current_task()
    if current_task is not None:
        register_execution_task(execution_id, current_task)
    try:
        runner_result = await runner.run(script_path, execution_id, script_id)
    except asyncio.CancelledError:
        update_execution(
            execution_id,
            status="cancelled",
            output="",
            error="Execution cancelled before the runner completed",
            finished_at=datetime.utcnow(),
            duration=round((datetime.utcnow() - started_at).total_seconds(), 3),
        )
        return {
            "execution_id": execution_id,
            "status": "cancelled",
            "output": "",
            "error": "Execution cancelled before the runner completed",
            "duration": round((datetime.utcnow() - started_at).total_seconds(), 3),
            "retryable": False,
            "runner": spec.get("runner", "legacy"),
            "catalog_id": spec.get("id"),
            "artifact_dir": None,
        }
    finally:
        if current_task is not None:
            unregister_execution_task(execution_id, current_task)

    stdout_str = runner_result.get("stdout", "")
    stderr_str = runner_result.get("stderr", "")
    returncode = int(runner_result.get("returncode", -1))
    finished_at = datetime.utcnow()
    duration = float(runner_result.get("duration", (finished_at - started_at).total_seconds()))

    final_status, summary, final_error, parsed_data = _parse_result_with_data(
        stdout_str,
        stderr_str,
        returncode,
        structured_required=bool(runner_result.get("structured_required", False)),
        result_policy=str(runner_result.get("result_policy", "json")),
        timed_out=bool(runner_result.get("timed_out", False)),
        cancelled=bool(runner_result.get("cancelled", False)),
    )
    persisted_execution = get_execution(execution_id)
    if persisted_execution and persisted_execution.get("status") == "cancelled":
        final_status = "cancelled"
        summary = persisted_execution.get("error") or "Execution cancelled"
        final_error = summary
        parsed_data = None
    retryable = (
        parsed_data.get("retryable")
        if isinstance(parsed_data, dict) and "retryable" in parsed_data
        else _retryable_for_status(final_status)
    )

    safe_output = redact_sensitive_text(stdout_str)
    safe_error = redact_sensitive_text(final_error or "") or None
    update_execution(
        execution_id,
        status=final_status,
        output=safe_output,
        error=safe_error,
        finished_at=finished_at,
        duration=duration,
    )

    if parsed_data and isinstance(parsed_data.get("accounts"), list):
        try:
            add_account_results(execution_id, script_id, parsed_data["accounts"])
        except Exception as exc:
            logger.warning("Could not persist account results for execution %s: %s", execution_id, exc)

    if final_status in {"success", "already_done", "skipped", "no_work"}:
        logger.info("Script %s execution %s finished: %s", script_id, execution_id, summary[:200])
    else:
        logger.warning("Script %s execution %s finished with %s: %s", script_id, execution_id, final_status, summary[:500])

    return {
        "execution_id": execution_id,
        "status": final_status,
        "output": safe_output,
        "error": safe_error,
        "duration": duration,
        "retryable": bool(retryable),
        "runner": spec.get("runner", "legacy"),
        "catalog_id": spec.get("id"),
        "artifact_dir": runner_result.get("artifact_dir"),
    }


def _parse_result_with_data(
    stdout_str: str,
    stderr_str: str,
    returncode: int,
    *,
    structured_required: bool = False,
    result_policy: str = "json",
    timed_out: bool = False,
    cancelled: bool = False,
) -> Tuple[str, str, Optional[str], Optional[Dict[str, Any]]]:
    if timed_out:
        return "timeout", "Runner timed out", stderr_str or "Runner timed out", None
    if cancelled:
        return "cancelled", "Execution cancelled", stderr_str or "Execution cancelled", None

    summary = stdout_str or stderr_str or "No output"
    parsed_data: Optional[Dict[str, Any]] = None
    final_status = "failed"

    for line in reversed(stdout_str.splitlines()):
        try:
            data = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(data, dict):
            parsed_data = data
            break

    if parsed_data and parsed_data.get("status") in KNOWN_STATUSES:
        final_status = str(parsed_data["status"])
        summary = str(parsed_data.get("message") or parsed_data.get("summary") or json.dumps(parsed_data, ensure_ascii=False))
    elif returncode == 0:
        if result_policy in {"exit_code", "json_or_exit_code"}:
            final_status = "success"
            summary = stdout_str or "Process exited successfully (exit-code verification)"
        elif structured_required:
            final_status = "unknown"
            summary = "Process exited successfully but emitted no structured completion result"
        else:
            final_status = "success"
            summary = stdout_str or "Execution completed"
    else:
        final_status = "crashed" if not stderr_str else "failed"
        summary = stderr_str or stdout_str or "Process failed"

    final_error = None
    if final_status in {"failed", "partial_success", "manual_required", "unknown", "timeout", "crashed", "cancelled"}:
        final_error = summary[:2000]
    return final_status, summary, final_error, parsed_data
