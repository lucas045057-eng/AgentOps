# -*- coding: utf-8 -*-
"""
Sport-IX 批量签到领奖

功能：
1. 模式1：自动生成 Solana 钱包并保存到 sportix.txt；
2. 模式2：从 sportix.txt 读取私钥；
3. 通过 Privy SIWS 完成钱包签名登录；
4. 自动领取 follow_twitter、follow_telegram 两个一次性任务；
5. 自动完成 daily_checkin 每日签到，并将结果实时写回 TXT。

依赖：requests、cryptography
"""

from __future__ import annotations

import argparse
import base64
import csv
import json
import os
import random
import re
import sys
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Iterable

try:
    import requests
except ImportError as exc:  # pragma: no cover - only runs on missing dependency
    raise SystemExit("缺少 requests，请执行：pip install requests") from exc

try:
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
    from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat
except ImportError as exc:  # pragma: no cover - only runs on missing dependency
    raise SystemExit("缺少 cryptography，请执行：pip install cryptography") from exc


PRIVY_APP_ID = "cmpklsdm8003u0cjlbj4hilwp"
PRIVY_BASE_URL = "https://auth.privy.io"
SPORTIX_ORIGIN = "https://www.sport-ix.net"
SPORTIX_BACKEND = "https://backend-production-2afb9.up.railway.app"
PRIVY_CLIENT_VERSION = "react-auth:2.25.0"
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/150.0.0.0 Safari/537.36"
)

BASE58_ALPHABET = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"
BASE58_INDEX = {char: index for index, char in enumerate(BASE58_ALPHABET)}

RESULT_FIELDS = [
    "钱包地址",
    "Twitter任务",
    "Telegram任务",
    "每日签到",
    "签到日期(UTC+9)",
    "Pass余额",
    "CP余额",
    "运行状态",
    "最后运行时间",
    "错误信息",
]
TXT_FIELDS = ["私钥", *RESULT_FIELDS]
UTC9 = timezone(timedelta(hours=9))
COMPLETED_SOCIAL_STATUSES = {"已完成", "领取成功"}
PRIVATE_KEY_NAMES = {
    "私钥",
    "privatekey",
    "private_key",
    "private key",
    "secretkey",
    "secret_key",
    "solanaprivatekey",
    "solana_private_key",
}

PRINT_LOCK = threading.Lock()
WRITE_LOCK = threading.Lock()
AIRDROP_TRUE_VALUES = {"1", "true", "yes", "on"}


def running_under_airdrop() -> bool:
    """Detect the runner contract without changing normal standalone usage."""
    return bool(
        os.environ.get("AIRDROP_EXECUTION_ID")
        or os.environ.get("AIRDROP_NONINTERACTIVE", "").strip().lower() in AIRDROP_TRUE_VALUES
    )


class SportIXError(RuntimeError):
    """Sport-IX 或 Privy 协议错误。"""


class TaskResult:
    __slots__ = (
        "success",
        "wallet",
        "twitter_status",
        "telegram_status",
        "checkin_status",
        "checkin_date",
        "passes",
        "cp_balance",
        "error",
    )

    def __init__(
        self,
        *,
        success: bool = False,
        wallet: str = "",
        twitter_status: str = "未执行",
        telegram_status: str = "未执行",
        checkin_status: str = "未执行",
        checkin_date: str = "",
        passes: str = "",
        cp_balance: str = "",
        error: str = "",
    ) -> None:
        self.success = success
        self.wallet = wallet
        self.twitter_status = twitter_status
        self.telegram_status = telegram_status
        self.checkin_status = checkin_status
        self.checkin_date = checkin_date
        self.passes = passes
        self.cp_balance = cp_balance
        self.error = error


# =========================
# Base58 与 Solana 私钥工具
# =========================


def b58encode(data: bytes) -> str:
    """不依赖第三方 base58 包进行编码。"""
    if not isinstance(data, (bytes, bytearray)):
        raise TypeError("Base58 编码输入必须是 bytes")
    raw = bytes(data)
    if not raw:
        return ""

    zero_count = len(raw) - len(raw.lstrip(b"\x00"))
    number = int.from_bytes(raw, "big")
    encoded = ""
    while number:
        number, remainder = divmod(number, 58)
        encoded = BASE58_ALPHABET[remainder] + encoded
    return "1" * zero_count + encoded


def b58decode(text: str) -> bytes:
    """不依赖第三方 base58 包进行解码。"""
    value = text.strip()
    if not value:
        raise ValueError("Base58 内容为空")

    number = 0
    for char in value:
        if char not in BASE58_INDEX:
            raise ValueError(f"Base58 中包含非法字符：{char!r}")
        number = number * 58 + BASE58_INDEX[char]

    decoded = number.to_bytes((number.bit_length() + 7) // 8, "big") if number else b""
    zero_count = len(value) - len(value.lstrip("1"))
    return b"\x00" * zero_count + decoded


def decode_private_key(text: str) -> bytes:
    """
    解析 Solana 私钥并统一返回 32 字节 seed。

    支持：
    - 64 位十六进制编码的 32 字节 seed；
    - Base58 编码的 32/64 字节私钥；
    - JSON 数组格式的 32/64 个整数。
    """
    value = str(text or "").strip().strip("\ufeff")
    if not value:
        raise ValueError("私钥为空")

    raw: bytes
    if value.startswith("["):
        try:
            numbers = json.loads(value)
        except json.JSONDecodeError as exc:
            raise ValueError("JSON 数组私钥格式错误") from exc
        if not isinstance(numbers, list) or not all(isinstance(item, int) for item in numbers):
            raise ValueError("JSON 私钥必须是整数数组")
        if any(item < 0 or item > 255 for item in numbers):
            raise ValueError("JSON 私钥中的每个数必须在 0-255")
        raw = bytes(numbers)
    else:
        raw = bytes.fromhex(value) if re.fullmatch(r"[0-9a-fA-F]{64}", value) else b58decode(value)

    if len(raw) == 64:
        seed = raw[:32]
        expected_public = raw[32:]
        derived_public = (
            Ed25519PrivateKey.from_private_bytes(seed)
            .public_key()
            .public_bytes(Encoding.Raw, PublicFormat.Raw)
        )
        # 某些导出格式后 32 字节可能没有附带公钥，因此只在非全零时校验。
        if expected_public != bytes(32) and expected_public != derived_public:
            raise ValueError("64 字节私钥中的公钥部分与 seed 不匹配")
        return seed
    if len(raw) == 32:
        return raw
    raise ValueError(f"私钥解码后长度为 {len(raw)} 字节，只支持 32 或 64 字节")


def derive_solana_address(secret: bytes) -> str:
    seed = secret[:32]
    if len(seed) != 32:
        raise ValueError("Solana seed 必须是 32 字节")
    public_key = (
        Ed25519PrivateKey.from_private_bytes(seed)
        .public_key()
        .public_bytes(Encoding.Raw, PublicFormat.Raw)
    )
    return b58encode(public_key)


def generate_solana_wallet() -> tuple[str, str]:
    """生成一个 Solana 钱包，返回（地址，64 字节 keypair 的 Base58 私钥）。"""
    secret = os.urandom(32)
    address = derive_solana_address(secret)
    public_key = b58decode(address)
    return address, b58encode(secret + public_key)


def isoformat_milliseconds(value: datetime) -> str:
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    value = value.astimezone(timezone.utc)
    return value.isoformat(timespec="milliseconds").replace("+00:00", "Z")


def build_siws_message(address: str, nonce: str, issued_at: datetime) -> str:
    issued_text = isoformat_milliseconds(issued_at)
    return (
        "www.sport-ix.net wants you to sign in with your Solana account:\n"
        f"{address}\n\n"
        f"You are proving you own {address}.\n\n"
        "URI: https://www.sport-ix.net\n"
        "Version: 1\n"
        "Chain ID: mainnet\n"
        f"Nonce: {nonce}\n"
        f"Issued At: {issued_text}\n"
        "Resources:\n"
        "- https://privy.io"
    )


def sign_siws_message(secret: bytes, message: str) -> str:
    seed = secret[:32]
    if len(seed) != 32:
        raise ValueError("Solana seed 必须是 32 字节")
    signature = Ed25519PrivateKey.from_private_bytes(seed).sign(message.encode("utf-8"))
    return base64.b64encode(signature).decode("ascii")


# =========================
# HTTP 与 tRPC
# =========================


def compact_error_text(value: Any, max_length: int = 300) -> str:
    text = re.sub(r"\s+", " ", str(value or "")).strip()
    return text if len(text) <= max_length else text[: max_length - 3] + "..."


def request_json(
    session: Any,
    method: str,
    url: str,
    *,
    retries: int = 3,
    timeout: int = 25,
    **kwargs: Any,
) -> Any:
    """发送请求；网络错误、429 和 5xx 最多重试 retries 次。"""
    last_error: Exception | None = None
    for attempt in range(1, retries + 1):
        try:
            response = session.request(method, url, timeout=timeout, **kwargs)
            status = int(getattr(response, "status_code", 0))
            if status == 429 or status >= 500:
                raise SportIXError(
                    f"HTTP {status}: {compact_error_text(getattr(response, 'text', ''))}"
                )
            if not 200 <= status < 300:
                try:
                    payload = response.json()
                    details = compact_error_text(payload)
                except Exception:
                    details = compact_error_text(getattr(response, "text", ""))
                raise SportIXError(f"HTTP {status}: {details}")
            try:
                return response.json()
            except Exception as exc:
                raise SportIXError(
                    f"接口返回的不是 JSON：{compact_error_text(getattr(response, 'text', ''))}"
                ) from exc
        except (requests.RequestException, SportIXError) as exc:
            last_error = exc
            retryable = isinstance(exc, requests.RequestException) or "HTTP 429" in str(exc) or re.search(
                r"HTTP 5\d\d", str(exc)
            )
            if attempt >= retries or not retryable:
                break
            time.sleep(min(5.0, 0.8 * (2 ** (attempt - 1))) + random.uniform(0.05, 0.35))

    raise SportIXError(compact_error_text(last_error or "未知网络错误"))


def extract_trpc_data(payload: Any, index: int = 0) -> Any:
    """提取 tRPC batch 响应中指定位置的 result.data。"""
    if isinstance(payload, list):
        if index >= len(payload):
            raise SportIXError(f"tRPC 响应缺少第 {index} 项")
        item = payload[index]
    elif isinstance(payload, dict):
        item = payload
    else:
        raise SportIXError("tRPC 响应格式不是对象或数组")

    if not isinstance(item, dict):
        raise SportIXError("tRPC 响应项格式错误")
    if "error" in item:
        error = item.get("error") or {}
        if isinstance(error, dict):
            message = error.get("message") or error.get("data") or error
        else:
            message = error
        raise SportIXError(compact_error_text(message))

    result = item.get("result")
    if not isinstance(result, dict) or "data" not in result:
        raise SportIXError("tRPC 响应缺少 result.data")
    return result["data"]


class PrivyClient:
    def __init__(self, session: Any | None = None, ca_id: str | None = None) -> None:
        self.session = session or requests.Session()
        self.session.headers.update(
            {
                "accept": "application/json",
                "content-type": "application/json",
                "origin": SPORTIX_ORIGIN,
                "referer": SPORTIX_ORIGIN + "/",
                "user-agent": USER_AGENT,
                "privy-app-id": PRIVY_APP_ID,
                "privy-client": PRIVY_CLIENT_VERSION,
                "privy-ca-id": ca_id or str(uuid.uuid4()),
            }
        )

    def authenticate(
        self, secret: bytes, *, issued_at: datetime | None = None
    ) -> tuple[str, str]:
        address = derive_solana_address(secret)
        init_payload = request_json(
            self.session,
            "POST",
            f"{PRIVY_BASE_URL}/api/v1/siws/init",
            json={"address": address},
        )
        nonce = init_payload.get("nonce") if isinstance(init_payload, dict) else None
        if not nonce:
            raise SportIXError("Privy init 响应缺少 nonce")

        issued_at = issued_at or datetime.now(timezone.utc)
        message = build_siws_message(address, str(nonce), issued_at)
        signature = sign_siws_message(secret, message)
        auth_payload = request_json(
            self.session,
            "POST",
            f"{PRIVY_BASE_URL}/api/v1/siws/authenticate",
            json={
                "message": message,
                "signature": signature,
                "walletClientType": "okx_wallet",
                "connectorType": "solana_adapter",
                "mode": "login-or-sign-up",
                "message_type": "plain",
            },
        )
        if not isinstance(auth_payload, dict):
            raise SportIXError("Privy authenticate 响应格式错误")
        # HAR 中 Sport-IX 后端实际使用的是 token，而不是 privy_access_token。
        token = auth_payload.get("token") or auth_payload.get("privy_access_token")
        if not token:
            raise SportIXError("Privy authenticate 响应缺少 token")
        return str(token), address

    def close(self) -> None:
        close = getattr(self.session, "close", None)
        if callable(close):
            close()


class SportIXClient:
    def __init__(self, session: Any | None = None) -> None:
        self.session = session or requests.Session()
        self.session.headers.update(
            {
                "accept": "*/*",
                "content-type": "application/json",
                "origin": SPORTIX_ORIGIN,
                "referer": SPORTIX_ORIGIN + "/",
                "user-agent": USER_AGENT,
            }
        )
        self.session_id = ""

    def login(self, privy_token: str) -> str:
        # 网页 HAR 曾将 user.login 重复批处理两次，导致第二次触发 wallet 唯一键冲突。
        # 这里使用标准的单过程 tRPC batch，只登录一次。
        payload = request_json(
            self.session,
            "POST",
            f"{SPORTIX_BACKEND}/api/trpc/user.login?batch=1",
            json={"0": {"privyAccessToken": privy_token}},
        )
        data = extract_trpc_data(payload, 0)
        if not isinstance(data, dict) or not data.get("sessionId"):
            raise SportIXError("Sport-IX 登录响应缺少 sessionId")
        self.session_id = str(data["sessionId"])
        self.session.headers.update({"authorization": f"Bearer {self.session_id}"})
        return self.session_id

    def get_tasks_and_user(self) -> tuple[list[dict[str, Any]], dict[str, Any]]:
        payload = request_json(
            self.session,
            "GET",
            f"{SPORTIX_BACKEND}/api/trpc/tasks.list,user.me?batch=1&input=%7B%7D",
        )
        tasks_data = extract_trpc_data(payload, 0)
        user_data = extract_trpc_data(payload, 1)
        tasks = tasks_data.get("tasks") if isinstance(tasks_data, dict) else None
        if not isinstance(tasks, list):
            raise SportIXError("任务列表响应缺少 tasks")
        if not isinstance(user_data, dict):
            raise SportIXError("用户信息响应格式错误")
        return tasks, user_data

    def claim_onetime(self, task_key: str) -> dict[str, Any]:
        payload = request_json(
            self.session,
            "POST",
            f"{SPORTIX_BACKEND}/api/trpc/tasks.claimOnetime?batch=1",
            json={"0": {"taskKey": task_key}},
        )
        data = extract_trpc_data(payload, 0)
        if not isinstance(data, dict):
            raise SportIXError(f"{task_key} 领奖响应格式错误")
        return data

    def checkin(self) -> dict[str, Any]:
        payload = request_json(
            self.session,
            "POST",
            f"{SPORTIX_BACKEND}/api/trpc/tasks.checkin?batch=1",
            json={},
        )
        data = extract_trpc_data(payload, 0)
        if not isinstance(data, dict):
            raise SportIXError("签到响应格式错误")
        return data

    def close(self) -> None:
        close = getattr(self.session, "close", None)
        if callable(close):
            close()


# =========================
# 任务执行
# =========================


def _task_map(tasks: Iterable[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    result: dict[str, dict[str, Any]] = {}
    for task in tasks:
        if isinstance(task, dict) and task.get("key"):
            result[str(task["key"])] = task
    return result


def current_utc9_date() -> str:
    return datetime.now(UTC9).strftime("%Y-%m-%d")


def process_tasks(client: Any) -> TaskResult:
    initial_tasks, initial_user = client.get_tasks_and_user()
    initial = _task_map(initial_tasks)
    attempted: set[str] = set()
    action_errors: dict[str, str] = {}

    for task_key in ("follow_twitter", "follow_telegram"):
        task = initial.get(task_key)
        if task is not None and task.get("available") is True:
            attempted.add(task_key)
            try:
                client.claim_onetime(task_key)
            except Exception as exc:
                action_errors[task_key] = compact_error_text(exc)

    daily = initial.get("daily_checkin")
    if daily is not None and daily.get("available") is True:
        attempted.add("daily_checkin")
        try:
            client.checkin()
        except Exception as exc:
            action_errors["daily_checkin"] = compact_error_text(exc)

    final_tasks, final_user = client.get_tasks_and_user()
    final = _task_map(final_tasks)

    labels: dict[str, str] = {}
    unresolved_errors: list[str] = []
    for task_key in ("follow_twitter", "follow_telegram", "daily_checkin"):
        task = final.get(task_key)
        is_daily = task_key == "daily_checkin"
        if task is None:
            labels[task_key] = "任务未返回"
            unresolved_errors.append(f"{task_key}: 最终任务列表未返回该任务")
            continue

        if task.get("available") is False:
            if task_key in attempted and task_key not in action_errors:
                labels[task_key] = "签到成功" if is_daily else "领取成功"
            else:
                labels[task_key] = "今日已签到" if is_daily else "已完成"
            continue

        if task_key in action_errors:
            labels[task_key] = "签到失败" if is_daily else "领取失败"
            unresolved_errors.append(f"{task_key}: {action_errors[task_key]}")
        elif task_key in attempted:
            labels[task_key] = "验证未完成"
            unresolved_errors.append(f"{task_key}: 操作后任务仍可领取")
        else:
            labels[task_key] = "未执行"
            unresolved_errors.append(f"{task_key}: 任务仍可领取但未执行")

    user = final_user if isinstance(final_user, dict) else initial_user
    success = not unresolved_errors and all(
        key in final and final[key].get("available") is False
        for key in ("follow_twitter", "follow_telegram", "daily_checkin")
    )
    checkin_status = labels.get("daily_checkin", "任务未返回")
    checkin_date = current_utc9_date() if checkin_status in {"签到成功", "今日已签到"} else ""
    return TaskResult(
        success=success,
        twitter_status=labels.get("follow_twitter", "任务未返回"),
        telegram_status=labels.get("follow_telegram", "任务未返回"),
        checkin_status=checkin_status,
        checkin_date=checkin_date,
        passes=str(user.get("passesRemaining", "")) if isinstance(user, dict) else "",
        cp_balance=str(user.get("cpBalance", "")) if isinstance(user, dict) else "",
        error=" | ".join(unresolved_errors),
    )


def run_account(
    private_key_text: str,
    *,
    privy_client_factory: Callable[[], PrivyClient] = PrivyClient,
    sportix_client_factory: Callable[[], SportIXClient] = SportIXClient,
    start_at: float = 0.0,
) -> TaskResult:
    if start_at:
        time.sleep(max(0.0, start_at - time.monotonic()))
    privy: PrivyClient | None = None
    sportix: SportIXClient | None = None
    wallet = ""
    try:
        secret = decode_private_key(private_key_text)
        wallet = derive_solana_address(secret)
        privy = privy_client_factory()
        token, authenticated_wallet = privy.authenticate(secret)
        if authenticated_wallet != wallet:
            raise SportIXError("签名登录返回的钱包地址不一致")

        sportix = sportix_client_factory()
        sportix.login(token)
        result = process_tasks(sportix)
        result.wallet = wallet
        return result
    except Exception as exc:
        return TaskResult(
            success=False,
            wallet=wallet,
            error=compact_error_text(exc),
        )
    finally:
        if privy is not None:
            privy.close()
        if sportix is not None:
            sportix.close()


# =========================
# TXT 与账号范围
# =========================


def _normalize_header(value: str) -> str:
    return re.sub(r"[\s_-]+", "", str(value or "").strip().lower().strip("\ufeff"))


def _looks_like_header(first_row: list[str]) -> bool:
    if not first_row:
        return False
    normalized = {_normalize_header(value) for value in first_row}
    private_names = {_normalize_header(value) for value in PRIVATE_KEY_NAMES}
    result_names = {_normalize_header(value) for value in RESULT_FIELDS}
    return bool(normalized & (private_names | result_names))


def _reassemble_json_private_key_row(raw: list[str]) -> list[str]:
    """
    修复未用双引号包裹的 JSON 数组私钥。

    CSV 会把 `[1,2,...]` 按逗号拆成多列；当首列以 `[` 开头时，
    找到以 `]` 结尾的片段，确认它确实是 32/64 字节整数数组后再拼回。
    """
    if len(raw) <= 1 or not str(raw[0]).lstrip().startswith("["):
        return raw

    for end_index, cell in enumerate(raw):
        if not str(cell).rstrip().endswith("]"):
            continue
        candidate = ",".join(raw[: end_index + 1]).strip()
        try:
            values = json.loads(candidate)
        except json.JSONDecodeError:
            continue
        if (
            isinstance(values, list)
            and len(values) in {32, 64}
            and all(isinstance(item, int) and 0 <= item <= 255 for item in values)
        ):
            return [candidate] + raw[end_index + 1 :]
    return raw


def default_wallet_path() -> Path:
    """Return a persistent wallet file for AirDrop, or the legacy local path."""
    explicit = os.environ.get("SPORTIX_WALLET_FILE") or os.environ.get("AIRDROP_WALLET_FILE")
    if explicit:
        return Path(explicit).expanduser()

    project_data_dir = os.environ.get("AIRDROP_PROJECT_DATA_DIR", "").strip()
    if project_data_dir:
        return Path(project_data_dir).expanduser() / "wallets" / "sportix.txt"

    # AirDrop mounts /app/logs (or the local logs directory) persistently.
    # The execution directory itself is disposable, so keep the wallet file
    # two levels above it: <log-root>/executions/<execution-id>.
    artifact_dir = os.environ.get("AIRDROP_ARTIFACT_DIR", "").strip()
    if artifact_dir:
        artifact_path = Path(artifact_dir).expanduser().resolve()
        return artifact_path.parent.parent / "wallets" / "SportIX" / "sportix.txt"

    return Path(__file__).resolve().parent / "sportix.txt"


def _positive_env_int(*names: str, default: int = 1) -> int:
    raw = next((os.environ.get(name, "").strip() for name in names if os.environ.get(name, "").strip()), "")
    value = int(raw) if raw else default
    if value < 1:
        raise ValueError(f"{names[0]} 必须是大于等于 1 的整数")
    return value


def _optional_nonnegative_env_int(*names: str) -> int:
    raw = next((os.environ.get(name, "").strip() for name in names if os.environ.get(name, "").strip()), "")
    if not raw:
        return 0
    value = int(raw)
    if value < 0:
        raise ValueError(f"{names[0]} 必须是大于等于 0 的整数")
    return value


def ensure_airdrop_wallet_file(path: Path) -> tuple[int, int]:
    """Create or extend the persistent wallet file, then reuse it for runs."""
    if not running_under_airdrop():
        return 0, 0

    auto_provision = os.environ.get("AIRDROP_AUTO_PROVISION", "1").strip().lower() in AIRDROP_TRUE_VALUES
    created = 0
    appended = 0
    if not path.exists() or path.stat().st_size == 0:
        if not auto_provision:
            raise FileNotFoundError(f"AirDrop 钱包文件不存在：{path}")
        created = _positive_env_int("AIRDROP_WALLET_COUNT", "SPORTIX_WALLET_COUNT", default=1)
        generate_wallet_file(path, created)

    # Optional expansion is explicit. Existing wallets are never regenerated.
    appended = _optional_nonnegative_env_int("AIRDROP_APPEND_WALLET_COUNT", "SPORTIX_APPEND_WALLET_COUNT")
    if appended:
        generate_wallet_file(path, appended)
    return created, appended


def load_wallet_txt(path: Path) -> tuple[list[dict[str, str]], list[str], str]:
    if not path.exists():
        raise FileNotFoundError(f"找不到钱包文件：{path}")

    with path.open("r", encoding="utf-8-sig", newline="") as file:
        raw_rows = [row for row in csv.reader(file, delimiter="\t") if any(str(cell).strip() for cell in row)]

    raw_rows = [_reassemble_json_private_key_row(row) for row in raw_rows]

    if not raw_rows:
        raise ValueError("TXT 文件中没有钱包数据")

    has_header = _looks_like_header(raw_rows[0])
    if has_header:
        fieldnames = [str(value).strip().strip("\ufeff") or f"原列{i + 1}" for i, value in enumerate(raw_rows[0])]
        data_rows = raw_rows[1:]
    else:
        max_columns = max(len(row) for row in raw_rows)
        fieldnames = ["私钥"] + [f"原列{i}" for i in range(2, max_columns + 1)]
        data_rows = raw_rows

    # 防止重复表头导致 DictWriter 覆盖。
    seen: dict[str, int] = {}
    unique_fieldnames: list[str] = []
    for name in fieldnames:
        count = seen.get(name, 0) + 1
        seen[name] = count
        unique_fieldnames.append(name if count == 1 else f"{name}_{count}")
    fieldnames = unique_fieldnames

    normalized_private_names = {_normalize_header(value) for value in PRIVATE_KEY_NAMES}
    private_key_field = next(
        (name for name in fieldnames if _normalize_header(name) in normalized_private_names),
        fieldnames[0],
    )

    for result_field in RESULT_FIELDS:
        if result_field not in fieldnames:
            fieldnames.append(result_field)

    rows: list[dict[str, str]] = []
    for raw in data_rows:
        row = {name: "" for name in fieldnames}
        for index, value in enumerate(raw[: len(unique_fieldnames)]):
            row[unique_fieldnames[index]] = str(value).strip()
        if row.get(private_key_field, "").strip():
            rows.append(row)

    if not rows:
        raise ValueError("TXT 中没有有效私钥")
    return rows, fieldnames, private_key_field


def atomic_save_txt(path: Path, rows: list[dict[str, str]], fieldnames: list[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp_path = path.with_name(path.name + ".tmp")
    with temp_path.open("w", encoding="utf-8-sig", newline="") as file:
        writer = csv.DictWriter(file, fieldnames=fieldnames, delimiter="\t", extrasaction="ignore")
        writer.writeheader()
        for row in rows:
            writer.writerow({name: row.get(name, "") for name in fieldnames})
        file.flush()
        os.fsync(file.fileno())
    os.replace(temp_path, path)


def generate_wallet_file(path: Path, count: int) -> int:
    """生成 count 个钱包；已有 TXT 文件时追加，不覆盖原有结果。"""
    if count < 1:
        raise ValueError("生成数量必须大于等于 1")

    if path.exists() and path.stat().st_size:
        rows, fieldnames, private_key_field = load_wallet_txt(path)
    else:
        rows = []
        fieldnames = list(TXT_FIELDS)
        private_key_field = "私钥"

    for _ in range(count):
        address, private_key = generate_solana_wallet()
        row = {name: "" for name in fieldnames}
        row[private_key_field] = private_key
        row["钱包地址"] = address
        rows.append(row)

    atomic_save_txt(path, rows, fieldnames)
    return len(rows)


def parse_account_selection(text: str, total: int) -> list[int]:
    if total <= 0:
        return []
    value = str(text or "").strip().replace("，", ",")
    if not value or value.lower() in {"all", "全部", "*"}:
        return list(range(total))

    selected: set[int] = set()
    for part in value.split(","):
        part = part.strip()
        if not part:
            continue
        if "-" in part:
            pieces = part.split("-", 1)
            if len(pieces) != 2 or not all(piece.strip().isdigit() for piece in pieces):
                raise ValueError(f"账号范围格式错误：{part}")
            start, end = int(pieces[0]), int(pieces[1])
            if start > end:
                start, end = end, start
            numbers = range(start, end + 1)
        else:
            if not part.isdigit():
                raise ValueError(f"账号编号格式错误：{part}")
            numbers = [int(part)]

        for number in numbers:
            if number < 1 or number > total:
                raise ValueError(f"账号编号 {number} 超出范围 1-{total}")
            selected.add(number - 1)

    if not selected:
        raise ValueError("没有选择任何账号")
    return sorted(selected)


def should_skip_today(row: dict[str, str]) -> bool:
    return (
        row.get("Twitter任务", "") in COMPLETED_SOCIAL_STATUSES
        and row.get("Telegram任务", "") in COMPLETED_SOCIAL_STATUSES
        and row.get("签到日期(UTC+9)", "") == current_utc9_date()
    )


def update_row_from_result(row: dict[str, str], result: TaskResult) -> None:
    row["钱包地址"] = result.wallet
    row["Twitter任务"] = result.twitter_status
    row["Telegram任务"] = result.telegram_status
    row["每日签到"] = result.checkin_status
    row["签到日期(UTC+9)"] = result.checkin_date or row.get("签到日期(UTC+9)", "")
    row["Pass余额"] = result.passes
    row["CP余额"] = result.cp_balance
    row["运行状态"] = "成功" if result.success else "失败"
    row["最后运行时间"] = datetime.now().astimezone().strftime("%Y-%m-%d %H:%M:%S")
    row["错误信息"] = result.error


def mask_wallet(wallet: str) -> str:
    if len(wallet) <= 12:
        return wallet or "未知钱包"
    return f"{wallet[:6]}...{wallet[-4:]}"


def safe_print(message: str) -> None:
    with PRINT_LOCK:
        print(message, flush=True)


def account_record(index: int, row: dict[str, str], result: TaskResult) -> dict[str, Any]:
    """Convert one account result to AirDrop' legacy account-result shape."""
    address = result.wallet or row.get("钱包地址", "")
    return {
        "address": address,
        "name": f"账号 {index + 1}",
        "status": "success" if result.success else "failed",
        "message": result.error or result.checkin_status,
        "points": 0,
        "error": result.error,
        "wallet_index": index + 1,
        "twitter_status": result.twitter_status,
        "telegram_status": result.telegram_status,
        "checkin_status": result.checkin_status,
        "checkin_date": result.checkin_date,
        "passes": result.passes,
        "cp_balance": result.cp_balance,
    }


def emit_airdrop_event(event: str, **data: Any) -> None:
    if running_under_airdrop():
        safe_print(json.dumps({"event": event, **data}, ensure_ascii=False, default=str))


def emit_airdrop_summary(accounts: list[dict[str, Any]]) -> None:
    """Emit one final structured result so AirDrop can persist per-wallet data."""
    if not running_under_airdrop():
        return
    success_count = sum(1 for item in accounts if item.get("status") == "success")
    already_done_count = sum(1 for item in accounts if item.get("status") == "already_done")
    failed_count = sum(1 for item in accounts if item.get("status") == "failed")
    if failed_count and success_count:
        status = "partial_success"
    elif failed_count:
        status = "failed"
    elif already_done_count and not success_count:
        status = "already_done"
    else:
        status = "success"
    safe_print(
        json.dumps(
            {
                "status": status,
                "project": "SportIX",
                "wallet_total": len(accounts),
                "success": success_count,
                "already_done": already_done_count,
                "failed": failed_count,
                "retryable": failed_count > 0,
                "accounts": accounts,
                "message": f"SportIX 本次处理 {len(accounts)} 个钱包，成功 {success_count} 个，失败 {failed_count} 个",
            },
            ensure_ascii=False,
            default=str,
        )
    )


def batch_rest_delay(completed: int, total: int) -> float:
    if completed > 0 and completed % 100 == 0 and completed < total:
        return random.uniform(45.0, 60.0)
    return 0.0


# =========================
# 命令行入口
# =========================


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Sport-IX 批量签到与任务领奖")
    parser.add_argument("--模式", choices=("1", "2"), help="1=生成 Solana 钱包，2=社交任务+每日签到")
    parser.add_argument("--数量", type=int, help="模式1生成的钱包数量，默认 1")
    parser.add_argument("--文件", default=None, help="钱包 TXT 文件路径；AirDrop 未指定时使用持久化项目目录")
    parser.add_argument("--线程", type=int, default=5, help="线程数，默认 5")
    parser.add_argument("--范围", default=None, help="账号范围，例如 1-50,55")
    parser.add_argument("--非交互", action="store_true", help="不询问参数，也不等待回车退出")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_arg_parser().parse_args(argv)
    airdrop_mode = running_under_airdrop()
    script_dir = Path(__file__).resolve().parent
    wallet_path = Path(args.文件).expanduser() if args.文件 else default_wallet_path()
    if not wallet_path.is_absolute():
        wallet_path = script_dir / wallet_path

    mode = args.模式
    if mode is None:
        if airdrop_mode:
            # AirDrop always performs the project operation. Wallet creation
            # is automatic only when the persistent wallet file is missing.
            mode = "2"
        elif args.非交互:
            safe_print("❌ 非交互模式必须指定 --模式 1 或 --模式 2")
            return 1
        else:
            mode = input("请选择模式：\n1. 自动生成 Solana 钱包\n2. 完成社交任务和每日签到\n请输入 1/2：").strip()

    if mode == "1":
        try:
            if args.数量 is not None:
                count = args.数量
            elif args.非交互:
                count = 1
            else:
                raw_count = input("生成钱包数量（回车=1）：").strip()
                count = int(raw_count) if raw_count else 1
            total = generate_wallet_file(wallet_path, count)
        except Exception as exc:
            safe_print(f"❌ 钱包生成失败：{compact_error_text(exc)}")
            return 1
        safe_print(f"✅ 模式1完成：新增 {count} 个钱包，共 {total} 个")
        safe_print(f"钱包已保存到：{wallet_path}")
        return 0

    if mode != "2":
        safe_print("❌ 模式只能是 1 或 2")
        return 1

    try:
        if airdrop_mode:
            created, appended = ensure_airdrop_wallet_file(wallet_path)
            if created or appended:
                safe_print(f"钱包文件已准备：初次生成 {created} 个，追加 {appended} 个，路径：{wallet_path}")
        rows, fieldnames, private_key_field = load_wallet_txt(wallet_path)
    except Exception as exc:
        safe_print(f"❌ {compact_error_text(exc)}")
        if not args.非交互 and sys.stdin.isatty():
            input("\n按回车键退出...")
        return 1

    total = len(rows)
    try:
        if args.范围 is not None:
            selection_text = args.范围
        elif args.非交互 or airdrop_mode:
            selection_text = ""
        else:
            selection_text = input(f"账号范围（共 {total} 个，回车=全部，例如 1-50,55）：").strip()
        selected_indices = parse_account_selection(selection_text, total)
        skipped_indices = [index for index in selected_indices if should_skip_today(rows[index])]
        skipped_set = set(skipped_indices)
        selected_indices = [index for index in selected_indices if index not in skipped_set]
        account_records: list[dict[str, Any]] = [
            {
                "address": rows[index].get("钱包地址", ""),
                "name": f"账号 {index + 1}",
                "status": "already_done",
                "message": "今日已完成",
                "points": 0,
                "error": "",
                "wallet_index": index + 1,
            }
            for index in skipped_indices
        ]
        if skipped_indices:
            safe_print(f"⏭️ 今日 UTC+9 {current_utc9_date()} 已完成，跳过 {len(skipped_indices)} 个账号")
        if not selected_indices:
            safe_print("✅ 所选账号今日任务均已完成，无需重复执行")
            emit_airdrop_summary(account_records)
            return 0

        if args.线程 is not None:
            threads = args.线程
        elif args.非交互:
            threads = min(5, len(selected_indices))
        else:
            raw_threads = input("线程数（回车=5）：").strip()
            threads = int(raw_threads) if raw_threads else 5
        if threads < 1:
            raise ValueError("线程数必须大于等于 1")
        threads = min(threads, len(selected_indices))
    except Exception as exc:
        safe_print(f"❌ 参数错误：{compact_error_text(exc)}")
        if not args.非交互 and sys.stdin.isatty():
            input("\n按回车键退出...")
        return 1

    safe_print("\n" + "=" * 68)
    safe_print("Sport-IX 批量签到领奖")
    safe_print(f"文件：{wallet_path}")
    safe_print(f"账号：{len(selected_indices)}/{total} | 今日跳过：{len(skipped_indices)} | 线程：{threads} | 网络：本地直连")
    safe_print("=" * 68)

    success_count = 0
    failure_count = 0

    with ThreadPoolExecutor(max_workers=threads) as executor:
        completed = 0
        for batch_start in range(0, len(selected_indices), 100):
            batch_indices = selected_indices[batch_start : batch_start + 100]
            futures: dict[Any, int] = {}
            next_start_at = time.monotonic()
            for position, index in enumerate(batch_indices):
                if position:
                    next_start_at += random.uniform(3.0, 5.0)
                future = executor.submit(
                    run_account,
                    rows[index].get(private_key_field, ""),
                    start_at=next_start_at,
                )
                futures[future] = index

            for future in as_completed(futures):
                index = futures[future]
                completed += 1
                try:
                    result = future.result()
                except Exception as exc:  # run_account normally catches account errors
                    result = TaskResult(success=False, error=compact_error_text(exc))

                with WRITE_LOCK:
                    update_row_from_result(rows[index], result)
                    try:
                        atomic_save_txt(wallet_path, rows, fieldnames)
                        write_error = ""
                    except Exception as exc:
                        write_error = compact_error_text(exc)

                record = account_record(index, rows[index], result)
                if write_error:
                    record["status"] = "failed"
                    record["error"] = f"TXT写入失败: {write_error}"
                account_records.append(record)
                emit_airdrop_event("wallet_result", **record)

                prefix = f"[{completed:>3}/{len(selected_indices)}] [账号 {index + 1:>4}]"
                wallet_text = mask_wallet(result.wallet)
                if result.success and not write_error:
                    success_count += 1
                    safe_print(
                        f"✅ {prefix} {wallet_text} | Twitter:{result.twitter_status} | "
                        f"TG:{result.telegram_status} | 签到:{result.checkin_status} | "
                        f"Pass:{result.passes} | CP:{result.cp_balance}"
                    )
                else:
                    failure_count += 1
                    errors = " | ".join(part for part in [result.error, write_error and f"TXT写入失败: {write_error}"] if part)
                    safe_print(f"❌ {prefix} {wallet_text} | {errors or '任务未全部完成'}")

            delay = batch_rest_delay(completed, len(selected_indices))
            if delay:
                safe_print(f"⏸️ 已处理 {completed} 个账号，休息 {delay:.1f} 秒")
                time.sleep(delay)

    safe_print("\n" + "=" * 68)
    safe_print(f"执行完成：成功 {success_count} | 失败 {failure_count} | 今日跳过 {len(skipped_indices)} | 本次处理 {len(selected_indices)}")
    safe_print(f"结果已写回：{wallet_path}")
    safe_print("=" * 68)

    if not args.非交互 and sys.stdin.isatty():
        input("\n按回车键退出...")
    emit_airdrop_summary(account_records)
    return 0 if failure_count == 0 else 2


if __name__ == "__main__":
    raise SystemExit(main())
