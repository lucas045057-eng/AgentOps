#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
DGrid 钱包登录与每日任务自动化

功能：
1. 从 wallets.txt / wallets.csv / wallets 读取私钥。
2. 多线程完成钱包签名登录。
3. 检查手动社交绑定和首次链上激活状态。
4. 在 BSC 主网完成每日 checkIn()。
5. 相同 question_id 跨账号共享答案；首个遇到该题的账号探路，其余账号跟随。
6. 保存成功、失败、待手动任务、CSV 汇总和每日答案缓存。

依赖：
    pip install requests eth-account web3
"""

from __future__ import annotations

import argparse
import csv
import json
import os
import random
import re
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Iterable, Optional

from _airdrop_compat import (
    emit_summary,
    env_int,
    is_airdrop,
    project_data_dir,
    specified_wallet_file,
    wallet_mode,
)

try:
    import requests
    from eth_account import Account
    from eth_account.messages import encode_defunct
    from web3 import HTTPProvider, Web3
except ImportError as exc:  # pragma: no cover - 仅在用户环境缺依赖时触发
    print(f"缺少依赖：{exc}")
    print("请执行：pip install requests eth-account web3")
    raise SystemExit(1)


API_BASE = "https://api2.dgrid.ai/api/v1"
WEB_ORIGIN = "https://dgrid.ai"
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/150.0.0.0 Safari/537.36"
)

BSC_CHAIN_ID = 56
CHECKIN_CONTRACT = "0x73eeC8dC8BBeB75033E04f67B186B1589082e0D0"
CHECKIN_CALLDATA = "0x183ff085"
BSC_RPCS = [
    "https://bsc-dataseed.bnbchain.org",
    "https://bsc-dataseed-public.bnbchain.org",
    "https://bsc-dataseed.defibit.io",
    "https://bsc-dataseed.nariox.org",
]

DEFAULT_THREADS = 5
HTTP_TIMEOUT = 30
HTTP_RETRIES = 3
MAX_DAILY_MISSIONS = 5
ACCOUNT_JITTER_MIN = 0.4
ACCOUNT_JITTER_MAX = 1.2
OPTION_JITTER_MIN = 0.25
OPTION_JITTER_MAX = 0.75
MISSION_JITTER_MIN = 0.3
MISSION_JITTER_MAX = 0.9
MISSIONS_RETRY_LIMIT = 6
MISSIONS_RETRY_MIN = 3.0
MISSIONS_RETRY_MAX = 6.0
RESULT_DIR_NAME = "DGrid结果"

PRIVATE_KEY_RE = re.compile(r"^(?:0x)?[0-9a-fA-F]{64}$")
CSV_KEY_HEADERS = {
    "privatekey",
    "private_key",
    "private key",
    "私钥",
    "key",
}

PRINT_LOCK = threading.Lock()


def airdrop_wallet_file() -> Path:
    return project_data_dir("DGrid") / "wallets.csv"


def ensure_airdrop_wallet_file(path: Path) -> None:
    if not is_airdrop():
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    if not path.exists() or path.stat().st_size == 0:
        count = env_int("AIRDROP_WALLET_COUNT", "DGRID_WALLET_COUNT", default=1, minimum=1)
        with path.open("w", encoding="utf-8-sig", newline="") as file:
            writer = csv.writer(file)
            writer.writerow(["PrivateKey"])
            for _ in range(count):
                writer.writerow([Account.create().key.hex()])

    append_count = env_int(
        "AIRDROP_APPEND_WALLET_COUNT",
        "DGRID_APPEND_WALLET_COUNT",
        default=0,
        minimum=0,
    )
    if append_count:
        with path.open("a", encoding="utf-8", newline="") as file:
            writer = csv.writer(file)
            for _ in range(append_count):
                writer.writerow([Account.create().key.hex()])


class DGridError(RuntimeError):
    pass


class DGridApiError(DGridError):
    pass


class ProbeResultUnavailable(DGridError):
    """POST 可能已成功，但没有拿到票数响应。"""


class WalletRecord:
    def __init__(self, index: int, private_key: str, address: str):
        self.index = index
        self.private_key = private_key
        self.address = address

    def __repr__(self) -> str:
        return f"WalletRecord(index={self.index}, address={self.address!r})"


class AnswerChoice:
    def __init__(
        self,
        answer_id: str,
        percent: float,
        count: int,
        total: int,
        updated_at: Optional[str] = None,
        day: Optional[str] = None,
        answer_ids: Optional[Iterable[str]] = None,
        answer_index: Optional[int] = None,
    ):
        self.answer_id = answer_id
        self.percent = float(percent)
        self.count = int(count)
        self.total = int(total)
        self.updated_at = updated_at or datetime.now().isoformat(timespec="seconds")
        self.day = str(day or "")
        self.answer_ids = self._normalize_answer_ids(answer_ids)
        self.answer_index = (
            int(answer_index) if answer_index is not None else None
        )

    @staticmethod
    def _normalize_answer_ids(answer_ids: Optional[Iterable[str]]) -> tuple[str, ...]:
        normalized: list[str] = []
        seen: set[str] = set()
        for item in answer_ids or []:
            value = str(item)
            if value and value not in seen:
                normalized.append(value)
                seen.add(value)
        return tuple(normalized)

    def is_valid_for(self, day: str, answer_ids: Iterable[str]) -> bool:
        valid_ids = self._normalize_answer_ids(answer_ids)
        return bool(
            self.day
            and self.day == str(day)
            and self.answer_ids == valid_ids
            and self.answer_id in valid_ids
            and self.answer_index is not None
            and 0 <= self.answer_index < len(valid_ids)
            and valid_ids[self.answer_index] == self.answer_id
        )

    def mapped_to(self, answer_ids: Iterable[str]) -> Optional["AnswerChoice"]:
        valid_ids = self._normalize_answer_ids(answer_ids)
        if self.answer_ids != valid_ids or self.answer_id not in valid_ids:
            return None
        answer_index = valid_ids.index(self.answer_id)
        return AnswerChoice(
            self.answer_id,
            self.percent,
            self.count,
            self.total,
            self.updated_at,
            self.day,
            valid_ids,
            answer_index,
        )

    def margin(self) -> float:
        return abs((2.0 * self.percent) - 1.0)

    def to_dict(self) -> dict[str, Any]:
        return {
            "answer_id": self.answer_id,
            "percent": self.percent,
            "count": self.count,
            "total": self.total,
            "updated_at": self.updated_at,
            "day": self.day,
            "answer_ids": list(self.answer_ids),
            "answer_index": self.answer_index,
        }

class ResolveResult:
    def __init__(
        self,
        choice: AnswerChoice,
        did_probe: bool,
        probe_submission: Optional[dict[str, Any]] = None,
    ):
        self.choice = choice
        self.did_probe = did_probe
        self.probe_submission = probe_submission


class PreparedAccount:
    def __init__(self, wallet: WalletRecord, client: "DGridClient", ticket: dict[str, Any]):
        self.wallet = wallet
        self.client = client
        self.ticket = ticket
        self.missions_data: Optional[dict[str, Any]] = None
        self.checkin_status = "未处理"
        self.tx_hash = ""


class AccountResult:
    def __init__(self, wallet: WalletRecord):
        self.index = wallet.index
        self.address = wallet.address
        self.status = "处理中"
        self.login_status = "未登录"
        self.checkin_status = "未处理"
        self.tx_hash = ""
        self.mission_status = "未处理"
        self.completed_count = 0
        self.points = 0
        self.today_points = 0
        self.total_points = 0
        self.detail = ""

    def as_csv_row(self) -> dict[str, Any]:
        return {
            "序号": self.index,
            "钱包地址": self.address,
            "状态": self.status,
            "登录": self.login_status,
            "签到": self.checkin_status,
            "签到交易": self.tx_hash,
            "答题": self.mission_status,
            "已完成题数": self.completed_count,
            "本次答题积分": self.points,
            "今日总积分": self.today_points,
            "累计积分": self.total_points,
            "详情": self.detail,
        }


def safe_print(message: str) -> None:
    with PRINT_LOCK:
        print(message, flush=True)


def short_address(address: str) -> str:
    return f"{address[:8]}...{address[-6:]}"


def normalize_private_key(value: str) -> str:
    raw = str(value).replace("\ufeff", "").strip().strip('"').strip("'")
    if not PRIVATE_KEY_RE.fullmatch(raw):
        raise ValueError("私钥必须是 64 位十六进制字符")
    if not raw.startswith("0x"):
        raw = "0x" + raw
    # Account.from_key 同时完成椭圆曲线范围校验
    Account.from_key(raw)
    return raw


def _extract_keys_from_csv(path: Path) -> list[str]:
    with path.open("r", encoding="utf-8-sig", newline="") as f:
        rows = list(csv.reader(f))
    if not rows:
        return []

    first = [str(item).strip().lower() for item in rows[0]]
    key_col: Optional[int] = None
    for idx, header in enumerate(first):
        if header in CSV_KEY_HEADERS:
            key_col = idx
            break

    start = 1 if key_col is not None else 0
    if key_col is None:
        key_col = 0

    values: list[str] = []
    for row in rows[start:]:
        if key_col < len(row) and str(row[key_col]).strip():
            values.append(str(row[key_col]).strip())
    return values


def _extract_keys_from_text(path: Path) -> list[str]:
    values: list[str] = []
    for line in path.read_text(encoding="utf-8-sig").splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        # 允许一行后面带备注，优先取逗号、制表符或空格前的第一段
        token = re.split(r"[,\t\s]+", stripped, maxsplit=1)[0]
        values.append(token)
    return values


def find_wallet_file(base_dir: Optional[Path] = None) -> Path:
    if is_airdrop():
        if wallet_mode() == "specified":
            path = specified_wallet_file()
            if not path.is_file():
                raise FileNotFoundError(f"指定钱包文件不存在：{path}")
            return path
        path = airdrop_wallet_file()
        ensure_airdrop_wallet_file(path)
        return path
    base = base_dir or Path.cwd()
    candidates = [base / "wallets.txt", base / "wallets.csv", base / "wallets"]
    for path in candidates:
        if path.is_file():
            return path
    raise FileNotFoundError("未找到 wallets.txt、wallets.csv 或 wallets 文件")


def load_wallets(path: Optional[os.PathLike[str] | str] = None) -> list[WalletRecord]:
    wallet_path = Path(path) if path is not None else find_wallet_file()
    if not wallet_path.is_file():
        raise FileNotFoundError(f"钱包文件不存在：{wallet_path}")

    raw_values = (
        _extract_keys_from_csv(wallet_path)
        if wallet_path.suffix.lower() == ".csv"
        else _extract_keys_from_text(wallet_path)
    )

    wallets: list[WalletRecord] = []
    seen_addresses: set[str] = set()
    errors: list[str] = []

    for source_index, raw in enumerate(raw_values, start=1):
        try:
            private_key = normalize_private_key(raw)
            address = Account.from_key(private_key).address
        except Exception as exc:
            errors.append(f"第 {source_index} 行：{exc}")
            continue

        lower = address.lower()
        if lower in seen_addresses:
            continue
        seen_addresses.add(lower)
        wallets.append(WalletRecord(len(wallets) + 1, private_key, address))

    if not wallets:
        detail = "；".join(errors[:5])
        raise ValueError(f"钱包文件中没有有效私钥。{detail}")

    if errors:
        safe_print(f"⚠️ 已忽略 {len(errors)} 条无效私钥记录")
    return wallets


def _submission_data(payload: dict[str, Any]) -> dict[str, Any]:
    if "chosen" in payload and "other" in payload:
        return payload
    data = payload.get("data")
    if isinstance(data, dict) and "chosen" in data and "other" in data:
        return data
    raise ValueError("提交响应缺少 chosen/other 票数信息")


def choose_majority_answer(payload: dict[str, Any]) -> tuple[str, float, int]:
    data = _submission_data(payload)
    chosen = data.get("chosen") or {}
    other = data.get("other") or {}

    chosen_count = int(chosen.get("count") or 0)
    other_count = int(other.get("count") or 0)
    chosen_percent = float(chosen.get("percent") or 0.0)
    other_percent = float(other.get("percent") or 0.0)

    if other_count > chosen_count:
        winner = other
        winner_count = other_count
        winner_percent = other_percent
    elif chosen_count > other_count:
        winner = chosen
        winner_count = chosen_count
        winner_percent = chosen_percent
    elif other_percent > chosen_percent:
        winner = other
        winner_count = other_count
        winner_percent = other_percent
    else:
        # 平票时保留本次选择，后续账号会继续强化这一侧
        winner = chosen
        winner_count = chosen_count
        winner_percent = chosen_percent

    answer_id = str(winner.get("answers_id") or "")
    if not answer_id:
        raise ValueError("票数响应缺少 answers_id")
    return answer_id, winner_percent, winner_count


def answer_choice_from_submission(payload: dict[str, Any]) -> AnswerChoice:
    data = _submission_data(payload)
    answer_id, percent, count = choose_majority_answer(data)
    chosen = data.get("chosen") or {}
    other = data.get("other") or {}
    total = int(
        chosen.get("total_choice_count")
        or other.get("total_choice_count")
        or chosen.get("count", 0) + other.get("count", 0)
    )
    return AnswerChoice(answer_id, percent, count, total)


def awarded_reward(payload: dict[str, Any]) -> int:
    try:
        data = _submission_data(payload)
    except ValueError:
        data = payload.get("data") if isinstance(payload.get("data"), dict) else payload
    value = data.get("reward", 0) if isinstance(data, dict) else 0
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def describe_missing_tasks(ticket: dict[str, Any]) -> list[str]:
    missing: list[str] = []
    if not ticket.get("hasBoundX"):
        missing.append("绑定 X")
    if not ticket.get("hasSubscribed"):
        missing.append("关注 X")

    telegram_required = bool(ticket.get("telegramTaskRequired", True))
    telegram_waived = bool(ticket.get("telegramTaskWaived", False))
    # hasTelegramAuth / hasTelegramCode 是完成群任务的中间状态，不要求两项同时为真。
    if telegram_required and not telegram_waived and not ticket.get("hasTelegramGroup"):
        missing.append("加入 Telegram 群")

    if not ticket.get("hasSignedChain"):
        missing.append("首次链上激活")
    return missing


class DGridClient:
    def __init__(
        self,
        private_key: str,
        session: Optional[requests.Session] = None,
        timeout: int = HTTP_TIMEOUT,
        retries: int = HTTP_RETRIES,
        sleep_fn: Callable[[float], None] = time.sleep,
    ):
        self.private_key = normalize_private_key(private_key)
        self.account = Account.from_key(self.private_key)
        self.address = self.account.address
        self.session = session or requests.Session()
        self.timeout = timeout
        self.retries = max(1, int(retries))
        self.sleep_fn = sleep_fn
        self.token = ""

    def base_headers(self) -> dict[str, str]:
        return {
            "Accept": "application/json, text/plain, */*",
            "Content-Type": "application/json",
            "Origin": WEB_ORIGIN,
            "Referer": WEB_ORIGIN + "/",
            "User-Agent": USER_AGENT,
            "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
        }

    def auth_headers(self) -> dict[str, str]:
        headers = self.base_headers()
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        return headers

    def _request_json(
        self,
        method: str,
        path: str,
        *,
        json_body: Optional[dict[str, Any]] = None,
        params: Optional[dict[str, Any]] = None,
        auth: bool = True,
        retries: Optional[int] = None,
        allow_relogin: bool = True,
    ) -> dict[str, Any]:
        attempts = retries if retries is not None else self.retries
        url = API_BASE + path
        last_error: Optional[BaseException] = None
        relogged = False

        max_attempts = max(1, attempts)
        attempt = 0
        while attempt < max_attempts:
            try:
                headers = self.auth_headers() if auth else self.base_headers()
                response = self.session.request(
                    method,
                    url,
                    headers=headers,
                    json=json_body,
                    params=params,
                    timeout=self.timeout,
                )

                if response.status_code == 401 and auth and allow_relogin and not relogged:
                    relogged = True
                    self.login()
                    # 401 请求没有被服务端执行，不消耗普通重试次数。
                    continue

                response.raise_for_status()
                payload = response.json()
                code = payload.get("code")
                if code is not None and str(code) not in {"0", "200"}:
                    raise DGridApiError(
                        f"接口返回失败 code={code} message={payload.get('message', '')}"
                    )
                data = payload.get("data", payload)
                if not isinstance(data, dict):
                    raise DGridApiError("接口 data 不是对象")
                return data
            except Exception as exc:
                last_error = exc
                attempt += 1
                if attempt >= max_attempts:
                    break
                self.sleep_fn(min(1.5 * attempt, 4.0))

        raise DGridApiError(f"{method} {path} 失败：{last_error}") from last_error

    def login(self) -> str:
        code_data = self._request_json(
            "POST",
            "/client-user/get-code",
            json_body={"address": self.address},
            auth=False,
            allow_relogin=False,
        )
        code = str(code_data.get("code") or "")
        if not code:
            raise DGridApiError("登录接口未返回 code")

        signed = Account.sign_message(encode_defunct(text=code), private_key=self.private_key)
        signature = signed.signature.hex()
        if not signature.startswith("0x"):
            signature = "0x" + signature

        challenge_data = self._request_json(
            "POST",
            "/client-user/challenge",
            json_body={
                "signature": signature,
                "address": self.address,
                "inviteCode": "",
            },
            auth=False,
            allow_relogin=False,
        )
        token = str(challenge_data.get("token") or "")
        if not token:
            raise DGridApiError("challenge 接口未返回 token")
        self.token = token
        return token

    def get_ticket(self) -> dict[str, Any]:
        return self._request_json("GET", "/arena/ticket")

    def _request_stream(self, path: str) -> str:
        url = API_BASE + path
        last_error: Optional[BaseException] = None
        relogged = False
        attempt = 0
        while attempt < self.retries:
            try:
                response = self.session.request(
                    "GET",
                    url,
                    headers=self.auth_headers(),
                    timeout=max(self.timeout, 120),
                )
                if response.status_code == 401 and not relogged:
                    relogged = True
                    self.login()
                    continue
                response.raise_for_status()
                return str(response.text or "")
            except Exception as exc:
                last_error = exc
                attempt += 1
                if attempt >= self.retries:
                    break
                self.sleep_fn(min(1.5 * attempt, 4.0))
        raise DGridApiError(f"GET {path} 流式答案失败：{last_error}") from last_error

    def prepare_question(
        self,
        group_id: str,
        question_id: str,
        answer_ids: Iterable[str],
    ) -> None:
        ids = [str(item) for item in answer_ids if item]
        if len(ids) < 2:
            raise DGridApiError("题目至少需要两个答案 ID")
        for option_index, answer_id in enumerate(ids[:2]):
            if option_index:
                self.sleep_fn(random.uniform(OPTION_JITTER_MIN, OPTION_JITTER_MAX))
            path = f"/arena/missions/{group_id}/questions/{question_id}/options/{answer_id}"
            self._request_stream(path)

    def get_missions(self) -> dict[str, Any]:
        return self._request_json("GET", "/arena/missions", params={"locale": "zh"})

    def submit_answer(
        self,
        group_id: str,
        question_id: str,
        answer_id: str,
    ) -> dict[str, Any]:
        path = f"/arena/missions/{group_id}/questions/{question_id}/options/{answer_id}"
        # 不在底层自动重复 POST，避免超时后重复提交；上层会先复查 dealt 状态。
        return self._request_json("POST", path, json_body={}, retries=1)

    def get_overview(self) -> dict[str, Any]:
        return self._request_json("GET", "/arena/overview")

    def close(self) -> None:
        try:
            self.session.close()
        except Exception:
            pass


class BscCheckInClient:
    def __init__(
        self,
        rpcs: Optional[Iterable[str]] = None,
        timeout: int = 30,
        receipt_timeout: int = 180,
        web3_factory: Optional[Callable[[str], Web3]] = None,
    ):
        self.rpcs = [str(rpc).strip() for rpc in (rpcs or BSC_RPCS) if str(rpc).strip()]
        if not self.rpcs:
            raise ValueError("至少需要一个 BSC RPC")
        self.timeout = timeout
        self.receipt_timeout = receipt_timeout
        self.web3_factory = web3_factory or self._default_web3_factory

    def _default_web3_factory(self, rpc: str) -> Web3:
        return Web3(HTTPProvider(rpc, request_kwargs={"timeout": self.timeout}))

    def build_transaction(self, w3: Web3, address: str) -> dict[str, Any]:
        chain_id = int(w3.eth.chain_id)
        if chain_id != BSC_CHAIN_ID:
            raise DGridError(f"RPC 链 ID 错误：{chain_id}，应为 {BSC_CHAIN_ID}")

        from_address = w3.to_checksum_address(address)
        contract = w3.to_checksum_address(CHECKIN_CONTRACT)
        nonce = int(w3.eth.get_transaction_count(from_address, "pending"))

        estimate_payload = {
            "from": from_address,
            "to": contract,
            "value": 0,
            "data": CHECKIN_CALLDATA,
        }
        try:
            estimated = int(w3.eth.estimate_gas(estimate_payload))
            gas_limit = min(max(int(estimated * 1.25), 60_000), 600_000)
        except Exception:
            # HAR 中网页钱包使用 600000；作为估算失败时的兼容值。
            gas_limit = 600_000

        gas_price = int(w3.eth.gas_price)
        gas_price = max(int(gas_price * 1.10), 50_000_001)

        return {
            "chainId": BSC_CHAIN_ID,
            "nonce": nonce,
            "to": contract,
            "value": 0,
            "data": CHECKIN_CALLDATA,
            "gas": gas_limit,
            "gasPrice": gas_price,
        }

    def check_in(self, private_key: str) -> str:
        key = normalize_private_key(private_key)
        account = Account.from_key(key)
        raw_tx: Optional[bytes] = None
        tx_hash_obj: Any = None
        errors: list[str] = []

        for rpc in self.rpcs:
            try:
                w3 = self.web3_factory(rpc)
                if hasattr(w3, "is_connected") and not w3.is_connected():
                    raise DGridError("RPC 无法连接")

                if raw_tx is None:
                    tx = self.build_transaction(w3, account.address)
                    signed = Account.sign_transaction(tx, private_key=key)
                    raw_tx = getattr(signed, "raw_transaction", None)
                    if raw_tx is None:
                        raw_tx = getattr(signed, "rawTransaction")
                    tx_hash_obj = getattr(signed, "hash", None) or w3.keccak(raw_tx)

                try:
                    sent_hash = w3.eth.send_raw_transaction(raw_tx)
                    tx_hash_obj = sent_hash or tx_hash_obj
                except Exception as send_exc:
                    text = str(send_exc).lower()
                    if not any(
                        marker in text
                        for marker in ("already known", "known transaction", "nonce too low")
                    ):
                        # 发送是否到达节点可能不确定，保留同一原始交易到下一个 RPC 再查/发送。
                        errors.append(f"{rpc} 发送失败：{send_exc}")

                receipt = w3.eth.wait_for_transaction_receipt(
                    tx_hash_obj,
                    timeout=self.receipt_timeout,
                    poll_latency=2,
                )
                status = receipt.get("status") if isinstance(receipt, dict) else receipt.status
                if int(status) != 1:
                    raise DGridError("签到交易执行失败，receipt status != 1")
                return tx_hash_obj.hex() if hasattr(tx_hash_obj, "hex") else str(tx_hash_obj)
            except Exception as exc:
                errors.append(f"{rpc}：{exc}")

        raise DGridError("BSC 签到失败；" + " | ".join(errors[-6:]))


class AnswerCoordinator:
    """按题目签名跨账号共享多数答案，并保证同一签名只探路一次。"""

    CACHE_PREFIX = "v2:"

    def __init__(self, cache_path: os.PathLike[str] | str):
        self.cache_path = Path(cache_path)
        self.cache_path.parent.mkdir(parents=True, exist_ok=True)
        self._condition = threading.Condition()
        self._probing: set[str] = set()
        self._unavailable: set[str] = set()
        self._answers: dict[str, AnswerChoice] = {}
        self._load()

    @staticmethod
    def _choice_from_item(item: dict[str, Any]) -> Optional[AnswerChoice]:
        if not isinstance(item, dict) or not item.get("answer_id"):
            return None
        stored_ids = [str(value) for value in (item.get("answer_ids") or []) if value]
        raw_index = item.get("answer_index")
        return AnswerChoice(
            str(item["answer_id"]),
            float(item.get("percent", 0.0)),
            int(item.get("count", 0)),
            int(item.get("total", 0)),
            str(item.get("updated_at") or ""),
            str(item.get("day") or ""),
            stored_ids,
            raw_index,
        )

    @staticmethod
    def _normalized_ids(answer_ids: Iterable[str]) -> tuple[str, ...]:
        return AnswerChoice._normalize_answer_ids(answer_ids)

    @classmethod
    def _cache_key(
        cls,
        group_id: str,
        question_id: str,
        answer_ids: Iterable[str],
    ) -> str:
        signature = [
            str(group_id),
            str(question_id),
            list(cls._normalized_ids(answer_ids)),
        ]
        return cls.CACHE_PREFIX + json.dumps(
            signature,
            ensure_ascii=False,
            separators=(",", ":"),
        )

    @staticmethod
    def _is_better_choice(candidate: AnswerChoice, current: AnswerChoice) -> bool:
        return candidate.total > current.total or (
            candidate.total == current.total and candidate.margin() > current.margin()
        )

    @classmethod
    def _attach_context(
        cls,
        choice: AnswerChoice,
        day: str,
        answer_ids: Iterable[str],
    ) -> AnswerChoice:
        valid_ids = cls._normalized_ids(answer_ids)
        choice.day = str(day)
        choice.answer_ids = valid_ids
        try:
            choice.answer_index = valid_ids.index(choice.answer_id)
        except ValueError:
            choice.answer_index = None
        return choice

    def _keep_better_choice(self, key: str, choice: AnswerChoice) -> None:
        current = self._answers.get(key)
        if current is None or self._is_better_choice(choice, current):
            self._answers[key] = choice

    def _load(self) -> None:
        if not self.cache_path.is_file():
            return
        try:
            payload = json.loads(self.cache_path.read_text(encoding="utf-8"))
            questions = payload.get("questions") or {}
            if not isinstance(questions, dict):
                return
            for key, item in questions.items():
                key = str(key)
                if not key.startswith(self.CACHE_PREFIX):
                    continue
                choice = self._choice_from_item(item)
                if choice is not None:
                    self._keep_better_choice(key, choice)
        except Exception:
            self._answers = {}

    def _save_locked(self) -> None:
        payload = {
            "updated_at": datetime.now().isoformat(timespec="seconds"),
            "cache_key": "version+group_id+question_id+ordered_answer_ids",
            "questions": {
                key: choice.to_dict()
                for key, choice in self._answers.items()
            },
        }
        temp = self.cache_path.with_suffix(self.cache_path.suffix + ".tmp")
        temp.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        temp.replace(self.cache_path)

    def get(
        self,
        group_id: str,
        question_id: str,
        answer_ids: Iterable[str],
    ) -> Optional[AnswerChoice]:
        key = self._cache_key(group_id, question_id, answer_ids)
        with self._condition:
            return self._answers.get(key)

    def update_from_submission(
        self,
        group_id: str,
        question_id: str,
        submission: dict[str, Any],
        answer_ids: Optional[Iterable[str]] = None,
    ) -> AnswerChoice:
        valid_ids = self._normalized_ids(answer_ids or [])
        if not valid_ids:
            raise ProbeResultUnavailable("提交响应缺少当前选项签名")
        choice = self._attach_context(
            answer_choice_from_submission(submission),
            datetime.now().date().isoformat(),
            valid_ids,
        )
        if choice.answer_index is None:
            raise ProbeResultUnavailable("提交响应的答案无法映射到当前选项")
        key = self._cache_key(group_id, question_id, valid_ids)
        with self._condition:
            current = self._answers.get(key)
            if current is None or self._is_better_choice(choice, current):
                self._answers[key] = choice
                self._save_locked()
            self._unavailable.discard(key)
            self._condition.notify_all()
            return self._answers[key]

    def resolve(
        self,
        group_id: str,
        question_id: str,
        answer_ids: Iterable[str],
        submit_probe: Callable[[str], dict[str, Any]],
    ) -> ResolveResult:
        valid_ids = self._normalized_ids(answer_ids)
        if not valid_ids:
            raise DGridError("题目没有可用答案 ID")
        key = self._cache_key(group_id, question_id, valid_ids)
        today = datetime.now().date().isoformat()

        with self._condition:
            while True:
                if key in self._unavailable:
                    raise ProbeResultUnavailable(
                        "本轮探路已提交但票数答案不可确认，已阻止重复提交"
                    )

                cached = self._answers.get(key)
                if cached is not None and cached.is_valid_for(today, valid_ids):
                    mapped = cached.mapped_to(valid_ids)
                    if mapped is not None:
                        return ResolveResult(mapped, False, None)

                if cached is not None:
                    self._answers.pop(key, None)
                    self._save_locked()

                if key not in self._probing:
                    self._probing.add(key)
                    break
                self._condition.wait(timeout=60)

        probe_answer_id = random.choice(valid_ids)
        try:
            submission = submit_probe(probe_answer_id)
        except ProbeResultUnavailable:
            with self._condition:
                self._probing.discard(key)
                self._unavailable.add(key)
                self._condition.notify_all()
            raise
        except Exception:
            with self._condition:
                self._probing.discard(key)
                self._condition.notify_all()
            raise

        try:
            choice = self._attach_context(
                answer_choice_from_submission(submission),
                today,
                valid_ids,
            )
            if choice.answer_index is None:
                raise ValueError("答案 ID 不在当前选项列表")
        except Exception as exc:
            with self._condition:
                self._probing.discard(key)
                self._unavailable.add(key)
                self._condition.notify_all()
            raise ProbeResultUnavailable(
                "随机探路已提交，但票数答案无法映射，未写入共享缓存"
            ) from exc

        with self._condition:
            current = self._answers.get(key)
            if current is None or self._is_better_choice(choice, current):
                self._answers[key] = choice
                self._save_locked()
            self._probing.discard(key)
            self._unavailable.discard(key)
            mapped = self._answers[key].mapped_to(valid_ids)
            self._condition.notify_all()
            if mapped is None:
                raise ProbeResultUnavailable("探路答案无法映射到当前选项")
            return ResolveResult(mapped, True, submission)

class DGridRunner:
    def __init__(
        self,
        wallets: list[WalletRecord],
        threads: int = DEFAULT_THREADS,
        result_dir: os.PathLike[str] | str = RESULT_DIR_NAME,
        rpcs: Optional[Iterable[str]] = None,
    ):
        self.wallets = wallets
        self.threads = max(1, min(int(threads), max(1, len(wallets))))
        self.result_dir = Path(result_dir)
        self.result_dir.mkdir(parents=True, exist_ok=True)
        self.results = {wallet.index: AccountResult(wallet) for wallet in wallets}
        self.coordinator = AnswerCoordinator(self.result_dir / "每日答案缓存.json")
        self.bsc = BscCheckInClient(rpcs=rpcs)

    def _login_one(self, wallet: WalletRecord) -> Optional[PreparedAccount]:
        result = self.results[wallet.index]
        label = f"[{wallet.index}/{len(self.wallets)}] {short_address(wallet.address)}"
        client = DGridClient(wallet.private_key)
        try:
            client.login()
            result.login_status = "成功"
            ticket = client.get_ticket()
            safe_print(f"✅ {label} 登录成功")
            return PreparedAccount(wallet, client, ticket)
        except Exception as exc:
            result.status = "失败"
            result.login_status = "失败"
            result.detail = str(exc)
            safe_print(f"❌ {label} 登录失败：{exc}")
            client.close()
            return None

    def _check_in_one(self, prepared: PreparedAccount) -> bool:
        wallet = prepared.wallet
        result = self.results[wallet.index]
        label = f"[{wallet.index}/{len(self.wallets)}] {short_address(wallet.address)}"

        missing = describe_missing_tasks(prepared.ticket)
        if missing:
            result.status = "待手动"
            result.checkin_status = "跳过"
            result.mission_status = "跳过"
            result.detail = "未完成：" + "、".join(missing)
            safe_print(f"⏭️ {label} 需手动完成：{'、'.join(missing)}")
            return False

        if prepared.ticket.get("hasCheckIn"):
            prepared.checkin_status = "今日已签到"
            result.checkin_status = "今日已签到"
            safe_print(f"ℹ️ {label} 今日已签到")
            return True

        try:
            safe_print(f"⛓️ {label} 正在发送 BSC 每日签到交易...")
            tx_hash = self.bsc.check_in(wallet.private_key)
            prepared.tx_hash = tx_hash
            result.tx_hash = tx_hash
            result.checkin_status = "交易成功，等待接口确认"

            # 网站 ticket 可能有缓存，最多等待约 35 秒。
            confirmed = False
            for attempt in range(7):
                if attempt:
                    time.sleep(5)
                ticket = prepared.client.get_ticket()
                prepared.ticket = ticket
                if ticket.get("hasCheckIn"):
                    confirmed = True
                    break

            if confirmed:
                prepared.checkin_status = "签到成功"
                result.checkin_status = "签到成功"
                safe_print(f"✅ {label} 签到成功：{tx_hash}")
            else:
                # 链上回执成功但 API 尚未刷新，保留可继续尝试 missions。
                prepared.checkin_status = "链上成功，接口待刷新"
                result.checkin_status = "链上成功，接口待刷新"
                safe_print(f"⚠️ {label} 链上成功，但 ticket 暂未刷新")
            return True
        except Exception as exc:
            # 交易可能因已经签到而回滚，最后再查一次接口。
            try:
                ticket = prepared.client.get_ticket()
                prepared.ticket = ticket
                if ticket.get("hasCheckIn"):
                    prepared.checkin_status = "今日已签到"
                    result.checkin_status = "今日已签到"
                    return True
            except Exception:
                pass

            result.status = "失败"
            result.checkin_status = "失败"
            result.detail = f"签到失败：{exc}"
            safe_print(f"❌ {label} 签到失败：{exc}")
            return False

    @staticmethod
    def _find_question(missions_data: dict[str, Any], question_id: str) -> Optional[dict[str, Any]]:
        for mission in missions_data.get("missions", []):
            if str(mission.get("question_id")) == question_id:
                return mission
        return None

    def _probe_submit(
        self,
        prepared: PreparedAccount,
        group_id: str,
        mission: dict[str, Any],
        probe_answer_id: str,
    ) -> dict[str, Any]:
        answer_ids = [str(item) for item in mission.get("answers_ids") or [] if item]
        prepared.client.prepare_question(
            group_id,
            str(mission["question_id"]),
            answer_ids,
        )
        return self._submit_with_recovery(
            prepared,
            group_id,
            mission,
            probe_answer_id,
        )

    def _submit_with_recovery(
        self,
        prepared: PreparedAccount,
        group_id: str,
        mission: dict[str, Any],
        answer_id: str,
    ) -> dict[str, Any]:
        question_id = str(mission["question_id"])
        try:
            return prepared.client.submit_answer(group_id, question_id, answer_id)
        except Exception as first_error:
            time.sleep(2)
            refreshed = self._get_missions_with_retry(prepared)
            latest = self._find_question(refreshed, question_id)
            if latest and latest.get("dealt"):
                raise ProbeResultUnavailable(
                    f"提交响应丢失，但复查显示已完成，积分={latest.get('get_points', 0)}"
                ) from first_error
            # 明确仍未完成时只重试一次。
            return prepared.client.submit_answer(group_id, question_id, answer_id)

    def _get_missions_with_retry(self, prepared: PreparedAccount) -> dict[str, Any]:
        result = self.results[prepared.wallet.index]
        last_error: Optional[BaseException] = None

        for attempt in range(MISSIONS_RETRY_LIMIT):
            try:
                data = prepared.client.get_missions()
                prepared.missions_data = data
                return data
            except DGridApiError as exc:
                last_error = exc
                if attempt + 1 >= MISSIONS_RETRY_LIMIT:
                    break

                try:
                    ticket = prepared.client.get_ticket()
                    prepared.ticket = ticket
                    if ticket.get("hasCheckIn"):
                        prepared.checkin_status = "签到成功"
                        result.checkin_status = "签到成功"
                except Exception:
                    pass

                self._sleep_jitter(MISSIONS_RETRY_MIN, MISSIONS_RETRY_MAX)

        raise DGridApiError(
            f"GET /arena/missions 重试 {MISSIONS_RETRY_LIMIT} 次仍失败：{last_error}"
        ) from last_error
    def _process_missions(self, prepared: PreparedAccount) -> None:
        wallet = prepared.wallet
        result = self.results[wallet.index]
        label = f"[{wallet.index}/{len(self.wallets)}] {short_address(wallet.address)}"

        try:
            data = prepared.missions_data if prepared.missions_data is not None else self._get_missions_with_retry(prepared)
            group_id = str(data.get("group_id") or "")
            missions = list(data.get("missions") or [])[:MAX_DAILY_MISSIONS]
            if not group_id:
                raise DGridError("missions 接口缺少 group_id")

            if not missions:
                result.status = "成功"
                result.mission_status = "今日无题目"
                safe_print(f"ℹ️ {label} 今日无模式选择题")
                self._refresh_summary(prepared)
                return

            awarded_total = 0
            for order, mission in enumerate(missions, start=1):
                if mission.get("dealt"):
                    continue

                question_id = str(mission.get("question_id") or "")
                answer_ids = [str(x) for x in mission.get("answers_ids") or [] if x]
                if not question_id or len(answer_ids) < 2:
                    raise DGridError(f"第 {order} 题字段不完整")

                self._sleep_jitter(MISSION_JITTER_MIN, MISSION_JITTER_MAX)

                try:
                    resolved = self.coordinator.resolve(
                        group_id,
                        question_id,
                        answer_ids,
                        lambda probe_id, m=mission: self._probe_submit(
                            prepared, group_id, m, probe_id
                        ),
                    )

                    if resolved.did_probe:
                        submission = resolved.probe_submission
                        mode = "探路"
                    else:
                        submission = self._submit_with_recovery(
                            prepared,
                            group_id,
                            mission,
                            resolved.choice.answer_id,
                        )
                        mode = "跟随"

                    if submission is None:
                        raise ProbeResultUnavailable("没有提交响应")

                    current_choice = self.coordinator.update_from_submission(
                        group_id, question_id, submission,
                        answer_ids=answer_ids,
                    )
                    reward = awarded_reward(submission)
                    awarded_total += reward
                    safe_print(
                        f"🎯 {label} 第 {order} 题[{question_id}] {mode}完成："
                        f"本次 {reward} 分，多数占比 {current_choice.percent:.2%}"
                    )
                except ProbeResultUnavailable as exc:
                    # 已提交但响应丢失时不重复盲选；最终复查会取真实 get_points。
                    safe_print(f"⚠️ {label} 第 {order} 题[{question_id}]：{exc}")

            final_data = self._get_missions_with_retry(prepared)
            final_missions = list(final_data.get("missions") or [])[:MAX_DAILY_MISSIONS]
            completed = sum(1 for item in final_missions if item.get("dealt"))
            points = sum(int(item.get("get_points") or 0) for item in final_missions)
            result.completed_count = completed
            result.points = points if final_missions else awarded_total

            if final_missions and completed == len(final_missions):
                result.status = "成功"
                result.mission_status = f"完成 {completed}/{len(final_missions)}"
                safe_print(f"✅ {label} 每日题目完成：{completed}/{len(final_missions)}，共 {points} 分")
            else:
                result.status = "失败"
                result.mission_status = f"仅完成 {completed}/{len(final_missions)}"
                result.detail = result.detail or "仍有题目未完成"
                safe_print(f"❌ {label} 每日题目未全部完成：{completed}/{len(final_missions)}")

            self._refresh_summary(prepared)
        except Exception as exc:
            result.status = "失败"
            result.mission_status = "失败"
            result.detail = f"每日题目失败：{exc}"
            safe_print(f"❌ {label} 每日题目失败：{exc}")

    def _refresh_summary(self, prepared: PreparedAccount) -> None:
        result = self.results[prepared.wallet.index]
        try:
            overview = prepared.client.get_overview()
            result.today_points = int(overview.get("todayPoints") or 0)
            result.total_points = int(overview.get("totalPoints") or 0)
        except Exception:
            pass

    @staticmethod
    def _sleep_jitter(minimum: float, maximum: float) -> None:
        time.sleep(random.uniform(minimum, maximum))

    def _run_account(self, wallet: WalletRecord) -> None:
        """单账号完整流水线：登录成功后立即签到、取题并执行。"""
        prepared: Optional[PreparedAccount] = None
        result = self.results[wallet.index]
        label = f"[{wallet.index}/{len(self.wallets)}] {short_address(wallet.address)}"
        try:
            self._sleep_jitter(ACCOUNT_JITTER_MIN, ACCOUNT_JITTER_MAX)
            prepared = self._login_one(wallet)
            if prepared is None:
                return

            self._sleep_jitter(ACCOUNT_JITTER_MIN, ACCOUNT_JITTER_MAX)
            if not self._check_in_one(prepared):
                return

            self._sleep_jitter(ACCOUNT_JITTER_MIN, ACCOUNT_JITTER_MAX)
            prepared.missions_data = self._get_missions_with_retry(prepared)
            self._sleep_jitter(ACCOUNT_JITTER_MIN, ACCOUNT_JITTER_MAX)
            self._process_missions(prepared)
        except Exception as exc:
            result.status = "失败"
            result.detail = result.detail or str(exc)
            if result.mission_status == "未处理":
                result.mission_status = "失败"
            safe_print(f"❌ {label} 账号流程异常：{exc}")
        finally:
            if prepared is not None:
                try:
                    prepared.client.close()
                except Exception:
                    pass

    def run(self) -> list[AccountResult]:
        safe_print(
            f"\n开始运行：账号 {len(self.wallets)} 个，"
            f"全局并行上限 {self.threads} 个，默认本地直连\n"
            "账号登录成功后立即继续签到、取题和答题；"
            "账号之间使用随机抖动。\n"
            "答案策略：按日期和选项集校验缓存，同题仅一个账号探路，"
            "异常时不重复盲提交。\n"
        )

        with ThreadPoolExecutor(max_workers=self.threads) as executor:
            futures = {
                executor.submit(self._run_account, wallet): wallet
                for wallet in self.wallets
            }
            for future in as_completed(futures):
                wallet = futures[future]
                try:
                    future.result()
                except Exception as exc:
                    result = self.results[wallet.index]
                    result.status = "失败"
                    result.detail = result.detail or str(exc)
                    safe_print(
                        f"❌ [{wallet.index}/{len(self.wallets)}] 全局工作线程异常：{exc}"
                    )

        output = [self.results[index] for index in sorted(self.results)]
        self._write_results(output)
        self._print_summary(output)
        return output
    def _write_results(self, results: list[AccountResult]) -> None:
        csv_path = self.result_dir / "运行结果.csv"
        fields = list(results[0].as_csv_row().keys()) if results else []
        if fields:
            with csv_path.open("w", encoding="utf-8-sig", newline="") as f:
                writer = csv.DictWriter(f, fieldnames=fields)
                writer.writeheader()
                writer.writerows(result.as_csv_row() for result in results)

        success_lines: list[str] = []
        failed_lines: list[str] = []
        manual_lines: list[str] = []
        for result in results:
            line = (
                f"{result.index}. {result.address} | 登录:{result.login_status} | "
                f"签到:{result.checkin_status} | 答题:{result.mission_status} | "
                f"今日积分:{result.today_points} | {result.detail}"
            ).rstrip(" |")
            if result.status == "成功":
                success_lines.append(line)
            elif result.status == "待手动":
                manual_lines.append(line)
            else:
                failed_lines.append(line)

        (self.result_dir / "成功结果.txt").write_text(
            "\n".join(success_lines), encoding="utf-8"
        )
        (self.result_dir / "失败结果.txt").write_text(
            "\n".join(failed_lines), encoding="utf-8"
        )
        (self.result_dir / "待手动任务.txt").write_text(
            "\n".join(manual_lines), encoding="utf-8"
        )

    def _print_summary(self, results: list[AccountResult]) -> None:
        success = sum(1 for result in results if result.status == "成功")
        manual = sum(1 for result in results if result.status == "待手动")
        failed = len(results) - success - manual
        safe_print("\n" + "=" * 62)
        safe_print(f"运行完成：成功 {success} | 待手动 {manual} | 失败 {failed}")
        safe_print(f"结果目录：{self.result_dir.resolve()}")
        safe_print("私钥未写入任何结果文件。")
        safe_print("=" * 62)


def run_self_test() -> int:
    test_key = "0x" + "1".zfill(64)
    account = Account.from_key(test_key)
    code = "DGRID_SELF_TEST"
    signed = Account.sign_message(encode_defunct(text=code), private_key=test_key)
    recovered = Account.recover_message(encode_defunct(text=code), signature=signed.signature)
    if recovered != account.address:
        print("自检失败：登录签名恢复地址不一致")
        return 1

    choice = choose_majority_answer(
        {
            "chosen": {
                "answers_id": "A",
                "count": 45,
                "percent": 0.45,
                "total_choice_count": 100,
            },
            "other": {
                "answers_id": "B",
                "count": 55,
                "percent": 0.55,
                "total_choice_count": 100,
            },
            "reward": 5,
        }
    )
    if choice[0] != "B":
        print("自检失败：多数选项判断错误")
        return 1

    if BSC_CHAIN_ID != 56 or CHECKIN_CALLDATA != "0x183ff085":
        print("自检失败：BSC 配置错误")
        return 1

    import tempfile

    with tempfile.TemporaryDirectory() as temp_dir:
        coordinator = AnswerCoordinator(Path(temp_dir) / "cache.json")
        probe_payload = {
            "chosen": {
                "answers_id": "A",
                "count": 40,
                "percent": 0.40,
                "total_choice_count": 100,
            },
            "other": {
                "answers_id": "B",
                "count": 60,
                "percent": 0.60,
                "total_choice_count": 100,
            },
            "reward": 5,
        }
        first = coordinator.resolve(
            "group-1", "question-1", ["A", "B"], lambda _: probe_payload
        )
        second = coordinator.resolve(
            "group-2",
            "question-1",
            ["A", "B"],
            lambda _: probe_payload,
        )
        if not first.did_probe or not second.did_probe:
            print("自检失败：跨 group 的相同 question_id 被错误复用")
            return 1

    print("自检通过：登录签名、多数选项判断、BSC 配置、跨账号答案共享均正常。")
    print("自检过程没有访问 DGrid 或 BSC 网络。")
    return 0


def parse_args(argv: Optional[list[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="DGrid 钱包登录与每日任务自动化")
    parser.add_argument("--钱包文件", dest="wallet_file", default=None)
    parser.add_argument("--线程", dest="threads", type=int, default=None)
    parser.add_argument("--结果目录", dest="result_dir", default=RESULT_DIR_NAME)
    parser.add_argument("--自检", dest="self_test", action="store_true")
    return parser.parse_args(argv)


def main(argv: Optional[list[str]] = None) -> int:
    args = parse_args(argv)
    if args.self_test:
        return run_self_test()

    try:
        wallet_path = Path(args.wallet_file) if args.wallet_file else find_wallet_file()
        wallets = load_wallets(wallet_path)
    except Exception as exc:
        print(f"读取钱包失败：{exc}")
        return 1

    threads = args.threads
    if threads is None:
        try:
            raw = input(f"请输入线程数，直接回车默认 {DEFAULT_THREADS}：").strip()
            threads = int(raw) if raw else DEFAULT_THREADS
        except (ValueError, EOFError):
            threads = DEFAULT_THREADS

    result_dir = args.result_dir
    if is_airdrop() and result_dir == RESULT_DIR_NAME:
        result_dir = str(project_data_dir("DGrid") / "results")

    runner = DGridRunner(
        wallets,
        threads=threads,
        result_dir=result_dir,
    )
    results = runner.run()
    emit_summary("DGrid", [result.as_csv_row() for result in results])
    return 0 if all(result.status == "成功" for result in results) else 2


if __name__ == "__main__":
    exit_code = main()
    if len(sys.argv) == 1 and sys.stdin.isatty():
        try:
            input("\n按回车键退出...")
        except EOFError:
            pass
    raise SystemExit(exit_code)










