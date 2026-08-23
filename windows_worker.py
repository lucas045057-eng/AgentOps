"""Small Windows host worker for AirDrop.

Run this on Windows with the E: drive available. It accepts authenticated,
short-lived execution requests from the WSL AirDrop service and never writes
back to the source script directory; external scripts run from a temporary
copy instead.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any


HOST = os.getenv("WINDOWS_WORKER_HOST", "0.0.0.0")
PORT = int(os.getenv("WINDOWS_WORKER_PORT", "8765"))
TOKEN = os.getenv("WINDOWS_WORKER_TOKEN", "")
EXTERNAL_ROOT = Path(os.getenv("DAILY_SCRIPTS_ROOT", r"E:\项目脚本\日签")).resolve()
SCRIPT_ROOT = Path(os.getenv("SCRIPT_ROOT", str(Path(__file__).resolve().parent / "scripts"))).resolve()
RUNTIME_ROOT = Path(__file__).resolve().parent / "app" / "runners" / "runtime"


def safe_script_path(script_ref: str) -> Path:
    normalized = str(script_ref or "").replace("\\", "/").lstrip("./")
    if normalized.startswith("external/daily/"):
        root = EXTERNAL_ROOT
        relative = normalized[len("external/daily/"):]
    else:
        root = SCRIPT_ROOT
        relative = normalized
    candidate = (root / relative).resolve()
    candidate.relative_to(root)
    if candidate.suffix.lower() != ".py" or not candidate.is_file():
        raise FileNotFoundError(f"Script not found: {candidate}")
    return candidate


def execute(payload: dict[str, Any]) -> dict[str, Any]:
    source = safe_script_path(str(payload.get("path", "")))
    timeout = int(payload.get("timeout", 900))
    workspace = Path(tempfile.mkdtemp(prefix="airdrop-worker-"))
    try:
        script_path = source
        if payload.get("workspace_mode", "isolated_copy") == "isolated_copy":
            shutil.copytree(source.parent, workspace, dirs_exist_ok=True)
            script_path = workspace / source.name

        env = os.environ.copy()
        env.update({str(k): str(v) for k, v in (payload.get("env") or {}).items()})
        env.update(
            {
                "AIRDROP_EXECUTION_ID": str(payload.get("execution_id", "")),
                "AIRDROP_SCRIPT_ID": str(payload.get("script_id", "")),
                "AIRDROP_RUNNER": str(payload.get("runner", "windows_worker")),
                "AIRDROP_NONINTERACTIVE": "1",
                "PYTHONUNBUFFERED": "1",
            }
        )
        if payload.get("auto_input"):
            old_pythonpath = env.get("PYTHONPATH", "")
            env["PYTHONPATH"] = os.pathsep.join(
                item for item in (str(RUNTIME_ROOT), old_pythonpath) if item
            )
            env["AIRDROP_AUTO_INPUT"] = "1"
            env["AIRDROP_INPUTS_JSON"] = json.dumps(payload.get("input_answers", []), ensure_ascii=False)

        command = [
            os.environ.get("PYTHON", "python"),
            str(script_path),
            *[str(item) for item in (payload.get("argv") or [])],
        ]
        started = time.monotonic()
        try:
            completed = subprocess.run(
                command,
                cwd=str(script_path.parent),
                env=env,
                stdin=subprocess.DEVNULL,
                capture_output=True,
                timeout=timeout,
            )
            timed_out = False
            returncode = completed.returncode
            stdout = completed.stdout.decode("utf-8", errors="replace")
            stderr = completed.stderr.decode("utf-8", errors="replace")
        except subprocess.TimeoutExpired as exc:
            timed_out = True
            returncode = -1
            stdout = (exc.stdout or b"").decode("utf-8", errors="replace") if isinstance(exc.stdout, bytes) else str(exc.stdout or "")
            stderr = "Windows worker timed out"
        return {
            "returncode": returncode,
            "timed_out": timed_out,
            "duration": round(time.monotonic() - started, 3),
            "stdout": stdout,
            "stderr": stderr,
        }
    finally:
        shutil.rmtree(workspace, ignore_errors=True)


class Handler(BaseHTTPRequestHandler):
    def _send(self, status: int, body: dict[str, Any]) -> None:
        data = json.dumps(body, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self) -> None:  # noqa: N802
        if self.path == "/health":
            self._send(200, {"status": "ok", "external_root": str(EXTERNAL_ROOT)})
            return
        self._send(404, {"error": "not found"})

    def do_POST(self) -> None:  # noqa: N802
        if self.path != "/run":
            self._send(404, {"error": "not found"})
            return
        if TOKEN and self.headers.get("X-Worker-Token", "") != TOKEN:
            self._send(401, {"error": "invalid worker token"})
            return
        try:
            length = int(self.headers.get("Content-Length", "0"))
            payload = json.loads(self.rfile.read(length).decode("utf-8"))
            self._send(200, execute(payload))
        except Exception as exc:
            self._send(500, {"error": str(exc)})

    def log_message(self, format: str, *args: Any) -> None:
        print(f"[windows-worker] {format % args}")


if __name__ == "__main__":
    print(f"Windows Worker listening on {HOST}:{PORT}; source={EXTERNAL_ROOT}")
    ThreadingHTTPServer((HOST, PORT), Handler).serve_forever()
