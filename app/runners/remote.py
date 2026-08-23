"""Remote execution adapter for scripts that need a Windows host worker."""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any, Dict

import httpx

from app.config import settings
from .base import BaseRunner, redact_sensitive_text


class RemoteRunner(BaseRunner):
    runner_name = "windows_worker"

    async def run(self, source_path: Path, execution_id: int, script_id: int) -> Dict[str, Any]:
        artifact_dir = self._artifact_dir(execution_id)
        event_file = artifact_dir / "events.jsonl"
        self._emit(
            event_file,
            "remote.started",
            execution_id,
            script_id=script_id,
            worker_url=settings.windows_worker_url,
            platform=self.spec.get("platform"),
        )

        payload = {
            "execution_id": execution_id,
            "script_id": script_id,
            "path": self.spec.get("path"),
            "runner": self.spec.get("runner", self.runner_name),
            "argv": self.spec.get("argv", []),
            "env": self.spec.get("env", {}),
            "timeout": int(self.spec.get("timeout", settings.execution_timeout)),
            "workspace_mode": self.spec.get("workspace_mode", "isolated_copy"),
            "auto_input": bool(self.spec.get("auto_input", False)),
            "input_answers": self.spec.get("input_answers", []),
        }
        started = time.monotonic()
        stdout = ""
        stderr = ""
        returncode = -1
        timed_out = False
        try:
            headers = {}
            if settings.windows_worker_token:
                headers["X-Worker-Token"] = settings.windows_worker_token
            timeout = max(int(payload["timeout"]) + 30, settings.windows_worker_timeout)
            async with httpx.AsyncClient(timeout=timeout) as client:
                response = await client.post(
                    settings.windows_worker_url.rstrip("/") + "/run",
                    json=payload,
                    headers=headers,
                )
                response.raise_for_status()
                data = response.json()
            stdout = str(data.get("stdout", ""))
            stderr = str(data.get("stderr", ""))
            returncode = int(data.get("returncode", -1))
            timed_out = bool(data.get("timed_out", False))
        except Exception as exc:
            stderr = f"Windows worker request failed: {exc}"
        duration = round(time.monotonic() - started, 3)

        (artifact_dir / "stdout.log").write_text(redact_sensitive_text(stdout), encoding="utf-8")
        (artifact_dir / "stderr.log").write_text(redact_sensitive_text(stderr), encoding="utf-8")
        (artifact_dir / "run.json").write_text(
            json.dumps(
                {
                    "execution_id": execution_id,
                    "script_id": script_id,
                    "runner": "windows_worker",
                    "source": str(source_path),
                    "returncode": returncode,
                    "timed_out": timed_out,
                    "duration": duration,
                    "worker_url": settings.windows_worker_url,
                },
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )
        self._emit(
            event_file,
            "remote.finished",
            execution_id,
            returncode=returncode,
            timed_out=timed_out,
            duration=duration,
        )
        return {
            "stdout": stdout.strip(),
            "stderr": stderr.strip(),
            "returncode": returncode,
            "duration": duration,
            "timed_out": timed_out,
            "artifact_dir": str(artifact_dir),
            "structured_required": bool(self.spec.get("structured_required", False)),
            "result_policy": self.spec.get("result_policy", "json"),
        }
