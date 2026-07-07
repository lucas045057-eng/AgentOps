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

        # 索引
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_account_results_execution_id ON account_results(execution_id);")
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_account_results_address ON account_results(address);")

    print("数据库表已检查（含 account_results）")


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


def get_today_executions() -> List[Dict[str, Any]]:
    """获取今日执行记录"""
    today = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0)
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
                SUM(CASE WHEN status = 'success' THEN 1 ELSE 0 END) as success_count,
                SUM(CASE WHEN status = 'failed' THEN 1 ELSE 0 END) as failed_count
               FROM account_results WHERE script_id = ?""",
            (script_id,)
        )
        row = cursor.fetchone()
        return dict(row) if row else {"total": 0, "success_count": 0, "failed_count": 0}