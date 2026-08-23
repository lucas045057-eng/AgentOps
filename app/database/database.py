import json
import sqlite3
from contextlib import contextmanager
from typing import Dict, List, Optional, Any
from datetime import datetime

from app.config import settings


@contextmanager
def get_db_connection():
    """使用上下文管理器自动管理连接生命周期"""
    conn = sqlite3.connect(settings.database_url)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON;")
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def init_db():
    """初始化数据库：创建所有表"""
    with get_db_connection() as conn:
        cursor = conn.cursor()

        # projects 表
        cursor.execute("""
        CREATE TABLE IF NOT EXISTS projects (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            url TEXT NOT NULL,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );
        """)

        # tasks 表
        cursor.execute("""
        CREATE TABLE IF NOT EXISTS tasks (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            project_id INTEGER NOT NULL,
            name TEXT NOT NULL,
            description TEXT,
            status TEXT DEFAULT 'pending',
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (project_id) REFERENCES projects (id) ON DELETE CASCADE
        );
        """)

        # scripts 表
        cursor.execute("""
        CREATE TABLE IF NOT EXISTS scripts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            task_id INTEGER NOT NULL,
            name TEXT NOT NULL,
            path TEXT NOT NULL,
            status TEXT DEFAULT 'active',
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (task_id) REFERENCES tasks (id) ON DELETE CASCADE
        );
        """)

        # executions 表
        cursor.execute("""
        CREATE TABLE IF NOT EXISTS executions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            script_id INTEGER NOT NULL,
            status TEXT NOT NULL,
            output TEXT,
            started_at TIMESTAMP,
            finished_at TIMESTAMP,
            duration REAL,
            error TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (script_id) REFERENCES scripts (id) ON DELETE CASCADE
        );
        """)

        # account_results 表
        cursor.execute("""
        CREATE TABLE IF NOT EXISTS account_results (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            execution_id INTEGER NOT NULL,
            script_id INTEGER NOT NULL,
            address TEXT,
            name TEXT,
            status TEXT,
            message TEXT,
            points INTEGER DEFAULT 0,
            error TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (execution_id) REFERENCES executions (id) ON DELETE CASCADE
        );
        """)

        # Engineering execution model. The legacy tables remain for
        # compatibility, while workflows/runs/attempts provide durable
        # orchestration state.
        cursor.execute("""
        CREATE TABLE IF NOT EXISTS workers (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL UNIQUE,
            platform TEXT NOT NULL,
            capabilities_json TEXT DEFAULT '{}',
            status TEXT DEFAULT 'offline',
            last_heartbeat TIMESTAMP,
            metadata_json TEXT DEFAULT '{}',
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );
        """)

        cursor.execute("""
        CREATE TABLE IF NOT EXISTS workflows (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            project_id INTEGER NOT NULL,
            name TEXT NOT NULL,
            description TEXT DEFAULT '',
            schedule TEXT DEFAULT '',
            timezone TEXT DEFAULT 'Asia/Shanghai',
            enabled INTEGER DEFAULT 1,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            UNIQUE(project_id, name),
            FOREIGN KEY (project_id) REFERENCES projects (id) ON DELETE CASCADE
        );
        """)

        cursor.execute("""
        CREATE TABLE IF NOT EXISTS workflow_steps (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            workflow_id INTEGER NOT NULL,
            script_id INTEGER NOT NULL,
            name TEXT NOT NULL,
            step_order INTEGER NOT NULL DEFAULT 0,
            required INTEGER DEFAULT 1,
            config_json TEXT DEFAULT '{}',
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            UNIQUE(workflow_id, script_id),
            FOREIGN KEY (workflow_id) REFERENCES workflows (id) ON DELETE CASCADE,
            FOREIGN KEY (script_id) REFERENCES scripts (id) ON DELETE CASCADE
        );
        """)

        cursor.execute("""
        CREATE TABLE IF NOT EXISTS runs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            workflow_id INTEGER NOT NULL,
            status TEXT NOT NULL DEFAULT 'queued',
            trigger TEXT DEFAULT 'manual',
            idempotency_key TEXT UNIQUE,
            worker_id INTEGER,
            started_at TIMESTAMP,
            finished_at TIMESTAMP,
            duration REAL,
            summary TEXT DEFAULT '',
            error TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (workflow_id) REFERENCES workflows (id) ON DELETE CASCADE,
            FOREIGN KEY (worker_id) REFERENCES workers (id) ON DELETE SET NULL
        );
        """)

        cursor.execute("""
        CREATE TABLE IF NOT EXISTS attempts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            run_id INTEGER NOT NULL,
            step_id INTEGER NOT NULL,
            script_id INTEGER NOT NULL,
            attempt_no INTEGER NOT NULL DEFAULT 1,
            status TEXT NOT NULL DEFAULT 'running',
            worker_id INTEGER,
            started_at TIMESTAMP,
            finished_at TIMESTAMP,
            duration REAL,
            output TEXT DEFAULT '',
            error TEXT,
            result_json TEXT DEFAULT '{}',
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            UNIQUE(run_id, step_id, attempt_no),
            FOREIGN KEY (run_id) REFERENCES runs (id) ON DELETE CASCADE,
            FOREIGN KEY (step_id) REFERENCES workflow_steps (id) ON DELETE CASCADE,
            FOREIGN KEY (script_id) REFERENCES scripts (id) ON DELETE CASCADE,
            FOREIGN KEY (worker_id) REFERENCES workers (id) ON DELETE SET NULL
        );
        """)

        cursor.execute("""
        CREATE TABLE IF NOT EXISTS artifacts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            run_id INTEGER,
            attempt_id INTEGER,
            execution_id INTEGER,
            kind TEXT NOT NULL,
            path TEXT NOT NULL,
            metadata_json TEXT DEFAULT '{}',
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (run_id) REFERENCES runs (id) ON DELETE CASCADE,
            FOREIGN KEY (attempt_id) REFERENCES attempts (id) ON DELETE CASCADE,
            FOREIGN KEY (execution_id) REFERENCES executions (id) ON DELETE CASCADE
        );
        """)

        # Keep upgrades safe for databases created by the first workflow
        # prototype, where runs did not yet have an updated_at column.
        run_columns = {
            row[1] for row in cursor.execute("PRAGMA table_info(runs)").fetchall()
        }
        if "updated_at" not in run_columns:
            cursor.execute("ALTER TABLE runs ADD COLUMN updated_at TIMESTAMP")

        # 索引
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_account_results_execution_id ON account_results(execution_id);")
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_account_results_address ON account_results(address);")
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_account_results_script_id ON account_results(script_id);")
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_executions_created_at ON executions(created_at);")
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_executions_script_id ON executions(script_id);")
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_workflows_project_id ON workflows(project_id);")
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_workflow_steps_workflow_id ON workflow_steps(workflow_id);")
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_runs_workflow_id ON runs(workflow_id);")
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_runs_created_at ON runs(created_at);")
        cursor.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS idx_runs_one_active_per_workflow "
            "ON runs(workflow_id) WHERE status IN ('queued', 'running');"
        )
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_attempts_run_id ON attempts(run_id);")
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_workers_status ON workers(status);")
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_artifacts_run_id ON artifacts(run_id);")

    recovered_executions = mark_stale_executions(settings.execution_timeout)
    recovered_runs = mark_stale_runs(settings.execution_timeout)
    interrupted_executions, interrupted_runs = recover_incomplete_records_on_startup()
    if recovered_executions:
        print(f"Recovered {recovered_executions} stale execution records")
    if recovered_runs:
        print(f"Recovered {recovered_runs} stale workflow runs")
    if interrupted_executions:
        print(f"Cancelled {interrupted_executions} incomplete execution records from the previous service")
    if interrupted_runs:
        print(f"Cancelled {interrupted_runs} incomplete workflow runs from the previous service")
    print("数据库表已检查（含 account_results）")


def mark_stale_executions(timeout_seconds: int) -> int:
    """Mark long-running records as failed after a service restart."""
    if timeout_seconds <= 0:
        return 0

    with get_db_connection() as conn:
        cursor = conn.cursor()
        cursor.execute(
            """
            UPDATE executions
               SET status = 'failed',
                   error = COALESCE(NULLIF(error, ''), 'Execution timed out or service restarted'),
                   finished_at = COALESCE(finished_at, CURRENT_TIMESTAMP),
                   duration = COALESCE(
                       duration,
                       (julianday(CURRENT_TIMESTAMP) - julianday(started_at)) * 86400
                   )
             WHERE status IN ('queued', 'running')
               AND COALESCE(started_at, created_at) < datetime('now', ?)
            """,
            (f"-{int(timeout_seconds)} seconds",),
        )
        return cursor.rowcount


def mark_stale_runs(timeout_seconds: int) -> int:
    """Mark workflow runs and their unfinished attempts as failed after a restart."""
    if timeout_seconds <= 0:
        return 0

    with get_db_connection() as conn:
        stale_rows = conn.execute(
            """
            SELECT id
              FROM runs
             WHERE status IN ('queued', 'running')
               AND COALESCE(updated_at, started_at, created_at) < datetime('now', ?)
            """,
            (f"-{int(timeout_seconds)} seconds",),
        ).fetchall()
        run_ids = [int(row[0]) for row in stale_rows]
        if not run_ids:
            return 0

        placeholders = ", ".join("?" for _ in run_ids)
        reason = "Workflow timed out or service restarted before completion"
        conn.execute(
            f"""
            UPDATE runs
               SET status = 'failed',
                   error = COALESCE(NULLIF(error, ''), ?),
                   summary = COALESCE(NULLIF(summary, ''), 'Workflow recovered as failed'),
                   finished_at = COALESCE(finished_at, CURRENT_TIMESTAMP),
                   duration = COALESCE(
                       duration,
                       (julianday(CURRENT_TIMESTAMP) - julianday(COALESCE(started_at, created_at))) * 86400
                   ),
                   updated_at = CURRENT_TIMESTAMP
             WHERE id IN ({placeholders})
            """,
            [reason, *run_ids],
        )
        conn.execute(
            f"""
            UPDATE attempts
               SET status = 'failed',
                   error = COALESCE(NULLIF(error, ''), ?),
                   finished_at = COALESCE(finished_at, CURRENT_TIMESTAMP)
             WHERE run_id IN ({placeholders})
               AND status IN ('queued', 'running')
            """,
            [reason, *run_ids],
        )
        return len(run_ids)


def recover_incomplete_records_on_startup() -> tuple[int, int]:
    """Finalize records left active by the previous service instance."""
    with get_db_connection() as conn:
        execution_rows = conn.execute(
            "SELECT id FROM executions WHERE status IN ('queued', 'running')"
        ).fetchall()
        run_rows = conn.execute(
            "SELECT id FROM runs WHERE status IN ('queued', 'running')"
        ).fetchall()
        execution_ids = [int(row[0]) for row in execution_rows]
        run_ids = [int(row[0]) for row in run_rows]

        if execution_ids:
            conn.execute(
                """
                UPDATE executions
                   SET status = 'cancelled',
                       error = COALESCE(NULLIF(error, ''), 'Execution cancelled because the service restarted'),
                       finished_at = COALESCE(finished_at, CURRENT_TIMESTAMP),
                       duration = COALESCE(
                           duration,
                           (julianday(CURRENT_TIMESTAMP) - julianday(COALESCE(started_at, created_at))) * 86400
                       )
                 WHERE status IN ('queued', 'running')
                """
            )

        if run_ids:
            conn.execute(
                """
                UPDATE runs
                   SET status = 'cancelled',
                       error = COALESCE(NULLIF(error, ''), 'Workflow cancelled because the service restarted'),
                       summary = COALESCE(NULLIF(summary, ''), 'Workflow interrupted by service restart'),
                       finished_at = COALESCE(finished_at, CURRENT_TIMESTAMP),
                       duration = COALESCE(
                           duration,
                           (julianday(CURRENT_TIMESTAMP) - julianday(COALESCE(started_at, created_at))) * 86400
                       ),
                       updated_at = CURRENT_TIMESTAMP
                 WHERE status IN ('queued', 'running')
                """
            )
            placeholders = ", ".join("?" for _ in run_ids)
            conn.execute(
                f"""
                UPDATE attempts
                   SET status = 'cancelled',
                       error = COALESCE(NULLIF(error, ''), 'Attempt cancelled because the service restarted'),
                       finished_at = COALESCE(finished_at, CURRENT_TIMESTAMP)
                 WHERE run_id IN ({placeholders})
                   AND status IN ('queued', 'running')
                """,
                run_ids,
            )

        return len(execution_ids), len(run_ids)

def reset_sequence_if_empty(table_name: str):
    """如果表为空，重置自增序列"""
    with get_db_connection() as conn:
        cursor = conn.cursor()
        cursor.execute(f"SELECT COUNT(*) FROM {table_name}")
        if cursor.fetchone()[0] == 0:
            cursor.execute(f"DELETE FROM sqlite_sequence WHERE name='{table_name}'")


# ---------- Project CRUD ----------
def add_project(name: str, url: str) -> int:
    with get_db_connection() as conn:
        cursor = conn.cursor()
        cursor.execute("INSERT INTO projects (name, url) VALUES (?, ?)", (name, url))
        return cursor.lastrowid


def get_projects() -> List[Dict[str, Any]]:
    with get_db_connection() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT id, name, url FROM projects ORDER BY id")
        return [dict(row) for row in cursor.fetchall()]


def get_project(project_id: int) -> Optional[Dict[str, Any]]:
    with get_db_connection() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT id, name, url FROM projects WHERE id = ?", (project_id,))
        row = cursor.fetchone()
        return dict(row) if row else None


def delete_project(project_id: int):
    with get_db_connection() as conn:
        cursor = conn.cursor()
        cursor.execute("DELETE FROM projects WHERE id = ?", (project_id,))
    reset_sequence_if_empty('projects')


def update_project(project_id: int, name: str, url: str):
    with get_db_connection() as conn:
        cursor = conn.cursor()
        cursor.execute("UPDATE projects SET name = ?, url = ? WHERE id = ?", (name, url, project_id))


# ---------- Task CRUD ----------
def add_task(project_id: int, name: str, description: str = "") -> int:
    with get_db_connection() as conn:
        cursor = conn.cursor()
        cursor.execute(
            "INSERT INTO tasks (project_id, name, description) VALUES (?, ?, ?)",
            (project_id, name, description)
        )
        return cursor.lastrowid


def get_tasks() -> List[Dict[str, Any]]:
    with get_db_connection() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT * FROM tasks ORDER BY id")
        return [dict(row) for row in cursor.fetchall()]


def get_tasks_by_project(project_id: int) -> List[Dict[str, Any]]:
    with get_db_connection() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT * FROM tasks WHERE project_id = ? ORDER BY id", (project_id,))
        return [dict(row) for row in cursor.fetchall()]


def get_task(task_id: int) -> Optional[Dict[str, Any]]:
    with get_db_connection() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT * FROM tasks WHERE id = ?", (task_id,))
        row = cursor.fetchone()
        return dict(row) if row else None


def delete_task(task_id: int):
    with get_db_connection() as conn:
        cursor = conn.cursor()
        cursor.execute("DELETE FROM tasks WHERE id = ?", (task_id,))
    reset_sequence_if_empty('tasks')


# ---------- Script CRUD ----------
def add_script(task_id: int, name: str, path: str) -> int:
    with get_db_connection() as conn:
        cursor = conn.cursor()
        cursor.execute(
            "INSERT INTO scripts (task_id, name, path) VALUES (?, ?, ?)",
            (task_id, name, path)
        )
        return cursor.lastrowid


def get_scripts() -> List[Dict[str, Any]]:
    with get_db_connection() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT * FROM scripts ORDER BY id")
        return [dict(row) for row in cursor.fetchall()]


def get_script(script_id: int) -> Optional[Dict[str, Any]]:
    with get_db_connection() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT * FROM scripts WHERE id = ?", (script_id,))
        row = cursor.fetchone()
        return dict(row) if row else None


def update_script(script_id: int, task_id: int, name: str, path: str) -> None:
    with get_db_connection() as conn:
        conn.execute(
            "UPDATE scripts SET task_id = ?, name = ?, path = ? WHERE id = ?",
            (task_id, name, path, script_id),
        )


def delete_script(script_id: int):
    with get_db_connection() as conn:
        cursor = conn.cursor()
        cursor.execute("DELETE FROM scripts WHERE id = ?", (script_id,))
    reset_sequence_if_empty('scripts')


# ---------- Execution CRUD ----------
def add_execution(script_id: int, status: str, output: str = "", started_at=None) -> int:
    if started_at is None:
        started_at = datetime.utcnow()
    with get_db_connection() as conn:
        cursor = conn.cursor()
        cursor.execute(
            "INSERT INTO executions (script_id, status, output, started_at) VALUES (?, ?, ?, ?)",
            (script_id, status, output, started_at)
        )
        return cursor.lastrowid


def get_executions(limit: int = 100) -> List[Dict[str, Any]]:
    with get_db_connection() as conn:
        cursor = conn.cursor()
        cursor.execute(
            "SELECT * FROM executions ORDER BY created_at DESC LIMIT ?",
            (limit,)
        )
        return [dict(row) for row in cursor.fetchall()]


# ✅ 新增：根据 ID 获取单条执行记录
def get_execution(execution_id: int) -> Optional[Dict[str, Any]]:
    with get_db_connection() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT * FROM executions WHERE id = ?", (execution_id,))
        row = cursor.fetchone()
        return dict(row) if row else None


def update_execution(execution_id: int, **kwargs):
    """更新执行记录"""
    if not kwargs:
        return
    with get_db_connection() as conn:
        cursor = conn.cursor()
        set_clause = ", ".join([f"{k} = ?" for k in kwargs.keys()])
        values = list(kwargs.values()) + [execution_id]
        cursor.execute(f"UPDATE executions SET {set_clause} WHERE id = ?", values)


def delete_execution(execution_id: int):
    with get_db_connection() as conn:
        cursor = conn.cursor()
        cursor.execute("DELETE FROM executions WHERE id = ?", (execution_id,))
    reset_sequence_if_empty('executions')


def clear_execution_history() -> Dict[str, Any]:
    """Delete finished execution/workflow history while preserving active work."""
    with get_db_connection() as conn:
        execution_rows = conn.execute(
            "SELECT id FROM executions WHERE status NOT IN ('queued', 'running')"
        ).fetchall()
        run_rows = conn.execute(
            "SELECT id FROM runs WHERE status NOT IN ('queued', 'running')"
        ).fetchall()
        active_execution_count = conn.execute(
            "SELECT COUNT(*) FROM executions WHERE status IN ('queued', 'running')"
        ).fetchone()[0]
        active_run_count = conn.execute(
            "SELECT COUNT(*) FROM runs WHERE status IN ('queued', 'running')"
        ).fetchone()[0]

        conn.execute("DELETE FROM executions WHERE status NOT IN ('queued', 'running')")
        conn.execute("DELETE FROM runs WHERE status NOT IN ('queued', 'running')")

        return {
            "execution_ids": [int(row[0]) for row in execution_rows],
            "executions": len(execution_rows),
            "runs": len(run_rows),
            "active_executions": int(active_execution_count),
            "active_runs": int(active_run_count),
        }


def get_today_executions() -> List[Dict[str, Any]]:
    """获取今日执行记录"""
    # SQLite CURRENT_TIMESTAMP is UTC; use the same timezone for daily queries.
    today = datetime.utcnow().replace(hour=0, minute=0, second=0, microsecond=0)
    with get_db_connection() as conn:
        cursor = conn.cursor()
        cursor.execute(
            "SELECT id, script_id, status, output, error, created_at FROM executions "
            "WHERE created_at >= ? ORDER BY created_at DESC",
            (today,)
        )
        return [dict(row) for row in cursor.fetchall()]


# ---------- Account Results CRUD ----------
def add_account_results(execution_id: int, script_id: int, accounts: list):
    """批量插入账号明细结果"""
    if not accounts:
        return
    with get_db_connection() as conn:
        cursor = conn.cursor()
        for acc in accounts:
            cursor.execute(
                """INSERT INTO account_results 
                   (execution_id, script_id, address, name, status, message, points, error)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    execution_id,
                    script_id,
                    acc.get("address", ""),
                    acc.get("name", ""),
                    acc.get("status", "unknown"),
                    acc.get("message", ""),
                    acc.get("points", 0),
                    acc.get("error", "")
                )
            )


def get_account_results_by_execution(execution_id: int) -> List[Dict[str, Any]]:
    with get_db_connection() as conn:
        cursor = conn.cursor()
        cursor.execute(
            "SELECT * FROM account_results WHERE execution_id = ? ORDER BY id",
            (execution_id,)
        )
        return [dict(row) for row in cursor.fetchall()]


def get_account_results_by_script(script_id: int, limit: int = 100) -> List[Dict[str, Any]]:
    with get_db_connection() as conn:
        cursor = conn.cursor()
        cursor.execute(
            "SELECT * FROM account_results WHERE script_id = ? ORDER BY created_at DESC LIMIT ?",
            (script_id, limit)
        )
        return [dict(row) for row in cursor.fetchall()]


def get_account_stats_by_script(script_id: int) -> Dict[str, Any]:
    with get_db_connection() as conn:
        cursor = conn.cursor()
        cursor.execute(
            """SELECT 
                COUNT(*) as total,
                COALESCE(SUM(CASE WHEN status = 'success' THEN 1 ELSE 0 END), 0) as success_count,
                COALESCE(SUM(CASE WHEN status = 'failed' THEN 1 ELSE 0 END), 0) as failed_count
               FROM account_results WHERE script_id = ?""",
            (script_id,)
        )
        row = cursor.fetchone()
        return dict(row) if row else {"total": 0, "success_count": 0, "failed_count": 0}


# ---------- Workflow orchestration ----------
def sync_workflows_from_projects() -> Dict[str, int]:
    """Create one workflow per project and one step per active script."""
    created_workflows = 0
    created_steps = 0
    with get_db_connection() as conn:
        projects = conn.execute("SELECT id, name FROM projects ORDER BY id").fetchall()
        for project in projects:
            workflow = conn.execute(
                "SELECT id FROM workflows WHERE project_id = ? AND name = ?",
                (project["id"], "每日自动化"),
            ).fetchone()
            if workflow is None:
                workflow_id = conn.execute(
                    "INSERT INTO workflows (project_id, name, description, schedule) VALUES (?, ?, ?, ?)",
                    (project["id"], "每日自动化", f"{project['name']} 的自动化工作流", "daily"),
                ).lastrowid
                created_workflows += 1
            else:
                workflow_id = workflow["id"]

            scripts = conn.execute(
                """
                SELECT s.id, s.name
                  FROM scripts s
                  JOIN tasks t ON t.id = s.task_id
                 WHERE t.project_id = ? AND s.status = 'active'
                 ORDER BY s.id
                """,
                (project["id"],),
            ).fetchall()
            active_script_ids = {script["id"] for script in scripts}
            if active_script_ids:
                placeholders = ", ".join("?" for _ in active_script_ids)
                conn.execute(
                    f"DELETE FROM workflow_steps WHERE workflow_id = ? AND script_id NOT IN ({placeholders})",
                    [workflow_id, *active_script_ids],
                )
            else:
                conn.execute("DELETE FROM workflow_steps WHERE workflow_id = ?", (workflow_id,))
            for order, script in enumerate(scripts, 1):
                cursor = conn.execute(
                    """
                    INSERT OR IGNORE INTO workflow_steps
                        (workflow_id, script_id, name, step_order)
                    VALUES (?, ?, ?, ?)
                    """,
                    (workflow_id, script["id"], script["name"], order),
                )
                created_steps += cursor.rowcount
        return {"created_workflows": created_workflows, "created_steps": created_steps}


def get_workflows() -> List[Dict[str, Any]]:
    with get_db_connection() as conn:
        rows = conn.execute(
            """
            SELECT w.*, p.name AS project_name,
                   COUNT(ws.id) AS step_count
              FROM workflows w
              JOIN projects p ON p.id = w.project_id
              LEFT JOIN workflow_steps ws ON ws.workflow_id = w.id
             GROUP BY w.id
             ORDER BY p.name, w.id
            """
        ).fetchall()
        return [dict(row) for row in rows]


def get_workflow(workflow_id: int) -> Optional[Dict[str, Any]]:
    with get_db_connection() as conn:
        row = conn.execute(
            """
            SELECT w.*, p.name AS project_name
              FROM workflows w
              JOIN projects p ON p.id = w.project_id
             WHERE w.id = ?
            """,
            (workflow_id,),
        ).fetchone()
        return dict(row) if row else None


def get_workflow_steps(workflow_id: int) -> List[Dict[str, Any]]:
    with get_db_connection() as conn:
        rows = conn.execute(
            """
            SELECT ws.*, s.name AS script_name, s.path AS script_path
              FROM workflow_steps ws
              JOIN scripts s ON s.id = ws.script_id
             WHERE ws.workflow_id = ?
             ORDER BY ws.step_order, ws.id
            """,
            (workflow_id,),
        ).fetchall()
        return [dict(row) for row in rows]


def create_run(workflow_id: int, trigger: str = "manual", idempotency_key: Optional[str] = None) -> Dict[str, Any]:
    with get_db_connection() as conn:
        if idempotency_key:
            conn.execute(
                "INSERT OR IGNORE INTO runs (workflow_id, trigger, idempotency_key) VALUES (?, ?, ?)",
                (workflow_id, trigger, idempotency_key),
            )
            row = conn.execute("SELECT * FROM runs WHERE idempotency_key = ?", (idempotency_key,)).fetchone()
        else:
            active = conn.execute(
                "SELECT * FROM runs WHERE workflow_id = ? AND status IN ('queued', 'running') ORDER BY id DESC LIMIT 1",
                (workflow_id,),
            ).fetchone()
            if active:
                return dict(active)
            run_id = conn.execute(
                "INSERT INTO runs (workflow_id, trigger) VALUES (?, ?)",
                (workflow_id, trigger),
            ).lastrowid
            row = conn.execute("SELECT * FROM runs WHERE id = ?", (run_id,)).fetchone()
        return dict(row)


def get_runs(limit: int = 100) -> List[Dict[str, Any]]:
    with get_db_connection() as conn:
        rows = conn.execute(
            """
            SELECT r.*, w.name AS workflow_name, p.name AS project_name
              FROM runs r
              JOIN workflows w ON w.id = r.workflow_id
              JOIN projects p ON p.id = w.project_id
             ORDER BY r.created_at DESC
             LIMIT ?
            """,
            (limit,),
        ).fetchall()
        return [dict(row) for row in rows]


def get_run(run_id: int) -> Optional[Dict[str, Any]]:
    with get_db_connection() as conn:
        row = conn.execute(
            """
            SELECT r.*, w.name AS workflow_name, p.name AS project_name
              FROM runs r
              JOIN workflows w ON w.id = r.workflow_id
              JOIN projects p ON p.id = w.project_id
             WHERE r.id = ?
            """,
            (run_id,),
        ).fetchone()
        return dict(row) if row else None


def update_run(run_id: int, **fields: Any) -> None:
    if not fields:
        return
    allowed = {
        "status", "worker_id", "started_at", "finished_at", "duration", "summary", "error"
    }
    fields = {key: value for key, value in fields.items() if key in allowed}
    if not fields:
        return
    with get_db_connection() as conn:
        clause = ", ".join(f"{key} = ?" for key in fields)
        conn.execute(
            f"UPDATE runs SET {clause}, updated_at = CURRENT_TIMESTAMP WHERE id = ?",
            [*fields.values(), run_id],
        )


def create_attempt(
    run_id: int,
    step_id: int,
    script_id: int,
    attempt_no: int = 1,
    worker_id: Optional[int] = None,
    started_at: Optional[datetime] = None,
) -> int:
    with get_db_connection() as conn:
        cursor = conn.execute(
            """
            INSERT INTO attempts
                (run_id, step_id, script_id, attempt_no, worker_id, started_at)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (run_id, step_id, script_id, attempt_no, worker_id, started_at or datetime.utcnow()),
        )
        return cursor.lastrowid


def update_attempt(attempt_id: int, **fields: Any) -> None:
    if not fields:
        return
    allowed = {
        "status", "worker_id", "finished_at", "duration", "output", "error", "result_json"
    }
    fields = {key: value for key, value in fields.items() if key in allowed}
    if not fields:
        return
    with get_db_connection() as conn:
        clause = ", ".join(f"{key} = ?" for key in fields)
        conn.execute(
            f"UPDATE attempts SET {clause} WHERE id = ?",
            [*fields.values(), attempt_id],
        )


def get_attempts(run_id: int) -> List[Dict[str, Any]]:
    with get_db_connection() as conn:
        rows = conn.execute(
            "SELECT * FROM attempts WHERE run_id = ? ORDER BY step_id, attempt_no",
            (run_id,),
        ).fetchall()
        return [dict(row) for row in rows]


def register_worker(name: str, platform: str, capabilities: Dict[str, Any], metadata: Dict[str, Any]) -> Dict[str, Any]:
    now = datetime.utcnow()
    with get_db_connection() as conn:
        conn.execute(
            """
            INSERT INTO workers (name, platform, capabilities_json, status, last_heartbeat, metadata_json, updated_at)
            VALUES (?, ?, ?, 'online', ?, ?, CURRENT_TIMESTAMP)
            ON CONFLICT(name) DO UPDATE SET
                platform = excluded.platform,
                capabilities_json = excluded.capabilities_json,
                status = 'online',
                last_heartbeat = excluded.last_heartbeat,
                metadata_json = excluded.metadata_json,
                updated_at = CURRENT_TIMESTAMP
            """,
            (name, platform, json.dumps(capabilities, ensure_ascii=False), now, json.dumps(metadata, ensure_ascii=False)),
        )
        row = conn.execute("SELECT * FROM workers WHERE name = ?", (name,)).fetchone()
        return dict(row)


def heartbeat_worker(worker_id: int, status: str = "online", metadata: Optional[Dict[str, Any]] = None) -> Optional[Dict[str, Any]]:
    with get_db_connection() as conn:
        conn.execute(
            "UPDATE workers SET status = ?, last_heartbeat = CURRENT_TIMESTAMP, metadata_json = COALESCE(?, metadata_json), updated_at = CURRENT_TIMESTAMP WHERE id = ?",
            (status, json.dumps(metadata, ensure_ascii=False) if metadata is not None else None, worker_id),
        )
        row = conn.execute("SELECT * FROM workers WHERE id = ?", (worker_id,)).fetchone()
        return dict(row) if row else None


def get_workers() -> List[Dict[str, Any]]:
    with get_db_connection() as conn:
        rows = conn.execute("SELECT * FROM workers ORDER BY name").fetchall()
        return [dict(row) for row in rows]


def add_artifact(
    kind: str,
    path: str,
    run_id: Optional[int] = None,
    attempt_id: Optional[int] = None,
    execution_id: Optional[int] = None,
    metadata: Optional[Dict[str, Any]] = None,
) -> int:
    with get_db_connection() as conn:
        cursor = conn.execute(
            """
            INSERT INTO artifacts (run_id, attempt_id, execution_id, kind, path, metadata_json)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (run_id, attempt_id, execution_id, kind, path, json.dumps(metadata or {}, ensure_ascii=False)),
        )
        return cursor.lastrowid


def get_artifacts(run_id: int) -> List[Dict[str, Any]]:
    with get_db_connection() as conn:
        rows = conn.execute("SELECT * FROM artifacts WHERE run_id = ? ORDER BY id", (run_id,)).fetchall()
        return [dict(row) for row in rows]
