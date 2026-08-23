"""Small compatibility layer used only by the copied daily scripts.

It deliberately does not create or manage wallets itself. Each project script
keeps control of its wallet format and generation logic; this module only
provides a persistent project directory and a structured result emitter.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any, Iterable


TRUE_VALUES = {"1", "true", "yes", "on"}


def is_airdrop() -> bool:
    return bool(
        os.environ.get("AIRDROP_EXECUTION_ID")
        or os.environ.get("AIRDROP_NONINTERACTIVE", "").strip().lower() in TRUE_VALUES
    )


def wallet_mode(default: str = "generated") -> str:
    value = os.environ.get("AIRDROP_WALLET_MODE", default).strip().lower()
    return value if value in {"specified", "generated"} else default


def specified_wallet_file() -> Path:
    value = os.environ.get("AIRDROP_SPECIFIED_WALLET_FILE", "").strip()
    if not value:
        raise FileNotFoundError("未配置 AIRDROP_SPECIFIED_WALLET_FILE")
    return Path(value).expanduser()


def project_data_dir(project: str) -> Path:
    explicit = os.environ.get("AIRDROP_PROJECT_DATA_DIR", "").strip()
    if explicit:
        path = Path(explicit).expanduser()
    else:
        artifact = os.environ.get("AIRDROP_ARTIFACT_DIR", "").strip()
        if artifact:
            # <persistent-log-root>/executions/<execution-id>
            path = Path(artifact).expanduser().resolve().parent.parent / "project-data" / project
        else:
            path = Path(__file__).resolve().parent / "data" / project
    path.mkdir(parents=True, exist_ok=True)
    return path


def env_int(*names: str, default: int = 1, minimum: int = 0) -> int:
    raw = next((os.environ.get(name, "").strip() for name in names if os.environ.get(name, "").strip()), "")
    value = int(raw) if raw else default
    if value < minimum:
        raise ValueError(f"{names[0]} 必须大于等于 {minimum}")
    return value


def emit_event(event: str, **data: Any) -> None:
    if is_airdrop():
        print(json.dumps({"event": event, **data}, ensure_ascii=False, default=str), flush=True)


def _status(item: dict[str, Any]) -> str:
    # DGrid's final per-wallet CSV/JSON row uses the Chinese field "状态".
    # Read it before falling back to the generic AirDrop field so the
    # platform judges the script's own final result instead of treating every
    # DGrid row as failed.
    raw = str(
        item.get("status")
        or item.get("状态")
        or item.get("最终状态")
        or item.get("运行状态")
        or ""
    ).strip().lower()
    if raw in {"success", "成功", "完成", "已完成", "签到成功", "already_done"}:
        return "success"
    if raw in {"partial_success", "部分完成", "部分成功"}:
        return "partial_success"
    if raw in {"skip", "skipped", "跳过", "今日已完成跳过", "今日已签到"}:
        return "already_done"
    if raw in {"manual_required", "待手动", "需要人工", "手动"}:
        return "manual_required"
    return "failed"


def emit_summary(project: str, accounts: Iterable[dict[str, Any]]) -> None:
    if not is_airdrop():
        return
    normalized: list[dict[str, Any]] = []
    for index, item in enumerate(accounts, 1):
        address = str(
            item.get("address")
            or item.get("钱包地址")
            or item.get("Address")
            or item.get("wallet_address")
            or ""
        )
        status = _status(item)
        normalized.append(
            {
                "address": address,
                "name": str(item.get("name") or item.get("账号") or f"账号 {index}"),
                "status": status,
                "message": str(item.get("message") or item.get("详情") or item.get("错误") or ""),
                "error": str(item.get("error") or item.get("错误") or item.get("SignError") or ""),
                "wallet_index": index,
            }
        )

    success = sum(item["status"] == "success" for item in normalized)
    partial = sum(item["status"] == "partial_success" for item in normalized)
    manual = sum(item["status"] == "manual_required" for item in normalized)
    failed = len(normalized) - success - partial - manual
    if failed and success:
        overall = "partial_success"
    elif failed:
        overall = "failed"
    elif manual and not success and not partial:
        overall = "manual_required"
    elif partial:
        overall = "partial_success"
    elif manual:
        overall = "partial_success"
    else:
        overall = "success" if normalized else "unknown"

    print(
        json.dumps(
            {
                "status": overall,
                "project": project,
                "wallet_total": len(normalized),
                "success": success,
                "partial_success": partial,
                "manual_required": manual,
                "failed": failed,
                "retryable": failed > 0,
                "accounts": normalized,
            },
            ensure_ascii=False,
            default=str,
        ),
        flush=True,
    )
