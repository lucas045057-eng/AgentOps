"""Independent project-progress storage.

This database intentionally has no foreign keys to the script-runner database.
Progress notes can therefore be managed without changing projects, scripts,
wallets, executions, workflows, or their history.
"""

from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from typing import Any, Dict, Iterator, List, Optional

from app.config import settings


@contextmanager
def get_progress_db_connection() -> Iterator[sqlite3.Connection]:
    conn = sqlite3.connect(settings.progress_database_url)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def init_progress_db() -> None:
    with get_progress_db_connection() as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS progress_projects (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL,
                url TEXT DEFAULT '',
                target_time TEXT DEFAULT '',
                progress TEXT DEFAULT '未开始',
                account TEXT DEFAULT '',
                notes TEXT DEFAULT '',
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
            """
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_progress_projects_updated_at "
            "ON progress_projects(updated_at)"
        )


def list_progress_projects() -> List[Dict[str, Any]]:
    with get_progress_db_connection() as conn:
        rows = conn.execute(
            "SELECT * FROM progress_projects ORDER BY updated_at DESC, id DESC"
        ).fetchall()
        return [dict(row) for row in rows]


def get_progress_project(project_id: int) -> Optional[Dict[str, Any]]:
    with get_progress_db_connection() as conn:
        row = conn.execute(
            "SELECT * FROM progress_projects WHERE id = ?", (project_id,)
        ).fetchone()
        return dict(row) if row else None


def add_progress_project(
    name: str,
    url: str = "",
    target_time: str = "",
    progress: str = "未开始",
    account: str = "",
    notes: str = "",
) -> Dict[str, Any]:
    with get_progress_db_connection() as conn:
        cursor = conn.execute(
            """
            INSERT INTO progress_projects
                (name, url, target_time, progress, account, notes)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (name, url, target_time, progress, account, notes),
        )
        project_id = int(cursor.lastrowid)
    return get_progress_project(project_id) or {"id": project_id, "name": name}


def update_progress_project(project_id: int, **fields: str) -> Optional[Dict[str, Any]]:
    allowed = {"name", "url", "target_time", "progress", "account", "notes"}
    fields = {key: str(value) for key, value in fields.items() if key in allowed}
    if not fields:
        return get_progress_project(project_id)

    fields["updated_at"] = "CURRENT_TIMESTAMP"
    assignments = []
    values: list[str] = []
    for key, value in fields.items():
        if key == "updated_at":
            assignments.append("updated_at = CURRENT_TIMESTAMP")
        else:
            assignments.append(f"{key} = ?")
            values.append(value)
    values.append(str(project_id))

    with get_progress_db_connection() as conn:
        cursor = conn.execute(
            f"UPDATE progress_projects SET {', '.join(assignments)} WHERE id = ?",
            values,
        )
        if cursor.rowcount == 0:
            return None
    return get_progress_project(project_id)


def delete_progress_project(project_id: int) -> bool:
    with get_progress_db_connection() as conn:
        cursor = conn.execute(
            "DELETE FROM progress_projects WHERE id = ?", (project_id,)
        )
        return cursor.rowcount > 0
