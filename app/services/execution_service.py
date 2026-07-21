import asyncio
import json
import logging
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, Any, Optional, Tuple

from app.database.database import (
    get_db_connection,
    add_execution,
    update_execution,
    get_script,
    add_account_results,
)

logger = logging.getLogger(__name__)
BASE_DIR = Path(__file__).resolve().parent.parent.parent


async def execute_script(
    script_id: int,
    max_retries: int = 2,
    retry_delay: int = 5,
    timeout_seconds: float = 300.0,
) -> Dict[str, Any]:
    """
    执行脚本，失败时自动重试

    Args:
        script_id: 脚本ID
        max_retries: 最大重试次数
        retry_delay: 重试间隔（秒）

    Returns:
        执行结果
    """
    last_result = None

    for attempt in range(max_retries + 1):
        try:
            if attempt > 0:
                logger.warning(f"脚本 {script_id} 第 {attempt} 次重试...")
                await asyncio.sleep(retry_delay)

            result = await _execute_script_once(script_id, timeout_seconds)

            if result["status"] == "success":
                if attempt > 0:
                    logger.info(f"脚本 {script_id} 第 {attempt} 次重试成功")
                return result

            last_result = result
            logger.warning(f"脚本 {script_id} 第 {attempt + 1} 次执行失败")

        except Exception as e:
            logger.error(f"脚本 {script_id} 执行异常: {e}")
            last_result = {
                "execution_id": None,
                "status": "failed",
                "output": "",
                "error": str(e),
                "duration": 0.0
            }

    logger.error(f"脚本 {script_id} 所有重试均失败")
    return last_result or {
        "execution_id": None,
        "status": "failed",
        "output": "",
        "error": "所有重试均失败",
        "duration": 0.0
    }


async def _execute_script_once(
    script_id: int,
    timeout_seconds: float = 300.0,
) -> Dict[str, Any]:
    """单次执行脚本（内部函数）"""
    # 1. 获取脚本路径
    script_info = get_script(script_id)
    if not script_info:
        raise ValueError(f"Script {script_id} not found")

    scripts_dir = (BASE_DIR / "scripts").resolve()
    script_abs_path = (BASE_DIR / script_info["path"]).resolve()
    if not script_abs_path.is_relative_to(scripts_dir):
        raise ValueError("Script path must stay inside the scripts directory")
    if script_abs_path.suffix.lower() != ".py":
        raise ValueError("Only Python scripts are supported")
    if not script_abs_path.exists():
        raise FileNotFoundError(f"Script file not found: {script_abs_path}")

    # 2. 创建执行记录
    started_at = datetime.now(timezone.utc)
    execution_id = add_execution(script_id, "running", "", started_at)

    # 3. 执行脚本
    stdout_str = ""
    stderr_str = ""
    returncode = -1
    finished_at = None
    duration = 0.0

    try:
        process = await asyncio.create_subprocess_exec(
            sys.executable, str(script_abs_path),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=script_abs_path.parent
        )
        try:
            stdout, stderr = await asyncio.wait_for(
                process.communicate(),
                timeout=timeout_seconds,
            )
        except asyncio.TimeoutError:
            process.kill()
            await process.wait()
            stdout = b""
            stderr = f"Process timed out after {timeout_seconds:g} seconds".encode()
        stdout_str = stdout.decode().strip()
        stderr_str = stderr.decode().strip()
        returncode = process.returncode
        finished_at = datetime.now(timezone.utc)
        duration = (finished_at - started_at).total_seconds()

    except Exception as e:
        stderr_str = f"Process error: {str(e)}"
        finished_at = datetime.now(timezone.utc)
        duration = (finished_at - started_at).total_seconds()
        returncode = -1

    # 4. 解析 JSON 判断业务状态
    final_status, summary, final_error, parsed_data = _parse_result_with_data(
        stdout_str, stderr_str, returncode
    )

    # 5. 更新数据库
    update_execution(
        execution_id,
        status=final_status,
        output=stdout_str,
        error=final_error,
        finished_at=finished_at,
        duration=duration
    )

    # 6. ✅ 保存账号明细（如果存在）
    if parsed_data and "accounts" in parsed_data and isinstance(parsed_data["accounts"], list):
        try:
            add_account_results(execution_id, script_id, parsed_data["accounts"])
            logger.info(f"保存了 {len(parsed_data['accounts'])} 个账号明细 (执行 {execution_id})")
        except Exception as e:
            logger.warning(f"保存账号明细失败（执行 {execution_id}）: {e}")

    # 7. 写入日志
    if final_status == "success":
        log_message = f"[{finished_at}] Script {script_id} SUCCESS: {summary[:200]}"
        logger.info(log_message)
    else:
        log_message = (
            f"[{finished_at}] Script {script_id} FAILED\n"
            f"STDOUT:\n{stdout_str}\n"
            f"STDERR:\n{stderr_str}"
        )
        logger.error(log_message)

    return {
        "execution_id": execution_id,
        "status": final_status,
        "output": stdout_str,
        "error": final_error,
        "duration": duration
    }


def _parse_result_with_data(
    stdout_str: str,
    stderr_str: str,
    returncode: int
) -> Tuple[str, str, Optional[str], Optional[Dict[str, Any]]]:
    """
    解析脚本输出，返回 (状态, 摘要, 错误, 完整解析后的数据)
    """
    final_status = "failed"
    summary = stdout_str or stderr_str or "No output"
    final_error = stderr_str if stderr_str else None
    parsed_data = None

    lines = stdout_str.splitlines()
    if lines:
        last_line = lines[-1]
        try:
            data = json.loads(last_line)
            parsed_data = data
            if "status" in data:
                final_status = data["status"]
                summary = data.get("message", json.dumps(data))
            else:
                if returncode == 0 and not stderr_str:
                    final_status = "success"
                    summary = stdout_str
                else:
                    final_status = "failed"
                    summary = stderr_str or stdout_str
        except json.JSONDecodeError:
            if returncode == 0 and not stderr_str:
                final_status = "success"
                summary = stdout_str or "Execution completed"
            else:
                final_status = "failed"
                summary = stderr_str or stdout_str
    else:
        if returncode == 0:
            final_status = "success"
            summary = "Script executed with no output"
        else:
            final_status = "failed"
            summary = stderr_str or "Process crashed"

    return final_status, summary, final_error, parsed_data
