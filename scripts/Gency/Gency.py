import time
import os
import random
import json
import sys
import threading
import requests
import openpyxl
from dataclasses import dataclass, asdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from eth_account import Account
from eth_account.messages import encode_defunct
from web3 import Web3

# Web3.py v6 / v7 兼容
try:
    from web3.middleware import ExtraDataToPOAMiddleware as POA_MIDDLEWARE
except ImportError:
    from web3.middleware import geth_poa_middleware as POA_MIDDLEWARE

# =========================================================
# 可调配置区
# =========================================================

KEY_FILE = "wallets.xlsx"
KEY_COLUMN = 1
SKIP_HEADER = False

UI_SHOW_TIME = True
UI_SHOW_THREAD_NAME = False
UI_PRINT_FINAL_TABLE = True
UI_PAUSE_AT_END = False   # ✅ 改为 False，不阻塞 AgentOps

LOG_LOCK = threading.Lock()

MAX_WORKERS = 10
ACCOUNT_RETRY_TIMES = 1
START_STAGGER_MIN = 0.5
START_STAGGER_MAX = 2.0

BASE_URL = "https://gencyai.io/api"
REQUEST_TIMEOUT = 15
API_RETRY_TIMES = 2
API_RETRY_SLEEP = 2
NET_ID = 1

BSC_RPC_URLS = [
    "https://bsc-dataseed1.binance.org/",
    "https://bsc-dataseed2.binance.org/",
    "https://bsc-dataseed3.binance.org/",
    "https://bsc-dataseed4.binance.org/",
]
CHAIN_ID = 56
CONTRACT_ADDRESS = "0xd6f6f6e56d02157c51bc41da7cbf074e764ae639"
INPUT_DATA = "0x183ff085"
GAS_MULTIPLIER = 1.2
TX_RECEIPT_TIMEOUT = 120
AFTER_TX_SLEEP = 5

MIN_BNB_BALANCE = 0

DEFAULT_HEADERS = {
    "Accept": "application/json, text/plain, */*",
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Origin": "https://gencyai.io",
    "Referer": "https://gencyai.io/",
}

SIGN_MESSAGE_TEXT = (
    "You hereby confirm that you are the owner of this connected wallet. "
    "This is a safe and gasless transaction to verify your ownership. "
    "Signing this message will not give GencyAI permission to make transactions with your wallet."
)


@dataclass
class AccountResult:
    index: int
    wallet: str
    status: str
    success: bool
    days: str = "-"
    before_credit: str = "-"
    after_credit: str = "-"
    earned: str = "-"
    tx_hash: str = "-"
    error: str = ""


def now_str():
    return time.strftime("%H:%M:%S")


def safe_print(msg: str):
    with LOG_LOCK:
        print(msg, flush=True)


def short_wallet(address: str) -> str:
    if not address or len(address) < 10:
        return address or "-"
    return f"{address[:6]}...{address[-4:]}"


def normalize_private_key(key: str) -> str:
    key = str(key).strip()
    return key if key else ""


def raw_tx_bytes(signed_tx):
    if hasattr(signed_tx, "raw_transaction"):
        return signed_tx.raw_transaction
    return signed_tx.rawTransaction


class GencyAI_Bot:
    def __init__(self, private_key, index=0):
        self.index = index
        self.base_url = BASE_URL
        self.session = requests.Session()
        self.session.headers.update(DEFAULT_HEADERS)

        self.access_token = None
        self.account = Account.from_key(private_key)
        self.wallet_address = self.account.address
        self.log_prefix = f"[号_{self.index:04d} | {short_wallet(self.wallet_address)}]"

        self.rpc_url = BSC_RPC_URLS[(self.index - 1) % len(BSC_RPC_URLS)]
        self.w3 = Web3(Web3.HTTPProvider(self.rpc_url, request_kwargs={"timeout": REQUEST_TIMEOUT}))
        self.w3.middleware_onion.inject(POA_MIDDLEWARE, layer=0)

    def log(self, message):
        parts = []
        if UI_SHOW_TIME:
            parts.append(now_str())
        parts.append(self.log_prefix)
        parts.append(message)
        safe_print(" ".join(parts))

    def request_json(self, method, url, *, data=None, json=None, headers=None, tag="请求"):
        last_error = None
        for attempt in range(1, API_RETRY_TIMES + 2):
            try:
                response = self.session.request(
                    method=method, url=url, data=data, json=json, headers=headers, timeout=REQUEST_TIMEOUT
                )
                try:
                    return response.json()
                except Exception:
                    raise RuntimeError(f"非 JSON 响应: HTTP {response.status_code}")
            except Exception as e:
                last_error = e
                if attempt <= API_RETRY_TIMES:
                    self.log(f"⚠️ {tag}异常，第 {attempt} 次失败：{e}，稍后重试")
                    time.sleep(API_RETRY_SLEEP)
        return {"code": -1, "message": str(last_error)}

    def generate_signature(self, message_text):
        message_encoded = encode_defunct(text=message_text)
        signed_message = self.account.sign_message(message_encoded)
        sig_hex = signed_message.signature.hex()
        if not sig_hex.startswith("0x"):
            sig_hex = "0x" + sig_hex
        return sig_hex

    def login(self, net_id=NET_ID):
        self.log("🔄 正在生成签名并尝试登录...")
        signature = self.generate_signature(SIGN_MESSAGE_TEXT)
        url = f"{self.base_url}/wallet_login"
        payload = {"wallet": self.wallet_address, "sign": signature, "net": net_id}
        headers = {"Content-Type": "application/x-www-form-urlencoded"}

        result = self.request_json("POST", url, data=payload, headers=headers, tag="登录")
        if result.get("code") == 0:
            self.access_token = result.get("data", {}).get("access_token")
            self.session.headers.update({"Authorization": f"Bearer {self.access_token}"})
            self.log("✅ 登录成功")
            return True
        self.log(f"❌ 登录失败: {result}")
        return False

    def get_checkin_info(self):
        url = f"{self.base_url}/checkin_info"
        result = self.request_json("POST", url, json={}, tag="查询签到状态")
        if result.get("code") == 0:
            data = result.get("data", {})
            days = data.get("days", 0)
            is_signed_today = data.get("s", False)
            self.log(f"📌 连续: {days} 天 | 状态: {'✅ 已签到' if is_signed_today else '❌ 未签到'}")
            return days, is_signed_today
        self.log(f"❌ 查询签到状态失败: {result}")
        return None, None

    def get_credit_info(self):
        url = f"{self.base_url}/credit_refresh"
        payload = {"wallet": self.wallet_address}
        headers = {"Content-Type": "application/x-www-form-urlencoded"}

        result = self.request_json("POST", url, data=payload, headers=headers, tag="查询真实积分")
        if result.get("code") == 0:
            data = result.get("data", {})
            total_credit = float(data.get("total_credit", 0))
            checkin_credit = float(data.get("checkin_credit", 0))
            self.log(f"📊 当前总积分 (Total): {total_credit} | 累计签到积分: {checkin_credit}")
            return data
        self.log(f"⚠️ 积分查询失败: {result}")
        return None

    def check_bnb_balance(self):
        if MIN_BNB_BALANCE <= 0:
            return True
        try:
            balance_wei = self.w3.eth.get_balance(self.wallet_address)
            balance = float(self.w3.from_wei(balance_wei, "ether"))
            if balance < MIN_BNB_BALANCE:
                self.log(f"⛽ BNB 不足：{balance:.8f}，跳过")
                return False
            return True
        except Exception:
            return False

    def perform_onchain_checkin(self, contract_address, input_data):
        self.log(f"⛓️ 开始执行 BSC 链上交易...")
        if not self.w3.is_connected() or not self.check_bnb_balance():
            return None

        try:
            to_address = self.w3.to_checksum_address(contract_address)
            tx_params = {
                "nonce": self.w3.eth.get_transaction_count(self.wallet_address),
                "to": to_address,
                "value": 0,
                "gasPrice": self.w3.eth.gas_price,
                "chainId": CHAIN_ID,
                "data": input_data,
            }
            tx_params["gas"] = int(self.w3.eth.estimate_gas(tx_params) * GAS_MULTIPLIER)
            signed_tx = self.w3.eth.account.sign_transaction(tx_params, self.account.key)
            tx_hash = self.w3.eth.send_raw_transaction(raw_tx_bytes(signed_tx))
            tx_hash_hex = tx_hash.hex()
            if not tx_hash_hex.startswith("0x"):
                tx_hash_hex = "0x" + tx_hash_hex

            self.log(f"🚀 已广播！哈希: {tx_hash_hex}")
            receipt = self.w3.eth.wait_for_transaction_receipt(tx_hash, timeout=TX_RECEIPT_TIMEOUT)
            if receipt.status == 1:
                self.log("✅ 链上打包成功")
                return tx_hash_hex
            self.log("❌ 链上执行失败 Revert")
            return None
        except Exception as e:
            self.log(f"⚠️ 链上交易异常: {e}")
            return None

    def api_checkin(self, tx_hash, day):
        self.log("🌐 正在提交 API 验证...")
        url = f"{self.base_url}/checkin"
        payload = {"wallet": self.wallet_address, "tx_hash": tx_hash, "day": day}
        headers = {"Content-Type": "application/x-www-form-urlencoded"}
        result = self.request_json("POST", url, data=payload, headers=headers, tag="API 验证")
        if result.get("code") == 0:
            self.log(f"🎉 验证成功！完成第 {day} 天签到")
            return True
        return False


def run_account(private_key, index):
    time.sleep(random.uniform(START_STAGGER_MIN, START_STAGGER_MAX))
    bot = GencyAI_Bot(private_key, index=index)

    for attempt in range(1, ACCOUNT_RETRY_TIMES + 2):
        try:
            if attempt > 1:
                bot.log(f"🔁 重试 {attempt}/{ACCOUNT_RETRY_TIMES + 1}")
                time.sleep(API_RETRY_SLEEP)

            if not bot.login():
                last_error = "登录失败"
                continue

            current_days, is_signed_today = bot.get_checkin_info()
            if is_signed_today is None:
                last_error = "获取状态失败"
                continue

            init_data = bot.get_credit_info()
            init_credit = float(init_data.get("total_credit", 0)) if init_data else 0.0

            if is_signed_today:
                bot.log("🛑 今日已签到")
                return AccountResult(
                    index=index, wallet=bot.wallet_address, status="已签到", success=True,
                    days=str(current_days), before_credit=str(init_credit), after_credit=str(init_credit), earned="0"
                )

            target_day = current_days + 1
            tx_hash = bot.perform_onchain_checkin(CONTRACT_ADDRESS, INPUT_DATA)
            if not tx_hash:
                last_error = "链上签到失败"
                continue

            time.sleep(AFTER_TX_SLEEP)

            if bot.api_checkin(tx_hash, target_day):
                bot.log("⏳ 启动轮询监听真实验证积分下发 (最高等待20秒)...")

                fin_credit = init_credit
                earn_credit = 0.0

                for i in range(1, 5):
                    time.sleep(5)
                    final_data = bot.get_credit_info()
                    fin_credit = float(final_data.get("total_credit", 0)) if final_data else init_credit
                    earn_credit = fin_credit - init_credit

                    if earn_credit > 0:
                        bot.log(f"✅ 第 {i} 次检测：积分已到账！")
                        break
                    else:
                        bot.log(f"👀 第 {i} 次检测：暂未到账，继续等待...")

                bot.log(f"💰 最终核对：本次净赚 {earn_credit} 积分！当前总积分: {fin_credit}")

                return AccountResult(
                    index=index, wallet=bot.wallet_address, status="成功", success=True,
                    days=str(target_day), before_credit=str(init_credit), after_credit=str(fin_credit),
                    earned=str(earn_credit), tx_hash=tx_hash,
                )

            last_error = "API 验证失败"
        except Exception as e:
            last_error = f"异常: {e}"
            bot.log(f"🔥 {last_error}")

    return AccountResult(
        index=index,
        wallet=getattr(bot, "wallet_address", "-"),
        status="失败",
        success=False,
        error=last_error
    )


def load_keys_from_excel(key_file):
    if not os.path.exists(key_file):
        safe_print(f"❌ 找不到 {key_file}！请填在第 {KEY_COLUMN} 列。")
        return []
    keys = []
    workbook = openpyxl.load_workbook(key_file, data_only=True)
    sheet = workbook.active
    start_row = 2 if SKIP_HEADER else 1
    for row in sheet.iter_rows(min_row=start_row, min_col=KEY_COLUMN, max_col=KEY_COLUMN, values_only=True):
        if row[0]:
            key = normalize_private_key(row[0])
            if len(key) >= 60:
                keys.append(key)
    return keys


def print_final_table(results):
    if not UI_PRINT_FINAL_TABLE:
        return
    safe_print("\n每个账号总览：")
    safe_print("-" * 140)
    safe_print(f"{'序号':<4} | {'钱包':<15} | {'状态':<6} | {'天数':<4} | {'初始积分':<10} | {'最终积分':<10} | {'获得积分':<10} | {'TX哈希':<15} | 失败原因")
    safe_print("-" * 140)
    for r in sorted(results, key=lambda x: x.index):
        safe_print(
            f"{r.index:<4} | {short_wallet(r.wallet):<15} | {r.status:<6} | {r.days:<4} | "
            f"{r.before_credit:<10} | {r.after_credit:<10} | {r.earned:<10} | {short_wallet(r.tx_hash):<15} | {r.error}"
        )
    safe_print("-" * 140)


def main():
    final_status = "failed"
    final_message = "脚本执行完成"
    all_account_results = []

    try:
        keys = load_keys_from_excel(KEY_FILE)
        if not keys:
            print(json.dumps({"status": "failed", "message": "没有可用私钥", "total": 0, "success_count": 0, "failed_count": 0, "accounts": []}))
            return

        workers = min(MAX_WORKERS, len(keys))
        safe_print("=" * 90)
        safe_print(f"🤖 GencyAI 自动签到 | 👛 有效私钥: {len(keys)} | 🧵 并发线程: {workers}")
        safe_print("=" * 90)

        results = []
        success_count = 0
        fail_count = 0

        with ThreadPoolExecutor(max_workers=workers) as executor:
            futures = {executor.submit(run_account, key, idx): idx for idx, key in enumerate(keys, start=1)}
            for future in as_completed(futures):
                idx = futures[future]
                try:
                    result = future.result()
                except Exception as exc:
                    result = AccountResult(index=idx, wallet="-", status="失败", success=False, error=str(exc))

                results.append(result)
                if result.success:
                    success_count += 1
                else:
                    fail_count += 1

        print_final_table(results)

        # ========== 构建账号明细 ==========
        for r in sorted(results, key=lambda x: x.index):
            all_account_results.append({
                "index": r.index,
                "address": r.wallet,
                "name": f"wallet_{r.index}",
                "status": "success" if r.success else "failed",
                "message": r.status,
                "points": int(float(r.earned)) if r.earned != "-" else 0,
                "error": r.error if not r.success else ""
            })

        total = len(all_account_results)
        success_accounts = sum(1 for a in all_account_results if a["status"] == "success")
        failed_accounts = total - success_accounts

        final_status = "failed" if failed_accounts > 0 else "success"
        final_message = f"执行完成：{total}个账号，{success_accounts}成功，{failed_accounts}失败"

        safe_print("=" * 90)
        safe_print(f"🎉 任务执行完毕 | ✅ 成功/已签到: {success_count} | ❌ 失败: {fail_count}")

    except Exception as e:
        final_status = "failed"
        final_message = f"脚本执行异常: {str(e)}"
        safe_print(f"[!] 捕获到顶层异常: {e}")

    # ========== AgentOps 标准输出（最后一行 JSON） ==========
    print(json.dumps({
        "status": final_status,
        "message": final_message,
        "total": len(all_account_results),
        "success_count": sum(1 for a in all_account_results if a["status"] == "success"),
        "failed_count": sum(1 for a in all_account_results if a["status"] == "failed"),
        "accounts": all_account_results
    }))


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        print(json.dumps({"status": "failed", "message": f"未捕获异常: {e}", "total": 0, "success_count": 0, "failed_count": 0, "accounts": []}))