#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
SwarmBase 批量自动化

流程：
1. 从 CSV 第一列读取私钥，第二列收集循环绑定地址池。
2. 在 opBNB 主网执行邀请注册、每日签到，并按积分条件 Mint Pioneer/Builder/OG NFT。
3. Deploy Swarm / Build 任务默认关闭，只有手动输入大于 0 时才执行。
4. 签到完成后立即写入 UTC 时间；同一 UTC 日期再次运行时直接跳过账号。
5. 默认 5 线程，账号启动间隔 3-5 秒；每 50 个账号休息 60-90 秒。

依赖：requests、web3
安装：pip install -r 依赖列表.txt
"""

from __future__ import annotations

import csv
import io
import json
import os
import random
import re
import sys
import tempfile
import threading
import time
import unicodedata
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, as_completed, wait
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterable

import requests

from _airdrop_compat import (
    emit_summary,
    env_int,
    is_airdrop,
    project_data_dir,
    specified_wallet_file,
    wallet_mode,
)


# 启用 Windows 10/11 CMD 的 ANSI 色彩支持
os.system("")

# ============================== ANSI 色彩及 UI ==============================

RESET = "\033[0m"
BOLD = "\033[1m"
DIM = "\033[2m"
RED = "\033[31m"
GREEN = "\033[32m"
YELLOW = "\033[33m"
BLUE = "\033[34m"
MAGENTA = "\033[35m"
CYAN = "\033[36m"
GRAY = "\033[90m"
COLOR_ENABLED = os.getenv("NO_COLOR") is None and (os.name == "nt" or sys.stdout.isatty())
ANSI_PATTERN = re.compile(r"\x1b\[[0-9;]*m")

UI_STYLES = {
    "INFO": (CYAN, "ℹ️"),
    "SUCCESS": (GREEN, "✅"),
    "ERROR": (RED, "❌"),
    "WARN": (YELLOW, "⚠️"),
    "TASK": (MAGENTA, "🎮"),
    "CHECKIN": (BLUE, "📅"),
    "NFT": (MAGENTA, "🏅"),
    "REGISTER": (CYAN, "🔗"),
    "POINTS": (YELLOW, "🏆"),
    "SWITCH": (CYAN, "🔄"),
    "SKIP": (GRAY, "⏭️"),
}


# ============================== 基础配置 ==============================

CHAIN_ID = 204
RPC_URLS = ["https://opbnb-mainnet-rpc.bnbchain.org"]
CORE_ADDRESS = "0x01f9Eb284F94b54CF0854ef3B6FeF69C10babe0C"
BADGE_ADDRESS = "0x6f7Cb024E5B285A9E7eE1b9D31e864e9d2B36627"
API_BASE = "https://core.swarmbase.io"
MIN_GAS_BALANCE_WEI = 100_000_000_000_000  # 0.0001 BNB，与网页预检阈值一致
DEFAULT_TASK_COUNT = 0
DEFAULT_THREADS = 5
SCORE_REFRESH_MAX_WORKERS = 3
SCORE_REFRESH_MAX_ATTEMPTS = 3
SCORE_REFRESH_RETRY_DELAY_SECONDS = 0.5
THREAD_START_DELAY_RANGE = (3.0, 5.0)
BATCH_SIZE = 200
BATCH_REST_RANGE = (60.0, 90.0)
BUILDER_MIN_SCORE = 1_000
OG_MIN_SCORE = 5_000
OG_MIN_REGISTRATION_DAYS = 14
OG_MAX_SUPPLY = 5_000
BUILDER_BADGE_ID = 2
OG_BADGE_ID = 3
CHAIN_CONFIRMATIONS = 2
CONTRACT_STATE_TIMEOUT = 120
TASK_DELAY_RANGE = (2.0, 4.0)
TASK_STREAM_TIMEOUT = 150
HTTP_TIMEOUT = 30
OUTPUT_LOCK = threading.Lock()

CORE_ABI = [
    {
        "inputs": [],
        "name": "register",
        "outputs": [],
        "stateMutability": "nonpayable",
        "type": "function",
    },
    {
        "inputs": [{"internalType": "address", "name": "referrer", "type": "address"}],
        "name": "registerWithReferral",
        "outputs": [],
        "stateMutability": "nonpayable",
        "type": "function",
    },
    {
        "inputs": [],
        "name": "hiveCheckIn",
        "outputs": [],
        "stateMutability": "nonpayable",
        "type": "function",
    },
    {
        "inputs": [{"internalType": "address", "name": "", "type": "address"}],
        "name": "registered",
        "outputs": [{"internalType": "bool", "name": "", "type": "bool"}],
        "stateMutability": "view",
        "type": "function",
    },
    {
        "inputs": [{"internalType": "address", "name": "", "type": "address"}],
        "name": "swarmScore",
        "outputs": [{"internalType": "uint256", "name": "", "type": "uint256"}],
        "stateMutability": "view",
        "type": "function",
    },
    {
        "inputs": [{"internalType": "address", "name": "", "type": "address"}],
        "name": "hiveStreak",
        "outputs": [{"internalType": "uint256", "name": "", "type": "uint256"}],
        "stateMutability": "view",
        "type": "function",
    },
    {
        "inputs": [{"internalType": "address", "name": "", "type": "address"}],
        "name": "lastHiveCheckIn",
        "outputs": [{"internalType": "uint256", "name": "", "type": "uint256"}],
        "stateMutability": "view",
        "type": "function",
    },
    {
        "inputs": [{"internalType": "address", "name": "", "type": "address"}],
        "name": "totalCheckIns",
        "outputs": [{"internalType": "uint256", "name": "", "type": "uint256"}],
        "stateMutability": "view",
        "type": "function",
    },
    {
        "inputs": [{"internalType": "address", "name": "", "type": "address"}],
        "name": "registrationTime",
        "outputs": [{"internalType": "uint256", "name": "", "type": "uint256"}],
        "stateMutability": "view",
        "type": "function",
    },
    {
        "inputs": [{"internalType": "address", "name": "", "type": "address"}],
        "name": "referralCount",
        "outputs": [{"internalType": "uint256", "name": "", "type": "uint256"}],
        "stateMutability": "view",
        "type": "function",
    },
]

BADGE_ABI = [
    {
        "inputs": [
            {"internalType": "address", "name": "account", "type": "address"},
            {"internalType": "uint256", "name": "id", "type": "uint256"},
        ],
        "name": "balanceOf",
        "outputs": [{"internalType": "uint256", "name": "", "type": "uint256"}],
        "stateMutability": "view",
        "type": "function",
    },
    {
        "inputs": [],
        "name": "mintPioneer",
        "outputs": [],
        "stateMutability": "nonpayable",
        "type": "function",
    },
    {
        "inputs": [],
        "name": "mintBuilder",
        "outputs": [],
        "stateMutability": "nonpayable",
        "type": "function",
    },
    {
        "inputs": [],
        "name": "mintOG",
        "outputs": [],
        "stateMutability": "nonpayable",
        "type": "function",
    },
    {
        "inputs": [{"internalType": "uint256", "name": "id", "type": "uint256"}],
        "name": "totalMinted",
        "outputs": [{"internalType": "uint256", "name": "", "type": "uint256"}],
        "stateMutability": "view",
        "type": "function",
    },
]

RESULT_COLUMNS = [
    "Address",
    "循环绑定地址",
    "BNB余额",
    "注册状态",
    "注册交易",
    "签到状态",
    "签到交易",
    "签到完成UTC",
    "PioneerNFT状态",
    "PioneerNFT交易",
    "BuilderNFT状态",
    "BuilderNFT交易",
    "OGNFT状态",
    "OGNFT交易",
    "任务计划数",
    "任务提交数",
    "任务完成数",
    "任务失败数",
    "任务详情",
    "链上积分",
    "账户累计积分",
    "任务积分",
    "总积分",
    "签到次数",
    "连续签到",
    "邀请数量",
    "最终状态",
    "更新时间",
]

TASK_TEMPLATES = [
    {
        "mode": "build",
        "agents": ["builder", "ux-critic", "deployer"],
        "goal": "Build a complete SaaS analytics dashboard with sidebar navigation, KPI cards, interactive charts, a sortable data table, and a dark mode toggle.",
    },
    {
        "mode": "build",
        "agents": ["builder", "ux-critic", "deployer"],
        "goal": "Build a responsive admin panel with user management, role permissions, application settings, an activity timeline, and a collapsible sidebar.",
    },
    {
        "mode": "build",
        "agents": ["builder", "creative", "deployer"],
        "goal": "Build a creative portfolio website with a filterable project grid, an about timeline, a blog section, and a validated contact form.",
    },
    {
        "mode": "build",
        "agents": ["builder", "ux-critic", "deployer"],
        "goal": "Build a premium e-commerce product page with image zoom, size and color selectors, cart quantity controls, reviews, and related products.",
    },
    {
        "mode": "build",
        "agents": ["builder", "code-reviewer", "deployer"],
        "goal": "Build a technical documentation site with tree navigation, syntax-highlighted code, copy buttons, breadcrumbs, and full-text search.",
    },
    {
        "mode": "build",
        "agents": ["builder", "ux-critic", "deployer"],
        "goal": "Build a project management dashboard with Kanban columns, task filters, team avatars, progress charts, and a responsive mobile layout.",
    },
    {
        "mode": "build",
        "agents": ["builder", "creative", "deployer"],
        "goal": "Build a modern restaurant landing page with a menu gallery, reservation form, customer testimonials, location map, and mobile navigation.",
    },
    {
        "mode": "build",
        "agents": ["builder", "ux-critic", "deployer"],
        "goal": "Build a finance tracking app interface with account cards, spending categories, transaction search, monthly charts, and budget alerts.",
    },
    {
        "mode": "swarm",
        "agents": ["researcher", "analyst", "creative"],
        "goal": "Research and summarize practical onboarding improvements for a new developer platform, then provide prioritized recommendations and example copy.",
    },
    {
        "mode": "swarm",
        "agents": ["researcher", "analyst", "code-reviewer"],
        "goal": "Analyze common usability and performance problems in modern dashboards and produce a concise implementation checklist with measurable targets.",
    },
    {
        "mode": "build",
        "agents": ["builder", "creative", "deployer"],
        "goal": "Build an event conference website with speaker cards, agenda tabs, ticket pricing, sponsor logos, FAQ accordion, and registration call-to-action.",
    },
    {
        "mode": "build",
        "agents": ["builder", "ux-critic", "deployer"],
        "goal": "Build a customer support portal with searchable knowledge base, category cards, ticket form, status tracker, and responsive layout.",
    },
]


# ============================== 纯函数 ==============================


def parse_account_range(text: str, total: int) -> list[int]:
    """把 1-3,5 解析成 0 基索引；空值/all 表示全部。"""
    if total < 0:
        raise ValueError("账号总数不能为负数")
    value = (text or "").strip().lower()
    if not value or value in {"all", "全部", "*"}:
        return list(range(total))

    selected: set[int] = set()
    for part in re.split(r"[,，\s]+", value):
        if not part:
            continue
        if "-" in part:
            left, right = part.split("-", 1)
            start, end = int(left), int(right)
            if start > end:
                start, end = end, start
            for number in range(start, end + 1):
                if 1 <= number <= total:
                    selected.add(number - 1)
        else:
            number = int(part)
            if 1 <= number <= total:
                selected.add(number - 1)
    if not selected:
        raise ValueError("账号范围没有匹配到有效账号")
    return sorted(selected)


def resolve_private_key_column(fieldnames: Iterable[str]) -> str:
    names = list(fieldnames or [])
    aliases = ["PrivateKey", "private_key", "privatekey", "私钥", "钱包私钥"]
    normalized = {str(name).strip().lower(): name for name in names}
    for alias in aliases:
        original = normalized.get(alias.lower())
        if original is not None:
            return original
    raise ValueError("CSV 中未找到私钥列，请使用 PrivateKey 或 私钥 作为表头")


def resolve_binding_column(fieldnames: Iterable[str]) -> str:
    """第二列固定作为循环绑定地址池。"""
    names = list(fieldnames or [])
    if len(names) < 2:
        raise ValueError("CSV 至少需要两列：第一列私钥，第二列循环绑定地址")
    return names[1]


def collect_binding_address_pool(
    rows: Iterable[dict[str, str]], binding_column: str
) -> list[str]:
    """按第二列出现顺序收集有效地址，并去重。"""
    pool: list[str] = []
    seen: set[str] = set()
    for row in rows:
        value = str(row.get(binding_column, "") or "").strip()
        if not re.fullmatch(r"0x[0-9a-fA-F]{40}", value):
            continue
        key = value.lower()
        if key in seen:
            continue
        seen.add(key)
        pool.append(value)
    return pool


def pick_binding_address(pool: list[str], position: int) -> str:
    if not pool:
        raise ValueError("循环绑定地址池为空")
    return pool[position % len(pool)]


def format_chain_timestamp_utc(timestamp: int) -> str:
    timestamp_value = parse_integer_value(timestamp)
    if timestamp_value <= 0:
        return ""
    return datetime.fromtimestamp(timestamp_value, tz=timezone.utc).strftime(
        "%Y-%m-%d %H:%M:%S UTC"
    )


def is_utc_marker_today(
    marker: str, now: datetime | None = None
) -> bool:
    value = str(marker or "").strip()
    if not value:
        return False
    current = now or datetime.now(timezone.utc)
    if current.tzinfo is None:
        current = current.replace(tzinfo=timezone.utc)
    try:
        marked = datetime.strptime(value, "%Y-%m-%d %H:%M:%S UTC").replace(
            tzinfo=timezone.utc
        )
    except ValueError:
        return False
    return marked.date() == current.astimezone(timezone.utc).date()


def split_batches(items: list[int], size: int = BATCH_SIZE) -> list[list[int]]:
    if size <= 0:
        raise ValueError("批次大小必须大于 0")
    return [items[start : start + size] for start in range(0, len(items), size)]


def normalize_private_key(value: str) -> str:
    key = str(value or "").strip().strip('"').strip("'")
    if key.startswith("0x"):
        key = key[2:]
    if not re.fullmatch(r"[0-9a-fA-F]{64}", key):
        raise ValueError("私钥格式错误，应为 64 位十六进制")
    return "0x" + key


def parse_sse_text(text: str) -> dict[str, Any]:
    """解析 HAR/HTTP 中的完整 SSE 文本。"""
    result: dict[str, Any] = {
        "status": "running",
        "terminal": False,
        "site_url": "",
        "error": "",
        "events": [],
    }
    normalized = (text or "").replace("\r\n", "\n")
    for block in re.split(r"\n\s*\n", normalized):
        if not block.strip():
            continue
        event_name = "message"
        data_lines: list[str] = []
        for line in block.splitlines():
            if line.startswith("event:"):
                event_name = line[6:].strip()
            elif line.startswith("data:"):
                data_lines.append(line[5:].strip())
        if not data_lines:
            continue
        raw_data = "\n".join(data_lines)
        try:
            payload: Any = json.loads(raw_data)
        except json.JSONDecodeError:
            payload = {"message": raw_data}
        result["events"].append({"event": event_name, "data": payload})
        _apply_sse_event(result, event_name, payload)
    return result


def _apply_sse_event(result: dict[str, Any], event_name: str, payload: Any) -> None:
    if not isinstance(payload, dict):
        return
    for key in ("siteUrl", "previewUrl", "url"):
        value = payload.get(key)
        if isinstance(value, str) and value.startswith("http"):
            result["site_url"] = value
            break
    if event_name in {"website_built", "preview", "preview_update"}:
        site_url = payload.get("siteUrl") or payload.get("previewUrl") or payload.get("url")
        if isinstance(site_url, str):
            result["site_url"] = site_url

    if event_name in {"task_complete", "complete", "done"}:
        status = str(payload.get("status") or "completed").lower()
        if status in {"failed", "error"}:
            result["status"] = "failed"
            result["error"] = str(payload.get("error") or payload.get("message") or "任务失败")
        else:
            result["status"] = "completed"
        result["terminal"] = True
    elif event_name == "error":
        result["status"] = "failed"
        result["terminal"] = True
        result["error"] = str(payload.get("error") or payload.get("message") or "任务流错误")


def parse_earn_response(payload: dict[str, Any]) -> dict[str, Any]:
    on_chain = payload.get("onChain") or {}
    off_chain = payload.get("offChain") or {}
    raw_score = parse_integer_value(on_chain.get("swarmScore"))
    account_points = raw_score // 10**18 if raw_score >= 10**18 else raw_score
    task_points = parse_integer_value(off_chain.get("taskCredits"))
    return {
        "account_points": account_points,
        "chain_points": account_points,
        "task_points": task_points,
        "total_points": account_points,
        "tasks_completed": parse_integer_value(off_chain.get("tasksCompleted")),
        "registered": bool(on_chain.get("registered")),
        "checkins": parse_integer_value(on_chain.get("totalCheckIns")),
        "streak": parse_integer_value(on_chain.get("streak")),
        "referrals": (
            None
            if on_chain.get("referrals") in (None, "")
            else parse_integer_value(on_chain.get("referrals"))
        ),
        "pioneer": bool((on_chain.get("badges") or {}).get("pioneer")),
        "history": off_chain.get("history") or [],
    }


def clean_error(exc: BaseException | str, limit: int = 350) -> str:
    text = str(exc).replace("\n", " ").replace("\r", " ").strip()
    text = re.sub(r"\s+", " ", text)
    return text[:limit]


def short_address(address: str) -> str:
    return address[:6] + "..." + address[-4:] if len(address) >= 12 else address


def _color(text: str, color: str, enabled: bool) -> str:
    return f"{color}{text}{RESET}" if enabled else text


def _display_width(text: str) -> int:
    plain = ANSI_PATTERN.sub("", str(text))
    return sum(2 if unicodedata.east_asian_width(char) in {"W", "F"} else 1 for char in plain)


def _box_row(text: str, width: int = 68) -> str:
    padding = " " * max(0, width - _display_width(text))
    return f"│ {text}{padding} │"


def _format_box(lines: list[str], border_color: str = CYAN, color_enabled: bool = True) -> str:
    width = max(68, max((_display_width(line) for line in lines), default=0))
    top = "╭" + "─" * (width + 2) + "╮"
    bottom = "╰" + "─" * (width + 2) + "╯"
    rendered = [top, *[_box_row(line, width) for line in lines], bottom]
    if not color_enabled:
        return "\n".join(rendered)
    return "\n".join(_color(line, border_color, True) for line in rendered)


def format_log_line(
    wallet: str,
    message: str,
    level: str = "INFO",
    account_tag: str = "",
    now_text: str | None = None,
    color_enabled: bool = COLOR_ENABLED,
) -> str:
    """生成与 Knidos 一致的时间戳、图标、账号进度和短地址日志。"""
    color, icon = UI_STYLES.get(level, (RESET, "•"))
    current_time = now_text or datetime.now().strftime("%H:%M:%S")
    short_wallet = wallet
    if wallet.startswith("0x") and len(wallet) > 12:
        short_wallet = short_address(wallet)
    elif wallet == "SYSTEM":
        short_wallet = "   SYSTEM   "
    account_prefix = f"[{account_tag:>7}] " if account_tag else ""
    if not color_enabled:
        return f"[{current_time}] {icon} {account_prefix}[{short_wallet}] {message}"
    return (
        f"{GRAY}[{current_time}]{RESET} {color}{icon}{RESET} "
        f"{BLUE}{account_prefix}{RESET}{CYAN}[{short_wallet}]{RESET} "
        f"{color}{message}{RESET}"
    )


def ui_print(message: str = "") -> None:
    """保证多线程日志按完整行输出，避免不同账号的字符互相覆盖。"""
    with OUTPUT_LOCK:
        print(message, flush=True)


def ui_log(wallet: str, message: str, level: str = "INFO", account_tag: str = "") -> None:
    ui_print(format_log_line(wallet, message, level, account_tag))


def parse_integer_value(value: Any, default: int = 0) -> int:
    """解析 RPC/API 常见的十进制、十六进制和整数值。"""
    if value is None or value == "":
        return default
    if isinstance(value, bool):
        return int(value)
    if isinstance(value, int):
        return value
    text = str(value).strip()
    if not text:
        return default
    base = 16 if text.lower().startswith("0x") else 10
    try:
        return int(text, base)
    except ValueError as exc:
        raise ValueError(f"无法解析整数：{value}") from exc


def normalize_account_score_fields(row: dict[str, Any]) -> dict[str, Any]:
    """迁移旧结果：账户累计积分优先，否则沿用旧链上积分；总积分不含任务积分。"""
    account_points = row.get("账户累计积分")
    if account_points in ("", None):
        account_points = row.get("链上积分")
    if account_points not in ("", None):
        row["账户累计积分"] = account_points
        row["总积分"] = account_points
    return row


def evaluate_required_chain_status(
    checkin_result: dict[str, Any], nft_result: dict[str, Any]
) -> tuple[bool, str]:
    """只根据每日签到和 Pioneer NFT 判断账号是否完成。"""
    checkin_status = str(checkin_result.get("status") or "").lower()
    checkin_ok = checkin_status == "success" or (
        checkin_status == "skipped" and bool(checkin_result.get("checkin_utc"))
    )
    nft_ok = str(nft_result.get("status") or "").lower() in {"success", "skipped"}
    if checkin_ok and nft_ok:
        return True, "完成"

    missing: list[str] = []
    if not checkin_ok:
        missing.append("每日签到未完成")
    if not nft_ok:
        missing.append("Pioneer NFT 未完成")
    return False, "链上未完成：" + " | ".join(missing)


def get_badge_eligibility(
    state: dict[str, Any], now_timestamp: int | None = None
) -> dict[str, dict[str, Any]]:
    """根据链上最新状态判断 Builder 和 OG 是否满足 Mint 条件。"""
    now = int(time.time() if now_timestamp is None else now_timestamp)
    registered = bool(state.get("registered"))
    score = parse_integer_value(state.get("chain_points"))
    registration_time = parse_integer_value(state.get("registration_time"))
    registration_days = (
        max(0, (now - registration_time) // 86_400) if registration_time else 0
    )
    builder_owned = bool(state.get("builder"))
    og_owned = bool(state.get("og"))
    og_minted = parse_integer_value(state.get("og_minted"))

    if builder_owned:
        builder_reason = "已持有"
    elif not registered:
        builder_reason = "未注册"
    elif score < BUILDER_MIN_SCORE:
        builder_reason = f"积分不足，需要 {BUILDER_MIN_SCORE:,}"
    else:
        builder_reason = "满足条件"

    if og_owned:
        og_reason = "已持有"
    elif not registered:
        og_reason = "未注册"
    elif score < OG_MIN_SCORE:
        og_reason = f"积分不足，需要 {OG_MIN_SCORE:,}"
    elif registration_days < OG_MIN_REGISTRATION_DAYS:
        og_reason = f"注册未满 {OG_MIN_REGISTRATION_DAYS} 天"
    elif og_minted >= OG_MAX_SUPPLY:
        og_reason = f"OG 已达 {OG_MAX_SUPPLY:,} 上限"
    else:
        og_reason = "满足条件"

    return {
        "builder": {
            "eligible": builder_reason == "满足条件",
            "owned": builder_owned,
            "reason": builder_reason,
            "score": score,
        },
        "og": {
            "eligible": og_reason == "满足条件",
            "owned": og_owned,
            "reason": og_reason,
            "score": score,
            "registration_days": registration_days,
            "og_minted": og_minted,
        },
    }


def format_score_referral_line(points: Any, referrals: Any) -> str:
    """生成签到完成后的积分和邀请数量单行提示。"""
    points_text = "-" if points in ("", None) else str(parse_integer_value(points))
    referral_text = "-" if referrals in ("", None) else str(parse_integer_value(referrals))
    return f"签到后积分：{points_text} | 邀请人数：{referral_text}"

def format_account_summary(
    row_number: int,
    address: str,
    output: dict[str, Any],
    total_accounts: int = 0,
    skip_daily: bool = False,
    color_enabled: bool = COLOR_ENABLED,
) -> str:
    """生成 Knidos 风格的单账号结束摘要。"""
    account_points = output.get("账户累计积分", "")
    task_points = output.get("任务积分", "")
    total_points = output.get("总积分", "")
    checkins = output.get("签到次数", "")
    streak = output.get("连续签到", "")
    account_text = "-" if account_points in ("", None) else f"{account_points} points"
    task_text = "-" if task_points in ("", None) else f"{task_points} points"
    total_text = "-" if total_points in ("", None) else f"{total_points} points"
    checkin_text = "-" if checkins in ("", None) else str(checkins)
    streak_text = "-" if streak in ("", None) else str(streak)
    account_label = f"{row_number}/{total_accounts}" if total_accounts else str(row_number)
    final_status = str(output.get("最终状态") or "-")
    status_icon = "✅" if final_status == "完成" else "⚠️"
    lines = [
        _color(f"{status_icon} 账号 {account_label} | {short_address(address)} 处理完成", BOLD, color_enabled),
        f"🔗 绑定地址：{short_address(str(output.get('循环绑定地址') or '-'))}",
        "⛓️ 链上："
        f"注册 {output.get('注册状态') or '-'} | "
        f"签到 {output.get('签到状态') or '-'} | "
        f"Pioneer NFT {output.get('PioneerNFT状态') or '-'}",
        f"📅 累计签到：{checkin_text} 次 | 连续签到：{streak_text} 天",
        "🎮 任务："
        f"提交 {output.get('任务提交数') or 0} | "
        f"完成 {output.get('任务完成数') or 0} | "
        f"失败 {output.get('任务失败数') or 0}",
        f"🏆 账户累计积分：{account_text} | 任务积分：{task_text}（不计入总分）",
        f"📊 总积分：{total_text} | 最终状态：{final_status}",
    ]
    return _format_box(lines, MAGENTA, color_enabled)


def _is_missing_metric(value: Any) -> bool:
    return value is None or str(value).strip() in {"", "-"}


def _is_unreliable_score_response(row: dict[str, Any], earn: dict[str, Any]) -> bool:
    """跳过账号的积分为 0 时视为接口瞬时异常，避免覆盖已有结果。"""
    raw_points = earn.get("account_points")
    if _is_missing_metric(raw_points):
        return True
    try:
        points = parse_integer_value(raw_points)
    except (TypeError, ValueError):
        return True
    if points > 0:
        return False
    # 这些账号已经有当天签到标记；此时接口返回 0 与已签到状态矛盾。
    return bool(str(row.get("签到完成UTC") or "").strip()) or bool(
        earn.get("registered") and parse_integer_value(earn.get("checkins")) > 0
    )


def apply_account_earn_points(row: dict[str, Any], earn: dict[str, Any]) -> dict[str, Any]:
    """把公开积分接口的账户分数和邀请数量写回结果行。"""
    account_points = earn.get("account_points")
    if account_points not in ("", None):
        row["链上积分"] = account_points
        row["账户累计积分"] = account_points
        row["总积分"] = account_points
    if earn.get("checkins") is not None:
        row["签到次数"] = earn["checkins"]
    if earn.get("streak") is not None:
        row["连续签到"] = earn["streak"]
    new_referrals = earn.get("referrals")
    if not _is_missing_metric(new_referrals):
        old_referrals = row.get("邀请数量")
        if _is_missing_metric(old_referrals):
            row["邀请数量"] = new_referrals
        else:
            try:
                # 成功邀请数应为单调不减，避免接口异常的 0 覆盖已有值。
                if parse_integer_value(new_referrals) >= parse_integer_value(old_referrals):
                    row["邀请数量"] = new_referrals
            except (TypeError, ValueError):
                row["邀请数量"] = new_referrals
    return row


def refresh_skipped_account_scores(
    rows: list[dict[str, Any]],
    skipped_indices: list[int],
    fetch_earn: Callable[[str], dict[str, Any]],
    max_workers: int = DEFAULT_THREADS,
    progress_callback: Callable[[int, int], None] | None = None,
    max_attempts: int = SCORE_REFRESH_MAX_ATTEMPTS,
    retry_delay_seconds: float = SCORE_REFRESH_RETRY_DELAY_SECONDS,
) -> int:
    """低并发、带重试地刷新当天跳过账号的只读积分。"""
    if not skipped_indices:
        return 0

    def fetch_one(index: int) -> tuple[int, dict[str, Any] | None]:
        address = str(rows[index].get("Address") or "").strip()
        if not address:
            return index, None
        attempts = max(1, int(max_attempts))
        for attempt in range(attempts):
            try:
                earn = fetch_earn(address)
                if not _is_unreliable_score_response(rows[index], earn):
                    return index, earn
            except Exception:
                pass
            if attempt + 1 < attempts and retry_delay_seconds > 0:
                time.sleep(float(retry_delay_seconds) * (attempt + 1))
        return index, None

    refreshed = 0
    completed = 0
    worker_count = max(1, min(int(max_workers), SCORE_REFRESH_MAX_WORKERS))
    with ThreadPoolExecutor(max_workers=worker_count) as executor:
        futures = [executor.submit(fetch_one, index) for index in skipped_indices]
        for future in as_completed(futures):
            index, earn = future.result()
            completed += 1
            if earn and earn.get("account_points") not in ("", None):
                apply_account_earn_points(rows[index], earn)
                refreshed += 1
            if progress_callback and (completed % 50 == 0 or completed == len(futures)):
                progress_callback(completed, refreshed)
    return refreshed

def format_chain_points_summary(
    rows: list[dict[str, Any]], selected_indices: list[int]
) -> str:
    """只列出选中账号的账户累计积分、连续签到天数和邀请数量。"""
    lines = ["最终汇总（积分 / 连续签到 / 邀请数量）："]
    for index in selected_indices:
        row = rows[index]
        address = str(row.get("Address") or "").strip()
        points = row.get("账户累计积分")
        if points in ("", None):
            points = row.get("链上积分")
        point_text = "-" if points in ("", None) else f"{format_number(points)} points"
        streak = row.get("连续签到")
        streak_text = "-" if streak in ("", None) else f"{format_number(streak)} 天"
        referrals = row.get("邀请数量")
        referral_text = "-" if referrals in ("", None) else format_number(referrals)
        account_prefix = f"账号 {index + 1} {short_address(address)}" if address else f"账号 {index + 1}"
        lines.append(f"{account_prefix}：{point_text} | 连续签到 {streak_text} | 邀请 {referral_text} 人")
    return "\n".join(lines)

def build_streak_summary(
    rows: list[dict[str, Any]], selected_indices: list[int]
) -> dict[str, Any]:
    """汇总选中账号的累计签到和连续签到数据。"""
    values: list[tuple[int, str, int, int]] = []
    for index in selected_indices:
        row = rows[index]
        raw_checkins = row.get("签到次数")
        raw_streak = row.get("连续签到")
        if raw_checkins in ("", None) and raw_streak in ("", None):
            continue
        try:
            checkins = max(0, parse_integer_value(raw_checkins))
            streak = max(0, parse_integer_value(raw_streak))
        except (TypeError, ValueError):
            continue
        values.append((index + 1, str(row.get("Address") or ""), checkins, streak))

    if not values:
        return {
            "data_count": 0,
            "total_checkins": 0,
            "streak_total": 0,
            "average_streak": 0.0,
            "max_streak": None,
            "min_streak": None,
            "max_account_number": None,
            "max_account_address": "",
        }

    highest = max(values, key=lambda item: item[3])
    streaks = [item[3] for item in values]
    return {
        "data_count": len(values),
        "total_checkins": sum(item[2] for item in values),
        "streak_total": sum(streaks),
        "average_streak": sum(streaks) / len(streaks),
        "max_streak": max(streaks),
        "min_streak": min(streaks),
        "max_account_number": highest[0],
        "max_account_address": highest[1],
    }



def classify_final_status(final_status: Any) -> str:
    """把单账号最终状态归类为完成、部分完成或失败。"""
    value = str(final_status or "").strip()
    if value == "完成":
        return "完成"
    if value == "今日已完成跳过":
        return "今日已完成跳过"
    if value.startswith(("部分完成", "链上未完成")):
        return "部分完成"
    return "失败"


def build_points_summary(
    rows: list[dict[str, Any]], selected_indices: list[int]
) -> dict[str, Any]:
    """按 Knidos 风格汇总选中账号的账户累计积分。"""
    values: list[float] = []
    for index in selected_indices:
        row = rows[index]
        raw_value = row.get("账户累计积分")
        if raw_value in ("", None):
            raw_value = row.get("链上积分")
        if raw_value in ("", None):
            continue
        try:
            values.append(float(str(raw_value).replace(",", "").strip()))
        except (TypeError, ValueError):
            continue

    if not values:
        return {
            "count": 0,
            "total": 0.0,
            "average": 0.0,
            "maximum": None,
            "minimum": None,
        }
    return {
        "count": len(values),
        "total": sum(values),
        "average": sum(values) / len(values),
        "maximum": max(values),
        "minimum": min(values),
    }


def format_number(value: Any) -> str:
    """整数不显示小数，小数最多保留两位。"""
    try:
        number = float(value)
    except (TypeError, ValueError):
        return str(value)
    if number.is_integer():
        return str(int(number))
    return f"{number:.2f}".rstrip("0").rstrip(".")


def build_exception_result(
    existing_row: dict[str, Any], binding_address: str, exc: BaseException | str
) -> dict[str, Any]:
    """线程异常时保留已落盘的地址、签到和积分字段，禁止空结果覆盖。"""
    result = {name: existing_row.get(name, "") for name in RESULT_COLUMNS}
    if not str(result.get("循环绑定地址") or "").strip():
        result["循环绑定地址"] = binding_address
    result["最终状态"] = "程序异常：" + clean_error(exc)
    result["更新时间"] = utc_now_text()
    return result

def format_final_summary(
    summary: dict[str, int],
    streak_summary: dict[str, Any],
    points_summary: dict[str, Any],
    output_path: Path,
    color_enabled: bool = COLOR_ENABLED,
) -> str:
    """生成 Knidos 风格的最终执行与连续签到汇总。"""
    lines = [
        _color("🎉 SwarmBase 批量任务执行完毕", BOLD, color_enabled),
        f"✅ 成功完成账号：{summary.get('完成', 0)}",
        f"⏭️ 今日完成跳过：{summary.get('今日已完成跳过', 0)}",
        f"⚠️ 部分完成账号：{summary.get('部分完成', 0)}",
        f"❌ 完全失败账号：{summary.get('失败', 0)}",
        "────────────────────────────────────────────────────────────",
    ]
    if points_summary.get("count", 0) > 0:
        lines.extend(
            [
                f"🏆 已查询积分账号：{points_summary['count']}",
                f"💰 当前积分总和：{format_number(points_summary['total'])}",
                f"📊 平均积分：{format_number(points_summary['average'])}",
                f"🔼 最高积分：{format_number(points_summary['maximum'])}",
                f"🔽 最低积分：{format_number(points_summary['minimum'])}",
                "────────────────────────────────────────────────────────────",
            ]
        )
    else:
        lines.extend(
            [
                "🏆 本轮没有成功读取到账户积分数据",
                "────────────────────────────────────────────────────────────",
            ]
        )

    if streak_summary.get("data_count", 0) > 0:
        max_address = short_address(str(streak_summary.get("max_account_address") or "-"))
        lines.extend(
            [
                f"📅 已获取签到数据：{streak_summary['data_count']}",
                f"🔢 累计签到次数总和：{streak_summary['total_checkins']}",
                f"🔥 连续签到次数总和：{streak_summary['streak_total']}",
                f"📊 平均连续签到：{streak_summary['average_streak']:.2f} 天",
                f"🔼 最高连续签到：{streak_summary['max_streak']} 天",
                f"🔽 最低连续签到：{streak_summary['min_streak']} 天",
                "🏆 最高连续账号："
                f"账号 {streak_summary['max_account_number']} | "
                f"{max_address} | {streak_summary['max_streak']} 天",
            ]
        )
    else:
        lines.append("📅 本轮没有成功读取到签到次数与连续签到数据")
    lines.extend(
        [
            "────────────────────────────────────────────────────────────",
            f"💾 结果文件：{output_path}",
        ]
    )
    return _format_box(lines, MAGENTA, color_enabled)


def format_runtime_banner(
    input_path: Path,
    output_path: Path,
    selected_count: int,
    total_count: int,
    skipped_count: int,
    work_count: int,
    threads: int,
    task_count: int,
    binding_count: int,
    color_enabled: bool = COLOR_ENABLED,
) -> str:
    lines = [
        _color("🚀 SwarmBase 批量自动化系统 | Knidos UI 风格", BOLD, color_enabled),
        f"📌 链：opBNB Mainnet | chainId {CHAIN_ID}",
        f"📂 输入：{input_path}",
        f"💾 输出：{output_path}",
        f"👥 选择账号：{selected_count}/{total_count} | 待执行 {work_count} | 今日跳过 {skipped_count}",
        f"⚙️ 并发线程：{threads} | 每账号任务：{task_count} | 绑定地址池：{binding_count}",
        f"⏱️ 启动错峰：{THREAD_START_DELAY_RANGE[0]:.0f}-{THREAD_START_DELAY_RANGE[1]:.0f} 秒 | "
        f"每 {BATCH_SIZE} 账号休息 {BATCH_REST_RANGE[0]:.0f}-{BATCH_REST_RANGE[1]:.0f} 秒",
        f"⛓️ 链上确认：交易回执 + {CHAIN_CONFIRMATIONS} 个区块 + 合约状态确认",
        f"🕒 当前 UTC：{utc_now_text()}",
    ]
    return _format_box(lines, CYAN, color_enabled)


def utc_now_text() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")


def make_task_plan(count: int, seed: str | int | None = None) -> list[dict[str, Any]]:
    if count <= 0:
        return []
    rng = random.Random(seed)
    plan: list[dict[str, Any]] = []
    cycle = 0
    while len(plan) < count:
        templates = [dict(item) for item in TASK_TEMPLATES]
        rng.shuffle(templates)
        for item in templates:
            if len(plan) >= count:
                break
            copied = {
                "mode": item["mode"],
                "agents": list(item["agents"]),
                "goal": item["goal"],
            }
            if cycle > 0:
                copied["goal"] += f" Variation {cycle + 1}: use realistic content and a polished responsive layout."
            plan.append(copied)
        cycle += 1
    # 任务数大于 1 时确保包含 Deploy Swarm 类任务。
    if count > 1 and not any(item["mode"] == "swarm" for item in plan):
        swarm_template = next(item for item in TASK_TEMPLATES if item["mode"] == "swarm")
        plan[-1] = {
            "mode": swarm_template["mode"],
            "agents": list(swarm_template["agents"]),
            "goal": swarm_template["goal"],
        }
    return plan


# ============================== CSV 工具 ==============================


CSV_READ_ENCODINGS = ("utf-8-sig", "gb18030", "utf-16")


def decode_csv_bytes(data: bytes) -> tuple[str, str]:
    """按常见 Excel CSV 编码顺序解码，返回文本和实际编码。"""
    if not data:
        return "", "utf-8-sig"

    encodings = list(CSV_READ_ENCODINGS)
    if data.startswith((b"\xff\xfe", b"\xfe\xff")):
        encodings.remove("utf-16")
        encodings.insert(0, "utf-16")

    errors: list[str] = []
    for encoding in encodings:
        try:
            text = data.decode(encoding)
        except UnicodeDecodeError as exc:
            errors.append(f"{encoding}: {exc}")
            continue
        return text, encoding

    detail = "; ".join(errors)
    raise UnicodeError(
        "CSV 编码无法识别，请在 Excel 中另存为 UTF-8 CSV。"
        + (f" 详细信息：{detail}" if detail else "")
    )


def read_csv_rows(path: Path) -> tuple[list[dict[str, str]], list[str]]:
    text, _encoding = decode_csv_bytes(path.read_bytes())
    with io.StringIO(text, newline="") as file:
        reader = csv.DictReader(file)
        if not reader.fieldnames:
            raise ValueError("CSV 没有表头")
        rows = [dict(row) for row in reader]
        return rows, list(reader.fieldnames)


def atomic_write_csv(path: Path, rows: list[dict[str, Any]], fieldnames: list[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_name = tempfile.mkstemp(prefix=path.stem + "_", suffix=".tmp", dir=str(path.parent))
    os.close(fd)
    temp_path = Path(temp_name)
    try:
        with temp_path.open("w", encoding="utf-8-sig", newline="") as file:
            writer = csv.DictWriter(file, fieldnames=fieldnames, extrasaction="ignore")
            writer.writeheader()
            for row in rows:
                writer.writerow({name: row.get(name, "") for name in fieldnames})
        os.replace(temp_path, path)
    finally:
        if temp_path.exists():
            temp_path.unlink(missing_ok=True)


def ensure_airdrop_input(path: Path) -> None:
    if not is_airdrop():
        return
    if wallet_mode() == "specified":
        if not path.is_file():
            raise FileNotFoundError(f"指定钱包文件不存在：{path}")
        return

    from eth_account import Account

    if path.exists() and path.stat().st_size:
        rows, fields = read_csv_rows(path)
    else:
        rows, fields = [], ["PrivateKey", "循环绑定地址"]

    new_count = 0
    if not rows:
        new_count = env_int("AIRDROP_WALLET_COUNT", "SWARMBASE_WALLET_COUNT", default=1, minimum=1)
    append_count = env_int(
        "AIRDROP_APPEND_WALLET_COUNT",
        "SWARMBASE_APPEND_WALLET_COUNT",
        default=0,
        minimum=0,
    )
    accounts = [Account.create() for _ in range(new_count + append_count)]
    referral = os.environ.get("AIRDROP_REFERRAL_ADDRESS", "").strip()
    if not re.fullmatch(r"0x[0-9a-fA-F]{40}", referral) and accounts:
        referral = accounts[0].address
    rows.extend(
        {"PrivateKey": account.key.hex(), fields[1]: referral}
        for account in accounts
    )
    if new_count or append_count or not path.exists():
        atomic_write_csv(path, rows, fields)


# ============================== 链上执行 ==============================


def load_web3() -> Any:
    try:
        from web3 import Web3
    except ImportError as exc:
        raise RuntimeError("缺少 web3：请先执行 pip install -r 依赖列表.txt") from exc
    return Web3


class TransactionPendingError(RuntimeError):
    """交易已广播，但当前无法确认最终状态。"""

    def __init__(self, message: str, tx_hash: str):
        super().__init__(message)
        self.tx_hash = tx_hash


class SwarmChainClient:
    def __init__(self, private_key: str, rpc_urls: list[str] | None = None):
        Web3 = load_web3()
        self.Web3 = Web3
        self.private_key = normalize_private_key(private_key)
        self.w3, self.rpc_url = self._connect(rpc_urls or RPC_URLS)
        self.account = self.w3.eth.account.from_key(self.private_key)
        self.address = self.Web3.to_checksum_address(self.account.address)
        self.core = self.w3.eth.contract(
            address=self.Web3.to_checksum_address(CORE_ADDRESS), abi=CORE_ABI
        )
        self.badge = self.w3.eth.contract(
            address=self.Web3.to_checksum_address(BADGE_ADDRESS), abi=BADGE_ABI
        )

    def _connect(self, urls: list[str]) -> tuple[Any, str]:
        errors: list[str] = []
        for url in urls:
            try:
                w3 = self.Web3(self.Web3.HTTPProvider(url, request_kwargs={"timeout": 20}))
                if w3.is_connected() and parse_integer_value(w3.eth.chain_id) == CHAIN_ID:
                    return w3, url
                errors.append(f"{url}: 无法连接或 chainId 不正确")
            except Exception as exc:
                errors.append(f"{url}: {clean_error(exc, 120)}")
        raise ConnectionError("opBNB RPC 全部不可用；" + " | ".join(errors))

    def get_state(self) -> dict[str, Any]:
        balance = parse_integer_value(self.w3.eth.get_balance(self.address))
        registered = bool(self.core.functions.registered(self.address).call())
        registration_time = parse_integer_value(
            self.core.functions.registrationTime(self.address).call()
        )
        referrals = parse_integer_value(
            self.core.functions.referralCount(self.address).call()
        )
        last_checkin = parse_integer_value(
            self.core.functions.lastHiveCheckIn(self.address).call()
        )
        checkins = parse_integer_value(self.core.functions.totalCheckIns(self.address).call())
        streak = parse_integer_value(self.core.functions.hiveStreak(self.address).call())
        score_raw = parse_integer_value(self.core.functions.swarmScore(self.address).call())
        pioneer = parse_integer_value(
            self.badge.functions.balanceOf(self.address, 1).call()
        ) > 0
        builder = parse_integer_value(
            self.badge.functions.balanceOf(self.address, BUILDER_BADGE_ID).call()
        ) > 0
        og = parse_integer_value(
            self.badge.functions.balanceOf(self.address, OG_BADGE_ID).call()
        ) > 0
        og_minted = parse_integer_value(
            self.badge.functions.totalMinted(OG_BADGE_ID).call()
        )
        return {
            "balance_wei": balance,
            "balance_bnb": balance / 10**18,
            "registered": registered,
            "registration_time": registration_time,
            "referrals": referrals,
            "last_checkin": last_checkin,
            "checkins": checkins,
            "streak": streak,
            "chain_points": score_raw // 10**18,
            "pioneer": pioneer,
            "builder": builder,
            "og": og,
            "og_minted": og_minted,
        }

    def _send(self, contract_function: Any, label: str) -> dict[str, Any]:
        """只在广播前重试；拿到 tx hash 后绝不重复广播。"""
        last_error: Exception | None = None
        tx_hash_bytes = None
        tx_hash = ""
        gas_price = 0

        for attempt in range(1, 4):
            try:
                nonce = self.w3.eth.get_transaction_count(self.address, "pending")
                estimate = parse_integer_value(contract_function.estimate_gas({"from": self.address}))
                gas_limit = max(int(estimate * 1.25), estimate + 5_000)
                rpc_gas_price = parse_integer_value(self.w3.eth.gas_price)
                gas_price = max(rpc_gas_price, 1_000_000)  # 0.001 gwei
                tx = contract_function.build_transaction(
                    {
                        "from": self.address,
                        "nonce": nonce,
                        "chainId": CHAIN_ID,
                        "gas": gas_limit,
                        "gasPrice": gas_price,
                        "value": 0,
                    }
                )
                signed = self.account.sign_transaction(tx)
                raw = getattr(signed, "raw_transaction", None)
                if raw is None:
                    raw = getattr(signed, "rawTransaction")
                tx_hash_bytes = self.w3.eth.send_raw_transaction(raw)
                tx_hash = tx_hash_bytes.hex()
                break
            except Exception as exc:
                last_error = exc
                message = clean_error(exc).lower()
                if "execution reverted" in message or "revert" in message:
                    break
                if attempt < 3:
                    time.sleep(2 * attempt)

        if tx_hash_bytes is None:
            raise RuntimeError(f"{label}广播失败：{clean_error(last_error or '未知错误')}")

        try:
            receipt = self.w3.eth.wait_for_transaction_receipt(
                tx_hash_bytes, timeout=180, poll_latency=2
            )
        except Exception as exc:
            raise TransactionPendingError(
                f"{label}已广播但等待回执失败，请先查询交易后再决定是否重试："
                f"{tx_hash}；{clean_error(exc, 180)}",
                tx_hash,
            ) from exc

        if parse_integer_value(receipt.get("status", 0)) != 1:
            raise RuntimeError(f"{label}交易已上链但执行失败：{tx_hash}")

        receipt_block = parse_integer_value(receipt.get("blockNumber", 0))
        if receipt_block > 0 and CHAIN_CONFIRMATIONS > 1:
            target_block = receipt_block + CHAIN_CONFIRMATIONS - 1
            deadline = time.monotonic() + CONTRACT_STATE_TIMEOUT
            while parse_integer_value(self.w3.eth.block_number) < target_block:
                if time.monotonic() >= deadline:
                    raise TransactionPendingError(
                        f"{label}已上链，但等待 {CHAIN_CONFIRMATIONS} 个区块确认超时：{tx_hash}",
                        tx_hash,
                    )
                time.sleep(2)

        gas_used = parse_integer_value(receipt.get("gasUsed", 0))
        effective_price = parse_integer_value(receipt.get("effectiveGasPrice", gas_price))
        l1_fee = parse_integer_value(receipt.get("l1Fee", 0))
        total_fee = gas_used * effective_price + l1_fee
        return {
            "status": "success",
            "tx_hash": tx_hash,
            "gas_used": gas_used,
            "fee_bnb": total_fee / 10**18,
        }

    def _wait_for_contract_state(
        self,
        predicate: Callable[[], bool],
        label: str,
        tx_hash: str,
        timeout: int = CONTRACT_STATE_TIMEOUT,
    ) -> None:
        deadline = time.monotonic() + timeout
        last_error = ""
        while time.monotonic() < deadline:
            try:
                if predicate():
                    return
            except Exception as exc:
                last_error = clean_error(exc, 160)
            time.sleep(2)
        extra = f"；最后错误：{last_error}" if last_error else ""
        raise TransactionPendingError(
            f"{label}交易已确认，但等待合约状态更新超时：{tx_hash}{extra}", tx_hash
        )

    def register(self, referrer: str | None) -> dict[str, Any]:
        if self.core.functions.registered(self.address).call():
            return {"status": "skipped", "message": "已注册", "tx_hash": ""}

        use_ref = False
        ref_checksum = None
        if referrer and self.Web3.is_address(referrer):
            ref_checksum = self.Web3.to_checksum_address(referrer)
            use_ref = ref_checksum.lower() != self.address.lower()

        if use_ref and ref_checksum:
            try:
                result = self._send(
                    self.core.functions.registerWithReferral(ref_checksum), "邀请注册"
                )
                self._wait_for_contract_state(
                    lambda: bool(self.core.functions.registered(self.address).call()),
                    "邀请注册",
                    result["tx_hash"],
                )
                return result
            except Exception as ref_error:
                if isinstance(ref_error, TransactionPendingError):
                    raise
                result = self._send(self.core.functions.register(), "普通注册")
                self._wait_for_contract_state(
                    lambda: bool(self.core.functions.registered(self.address).call()),
                    "普通注册",
                    result["tx_hash"],
                )
                result["message"] = (
                    "邀请注册失败后已回退普通注册：" + clean_error(ref_error, 160)
                )
                return result

        result = self._send(self.core.functions.register(), "普通注册")
        self._wait_for_contract_state(
            lambda: bool(self.core.functions.registered(self.address).call()),
            "普通注册",
            result["tx_hash"],
        )
        return result

    def check_in(self) -> dict[str, Any]:
        if not self.core.functions.registered(self.address).call():
            return {"status": "blocked", "message": "未注册，无法签到", "tx_hash": ""}
        last_checkin = parse_integer_value(self.core.functions.lastHiveCheckIn(self.address).call())
        if last_checkin > 0 and int(time.time()) - last_checkin < 86_400:
            return {
                "status": "skipped",
                "message": "距离上次签到不足 24 小时",
                "tx_hash": "",
                "checkin_utc": format_chain_timestamp_utc(last_checkin),
            }
        self.core.functions.hiveCheckIn().call({"from": self.address})
        result = self._send(self.core.functions.hiveCheckIn(), "每日签到")
        self._wait_for_contract_state(
            lambda: parse_integer_value(self.core.functions.lastHiveCheckIn(self.address).call())
            > last_checkin,
            "每日签到",
            result["tx_hash"],
        )
        confirmed_timestamp = int(
            self.core.functions.lastHiveCheckIn(self.address).call()
        )
        result["checkin_utc"] = format_chain_timestamp_utc(confirmed_timestamp)
        return result

    def mint_pioneer(self) -> dict[str, Any]:
        owned = parse_integer_value(self.badge.functions.balanceOf(self.address, 1).call()) > 0
        if owned:
            return {"status": "skipped", "message": "Pioneer NFT 已持有", "tx_hash": ""}
        self.badge.functions.mintPioneer().call({"from": self.address})
        result = self._send(self.badge.functions.mintPioneer(), "Mint Pioneer NFT")
        self._wait_for_contract_state(
            lambda: parse_integer_value(self.badge.functions.balanceOf(self.address, 1).call()) > 0,
            "Mint Pioneer NFT",
            result["tx_hash"],
        )
        return result


    def _mint_badge(
        self, badge_id: int, method_name: str, label: str, owned_message: str
    ) -> dict[str, Any]:
        owned = parse_integer_value(
            self.badge.functions.balanceOf(self.address, badge_id).call()
        ) > 0
        if owned:
            return {"status": "skipped", "message": owned_message, "tx_hash": ""}
        contract_function = getattr(self.badge.functions, method_name)()
        # 先 eth_call 预检，失败时不会广播交易。
        contract_function.call({"from": self.address})
        result = self._send(contract_function, label)
        self._wait_for_contract_state(
            lambda: parse_integer_value(
                self.badge.functions.balanceOf(self.address, badge_id).call()
            ) > 0,
            label,
            result["tx_hash"],
        )
        return result

    def mint_builder(self) -> dict[str, Any]:
        return self._mint_badge(
            BUILDER_BADGE_ID,
            "mintBuilder",
            "Mint Builder NFT",
            "Builder NFT 已持有",
        )

    def mint_og(self) -> dict[str, Any]:
        return self._mint_badge(
            OG_BADGE_ID,
            "mintOG",
            "Mint OG NFT",
            "OG NFT 已持有",
        )

    def mint_eligible_badges(
        self, state: dict[str, Any] | None = None
    ) -> dict[str, dict[str, Any]]:
        current_state = state or self.get_state()
        eligibility = get_badge_eligibility(current_state)
        results: dict[str, dict[str, Any]] = {}
        for key, mint_method, label in (
            ("builder", self.mint_builder, "Builder"),
            ("og", self.mint_og, "OG"),
        ):
            if not eligibility[key]["eligible"]:
                results[key] = {
                    "status": "skipped",
                    "message": f"{label}：" + eligibility[key]["reason"],
                    "tx_hash": "",
                }
                continue
            try:
                results[key] = mint_method()
            except Exception as exc:
                results[key] = {
                    "status": "failed",
                    "message": f"{label} Mint 失败：" + clean_error(exc),
                    "tx_hash": "",
                }
        return results


# ============================== 任务 API ==============================


class SwarmTaskClient:
    def __init__(self):
        self.session = requests.Session()
        self.session.headers.update(
            {
                "User-Agent": (
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/150.0.0.0 Safari/537.36"
                ),
                "Origin": API_BASE,
                "Referer": API_BASE + "/",
                "Accept": "application/json, text/plain, */*",
            }
        )

    def submit_task(self, wallet: str, task: dict[str, Any]) -> str:
        response = self.session.post(
            API_BASE + "/api/task",
            json={
                "goal": task["goal"],
                "wallet": wallet.lower(),
                "agent": None,
                "agents": task.get("agents") or None,
                "mode": task.get("mode") or "build",
            },
            timeout=HTTP_TIMEOUT,
        )
        response.raise_for_status()
        data = response.json()
        task_id = data.get("taskId")
        if not task_id:
            raise RuntimeError("任务接口未返回 taskId")
        return str(task_id)

    def stream_task(self, wallet: str, task_id: str) -> dict[str, Any]:
        url = f"{API_BASE}/api/task/{task_id}/stream"
        result: dict[str, Any] = {
            "status": "running",
            "terminal": False,
            "site_url": "",
            "error": "",
            "events": [],
        }
        current_event = "message"
        data_lines: list[str] = []
        try:
            with self.session.get(
                url,
                params={"wallet": wallet.lower()},
                headers={"Accept": "text/event-stream"},
                stream=True,
                timeout=(15, TASK_STREAM_TIMEOUT),
            ) as response:
                response.raise_for_status()
                for raw_line in response.iter_lines(decode_unicode=True):
                    line = raw_line if isinstance(raw_line, str) else raw_line.decode("utf-8", "replace")
                    if line == "":
                        if data_lines:
                            raw_data = "\n".join(data_lines)
                            try:
                                payload: Any = json.loads(raw_data)
                            except json.JSONDecodeError:
                                payload = {"message": raw_data}
                            result["events"].append({"event": current_event, "data": payload})
                            _apply_sse_event(result, current_event, payload)
                            current_event = "message"
                            data_lines = []
                            if result["terminal"]:
                                break
                        continue
                    if line.startswith("event:"):
                        current_event = line[6:].strip()
                    elif line.startswith("data:"):
                        data_lines.append(line[5:].strip())
                if data_lines and not result["terminal"]:
                    raw_data = "\n".join(data_lines)
                    try:
                        payload = json.loads(raw_data)
                    except json.JSONDecodeError:
                        payload = {"message": raw_data}
                    result["events"].append({"event": current_event, "data": payload})
                    _apply_sse_event(result, current_event, payload)
        except requests.RequestException as exc:
            result["stream_error"] = clean_error(exc)

        if not result["terminal"]:
            fallback = self.get_task(task_id)
            if fallback:
                status = str(fallback.get("status") or "").lower()
                if status in {"completed", "complete", "done"}:
                    result["status"] = "completed"
                    result["terminal"] = True
                elif status in {"failed", "error"}:
                    result["status"] = "failed"
                    result["terminal"] = True
                    result["error"] = str(fallback.get("error") or "任务失败")
                result["site_url"] = result["site_url"] or self._extract_site_url(fallback)
        return result

    def get_task(self, task_id: str) -> dict[str, Any] | None:
        try:
            response = self.session.get(
                f"{API_BASE}/api/task/{task_id}", timeout=HTTP_TIMEOUT
            )
            if response.status_code == 404:
                return None
            response.raise_for_status()
            data = response.json()
            return data if isinstance(data, dict) else None
        except (requests.RequestException, ValueError):
            return None

    @staticmethod
    def _extract_site_url(data: Any) -> str:
        if isinstance(data, dict):
            for key in ("siteUrl", "previewUrl", "url", "shareUrl"):
                value = data.get(key)
                if isinstance(value, str) and value:
                    return value
            for value in data.values():
                found = SwarmTaskClient._extract_site_url(value)
                if found:
                    return found
        elif isinstance(data, list):
            for item in data:
                found = SwarmTaskClient._extract_site_url(item)
                if found:
                    return found
        return ""

    def fetch_earn(self, wallet: str) -> dict[str, Any]:
        response = self.session.get(
            f"{API_BASE}/api/earn/{wallet.lower()}", timeout=HTTP_TIMEOUT
        )
        response.raise_for_status()
        return parse_earn_response(response.json())

    def run_tasks(
        self,
        wallet: str,
        count: int,
        seed: str | int | None = None,
        log_prefix: str = "",
        account_tag: str = "",
    ) -> dict[str, Any]:
        plan = make_task_plan(count, seed)
        details: list[dict[str, Any]] = []
        submitted = completed = failed = 0
        for number, task in enumerate(plan, 1):
            task_id = ""
            try:
                task_id = self.submit_task(wallet, task)
                submitted += 1
                ui_log(
                    wallet,
                    f"任务 {number}/{count} 已提交 [{task['mode']}] {task_id[:8]}...",
                    "TASK",
                    account_tag,
                )
                streamed = self.stream_task(wallet, task_id)
                status = streamed.get("status", "unknown")
                if status == "completed":
                    completed += 1
                    ui_log(wallet, f"任务 {number}/{count} 已完成", "SUCCESS", account_tag)
                elif status == "failed":
                    failed += 1
                    ui_log(
                        wallet,
                        f"任务 {number}/{count} 失败："
                        f"{clean_error(streamed.get('error', '未知错误'), 100)}",
                        "ERROR",
                        account_tag,
                    )
                details.append(
                    {
                        "task_id": task_id,
                        "mode": task["mode"],
                        "status": status,
                        "site_url": streamed.get("site_url", ""),
                        "error": streamed.get("error", ""),
                    }
                )
            except Exception as exc:
                failed += 1
                details.append(
                    {
                        "task_id": task_id,
                        "mode": task["mode"],
                        "status": "submit_failed",
                        "site_url": "",
                        "error": clean_error(exc),
                    }
                )
                ui_log(
                    wallet,
                    f"任务 {number}/{count} 提交失败：{clean_error(exc, 120)}",
                    "ERROR",
                    account_tag,
                )
            if number < count:
                time.sleep(random.uniform(*TASK_DELAY_RANGE))
        return {
            "planned": count,
            "submitted": submitted,
            "completed": completed,
            "failed": failed,
            "details": details,
        }


def should_check_nfts(skip_daily: bool, checkin_result: dict[str, Any]) -> bool:
    """仅在本次实际完成每日签到后检查和 Mint NFT。"""
    return not skip_daily and str(checkin_result.get("status") or "").lower() == "success"

# ============================== 批量调度 ==============================


def result_status_text(result: dict[str, Any]) -> str:
    status = result.get("status", "")
    if status == "success":
        fee = result.get("fee_bnb")
        return f"成功，Gas {fee:.10f} BNB" if isinstance(fee, (int, float)) else "成功"
    if status == "skipped":
        return str(result.get("message") or "已跳过")
    if status == "blocked":
        return str(result.get("message") or "条件不满足")
    return str(result.get("message") or status or "未知")


def process_wallet(
    row_number: int,
    row: dict[str, str],
    private_key_column: str,
    binding_address: str,
    task_count: int,
    checkpoint_callback: Callable[[dict[str, Any]], None] | None = None,
    total_accounts: int = 0,
    skip_daily: bool = False,
) -> dict[str, Any]:
    account_tag = f"{row_number}/{total_accounts}" if total_accounts else str(row_number)
    output: dict[str, Any] = {name: "" for name in RESULT_COLUMNS}
    output["任务计划数"] = task_count
    output["循环绑定地址"] = binding_address
    chain: SwarmChainClient | None = None
    address = ""
    chain_errors: list[str] = []
    checkin_result: dict[str, Any] = {"status": "failed"}
    nft_result: dict[str, Any] = {"status": "failed"}

    latest_state: dict[str, Any] = {}

    try:
        chain = SwarmChainClient(row.get(private_key_column, ""))
        address = chain.address
        output["Address"] = address
        ui_log(
            address,
            f"开始处理 | 绑定 {short_address(binding_address)}",
            "SWITCH",
            account_tag,
        )
        state = chain.get_state()
        output["BNB余额"] = f"{state['balance_bnb']:.8f}"

        if state["balance_wei"] < MIN_GAS_BALANCE_WEI:
            chain_errors.append("opBNB BNB 余额低于 0.0001，链上动作可能失败")
            ui_log(
                address,
                f"Gas 余额偏低：{output['BNB余额']} BNB",
                "WARN",
                account_tag,
            )

        if skip_daily:
            output["注册状态"] = "已注册" if state.get("registered") else "未注册"
            output["注册交易"] = str(row.get("注册交易") or "")
            output["签到状态"] = "今日已签到"
            output["签到交易"] = str(row.get("签到交易") or "")
            output["签到完成UTC"] = str(row.get("签到完成UTC") or "")
            for column in ("PioneerNFT状态", "PioneerNFT交易", "BuilderNFT状态", "BuilderNFT交易", "OGNFT状态", "OGNFT交易"):
                output[column] = str(row.get(column) or "")
            checkin_result = {
                "status": "skipped",
                "message": "今日已签到，跳过签到交易",
                "tx_hash": output["签到交易"],
                "checkin_utc": output["签到完成UTC"],
            }
            ui_log(
                address,
                "今日已完成签到，跳过注册/签到交易，跳过 NFT 检查",
                "SKIP",
                account_tag,
            )
        else:
            try:
                register_result = chain.register(binding_address)
                output["注册状态"] = result_status_text(register_result)
                output["注册交易"] = register_result.get("tx_hash", "")
                register_level = (
                    "SUCCESS"
                    if register_result.get("status") == "success"
                    else "SKIP"
                )
                ui_log(
                    address,
                    f"注册：{output['注册状态']}",
                    register_level,
                    account_tag,
                )
            except Exception as exc:
                output["注册状态"] = "失败：" + clean_error(exc)
                chain_errors.append(output["注册状态"])
                ui_log(
                    address,
                    f"注册失败：{clean_error(exc, 150)}",
                    "ERROR",
                    account_tag,
                )

            try:
                checkin_result = chain.check_in()
                output["签到状态"] = result_status_text(checkin_result)
                output["签到交易"] = checkin_result.get("tx_hash", "")
                checkin_utc = str(checkin_result.get("checkin_utc") or "").strip()
                if checkin_utc:
                    output["签到完成UTC"] = checkin_utc
                    output["更新时间"] = utc_now_text()
                    if checkpoint_callback:
                        checkpoint_callback(
                            {
                                "Address": output["Address"],
                                "循环绑定地址": output["循环绑定地址"],
                                "BNB余额": output["BNB余额"],
                                "注册状态": output["注册状态"],
                                "注册交易": output["注册交易"],
                                "签到状态": output["签到状态"],
                                "签到交易": output["签到交易"],
                                "签到完成UTC": output["签到完成UTC"],
                                "更新时间": output["更新时间"],
                            }
                        )
                checkin_level = (
                    "CHECKIN"
                    if checkin_result.get("status") == "success"
                    else "SKIP"
                )
                ui_log(
                    address,
                    f"签到：{output['签到状态']}"
                    + (f" | UTC {checkin_utc}" if checkin_utc else ""),
                    checkin_level,
                    account_tag,
                )
            except Exception as exc:
                output["签到状态"] = "失败：" + clean_error(exc)
                checkin_result = {"status": "failed", "message": output["签到状态"]}
                chain_errors.append(output["签到状态"])
                ui_log(
                    address,
                    f"签到失败：{clean_error(exc, 150)}",
                    "ERROR",
                    account_tag,
                )

        try:
            latest_state = chain.get_state()
        except Exception as exc:
            chain_errors.append("链上状态刷新失败：" + clean_error(exc))
            latest_state = state
        output["链上积分"] = latest_state.get("chain_points", "")
        output["账户累计积分"] = latest_state.get("chain_points", "")
        output["总积分"] = latest_state.get("chain_points", "")
        output["签到次数"] = latest_state.get("checkins", "")
        output["连续签到"] = latest_state.get("streak", "")
        output["邀请数量"] = latest_state.get("referrals", "")
        if not output["签到完成UTC"] and latest_state.get("last_checkin", 0):
            output["签到完成UTC"] = format_chain_timestamp_utc(
                latest_state["last_checkin"]
            )
        ui_log(
            address,
            format_score_referral_line(
                latest_state.get("chain_points", ""),
                latest_state.get("referrals", ""),
            ),
            "POINTS",
            account_tag,
        )
        ui_log(
            address,
            f"累计签到：{output.get('签到次数') or 0} 次 | "
            f"连续签到：{output.get('连续签到') or 0} 天",
            "INFO",
            account_tag,
        )
        if should_check_nfts(skip_daily, checkin_result):
            try:
                nft_result = chain.mint_pioneer()
                output["PioneerNFT状态"] = result_status_text(nft_result)
                output["PioneerNFT交易"] = nft_result.get("tx_hash", "")
                nft_level = "NFT" if nft_result.get("status") == "success" else "SKIP"
                ui_log(
                    address,
                    f"Pioneer NFT：{output['PioneerNFT状态']}",
                    nft_level,
                    account_tag,
                )
            except Exception as exc:
                output["PioneerNFT状态"] = "失败：" + clean_error(exc)
                nft_result = {"status": "failed", "message": output["PioneerNFT状态"]}
                chain_errors.append(output["PioneerNFT状态"])
                ui_log(
                    address,
                    f"Pioneer NFT 失败：{clean_error(exc, 150)}",
                    "ERROR",
                    account_tag,
                )

            try:
                badge_results = chain.mint_eligible_badges(latest_state)
                for key, state_column, tx_column, label in (
                    ("builder", "BuilderNFT状态", "BuilderNFT交易", "Builder"),
                    ("og", "OGNFT状态", "OGNFT交易", "OG"),
                ):
                    badge_result = badge_results[key]
                    output[state_column] = result_status_text(badge_result)
                    output[tx_column] = badge_result.get("tx_hash", "")
                    status = str(badge_result.get("status") or "").lower()
                    level = "NFT" if status == "success" else "SKIP"
                    ui_log(
                        address,
                        f"{label} NFT：{output[state_column]}",
                        level,
                        account_tag,
                    )
            except Exception as exc:
                message = "Builder/OG NFT 检查失败：" + clean_error(exc)
                chain_errors.append(message)
                for state_column in ("BuilderNFT状态", "OGNFT状态"):
                    if not output[state_column]:
                        output[state_column] = "失败：" + clean_error(exc)
                ui_log(address, message, "ERROR", account_tag)
        else:
            nft_result = {
                "status": "skipped",
                "message": (
                    "今日已签到，跳过 NFT 检查"
                    if skip_daily
                    else "本次未完成新的签到，跳过 NFT 检查"
                ),
            }
            if not skip_daily:
                ui_log(address, nft_result["message"], "SKIP", account_tag)
    except Exception as exc:
        output["最终状态"] = "链上初始化失败：" + clean_error(exc)
        chain_errors.append(output["最终状态"])
        ui_log(
            address or "UNKNOWN",
            f"链上初始化失败：{clean_error(exc, 150)}",
            "ERROR",
            account_tag,
        )
        try:
            Web3 = load_web3()
            from eth_account import Account

            account = Account.from_key(normalize_private_key(row.get(private_key_column, "")))
            address = Web3.to_checksum_address(account.address)
            output["Address"] = address
        except Exception:
            output["更新时间"] = utc_now_text()
            return output

    task_result = {
        "planned": task_count,
        "submitted": 0,
        "completed": 0,
        "failed": 0,
        "details": [],
    }
    earn: dict[str, Any] = {}
    if address and task_count > 0:
        task_client = SwarmTaskClient()
        try:
            task_result = task_client.run_tasks(
                address,
                task_count,
                seed=address.lower(),
                log_prefix="",
                account_tag=account_tag,
            )
        except Exception as exc:
            chain_errors.append("任务阶段异常：" + clean_error(exc))
            task_result["failed"] = max(task_result["failed"], task_count)
            task_result["details"].append(
                {
                    "task_id": "",
                    "mode": "unknown",
                    "status": "program_error",
                    "site_url": "",
                    "error": clean_error(exc),
                }
            )
            ui_log(
                address,
                f"任务阶段异常：{clean_error(exc, 150)}",
                "ERROR",
                account_tag,
            )
        time.sleep(1.5)
        try:
            earn = task_client.fetch_earn(address)
        except Exception as exc:
            chain_errors.append("积分查询失败：" + clean_error(exc))



    output["任务提交数"] = task_result["submitted"]
    output["任务完成数"] = task_result["completed"]
    output["任务失败数"] = task_result["failed"]
    output["任务详情"] = json.dumps(
        task_result["details"], ensure_ascii=False, separators=(",", ":")
    )

    if earn:
        output["任务积分"] = earn["task_points"]
        # 链上状态是签到后的权威值，避免 API 延迟返回 0 覆盖真实积分。
        if output["链上积分"] in ("", None):
            output["链上积分"] = earn["account_points"]
            output["账户累计积分"] = earn["account_points"]
            output["总积分"] = earn["account_points"]
        if output["签到次数"] in ("", None):
            output["签到次数"] = earn["checkins"]
        if output["连续签到"] in ("", None):
            output["连续签到"] = earn["streak"]
        if output["邀请数量"] in ("", None) and earn.get("referrals") not in ("", None):
            output["邀请数量"] = earn["referrals"]
    elif chain is not None and not latest_state:
        try:
            latest_state = chain.get_state()
            output["链上积分"] = latest_state.get("chain_points", "")
            output["账户累计积分"] = latest_state.get("chain_points", "")
            output["总积分"] = latest_state.get("chain_points", "")
            output["签到次数"] = latest_state.get("checkins", "")
            output["连续签到"] = latest_state.get("streak", "")
            output["邀请数量"] = latest_state.get("referrals", "")
        except Exception:
            pass
    if task_count > 0:
        if task_result["submitted"] < task_result["planned"]:
            chain_errors.append("存在任务提交失败")
        if task_result["completed"] < task_result["submitted"]:
            chain_errors.append("部分已提交任务未确认完成")

    _chain_completed, chain_status = evaluate_required_chain_status(
        checkin_result, nft_result
    )
    output["最终状态"] = chain_status
    output["更新时间"] = utc_now_text()
    return output


def merge_resume_results(
    rows: list[dict[str, str]],
    output_path: Path,
    private_key_column: str,
    binding_column: str,
) -> None:
    """把已有结果按私钥合并回来，使 UTC 完成标记可以跨运行生效。"""
    if not output_path.exists():
        return
    try:
        previous_rows, previous_fields = read_csv_rows(output_path)
        previous_key_column = resolve_private_key_column(previous_fields)
    except Exception:
        return

    previous_by_key: dict[str, dict[str, str]] = {}
    for previous in previous_rows:
        try:
            key = normalize_private_key(previous.get(previous_key_column, "")).lower()
        except Exception:
            continue
        previous_by_key[key] = previous

    for row in rows:
        try:
            key = normalize_private_key(row.get(private_key_column, "")).lower()
        except Exception:
            continue
        previous = previous_by_key.get(key)
        if not previous:
            continue
        for column in RESULT_COLUMNS:
            # 第二列是本次运行的地址池来源，不能被旧结果覆盖。
            if column == binding_column:
                continue
            if str(previous.get(column, "") or "").strip():
                row[column] = previous[column]
        normalize_account_score_fields(row)


def run_batch(
    input_path: Path,
    output_path: Path,
    account_range: str,
    threads: int,
    task_count: int,
) -> None:
    rows, original_fields = read_csv_rows(input_path)
    private_key_column = resolve_private_key_column(original_fields)
    binding_column = resolve_binding_column(original_fields)
    merge_resume_results(rows, output_path, private_key_column, binding_column)

    selected_indices = parse_account_range(account_range, len(rows))
    binding_pool = collect_binding_address_pool(rows, binding_column)
    if not binding_pool:
        raise ValueError(f"CSV 第二列“{binding_column}”没有有效的 0x 开头 40 位地址")

    fields = list(original_fields)
    for column in RESULT_COLUMNS:
        if column not in fields:
            fields.append(column)
    for row in rows:
        for column in RESULT_COLUMNS:
            row.setdefault(column, "")
        normalize_account_score_fields(row)

    skipped_indices = [
        index
        for index in selected_indices
        if is_utc_marker_today(rows[index].get("签到完成UTC", ""))
    ]
    skipped_set = set(skipped_indices)
    work_indices = [index for index in selected_indices if index not in skipped_set]

    # 只把未在当天完成签到的账号提交到线程池；已签到账号不刷新积分、不检查 NFT。
    binding_by_index = {
        index: pick_binding_address(binding_pool, position)
        for position, index in enumerate(work_indices)
    }
    if skipped_indices:
        ui_log(
            "SYSTEM",
            f"今日已完成签到账号：{len(skipped_indices)} 个，跳过签到和 NFT 检查；直接处理可签到账号",
            "SKIP",
        )

    atomic_write_csv(output_path, rows, fields)
    ui_print(
        format_runtime_banner(
            input_path=input_path,
            output_path=output_path,
            selected_count=len(selected_indices),
            total_count=len(rows),
            skipped_count=len(skipped_indices),
            work_count=len(work_indices),
            threads=threads,
            task_count=task_count,
            binding_count=len(binding_pool),
        )
    )

    write_lock = threading.Lock()
    summary = {
        "完成": 0,
        "部分完成": 0,
        "失败": 0,
        "今日已完成跳过": len(skipped_indices),
    }

    def save_checkpoint(index: int, partial: dict[str, Any]) -> None:
        with write_lock:
            rows[index].update(partial)
            atomic_write_csv(output_path, rows, fields)

    def save_final(index: int, result: dict[str, Any]) -> None:
        with write_lock:
            rows[index].update(result)
            atomic_write_csv(output_path, rows, fields)
            if index in skipped_set:
                return
            category = classify_final_status(result.get("最终状态", ""))
            if category == "完成":
                summary["完成"] += 1
            elif category == "部分完成":
                summary["部分完成"] += 1
            else:
                summary["失败"] += 1

    # 已签到账号不进入线程池；本轮只处理未完成当天签到的账号。
    process_indices = list(work_indices)
    batches = split_batches(process_indices, BATCH_SIZE)
    worker_count = max(1, min(int(threads), 20))
    for batch_number, batch in enumerate(batches, 1):
        ui_log(
            "SYSTEM",
            f"开始批次 {batch_number}/{len(batches)}，账号数 {len(batch)}",
            "SWITCH",
        )
        with ThreadPoolExecutor(max_workers=worker_count) as executor:
            pending: dict[Any, int] = {}
            next_position = 0
            started_count = 0

            def submit_next() -> None:
                nonlocal next_position, started_count
                index = batch[next_position]
                if started_count > 0:
                    delay = random.uniform(*THREAD_START_DELAY_RANGE)
                    ui_log("SYSTEM", f"下一个账号将在 {delay:.1f} 秒后异步启动", "INFO")
                    time.sleep(delay)
                future = executor.submit(
                    process_wallet,
                    index + 1,
                    rows[index],
                    private_key_column,
                    binding_by_index[index],
                    task_count,
                    lambda partial, row_index=index: save_checkpoint(row_index, partial),
                    len(rows),
                    index in skipped_set,
                )
                pending[future] = index
                next_position += 1
                started_count += 1

            while next_position < len(batch) and len(pending) < worker_count:
                submit_next()

            while pending:
                done, _ = wait(set(pending), return_when=FIRST_COMPLETED)
                for future in done:
                    index = pending.pop(future)
                    try:
                        result = future.result()
                    except Exception as exc:
                        result = build_exception_result(
                            rows[index], binding_by_index[index], exc
                        )
                    save_final(index, result)
                while next_position < len(batch) and len(pending) < worker_count:
                    submit_next()

        ui_log(
            "SYSTEM",
            f"批次 {batch_number}/{len(batches)} 已完成",
            "SUCCESS",
        )
        if batch_number < len(batches):
            rest = random.uniform(*BATCH_REST_RANGE)
            ui_log("SYSTEM", f"批次休息 {rest:.1f} 秒后继续下一批", "INFO")
            time.sleep(rest)

    atomic_write_csv(output_path, rows, fields)
    ui_print("=" * 72)
    ui_print(
        f"完成：{summary['完成']} | 部分完成：{summary['部分完成']} | "
        f"失败：{summary['失败']} | 今日已完成跳过：{summary['今日已完成跳过']}"
    )
    ui_print(format_chain_points_summary(rows, selected_indices))
    ui_print(f"结果已保存：{output_path}")
    ui_print("=" * 72)


def ask_int(prompt: str, default: int, minimum: int, maximum: int) -> int:
    raw = input(f"{prompt}（默认 {default}）：").strip()
    if not raw:
        return default
    value = int(raw)
    if not minimum <= value <= maximum:
        raise ValueError(f"请输入 {minimum}-{maximum} 之间的整数")
    return value


def main() -> int:
    print("SwarmBase 批量自动化：注册 + 签到 + Pioneer/Builder/OG NFT + Deploy Swarm / Build")
    print("链：opBNB Mainnet（chainId 204）\n")

    load_web3()

    script_dir = Path(__file__).resolve().parent
    if is_airdrop():
        data_dir = project_data_dir("SwarmBase")
        input_path = (
            specified_wallet_file()
            if wallet_mode() == "specified"
            else data_dir / "SwarmBase账号.csv"
        )
        output_path = data_dir / "SwarmBase批量结果.csv"
        ensure_airdrop_input(input_path)
        threads = env_int("AIRDROP_THREADS", "SWARMBASE_THREADS", default=DEFAULT_THREADS, minimum=1)
        task_count = env_int("SWARMBASE_TASK_COUNT", default=DEFAULT_TASK_COUNT, minimum=0)
        run_batch(
            input_path=input_path,
            output_path=output_path,
            account_range="",
            threads=threads,
            task_count=task_count,
        )
        result_rows, _ = read_csv_rows(output_path)
        accounts = []
        for row in result_rows:
            accounts.append(
                {
                    "address": row.get("地址", "") or row.get("Address", ""),
                    "status": row.get("最终状态", ""),
                    "message": row.get("积分", "") or row.get("错误", ""),
                    "error": row.get("错误", "") or row.get("失败原因", ""),
                }
            )
        emit_summary("SwarmBase", accounts)
        good = {"完成", "今日已完成跳过", "成功"}
        return 0 if accounts and all(str(item.get("status", "")).strip() in good for item in accounts) else 2

    default_input = script_dir / "SwarmBase账号.csv"
    input_text = input(f"CSV 路径（默认脚本目录下 {default_input.name}）：").strip()
    input_path = Path(input_text).expanduser() if input_text else default_input
    if not input_path.is_absolute():
        input_path = script_dir / input_path
    if not input_path.exists():
        raise FileNotFoundError(f"找不到 CSV：{input_path}")

    rows, _ = read_csv_rows(input_path)
    range_text = input(
        f"账号范围（共 {len(rows)} 个；示例 1-50,52；回车=全部）："
    ).strip()
    threads = ask_int("线程数", DEFAULT_THREADS, 1, 20)
    task_count = ask_int(
        "每个账号提交的 Deploy/Build 任务数", DEFAULT_TASK_COUNT, 0, 20
    )

    output_path = input_path.with_name("SwarmBase批量结果.csv")
    run_batch(
        input_path=input_path,
        output_path=output_path,
        account_range=range_text,
        threads=threads,
        task_count=task_count,
    )
    return 0


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\n用户终止运行。")
    except Exception as exc:
        ui_log("SYSTEM", f"运行失败：{clean_error(exc, 600)}", "ERROR")
    finally:
        if not is_airdrop():
            try:
                input("\n链上积分汇总已显示，按回车键退出...")
            except EOFError:
                pass
