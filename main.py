import asyncio
import logging
from contextlib import asynccontextmanager
from pathlib import Path
from fastapi import FastAPI, HTTPException
from fastapi.staticfiles import StaticFiles
from fastapi.responses import RedirectResponse
from datetime import datetime

from app.config import settings
from app.models.script import ScriptCreate
from app.services.execution_service import execute_script
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
    get_execution,              # ✅ 已添加
    get_today_executions,
    delete_execution,
    get_account_results_by_execution,
    get_account_stats_by_script,
)
from app.models.project import ProjectCreate, ProjectUpdate
from app.models.task import TaskCreate

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


async def run_startup_scripts():
    """在后台异步执行开机脚本"""
    if not settings.startup_script_ids:
        return
    logger.info(f"开始执行开机脚本: {settings.startup_script_ids}")
    for script_id in settings.startup_script_ids:
        try:
            logger.info(f"正在执行开机脚本 {script_id}...")
            result = await execute_script(
                script_id,
                max_retries=1,
                timeout_seconds=settings.script_timeout_seconds,
            )
            if result["status"] == "failed":
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


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    logger.info(f"AgentOps 服务启动 (debug={settings.debug})")
    if settings.startup_script_ids:
        asyncio.create_task(run_startup_scripts())
    logger.info("AgentOps 服务已就绪")
    yield
    logger.info("AgentOps 服务已关闭")


app = FastAPI(lifespan=lifespan, title="AgentOps API", version="1.0.0")
app.mount("/static", StaticFiles(directory="static"), name="static")


@app.get("/")
async def root():
    return RedirectResponse(url="/static/index.html")


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
    return {"count": len(get_projects()), "data": get_projects()}


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
async def execute_script_endpoint(script_id: int, max_retries: int = 2):
    try:
        if not get_script(script_id):
            raise HTTPException(status_code=404, detail="Script not found")
        result = await execute_script(
            script_id,
            max_retries=max_retries,
            timeout_seconds=settings.script_timeout_seconds,
        )
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


@app.get("/executions")
def list_executions(limit: int = 50):
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
@app.get("/dashboard/stats")
def get_stats():
    projects = get_projects()
    tasks = get_tasks()
    scripts = get_scripts()
    executions = get_executions(limit=1000)
    total = len(executions)
    success = sum(1 for e in executions if e["status"] == "success")
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
    records = get_today_executions()
    success_count = sum(1 for r in records if r["status"] == "success")
    failed_count = sum(1 for r in records if r["status"] == "failed")
    return {
        "total": len(records),
        "success": success_count,
        "failed": failed_count,
        "records": records
    }
