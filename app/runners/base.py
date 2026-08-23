"""Common subprocess isolation, structured events, and artifact handling."""

from __future__ import annotations

import asyncio
import json
import os
import re
import signal
import shutil
import sys
import time
from pathlib import Path
from typing import Any, Dict, Optional

from app.config import settings


BASE_DIR = Path(__file__).resolve().parents[2]
KNOWN_STATUSES = {
    "success",
    "already_done",
    "skipped",
    "no_work",
    "manual_required",
    "cancelled",
    "failed",
    "partial_success",
    "unknown",
    "timeout",
    "crashed",
}


def _kill_process_tree(process: asyncio.subprocess.Process) -> None:
    if process.returncode is not None:
        return
    if os.name == "posix":
        try:
            os.killpg(process.pid, signal.SIGKILL)
            return
        except (AttributeError, ProcessLookupError, PermissionError):
            pass
    process.kill()


def redact_sensitive_text(value: str) -> str:
    """Remove common credential values from persisted logs.

    This is deliberately conservative: raw output is still parsed in memory for
    status detection, but database/filesystem logs receive a redacted copy.
    """
    if not value:
        return value
    patterns = [
        r"(?i)(private[_ -]?key|api[_ -]?key|authorization|cookie|password|secret|token)\s*([:=])\s*(\"[^\"]*\"|'[^']*'|[^\s,;]+)",
        r"(?i)(bearer\s+)[A-Za-z0-9._~+/=-]+",
    ]
    result = value
    result = re.sub(patterns[0], r"\1\2<redacted>", result)
    result = re.sub(patterns[1], r"\1<redacted>", result)
    return result


class BaseRunner:
    """Run one script with a consistent process contract."""

    runner_name = "legacy"

    def __init__(self, spec: Dict[str, Any]):
        self.spec = spec

    def _rooted_path(self, configured: str) -> Path:
        root = Path(configured)
        if not root.is_absolute():
            root = BASE_DIR / root
        return root

    def _artifact_dir(self, execution_id: int) -> Path:
        root = self._rooted_path(settings.execution_log_root)
        directory = root / str(execution_id)
        directory.mkdir(parents=True, exist_ok=True)
        return directory

    def _workspace_dir(self, execution_id: int) -> Path:
        root = self._rooted_path(settings.runner_workspace_root)
        directory = root / str(execution_id)
        directory.mkdir(parents=True, exist_ok=True)
        return directory

    def _emit(self, event_file: Path, event: str, execution_id: int, **data: Any) -> None:
        payload = {
            "ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "event": event,
            "execution_id": execution_id,
            "runner": self.spec.get("runner", self.runner_name),
            **data,
        }
        with event_file.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(payload, ensure_ascii=False, default=str) + "\n")

    def extra_environment(self) -> Dict[str, str]:
        return {}

    def _manual_result(self, reason: str, execution_id: int, artifact_dir: Path) -> Dict[str, Any]:
        message = {
            "status": "manual_required",
            "retryable": False,
            "message": reason,
        }
        stdout = json.dumps(message, ensure_ascii=False)
        self._emit(
            artifact_dir / "events.jsonl",
            "run.manual_required",
            execution_id,
            reason=reason,
        )
        (artifact_dir / "stdout.log").write_text(stdout + "\n", encoding="utf-8")
        (artifact_dir / "stderr.log").write_text("", encoding="utf-8")
        return {
            "stdout": stdout,
            "stderr": "",
            "returncode": 0,
            "duration": 0.0,
            "timed_out": False,
            "artifact_dir": str(artifact_dir),
            "structured_required": bool(self.spec.get("structured_required", False)),
        }

    async def run(
        self,
        source_path: Path,
        execution_id: int,
        script_id: int,
    ) -> Dict[str, Any]:
        artifact_dir = self._artifact_dir(execution_id)
        event_file = artifact_dir / "events.jsonl"
        self._emit(
            event_file,
            "run.started",
            execution_id,
            script_id=script_id,
            catalog_id=self.spec.get("id"),
            source=str(source_path),
            platform=self.spec.get("platform", "linux"),
        )

        required_platform = self.spec.get("platform", "linux")
        if required_platform != settings.execution_platform:
            return self._manual_result(
                f"This script requires platform={required_platform}; current worker is {settings.execution_platform}",
                execution_id,
                artifact_dir,
            )
        auto_input = bool(self.spec.get("auto_input", False))
        if self.spec.get("interactive") and not auto_input and not settings.allow_interactive:
            return self._manual_result(
                "This script still requires console input. Add a non-interactive adapter or enable an operator worker.",
                execution_id,
                artifact_dir,
            )

        workspace: Optional[Path] = None
        script_path = source_path
        workspace_mode = self.spec.get("workspace_mode", "direct")
        if workspace_mode == "isolated_copy":
            workspace = self._workspace_dir(execution_id)
            shutil.copytree(
                source_path.parent,
                workspace,
                dirs_exist_ok=True,
                ignore=shutil.ignore_patterns("__pycache__", "*.pyc"),
            )
            script_path = workspace / source_path.name
            self._emit(event_file, "workspace.staged", execution_id, workspace=str(workspace))

        env = os.environ.copy()
        env.update(
            {
                "AIRDROP_EXECUTION_ID": str(execution_id),
                "AIRDROP_SCRIPT_ID": str(script_id),
                "AIRDROP_RUNNER": self.spec.get("runner", self.runner_name),
                "AIRDROP_NONINTERACTIVE": "1",
                "AIRDROP_ARTIFACT_DIR": str(artifact_dir),
                "AIRDROP_WORKSPACE_DIR": str(workspace or source_path.parent),
                "PYTHONUNBUFFERED": "1",
            }
        )
        if auto_input:
            runtime_dir = BASE_DIR / "app" / "runners" / "runtime"
            existing_pythonpath = env.get("PYTHONPATH", "")
            env["PYTHONPATH"] = os.pathsep.join(
                part for part in (str(runtime_dir), existing_pythonpath) if part
            )
            env["AIRDROP_AUTO_INPUT"] = "1"
            env["AIRDROP_INPUTS_JSON"] = json.dumps(
                self.spec.get("input_answers", []), ensure_ascii=False
            )
            self._emit(
                event_file,
                "run.auto_input_enabled",
                execution_id,
                policy=self.spec.get("input_policy", "safe_default"),
            )
        env.update({str(k): str(v) for k, v in self.spec.get("env", {}).items()})
        env.update(self.extra_environment())

        argv = [str(x) for x in self.spec.get("argv", [])]
        command = [sys.executable, str(script_path), *argv]
        self._emit(event_file, "process.started", execution_id, command=command, cwd=str(script_path.parent))

        started = time.monotonic()
        returncode = -1
        timed_out = False
        cancelled = False
        stdout = b""
        stderr = b""
        input_text = self.spec.get("stdin")
        process = None
        pid_path = artifact_dir / "process.pid"
        try:
            process_options: Dict[str, Any] = {}
            if os.name == "posix":
                process_options["start_new_session"] = True
            process = await asyncio.create_subprocess_exec(
                *command,
                stdin=asyncio.subprocess.PIPE if input_text is not None else asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=str(script_path.parent),
                env=env,
                **process_options,
            )
            pid_path.write_text(str(process.pid), encoding="ascii")
            try:
                stdout, stderr = await asyncio.wait_for(
                    process.communicate(
                        input=str(input_text).encode("utf-8") if input_text is not None else None
                    ),
                    timeout=int(self.spec.get("timeout", settings.execution_timeout)),
                )
            except asyncio.TimeoutError:
                timed_out = True
                _kill_process_tree(process)
                stdout, stderr = await process.communicate()
                stderr += b"\nAirDrop runner timed out"
            except asyncio.CancelledError:
                cancelled = True
                _kill_process_tree(process)
                stdout, stderr = await process.communicate()
                stderr += b"\nAirDrop execution cancelled"
            returncode = process.returncode
        except asyncio.CancelledError:
            cancelled = True
            if process is not None:
                _kill_process_tree(process)
                stdout, stderr = await process.communicate()
                returncode = process.returncode
            stderr += b"\nAirDrop execution cancelled"
        except Exception as exc:
            stderr = f"Runner process error: {exc}".encode("utf-8", errors="replace")
        finally:
            duration = round(time.monotonic() - started, 3)
            pid_path.unlink(missing_ok=True)

        stdout_text = stdout.decode("utf-8", errors="replace").strip()
        stderr_text = stderr.decode("utf-8", errors="replace").strip()
        (artifact_dir / "stdout.log").write_text(redact_sensitive_text(stdout_text), encoding="utf-8")
        (artifact_dir / "stderr.log").write_text(redact_sensitive_text(stderr_text), encoding="utf-8")
        self._emit(
            event_file,
            "process.finished",
            execution_id,
            returncode=returncode,
            timed_out=timed_out,
            cancelled=cancelled,
            duration=duration,
            stdout_bytes=len(stdout),
            stderr_bytes=len(stderr),
        )

        metadata = {
            "execution_id": execution_id,
            "script_id": script_id,
            "catalog_id": self.spec.get("id"),
            "runner": self.spec.get("runner", self.runner_name),
            "source": str(source_path),
            "workspace_mode": workspace_mode,
            "returncode": returncode,
            "timed_out": timed_out,
            "cancelled": cancelled,
            "duration": duration,
            "artifact_dir": str(artifact_dir),
            "result_policy": self.spec.get("result_policy", "json"),
            "auto_input": auto_input,
        }
        (artifact_dir / "run.json").write_text(
            json.dumps(metadata, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        self._emit(
            event_file,
            "run.finished",
            execution_id,
            returncode=returncode,
            timed_out=timed_out,
            duration=duration,
        )

        if workspace is not None and not settings.keep_runner_workspace:
            shutil.rmtree(workspace, ignore_errors=True)

        return {
            "stdout": stdout_text,
            "stderr": stderr_text,
            "returncode": returncode,
            "duration": duration,
            "timed_out": timed_out,
            "cancelled": cancelled,
            "artifact_dir": str(artifact_dir),
            "structured_required": bool(self.spec.get("structured_required", False)),
            "result_policy": self.spec.get("result_policy", "json"),
            "auto_input": auto_input,
        }
