"""Optional non-interactive input adapter injected into child Python scripts.

The external scripts remain untouched. When the catalog explicitly enables
``auto_input``, this module supplies safe defaults for menu prompts and writes
an audit trail beside the execution artifacts.
"""

from __future__ import annotations

import builtins
import getpass
import json
import os
import re
import time
from pathlib import Path
from typing import Any


def _load_answers() -> list[str]:
    raw = os.environ.get("AIRDROP_INPUTS_JSON", "[]")
    try:
        value = json.loads(raw)
    except json.JSONDecodeError:
        return []
    return [str(item) for item in value] if isinstance(value, list) else []


def _default_answer(prompt: str) -> str:
    text = (prompt or "").strip().lower()

    # Leave exit/pause prompts alone.
    if any(token in text for token in ("按回车", "press enter", "回车键", "退出", "返回主菜单", "关闭")):
        return ""
    # Start/confirmation prompts are the only affirmative defaults.
    if any(token in text for token in ("确认", "开始执行", "是否开始", "y/n", "yes/no")):
        return "y"
    # Do not overwrite files unless a script-specific answer explicitly says so.
    if any(token in text for token in ("覆盖", "overwrite")):
        return "n"
    if any(token in text for token in ("模式", "mode", "选择", "choice")):
        return "1"
    # Empty input means “use the script's documented default/all accounts”.
    if any(token in text for token in ("线程", "thread", "账号数量", "账号范围", "范围", "路径", "path")):
        return ""
    return ""


def _write_event(prompt: str, answer: str, source: str) -> None:
    artifact_dir = os.environ.get("AIRDROP_ARTIFACT_DIR")
    if not artifact_dir:
        return
    try:
        path = Path(artifact_dir) / "input-events.jsonl"
        payload: dict[str, Any] = {
            "ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "event": "input.auto_answered",
            "prompt": re.sub(r"\s+", " ", prompt or "")[:300],
            "answer": answer,
            "source": source,
        }
        with path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(payload, ensure_ascii=False) + "\n")
    except OSError:
        pass


def _install() -> None:
    if os.environ.get("AIRDROP_AUTO_INPUT") != "1":
        return

    answers = _load_answers()
    position = 0

    def auto_input(prompt: str = "") -> str:
        nonlocal position
        if position < len(answers):
            answer = answers[position]
            source = "catalog"
        else:
            answer = _default_answer(prompt)
            source = "safe_default"
        position += 1
        _write_event(prompt, answer, source)
        return answer

    builtins.input = auto_input
    getpass.getpass = auto_input


_install()
