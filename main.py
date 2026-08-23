import asyncio
import logging
import secrets
import shutil
from contextlib import asynccontextmanager
from pathlib import Path
from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.staticfiles import StaticFiles
from fastapi.responses import JSONResponse, RedirectResponse
from datetime import datetime

from app.config import settings
from app.models.script import ScriptCreate
from app.services.execution_service import execute_script
from app.services.execution_service import (
    cancel_active_execution,
    cancel_persisted_process,
    register_execution_task,
    unregister_execution_task,
)
from app.services.notifier import send_failure_notification
from app.database.database import (
    init_db,
    add_project,
    get_projects,
    get_project,
    delete_project,
    update_project,
    add_task,
    get_tasks,
    get_tasks_by_project,
    get_task,
    delete_task,
    add_script,
    get_scripts,
    get_script,
    delete_script,
    get_executions,
    add_execution,
    get_execution,              # ✅ 已添加
    update_execution,
    get_today_executions,
    delete_execution,
    clear_execution_history,
    mark_stale_executions,
    mark_stale_runs,
    get_account_results_by_execution,
    get_account_stats_by_script,
    get_workflows,
    get_workflow,
    get_workflow_steps,
    get_runs,
    get_run,
    get_attempts,
    get_artifacts,
    register_worker,
    heartbeat_worker,
    get_workers,
)
from app.database.progress_database import (
    add_progress_project,
    delete_progress_project,
    get_progress_project,
    init_progress_db,
    list_progress_projects,
    update_progress_project,
)
from app.models.project import ProjectCreate, ProjectUpdate
from app.models.task import TaskCreate
from app.runners.catalog import list_catalog, sync_catalog
from app.services.workflow_service import run_all_workflows, run_workflow

# ---------- 日志配置 ----------
Path("logs").mkdir(exist_ok=True)
logging.basicConfig(
    level=logging.INFO if not settings.debug else logging.DEBUG,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    handlers=[
        logging.FileHandler("logs/agent.log"),
        logging.StreamHandler()
    ]
)
logger = logging.getLogger(__name__)
SUCCESS_STATUSES = {"success", "already_done", "skipped", "no_work"}
AUTO_RUN_LOCK = asyncio.Lock()


def reconcile_stale_records() -> dict[str, int]:
    """Repair old queued/running records left behind by a restart or crash."""
    executions = mark_stale_executions(settings.execution_timeout)
    runs = mark_stale_runs(settings.execution_timeout)
    if executions or runs:
        logger.warning("Reconciled stale records: executions=%s runs=%s", executions, runs)
    return {"executions": executions, "runs": runs}


def remove_execution_artifacts(execution_ids: list[int]) -> int:
    """Remove only numeric execution artifact directories under the configured log root."""
    root = Path(settings.execution_log_root)
    if not root.is_absolute():
        root = Path(__file__).resolve().parent / root
    root = root.resolve()
    removed = 0
    for execution_id in execution_ids:
        target = (root / str(int(execution_id))).resolve()
        try:
            target.relative_to(root)
        except ValueError:
            continue
        if target.is_dir():
            shutil.rmtree(target)
            removed += 1
    return removed


async def run_startup_scripts():
    """在后台异步执行开机脚本"""
    if not settings.startup_script_ids:
        return
    logger.info(f"开始执行开机脚本: {settings.startup_script_ids}")
    for script_id in settings.startup_script_ids:
        try:
            logger.info(f"正在执行开机脚本 {script_id}...")
            result = await execute_script(script_id, max_retries=1)
            if result["status"] in {"failed", "cancelled"}:
                await send_failure_notification(
                    script_id=script_id,
                    execution_id=result.get("execution_id"),
                    error=result.get("error") or result.get("output", "")[:200]
                )
                logger.warning(f"开机脚本 {script_id} 执行失败，已发送通知")
            else:
                logger.info(f"开机脚本 {script_id} 执行成功")
        except Exception as e:
            logger.error(f"开机脚本 {script_id} 执行异常: {e}")
            await send_failure_notification(
                script_id=script_id,
                execution_id=None,
                error=str(e)
            )
    logger.info("所有开机脚本执行完成")


async def run_auto_catalog() -> dict:
    """Run enabled workflows once for the current UTC day."""
    async with AUTO_RUN_LOCK:
        sync_result = sync_catalog()
        selected_workflow_ids = None
        if settings.auto_run_catalog_ids:
            selected_catalog_ids = set(settings.auto_run_catalog_ids)
            selected_script_ids = {
                item["script_id"]
                for item in sync_result.get("scripts", [])
                if item["id"] in selected_catalog_ids
            }
            selected_workflow_ids = {
                workflow["id"]
                for workflow in get_workflows()
                if any(
                    step["script_id"] in selected_script_ids
                    for step in get_workflow_steps(workflow["id"])
                )
            }
        workflow_results = await run_all_workflows(
            trigger="schedule",
            idempotency_prefix="daily",
            workflow_ids=selected_workflow_ids,
        )
        return {
            "status": "completed",
            "catalog_scripts": sync_result.get("total", 0),
            "selected_workflows": len(selected_workflow_ids) if selected_workflow_ids is not None else None,
            "workflows": workflow_results,
        }


async def auto_run_loop():
    await run_auto_catalog()
    interval = int(settings.auto_run_interval_minutes)
    if interval <= 0:
        return
    while True:
        await asyncio.sleep(interval * 60)
        try:
            await run_auto_catalog()
        except Exception:
            logger.exception("Automatic catalog run failed")


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    init_progress_db()
    if settings.sync_runner_catalog_on_startup:
        try:
            sync_result = sync_catalog()
            logger.info("Runner catalog synced: %s scripts", sync_result.get("total", 0))
        except Exception as exc:
            logger.error("Runner catalog sync failed: %s", exc, exc_info=True)
    logger.info(f"AirDrop 服务启动 (debug={settings.debug})")
    auto_run_task = None
    # Automatic execution is intentionally disabled. Scripts can still be
    # started explicitly through the frontend/API manual-run endpoints.
    if settings.auto_run_on_startup or settings.startup_script_ids:
        logger.warning(
            "Automatic execution settings were provided, but startup and "
            "scheduled runs are disabled; use a manual run instead."
        )
    logger.info("AirDrop 服务已就绪")
    yield
    if auto_run_task:
        auto_run_task.cancel()
    logger.info("AirDrop 服务已关闭")


app = FastAPI(lifespan=lifespan, title="AirDrop API", version="1.0.0")
app.mount("/static", StaticFiles(directory="static"), name="static")

@app.middleware("http")
async def api_key_middleware(request: Request, call_next):
    if settings.api_key and request.url.path not in {"/", "/health"} and not request.url.path.startswith("/static/"):
        provided_key = request.headers.get("X-API-Key", "")
        if not secrets.compare_digest(provided_key, settings.api_key):
            return JSONResponse(status_code=401, content={"detail": "Missing or invalid X-API-Key"})
    return await call_next(request)


@app.get("/")
async def root():
    return RedirectResponse(url="/static/index.html")


@app.get("/health")
async def health():
    return {
        "status": "ok",
        "execution_platform": settings.execution_platform,
        "windows_worker_configured": bool(settings.windows_worker_url),
    }


# ---------- Projects ----------
@app.post("/projects")
def create_project(project: ProjectCreate):
    try:
        project_id = add_project(project.name, project.url)
        return {"message": "project created", "id": project_id}
    except Exception as e:
        logger.error(f"创建项目失败: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/projects")
def list_projects():
    projects = get_projects()
    return {"count": len(projects), "data": projects}


@app.get("/projects/{project_id}")
def get_project_detail(project_id: int):
    project = get_project(project_id)
    if not project:
        raise HTTPException(status_code=404, detail="Project not found")
    return project


@app.delete("/projects/{project_id}")
def remove_project(project_id: int):
    if not get_project(project_id):
        raise HTTPException(status_code=404, detail="Project not found")
    delete_project(project_id)
    return {"message": "project deleted"}


@app.put("/projects/{project_id}")
def edit_project(project_id: int, project: ProjectUpdate):
    if not get_project(project_id):
        raise HTTPException(status_code=404, detail="Project not found")
    update_project(project_id, project.name, project.url)
    return {"message": "project updated"}


# ---------- Tasks ----------
@app.post("/tasks")
def create_task(task: TaskCreate):
    if not get_project(task.project_id):
        raise HTTPException(status_code=404, detail="Project not found")
    try:
        task_id = add_task(task.project_id, task.name, task.description)
        return {"message": "task created", "id": task_id}
    except Exception as e:
        logger.error(f"创建任务失败: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/tasks")
def list_tasks():
    return get_tasks()


@app.get("/projects/{project_id}/tasks")
def list_project_tasks(project_id: int):
    return get_tasks_by_project(project_id)


@app.get("/tasks/{task_id}")
def get_task_detail(task_id: int):
    task = get_task(task_id)
    if not task:
        raise HTTPException(status_code=404, detail="Task not found")
    return task


@app.delete("/tasks/{task_id}")
def remove_task(task_id: int):
    if not get_task(task_id):
        raise HTTPException(status_code=404, detail="Task not found")
    delete_task(task_id)
    return {"message": "task deleted"}


# ---------- Scripts ----------
@app.post("/scripts")
def create_script(script: ScriptCreate):
    if not get_task(script.task_id):
        raise HTTPException(status_code=404, detail="Task not found")
    try:
        script_id = add_script(script.task_id, script.name, script.path)
        return {"message": "script created", "id": script_id}
    except Exception as e:
        logger.error(f"创建脚本失败: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/scripts")
def list_scripts():
    return get_scripts()


@app.get("/scripts/{script_id}")
def get_script_detail(script_id: int):
    script = get_script(script_id)
    if not script:
        raise HTTPException(status_code=404, detail="Script not found")
    return script


@app.delete("/scripts/{script_id}")
def remove_script(script_id: int):
    script = get_script(script_id)
    if not script:
        raise HTTPException(status_code=404, detail="Script not found")
    delete_script(script_id)
    return {"message": "script deleted"}


# ---------- Execution ----------
@app.post("/execute/{script_id}")
async def execute_script_endpoint(
    script_id: int,
    max_retries: int = Query(default=2, ge=0, le=5),
    background: bool = Query(default=False),
):
    try:
        if not get_script(script_id):
            raise HTTPException(status_code=404, detail="Script not found")

        if background:
            execution_id = add_execution(script_id, "queued", "", datetime.utcnow())

            async def run_background() -> None:
                try:
                    result = await execute_script(
                        script_id,
                        max_retries=max_retries,
                        execution_id=execution_id,
                    )
                    if result["status"] == "failed":
                        await send_failure_notification(
                            script_id=script_id,
                            execution_id=result.get("execution_id"),
                            error=result.get("error") or result.get("output", "")[:200],
                        )
                except asyncio.CancelledError:
                    update_execution(
                        execution_id,
                        status="cancelled",
                        error="Execution cancelled before it started",
                        finished_at=datetime.utcnow(),
                    )
                    raise
                except Exception as exc:
                    logger.error("后台执行脚本 %s 异常: %s", script_id, exc, exc_info=True)
                    update_execution(
                        execution_id,
                        status="failed",
                        error=str(exc),
                        finished_at=datetime.utcnow(),
                    )

            task = asyncio.create_task(run_background())
            register_execution_task(execution_id, task)

            def cleanup_background(done_task: asyncio.Task) -> None:
                unregister_execution_task(execution_id, done_task)
                if done_task.cancelled():
                    update_execution(
                        execution_id,
                        status="cancelled",
                        error="Execution cancelled before it started",
                        finished_at=datetime.utcnow(),
                    )

            task.add_done_callback(cleanup_background)
            return {
                "execution_id": execution_id,
                "status": "queued",
                "message": "Execution queued",
                "retryable": False,
            }

        result = await execute_script(script_id, max_retries=max_retries)
        if result["status"] == "failed":
            await send_failure_notification(
                script_id=script_id,
                execution_id=result.get("execution_id"),
                error=result.get("error") or result.get("output", "")[:200]
            )
        return result
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"执行脚本 {script_id} 异常: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/executions/{execution_id}/cancel")
async def cancel_execution_endpoint(execution_id: int):
    execution = get_execution(execution_id)
    if not execution:
        raise HTTPException(status_code=404, detail="Execution not found")
    if execution["status"] not in {"queued", "running"}:
        return {
            "execution_id": execution_id,
            "status": execution["status"],
            "cancelled": False,
            "message": "Execution is no longer running",
        }

    requested = cancel_active_execution(execution_id)
    if not requested:
        requested = cancel_persisted_process(execution_id)
        if requested:
            update_execution(
                execution_id,
                status="cancelled",
                error="Execution cancelled through its persisted process handle",
                finished_at=datetime.utcnow(),
            )
            return {
                "execution_id": execution_id,
                "status": "cancelled",
                "cancelled": True,
                "message": "已通过持久化进程句柄中断执行",
            }

    if not requested:
        latest = get_execution(execution_id) or execution
        if latest["status"] in {"queued", "running"}:
            update_execution(
                execution_id,
                status="cancelled",
                error="Execution record reconciled as cancelled; no live process handle was found",
                finished_at=datetime.utcnow(),
            )
            return {
                "execution_id": execution_id,
                "status": "cancelled",
                "cancelled": True,
                "message": "已清理残留执行记录；未发现仍在运行的进程",
            }
        return {
            "execution_id": execution_id,
            "status": latest["status"],
            "cancelled": False,
            "message": "Execution already finished",
        }
    return {
        "execution_id": execution_id,
        "status": "cancelling",
        "cancelled": True,
        "message": "Cancellation requested",
    }


@app.get("/executions")
def list_executions(limit: int = Query(default=50, ge=1, le=500)):
    reconcile_stale_records()
    return get_executions(limit=limit)


@app.get("/executions/{execution_id}")
def get_execution_detail(execution_id: int):
    execution = get_execution(execution_id)
    if not execution:
        raise HTTPException(status_code=404, detail="Execution not found")
    return execution


@app.delete("/executions/{execution_id}")
def remove_execution(execution_id: int):
    execution = get_execution(execution_id)
    if not execution:
        raise HTTPException(status_code=404, detail="Execution not found")
    delete_execution(execution_id)
    return {"message": "execution deleted"}


@app.delete("/history")
def clear_history():
    """Clear finished execution/workflow history without deleting project definitions."""
    reconcile_stale_records()
    deleted = clear_execution_history()
    execution_ids = deleted.pop("execution_ids", [])
    deleted["artifacts"] = remove_execution_artifacts(execution_ids)
    return {
        "status": "success",
        "message": "历史执行记录已清除，正在运行的任务已保留",
        "deleted": deleted,
    }


@app.get("/executions/{execution_id}/accounts")
def get_execution_accounts(execution_id: int):
    execution = get_execution(execution_id)
    if not execution:
        raise HTTPException(status_code=404, detail="Execution not found")
    records = get_account_results_by_execution(execution_id)
    return {
        "execution_id": execution_id,
        "script_id": execution["script_id"],
        "total": len(records),
        "accounts": records
    }


@app.get("/scripts/{script_id}/stats")
def get_script_stats(script_id: int):
    if not get_script(script_id):
        raise HTTPException(status_code=404, detail="Script not found")
    return get_account_stats_by_script(script_id)


# ---------- Dashboard ----------
@app.get("/executions/{execution_id}/artifacts")
def list_execution_artifacts(execution_id: int):
    execution = get_execution(execution_id)
    if not execution:
        raise HTTPException(status_code=404, detail="Execution not found")
    root = Path(settings.execution_log_root)
    if not root.is_absolute():
        root = Path(__file__).resolve().parent / root
    root = root.resolve()
    artifact_dir = (root / str(execution_id)).resolve()
    try:
        artifact_dir.relative_to(root)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail="Invalid execution artifact path") from exc
    if not artifact_dir.is_dir():
        return {"execution_id": execution_id, "files": []}
    files = [str(path.relative_to(artifact_dir)) for path in artifact_dir.rglob("*") if path.is_file()]
    return {"execution_id": execution_id, "files": sorted(files)}


@app.get("/runners/catalog")
def get_runners_catalog():
    """Return the read-only external script catalog without executing anything."""
    return {"count": len(list_catalog()), "scripts": list_catalog()}


@app.post("/runners/catalog/sync")
def sync_runners_catalog():
    """Register catalog entries in AirDrop' database; the source stays read-only."""
    try:
        return sync_catalog()
    except Exception as exc:
        logger.error("Runner catalog sync failed: %s", exc, exc_info=True)
        raise HTTPException(status_code=500, detail=str(exc))


@app.post("/runners/auto-run")
async def trigger_auto_run():
    """Run all eligible catalog entries once, with the per-day duplicate guard."""
    try:
        return await run_auto_catalog()
    except Exception as exc:
        logger.error("Automatic catalog run failed: %s", exc, exc_info=True)
        raise HTTPException(status_code=500, detail=str(exc))


# ---------- Independent project progress ----------
@app.get("/progress/projects")
def list_progress_projects_endpoint():
    projects = list_progress_projects()
    return {"count": len(projects), "data": projects}


@app.post("/progress/projects")
def create_progress_project(payload: dict):
    name = str(payload.get("name", "")).strip()
    if not name:
        raise HTTPException(status_code=422, detail="项目名称不能为空")
    return add_progress_project(
        name=name,
        url=str(payload.get("url", "")).strip(),
        target_time=str(payload.get("target_time", "")).strip(),
        progress=str(payload.get("progress", "未开始")).strip() or "未开始",
        account=str(payload.get("account", "")).strip(),
        notes=str(payload.get("notes", "")).strip(),
    )


@app.get("/progress/projects/{project_id}")
def get_progress_project_endpoint(project_id: int):
    project = get_progress_project(project_id)
    if not project:
        raise HTTPException(status_code=404, detail="进度项目不存在")
    return project


@app.put("/progress/projects/{project_id}")
def update_progress_project_endpoint(project_id: int, payload: dict):
    if "name" in payload and not str(payload.get("name", "")).strip():
        raise HTTPException(status_code=422, detail="项目名称不能为空")
    fields = {
        key: str(payload[key]).strip()
        for key in ("name", "url", "target_time", "progress", "account", "notes")
        if key in payload
    }
    project = update_progress_project(project_id, **fields)
    if not project:
        raise HTTPException(status_code=404, detail="进度项目不存在")
    return project


@app.delete("/progress/projects/{project_id}")
def delete_progress_project_endpoint(project_id: int):
    if not delete_progress_project(project_id):
        raise HTTPException(status_code=404, detail="进度项目不存在")
    return {"status": "success", "message": "进度项目已删除"}


# ---------- Workflows / runs / workers ----------
@app.get("/workflows")
def list_workflows_endpoint():
    reconcile_stale_records()
    workflows = get_workflows()
    return {"count": len(workflows), "data": workflows}


@app.get("/workflows/{workflow_id}")
def get_workflow_detail(workflow_id: int):
    workflow = get_workflow(workflow_id)
    if not workflow:
        raise HTTPException(status_code=404, detail="Workflow not found")
    return {"workflow": workflow, "steps": get_workflow_steps(workflow_id)}


@app.post("/workflows/{workflow_id}/run")
async def run_workflow_endpoint(
    workflow_id: int,
    trigger: str = Query(default="manual", min_length=1, max_length=40),
    idempotency_key: str | None = Query(default=None, max_length=200),
    max_retries: int = Query(default=1, ge=0, le=5),
):
    try:
        return await run_workflow(
            workflow_id,
            trigger=trigger,
            idempotency_key=idempotency_key,
            max_retries=max_retries,
        )
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except Exception as exc:
        logger.error("Workflow %s failed to start: %s", workflow_id, exc, exc_info=True)
        raise HTTPException(status_code=500, detail=str(exc)) from exc


@app.get("/runs")
def list_runs_endpoint(limit: int = Query(default=50, ge=1, le=500)):
    reconcile_stale_records()
    return get_runs(limit=limit)


@app.get("/runs/{run_id}")
def get_run_detail(run_id: int):
    run = get_run(run_id)
    if not run:
        raise HTTPException(status_code=404, detail="Run not found")
    return {
        "run": run,
        "attempts": get_attempts(run_id),
        "artifacts": get_artifacts(run_id),
    }


@app.get("/workers")
def list_workers_endpoint():
    workers = get_workers()
    return {"count": len(workers), "data": workers}


@app.post("/workers/register")
def register_worker_endpoint(payload: dict):
    name = str(payload.get("name", "")).strip()
    platform = str(payload.get("platform", "")).strip()
    if not name or not platform:
        raise HTTPException(status_code=422, detail="Worker name and platform are required")
    return register_worker(
        name=name,
        platform=platform,
        capabilities=payload.get("capabilities") or {},
        metadata=payload.get("metadata") or {},
    )


@app.post("/workers/{worker_id}/heartbeat")
def heartbeat_worker_endpoint(worker_id: int, payload: dict | None = None):
    payload = payload or {}
    worker = heartbeat_worker(
        worker_id,
        status=str(payload.get("status", "online")),
        metadata=payload.get("metadata"),
    )
    if not worker:
        raise HTTPException(status_code=404, detail="Worker not found")
    return worker


@app.get("/dashboard/stats")
def get_stats():
    projects = get_projects()
    tasks = get_tasks()
    scripts = get_scripts()
    executions = get_executions(limit=1000)
    total = len(executions)
    success = sum(1 for e in executions if e["status"] in SUCCESS_STATUSES)
    failed = total - success
    return {
        "projects": len(projects),
        "tasks": len(tasks),
        "scripts": len(scripts),
        "executions": total,
        "success": success,
        "failed": failed,
        "success_rate": round(success / total * 100, 2) if total else 0
    }


@app.get("/dashboard/today")
def get_today_stats():
    reconcile_stale_records()
    records = get_today_executions()
    success_count = sum(1 for r in records if r["status"] in SUCCESS_STATUSES)
    failed_count = sum(1 for r in records if r["status"] not in SUCCESS_STATUSES)
    return {
        "total": len(records),
        "success": success_count,
        "failed": failed_count,
        "records": records
    }
