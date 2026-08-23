"""Catalog and path resolution for scripts kept outside the AirDrop repo."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, List, Optional

from app.config import settings


BASE_DIR = Path(__file__).resolve().parents[2]
CATALOG_PATH = BASE_DIR / settings.runner_catalog_path
EXTERNAL_PREFIX = "external/daily/"


def _normalise(value: str) -> str:
    return str(value).replace("\\", "/").lstrip("./")


def load_catalog() -> Dict[str, Any]:
    with CATALOG_PATH.open("r", encoding="utf-8") as handle:
        data = json.load(handle)
    if not isinstance(data, dict) or not isinstance(data.get("scripts"), list):
        raise ValueError(f"Invalid runner catalog: {CATALOG_PATH}")
    return data


def list_catalog() -> List[Dict[str, Any]]:
    return [item for item in load_catalog()["scripts"] if item.get("enabled", True)]


def get_runner_spec(script_ref: str) -> Dict[str, Any]:
    wanted = _normalise(script_ref)
    for item in list_catalog():
        if _normalise(item.get("path", "")) == wanted:
            return dict(item)

    # Existing in-repository scripts keep the old exit-code behaviour.
    return {
        "id": wanted,
        "name": Path(wanted).name,
        "path": wanted,
        "runner": "legacy",
        "workspace_mode": "direct",
        "structured_required": False,
        "interactive": False,
        "platform": "linux",
    }


def _safe_join(root: Path, relative: str) -> Path:
    candidate = (root / relative).resolve()
    try:
        candidate.relative_to(root)
    except ValueError as exc:
        raise ValueError("Script path escapes its configured root") from exc
    return candidate


def resolve_script_path(script_ref: str) -> Path:
    """Resolve an internal or read-only external script reference."""
    if not script_ref:
        raise ValueError("Script path is required")

    normalised = _normalise(script_ref)
    if normalised.startswith(EXTERNAL_PREFIX):
        root = Path(settings.external_script_root)
        if not root.is_absolute():
            root = BASE_DIR / root
        root = root.resolve()
        candidate = _safe_join(root, normalised[len(EXTERNAL_PREFIX):])
    else:
        root = Path(settings.script_root)
        if not root.is_absolute():
            root = BASE_DIR / root
        root = root.resolve()
        candidate = _safe_join(BASE_DIR, normalised)
        try:
            candidate.relative_to(root)
        except ValueError as exc:
            raise ValueError("Script path is outside the configured script root") from exc

    if candidate.suffix.lower() != ".py":
        raise ValueError("Only Python scripts can be executed")
    if not candidate.is_file():
        raise FileNotFoundError(f"Script file not found: {candidate}")
    return candidate


def sync_catalog() -> Dict[str, Any]:
    """Register catalog entries in the existing Project/Task/Script tables.

    This only writes AirDrop' SQLite database. The external source directory is
    never written to; it is mounted read-only by docker-compose.
    """
    from app.database.database import (
        add_project,
        add_script,
        add_task,
        get_projects,
        get_scripts,
        get_tasks_by_project,
        sync_workflows_from_projects,
        update_script,
        update_project,
    )

    entries = list_catalog()
    projects = get_projects()
    scripts = get_scripts()
    created_projects = 0
    created_tasks = 0
    created_scripts = 0
    registered: List[Dict[str, Any]] = []

    for entry in entries:
        project_name = str(entry.get("project") or entry.get("name") or "未分类")
        project = next((item for item in projects if item["name"] == project_name), None)
        project_url = f"external://daily/{project_name}"
        if project is None:
            project_id = add_project(project_name, project_url)
            created_projects += 1
            projects.append({"id": project_id, "name": project_name, "url": project_url})
        else:
            project_id = project["id"]
            if project["url"] != project_url:
                update_project(project_id, project_name, project_url)

        project_tasks = get_tasks_by_project(project_id)
        task = next((item for item in project_tasks if item["name"] == "自动化脚本"), None)
        if task is None:
            task_id = add_task(project_id, "自动化脚本", f"{project_name} 的自动化脚本")
            created_tasks += 1
        else:
            task_id = task["id"]
        path = _normalise(entry["path"])
        existing = next((s for s in scripts if _normalise(s["path"]) == path), None)
        if existing is None:
            # Re-point a previously imported external copy instead of creating
            # a duplicate script when the catalog switches to an internal copy.
            existing = next(
                (
                    s
                    for s in scripts
                    if s.get("task_id") == task_id and s.get("name") == entry["name"]
                ),
                None,
            )
        if existing is None:
            script_id = add_script(task_id, entry["name"], path)
            created_scripts += 1
        else:
            script_id = existing["id"]
            if _normalise(existing["path"]) != path or existing.get("name") != entry["name"]:
                update_script(script_id, task_id, entry["name"], path)
                existing["path"] = path
                existing["name"] = entry["name"]
        registered.append({"id": entry["id"], "script_id": script_id, "path": path})

    workflow_sync = sync_workflows_from_projects()
    return {
        "total": len(registered),
        "created_projects": created_projects,
        "created_tasks": created_tasks,
        "created_scripts": created_scripts,
        "created_workflows": workflow_sync["created_workflows"],
        "created_steps": workflow_sync["created_steps"],
        "scripts": registered,
    }
