import os
import time
import json
import random
from pathlib import Path
import pandas as pd
import requests
from concurrent.futures import ThreadPoolExecutor, as_completed
from eth_account import Account
from eth_account.messages import encode_defunct
from web3 import Web3

from _airdrop_compat import (
    emit_summary,
    env_int,
    is_airdrop,
    project_data_dir,
    specified_wallet_file,
    wallet_mode,
)

Account.enable_unaudited_hdwallet_features()

# =========================================================
# 配置区
# =========================================================

PROJECT_DATA_DIR = project_data_dir("Simple") if is_airdrop() else None
if is_airdrop() and wallet_mode() == "specified":
    ACCOUNTS_EXCEL = str(specified_wallet_file())
else:
    ACCOUNTS_EXCEL = str(PROJECT_DATA_DIR / "simple账号.xlsx") if PROJECT_DATA_DIR else "simple账号.xlsx"
RESULT_EXCEL = str(PROJECT_DATA_DIR / "run_result.xlsx") if PROJECT_DATA_DIR else "run_result.xlsx"

# 是否保存结果文件
SAVE_RESULT_EXCEL = True

API_BASE = "https://task.simplechain.com"

NONCE_URL = f"{API_BASE}/api/v1/get/nonce"
LOGIN_URL = f"{API_BASE}/api/v1/login"

# 任务列表接口
TASK_LIST_URL = f"{API_BASE}/api/v1/task/list"

# 任务提交接口
TASK_COMPLETE_URL = f"{API_BASE}/api/v1/task/complete"

# 每日签到接口
CHECKIN_URL = f"{API_BASE}/api/v1/campaign/checkin"
USER_INFO_URL = f"{API_BASE}/api/v1/user/get/info"

# 每日任务：每天都会重置
DAILY_TASKS = [
    {
        "task_id": "CHECK_IN",
        "task_name": "Daily Check In",
        "task_type": "DAILY_CHECKIN",
        "reward": 60,
    },
    {
        "task_id": "TK-202604-DT-0007",
        "task_name": "Visit Official Website",
        "task_type": "DAILY_BROWSE",
        "reward": 30,
    },
    #{
    #    "task_id": "TK-202604-DT-0008",
     #   "task_name": "Visit Official Website",
      #  "task_type": "DAILY_BROWSE",
    #    "reward": 150,
  #  },
]

# 一次性任务：做过一次后以后只会显示已完成
ONCE_TASKS = [
    {
        "task_id": "TK-202604-CT-0006",
        "task_name": "Check Out the Block Explorer",
        "task_type": "ONCE",
        "reward": 100,
    },
    #{
     #   "task_id": "TK-202604-ST-0025",
      #  "task_name": "One-time Task ST-0025",
     #   "task_type": "ONCE",
    #    "reward": 300,
   # },
    #{
     #   "task_id": "TK-202604-ST-0013",
     #   "task_name": "One-time Task ST-0013",
      #  "task_type": "ONCE",
     #   "reward": 300,
   # },
   # {
      #  "task_id": "TK-202604-ST-0014",
        #"task_name": "One-time Task CT-0009",
       # "task_type": "ONCE",
       # "reward": 200,
    #},
    #{
      #  "task_id": "TK-202604-ST-0015",
       # "task_name": "One-time Task CT-0009",
       # "task_type": "ONCE",
        #"reward": 300,
   # },
    #{
      #  "task_id": "TK-202604-CT-0009",
      #  "task_name": "One-time Task CT-0009",
       # "task_type": "ONCE",
      #  "reward": 1,
    #},
    #{
       # "task_id": "TK-202604-CT-0007",
       # "task_name": "One-time Task CT-0009",
       # "task_type": "ONCE",
       # "reward": 200,
  #  },
]

# 实际需要提交到 /task/complete 的任务
AUTO_COMPLETE_TASKS = [
    t for t in DAILY_TASKS + ONCE_TASKS
    if t["task_id"] != "CHECK_IN"
]
# 每个任务之间的间隔
ACCOUNT_INTERVAL = 2
TASK_INTERVAL = 2
BROWSE_WAIT_SECONDS = 3
# =========================================================
# 多线程配置
# =========================================================
# 建议先从 2 或 3 开始，不要一上来开太大
MAX_WORKERS = 10

# 每个线程启动前随机等待，避免所有账号同一秒请求
THREAD_START_DELAY_MIN = 1
THREAD_START_DELAY_MAX = 5

COMMON_HEADERS = {
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
    "Origin": "https://task.simplechain.com",
    "Referer": "https://task.simplechain.com/",
    "Content-Type": "application/json",
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/120.0.0.0 Safari/537.36"
    ),
}


# =========================================================
# SimpleFlow 链上一次性任务配置（来自 simple_2026_07_12_14_45_56.har）
# =========================================================

SIMPLE_RPC_URL = "https://prod-simple-abroad.qukuaicunzheng.top/rpc/"
SIMPLE_CHAIN_ID = 1913
WRAPPED_SRW_ADDRESS = "0xec1bF294Ea5b3271A87606B51F5465352bc19bE5"
SWAP_ROUTER_ADDRESS = "0x43b06d73dc0ddb9214b28349a913a2b7faafcee8"
POSITION_MANAGER_ADDRESS = "0x6e172ba709487fd0dc47d8a23e128c0328e0646c"
SWAP_RECIPIENT_MSG_SENDER = "0x0000000000000000000000000000000000000001"
POOL_FEE = 3000
TICK_LOWER = -887220
TICK_UPPER = 887220
MAX_UINT256 = 2 ** 256 - 1
AMOUNT_RANDOM_UP_MAX = 0.30

SIMPLE_TOKENS = {
    "MARS": "0xFC12Ae35889A4a6D0b1cE94a6675Ef869F6eb207",
    "MERCURY": "0x8c0c42fD298623d035eeFd8b2783c94069610d2B",
}

ONCHAIN_ONCE_TASKS = [
    {
        "task_id": "TK-202606-CT-0012",
        "task_name": "Token Swap - MARS",
        "task_type": "ONCHAIN_SWAP",
        "action": "swap",
        "symbol": "MARS",
        "reward": 50,
        "amount_in_wei": 5_000_000_000_000_000,
    },
    {
        "task_id": "TK-202606-CT-0011",
        "task_name": "Token Swap - MERCURY",
        "task_type": "ONCHAIN_SWAP",
        "action": "swap",
        "symbol": "MERCURY",
        "reward": 50,
        "amount_in_wei": 5_000_000_000_000_000,
    },
    {
        "task_id": "TK-202606-CT-0014",
        "task_name": "Liquidity Provision - MARS",
        "task_type": "ONCHAIN_LIQUIDITY",
        "action": "liquidity",
        "symbol": "MARS",
        "reward": 30,
        "native_amount_wei": 1_000_000_000_000_000,
        "max_token_amount_wei": 20_000_000_000_000_000,
    },
    {
        "task_id": "TK-202606-CT-0013",
        "task_name": "Liquidity Provision - MERCURY",
        "task_type": "ONCHAIN_LIQUIDITY",
        "action": "liquidity",
        "symbol": "MERCURY",
        "reward": 30,
        "native_amount_wei": 1_000_000_000_000_000,
        "max_token_amount_wei": 20_000_000_000_000_000,
    },
]

ERC20_ABI = [
    {
        "type": "function",
        "name": "balanceOf",
        "stateMutability": "view",
        "inputs": [{"name": "owner", "type": "address"}],
        "outputs": [{"name": "", "type": "uint256"}],
    },
    {
        "type": "function",
        "name": "allowance",
        "stateMutability": "view",
        "inputs": [
            {"name": "owner", "type": "address"},
            {"name": "spender", "type": "address"},
        ],
        "outputs": [{"name": "", "type": "uint256"}],
    },
    {
        "type": "function",
        "name": "approve",
        "stateMutability": "nonpayable",
        "inputs": [
            {"name": "spender", "type": "address"},
            {"name": "amount", "type": "uint256"},
        ],
        "outputs": [{"name": "", "type": "bool"}],
    },
]

SWAP_ROUTER_ABI = [
    {
        "type": "function",
        "name": "exactInputSingle",
        "stateMutability": "payable",
        "inputs": [{
            "name": "params",
            "type": "tuple",
            "components": [
                {"name": "tokenIn", "type": "address"},
                {"name": "tokenOut", "type": "address"},
                {"name": "fee", "type": "uint24"},
                {"name": "recipient", "type": "address"},
                {"name": "amountIn", "type": "uint256"},
                {"name": "amountOutMinimum", "type": "uint256"},
                {"name": "sqrtPriceLimitX96", "type": "uint160"},
            ],
        }],
        "outputs": [{"name": "amountOut", "type": "uint256"}],
    },
    {
        "type": "function",
        "name": "multicall",
        "stateMutability": "payable",
        "inputs": [
            {"name": "deadline", "type": "uint256"},
            {"name": "data", "type": "bytes[]"},
        ],
        "outputs": [{"name": "results", "type": "bytes[]"}],
    },
]

POSITION_MANAGER_ABI = [
    {
        "type": "function",
        "name": "mint",
        "stateMutability": "payable",
        "inputs": [{
            "name": "params",
            "type": "tuple",
            "components": [
                {"name": "token0", "type": "address"},
                {"name": "token1", "type": "address"},
                {"name": "fee", "type": "uint24"},
                {"name": "tickLower", "type": "int24"},
                {"name": "tickUpper", "type": "int24"},
                {"name": "amount0Desired", "type": "uint256"},
                {"name": "amount1Desired", "type": "uint256"},
                {"name": "amount0Min", "type": "uint256"},
                {"name": "amount1Min", "type": "uint256"},
                {"name": "recipient", "type": "address"},
                {"name": "deadline", "type": "uint256"},
            ],
        }],
        "outputs": [
            {"name": "tokenId", "type": "uint256"},
            {"name": "liquidity", "type": "uint128"},
            {"name": "amount0", "type": "uint256"},
            {"name": "amount1", "type": "uint256"},
        ],
    },
    {
        "type": "function",
        "name": "refundETH",
        "stateMutability": "payable",
        "inputs": [],
        "outputs": [],
    },
    {
        "type": "function",
        "name": "multicall",
        "stateMutability": "payable",
        "inputs": [{"name": "data", "type": "bytes[]"}],
        "outputs": [{"name": "results", "type": "bytes[]"}],
    },
]


def randomized_wei(base_wei, rng=None):
    rng = rng or random.random
    return int(base_wei * (1 + AMOUNT_RANDOM_UP_MAX * rng()))


def get_tasks_for_mode(run_mode: str):
    run_mode = str(run_mode or "1").strip()
    if run_mode == "2":
        return {"daily": [], "onchain_once": ONCHAIN_ONCE_TASKS}
    return {"daily": DAILY_TASKS, "onchain_once": []}


def choose_run_mode():
    if is_airdrop():
        choice = os.environ.get("SIMPLE_MODE", "1").strip()
        return "2" if choice == "2" else "1"
    print("\n请选择运行模式：")
    print("  1 = 日常两个签到任务")
    print("  2 = 一次性链上任务（2 次兑换 + 2 次加池）")
    choice = input("请输入模式编号（默认 1）：").strip()
    if choice == "2":
        return "2"
    return "1"
# =========================================================
# Excel 工具
# =========================================================

def create_accounts_template():
    if is_airdrop() and wallet_mode() == "specified":
        if not os.path.isfile(ACCOUNTS_EXCEL):
            raise FileNotFoundError(f"指定钱包文件不存在：{ACCOUNTS_EXCEL}")
        return

    if is_airdrop():
        if os.path.exists(ACCOUNTS_EXCEL):
            append_count = env_int(
                "AIRDROP_APPEND_WALLET_COUNT",
                "SIMPLE_APPEND_WALLET_COUNT",
                default=0,
                minimum=0,
            )
            if append_count:
                existing = pd.read_excel(ACCOUNTS_EXCEL)
                rows = []
                for index in range(append_count):
                    account = Account.create()
                    rows.append(
                        {
                            "name": f"wallet_{len(existing) + index + 1}",
                            "private_key": account.key.hex(),
                            "account_type": "auto",
                            "invite_code": "",
                            "api_key": "",
                            "checkin_task_id": "",
                        }
                    )
                pd.concat([existing, pd.DataFrame(rows)], ignore_index=True).to_excel(
                    ACCOUNTS_EXCEL,
                    index=False,
                )
            return

        count = env_int("AIRDROP_WALLET_COUNT", "SIMPLE_WALLET_COUNT", default=1, minimum=1)
        rows = []
        for index in range(count):
            account = Account.create()
            rows.append(
                {
                    "name": f"wallet_{index + 1}",
                    "private_key": account.key.hex(),
                    "account_type": "auto",
                    "invite_code": "",
                    "api_key": "",
                    "checkin_task_id": "",
                }
            )
        pd.DataFrame(rows).to_excel(ACCOUNTS_EXCEL, index=False)
        print(f"[+] 已创建 {count} 个持久化钱包：{ACCOUNTS_EXCEL}")
        return

    if os.path.exists(ACCOUNTS_EXCEL):
        return

    df = pd.DataFrame([
        {
            "name": "wallet1",
            "private_key": "0x你的私钥",
            "account_type": "auto",
            "invite_code": "",
            "api_key": "",
            "checkin_task_id": ""
        }
    ])
    df.to_excel(ACCOUNTS_EXCEL, index=False)

    print(f"[!] 未找到 {ACCOUNTS_EXCEL}，已自动创建模板。")
    print("[!] 请打开 simple账号.xlsx 填入 private_key / account_type / invite_code 后重新运行。")

def clean_cell(value):
    if pd.isna(value):
        return ""
    value = str(value).strip()
    if value.lower() == "nan":
        return ""
    return value


def load_accounts_from_excel():
    create_accounts_template()

    df = pd.read_excel(ACCOUNTS_EXCEL)

    required_columns = ["name", "private_key"]
    for col in required_columns:
        if col not in df.columns:
            raise ValueError(f"Excel 缺少必要列: {col}")

    accounts = []

    for index, row in df.iterrows():
        name = clean_cell(row.get("name", ""))
        private_key = clean_cell(row.get("private_key", ""))

        # 新增：账号类型 old / new / auto
        account_type = clean_cell(row.get("account_type", "auto")) if "account_type" in df.columns else "auto"
        account_type = account_type.lower().strip()

        if account_type not in ["old", "new", "auto"]:
            print(f"[!] 第 {index + 2} 行 account_type 填写不正确，已自动改为 auto。")
            account_type = "auto"

        # 新增：邀请码
        invite_code = clean_cell(row.get("invite_code", "")) if "invite_code" in df.columns else ""

        api_key = clean_cell(row.get("api_key", "")) if "api_key" in df.columns else ""
        checkin_task_id = clean_cell(row.get("checkin_task_id", "")) if "checkin_task_id" in df.columns else ""

        if not private_key:
            print(f"[-] 第 {index + 2} 行 private_key 为空，已跳过。")
            continue

        if not private_key.startswith("0x"):
            private_key = "0x" + private_key

        accounts.append({
            "name": name or f"wallet_{index + 1}",
            "private_key": private_key,
            "account_type": account_type,
            "invite_code": invite_code,
            "api_key": api_key,
            "checkin_task_id": checkin_task_id,
        })

    return accounts

def save_results(results):
    df = pd.DataFrame(results)
    df.to_excel(RESULT_EXCEL, index=False)
    print(f"\n[+] 执行结果已保存到: {RESULT_EXCEL}")


# =========================================================
# 通用请求工具
# =========================================================

def safe_json_response(response):
    try:
        return response.json()
    except Exception:
        return {
            "code": -1,
            "message": "响应不是 JSON",
            "raw_text": response.text[:500]
        }


def normalize_signature(signature: str):
    if not signature.startswith("0x"):
        signature = "0x" + signature
    return signature


def make_auth_headers(auth_token: str):
    headers = COMMON_HEADERS.copy()
    headers["authorization"] = auth_token
    headers["Authorization"] = auth_token
    return headers


def is_success_response(data):
    return isinstance(data, dict) and data.get("code") == 0


def is_already_completed_response(data):
    if not isinstance(data, dict):
        return False

    text = json.dumps(data, ensure_ascii=False).lower()

    return (
        data.get("code") == 20101
        or data.get("reason") == "TASK_ALREADY_COMPLETED"
        or "task already completed" in text
        or "already completed" in text
        or "已完成" in text
        or "已签到" in text
    )

def is_retryable_onchain_verification_failure(data):
    if not isinstance(data, dict):
        return False
    text = json.dumps(data, ensure_ascii=False).lower()
    return (
        data.get("reason") == "TASK_VERIFICATION_FAILED"
        and "thegraph" in text
        and "database unavailable" in text
    )
def fetch_user_info(auth_token: str):
    """
    获取主页用户信息：
    totalPoints / availablePoints / completedTasks / totalTasks / levelInfo / 绑定状态等
    """
    headers = make_auth_headers(auth_token)

    try:
        res = requests.get(
            USER_INFO_URL,
            headers=headers,
            timeout=20
        )
        data = safe_json_response(res)

        if not is_success_response(data):
            print(f"[-] 获取 user info 失败: {data}")
            return {}

        user_info = data.get("data", {}) or {}
        return user_info

    except Exception as e:
        print(f"[!] 获取 user info 异常: {e}")
        return {}

def fetch_task_list(auth_token: str):
    """
    获取任务列表，用来统计每个账号的总任务完成情况，例如 4/10。
    """
    headers = make_auth_headers(auth_token)

    try:
        res = requests.get(
            TASK_LIST_URL,
            headers=headers,
            timeout=20
        )
        data = safe_json_response(res)

        if not is_success_response(data):
            print(f"[-] 获取 task/list 失败: {data}")
            return []

        tasks = data.get("data", {}).get("tasks", [])
        if not isinstance(tasks, list):
            return []

        return tasks

    except Exception as e:
        print(f"[!] 获取 task/list 异常: {e}")
        return []


def analyze_task_progress(tasks: list):
    """
    统计任务列表完成情况：
    completed_count / total_count
    """
    total_count = 0
    completed_count = 0
    completed_tasks = []
    not_completed_tasks = []

    completed_statuses = [
        "COMPLETED",
        "COMPLETED_TODAY",
        "CLAIMED",
    ]

    for task in tasks:
        task_id = task.get("taskId", "")
        task_name = task.get("taskName", "")
        task_code = task.get("taskCode", "")
        task_type = task.get("taskType", "")
        status = task.get("completionStatus", "")
        reward = task.get("rewardPoints", 0)

        total_count += 1

        item = {
            "task_id": task_id,
            "task_name": task_name,
            "task_code": task_code,
            "task_type": task_type,
            "status": status,
            "reward": reward,
        }

        if status in completed_statuses:
            completed_count += 1
            completed_tasks.append(item)
        else:
            not_completed_tasks.append(item)

    return {
        "total_count": total_count,
        "completed_count": completed_count,
        "not_completed_count": total_count - completed_count,
        "completed_tasks": completed_tasks,
        "not_completed_tasks": not_completed_tasks,
        "progress_text": f"{completed_count}/{total_count}",
    }

# =========================================================
# 登录逻辑
# =========================================================

def login_account(private_key: str, account_name: str = "", account_type: str = "auto", invite_code: str = ""):
    """
    兼容老号 / 新号登录：

    account_type = old
        老号模式：不传 inviteCode

    account_type = new
        新号模式：必须传 inviteCode

    account_type = auto
        自动模式：先不传 inviteCode 登录
        如果失败，再带 inviteCode 重试
    """
    session = requests.Session()
    session.headers.update(COMMON_HEADERS)

    try:
        account = Account.from_key(private_key)
        address = account.address
    except Exception as e:
        return {
            "success": False,
            "address": "",
            "token": "",
            "login_mode": "",
            "error": f"私钥解析失败: {e}",
            "server_response": ""
        }

    print("\n" + "=" * 60)
    print(f"[*] 当前账号: {account_name}")
    print(f"[*] 当前钱包: {address}")
    print(f"[*] 账号类型: {account_type}")
    print(f"[*] 邀请码: {invite_code if invite_code else '未填写'}")

    # 1. 获取 nonce
    print("[*] 正在获取动态 Nonce...")

    try:
        nonce_res = session.post(
            NONCE_URL,
            json={"address": address},
            timeout=20
        )
        nonce_data = safe_json_response(nonce_res)

        if nonce_res.status_code != 200 or nonce_data.get("code") != 0:
            print(f"[!] 获取 Nonce 失败: {nonce_data}")
            return {
                "success": False,
                "address": address,
                "token": "",
                "login_mode": "",
                "error": "获取 Nonce 失败",
                "server_response": json.dumps(nonce_data, ensure_ascii=False)
            }

        message_text = nonce_data.get("data", {}).get("message")

        if not message_text:
            print(f"[!] Nonce 返回缺少 message: {nonce_data}")
            return {
                "success": False,
                "address": address,
                "token": "",
                "login_mode": "",
                "error": "Nonce 返回缺少 message",
                "server_response": json.dumps(nonce_data, ensure_ascii=False)
            }

        print("[+] 成功拿到 Nonce 签名模板。")

    except Exception as e:
        print(f"[!] Nonce 接口异常: {e}")
        return {
            "success": False,
            "address": address,
            "token": "",
            "login_mode": "",
            "error": f"Nonce 接口异常: {e}",
            "server_response": ""
        }

    # 2. 本地签名
    print("[*] 正在本地签名...")

    try:
        signable_message = encode_defunct(text=message_text)
        signed_message = Account.sign_message(
            signable_message,
            private_key=private_key
        )

        signature = normalize_signature(signed_message.signature.hex())

        print("[+] 签名完成。")
        print(f"[*] signature 长度: {len(signature)}")

    except Exception as e:
        print(f"[!] 签名失败: {e}")
        return {
            "success": False,
            "address": address,
            "token": "",
            "login_mode": "",
            "error": f"签名失败: {e}",
            "server_response": ""
        }

    def submit_login(payload: dict, mode_name: str):
        print(f"[*] 正在发送登录请求：{mode_name}")

        try:
            login_res = session.post(
                LOGIN_URL,
                json=payload,
                timeout=20
            )
            login_data = safe_json_response(login_res)

            if login_res.status_code == 200 and login_data.get("code") == 0:
                token = login_data.get("data", {}).get("token", "")

                if token and not token.startswith("Bearer "):
                    token = f"Bearer {token}"

                print(f"[+] 登录成功。模式: {mode_name}")

                return {
                    "success": True,
                    "address": address,
                    "token": token,
                    "login_mode": mode_name,
                    "error": "",
                    "server_response": json.dumps(login_data, ensure_ascii=False)
                }

            print(f"[-] 登录未通过。模式: {mode_name}")
            print(f"[*] HTTP 状态码: {login_res.status_code}")
            print(f"[*] 服务端返回: {login_data}")

            return {
                "success": False,
                "address": address,
                "token": "",
                "login_mode": mode_name,
                "error": "登录失败",
                "server_response": json.dumps(login_data, ensure_ascii=False)
            }

        except Exception as e:
            print(f"[!] 登录接口异常。模式: {mode_name} | {e}")
            return {
                "success": False,
                "address": address,
                "token": "",
                "login_mode": mode_name,
                "error": f"登录接口异常: {e}",
                "server_response": ""
            }

    # 老号 payload：不传 inviteCode
    old_payload = {
        "address": address,
        "message": message_text,
        "signature": signature
    }

    # 新号 payload：传 inviteCode
    new_payload = {
        "address": address,
        "inviteCode": invite_code,
        "message": message_text,
        "signature": signature
    }

    account_type = (account_type or "auto").lower().strip()

    # old：只走老号
    if account_type == "old":
        print("[*] 当前设置为 old：老号登录，不传 inviteCode。")
        return submit_login(old_payload, "old_no_invite")

    # new：只走新号
    if account_type == "new":
        if not invite_code:
            print("[!] 当前账号设置为 new，但是 invite_code 为空。")
            return {
                "success": False,
                "address": address,
                "token": "",
                "login_mode": "new_with_invite",
                "error": "新号缺少 invite_code",
                "server_response": ""
            }

        print("[*] 当前设置为 new：新号登录，传 inviteCode。")
        return submit_login(new_payload, "new_with_invite")

    # auto：先老号，失败再新号
    print("[*] 当前设置为 auto：先尝试老号不传 inviteCode。")
    old_result = submit_login(old_payload, "auto_old_no_invite")

    if old_result.get("success"):
        return old_result

    print("[=] 老号模式未通过，准备尝试新号邀请码模式。")

    if not invite_code:
        print("[!] auto 模式下没有 invite_code，无法继续尝试新号模式。")
        old_result["error"] = "老号登录失败，且未填写 invite_code，无法尝试新号注册/登录"
        return old_result

    print("[*] auto 模式：尝试新号传 inviteCode。")
    new_result = submit_login(new_payload, "auto_new_with_invite")

    if new_result.get("success"):
        return new_result

    return new_result


# =========================================================
# 任务列表逻辑
# =========================================================

def fetch_task_list(auth_token: str):
    """
    获取任务列表。
    """
    headers = make_auth_headers(auth_token)

    print("\n[*] 正在获取任务列表...")

    try:
        res = requests.get(
            TASK_LIST_URL,
            headers=headers,
            timeout=20
        )
        data = safe_json_response(res)

        if is_success_response(data):
            tasks = data.get("data", {}).get("tasks", [])
            print(f"[+] 成功获取任务列表，共 {len(tasks)} 个任务。")
            return tasks

        print(f"[-] 获取任务列表失败: {data}")
        return []

    except Exception as e:
        print(f"[!] 获取任务列表异常: {e}")
        return []


def analyze_tasks(tasks):
    """
    分析任务列表：
    - 识别签到任务
    - 识别浏览任务
    - 统计今日已完成积分
    - 统计待完成积分
    """
    summary = {
        "all_tasks_count": len(tasks),

        "daily_checkin": None,
        "daily_browse_tasks": [],

        "completed_today_tasks": [],
        "completed_once_tasks": [],
        "not_started_tasks": [],
        "available_tasks": [],

        "completed_today_points": 0,
        "completed_total_visible_points": 0,
        "not_started_points": 0,
        "available_points": 0,
    }

    for task in tasks:
        task_name = task.get("taskName", "")
        task_code = task.get("taskCode", "")
        completion_status = task.get("completionStatus", "")
        reward_points = task.get("rewardPoints", 0) or 0

        # 今日已完成
        if completion_status == "COMPLETED_TODAY":
            summary["completed_today_tasks"].append(task)
            summary["completed_today_points"] += reward_points
            summary["completed_total_visible_points"] += reward_points

        # 一次性任务已完成
        if completion_status in ["COMPLETED", "CLAIMED"]:
            summary["completed_once_tasks"].append(task)
            summary["completed_total_visible_points"] += reward_points

        # 未开始
        if completion_status == "NOT_STARTED":
            summary["not_started_tasks"].append(task)
            summary["not_started_points"] += reward_points

        # 可用
        if completion_status in ["AVAILABLE", "NOT_STARTED"]:
            summary["available_tasks"].append(task)
            summary["available_points"] += reward_points

        # 每日签到
        if task_code == "DAILY_CHECK_IN":
            summary["daily_checkin"] = task

        # 每日浏览/访问链接任务
        if (
            task_code == "ACCESS_LINK"
            or "visit" in task_name.lower()
            or "website" in task_name.lower()
        ):
            summary["daily_browse_tasks"].append(task)

    return summary


def print_task_summary(task_summary):
    """
    打印当前账号任务摘要。
    """
    print("\n[*] 当前账号任务摘要：")
    print(f"    任务总数: {task_summary['all_tasks_count']}")
    print(f"    今日已完成任务数: {len(task_summary['completed_today_tasks'])}")
    print(f"    未开始任务数: {len(task_summary['not_started_tasks'])}")
    print(f"    可用/未开始任务潜在积分: {task_summary['available_points']}")
    print(f"    今日已完成任务积分: {task_summary['completed_today_points']}")

    daily_checkin = task_summary.get("daily_checkin")
    if daily_checkin:
        print("    每日签到:")
        print(f"      名称: {daily_checkin.get('taskName')}")
        print(f"      状态: {daily_checkin.get('completionStatus')}")
        print(f"      奖励: {daily_checkin.get('rewardPoints')}")
        print(f"      下次可用: {daily_checkin.get('nextAvailableText')}")
        print(f"      taskId: {daily_checkin.get('taskId')}")

    if task_summary.get("daily_browse_tasks"):
        print("    浏览/访问任务:")
        for t in task_summary["daily_browse_tasks"]:
            print(
                f"      - {t.get('taskName')} | "
                f"状态: {t.get('completionStatus')} | "
                f"奖励: {t.get('rewardPoints')} | "
                f"下次可用: {t.get('nextAvailableText')} | "
                f"taskId: {t.get('taskId')}"
            )


# =========================================================
# 签到逻辑
# =========================================================

def do_daily_checkin(auth_token: str, checkin_task_id: str = ""):
    """
    SimpleChain Check In：
    接口：/api/v1/campaign/checkin
    Payload：空 {}
    TASK_ALREADY_COMPLETED 视为已完成，不算失败。
    """
    headers = make_auth_headers(auth_token)

    print("\n[*] 开始执行 Check In...")
    print(f"[*] Check In URL: {CHECKIN_URL}")

    # 方式 A：POST json={}
    try:
        print("[*] 尝试方式 A：POST json={} ...")

        res = requests.post(
            CHECKIN_URL,
            json={},
            headers=headers,
            timeout=20
        )

        data = safe_json_response(res)

        if is_success_response(data):
            print("[+] Check In 成功。")
            return {
                "success": True,
                "status": "success",
                "method": "POST json={}",
                "response": json.dumps(data, ensure_ascii=False)
            }

        if is_already_completed_response(data):
            print("[=] 今日已经 Check In，跳过。")
            return {
                "success": True,
                "status": "already_completed",
                "method": "POST json={}",
                "response": json.dumps(data, ensure_ascii=False)
            }

        print(f"[-] 方式 A 未成功: {data}")

    except Exception as e:
        print(f"[!] 方式 A 请求异常: {e}")

    # 方式 B：POST 无 body
    try:
        print("[*] 尝试方式 B：POST 无 body ...")

        res = requests.post(
            CHECKIN_URL,
            headers=headers,
            timeout=20
        )

        data = safe_json_response(res)

        if is_success_response(data):
            print("[+] Check In 成功。")
            return {
                "success": True,
                "status": "success",
                "method": "POST no body",
                "response": json.dumps(data, ensure_ascii=False)
            }

        if is_already_completed_response(data):
            print("[=] 今日已经 Check In，跳过。")
            return {
                "success": True,
                "status": "already_completed",
                "method": "POST no body",
                "response": json.dumps(data, ensure_ascii=False)
            }

        print(f"[-] 方式 B 未成功: {data}")

        return {
            "success": False,
            "status": "failed",
            "method": "campaign_checkin",
            "response": json.dumps(data, ensure_ascii=False)
        }

    except Exception as e:
        print(f"[!] 方式 B 请求异常: {e}")

        return {
            "success": False,
            "status": "exception",
            "method": "campaign_checkin",
            "response": str(e)
        }


# =========================================================
# 浏览任务逻辑
# =========================================================

def do_auto_complete_tasks(auth_token: str, tasks: list):
    headers = make_auth_headers(auth_token)

    print("\n[*] 开始执行自动提交任务...")

    if not tasks:
        print("[*] 自动任务列表为空，跳过。")
        return {
            "success_count": 0,
            "already_count": 0,
            "fail_count": 0,
            "reward_total": 0,
            "details": []
        }

    success_count = 0
    already_count = 0
    fail_count = 0
    reward_total = 0
    details = []

    for task in tasks:
        task_id = task.get("task_id", "")
        task_name = task.get("task_name", "")
        task_type = task.get("task_type", "")
        expect_reward = task.get("reward", 0)

        print(f"\n[*] 自动任务: {task_name}")
        print(f"[*] TaskID: {task_id}")
        print(f"[*] 类型: {task_type}")
        print(f"[*] 正在模拟停留 {BROWSE_WAIT_SECONDS} 秒...")
        time.sleep(BROWSE_WAIT_SECONDS)

        try:
            res = requests.post(
                TASK_COMPLETE_URL,
                json={"taskId": task_id},
                headers=headers,
                timeout=20
            )
            data = safe_json_response(res)

            if is_success_response(data):
                response_data = data.get("data", {}) or {}

                reward = response_data.get("rewardPoints", expect_reward) or 0
                total = response_data.get("totalPoints", "")

                try:
                    reward_int = int(reward)
                except Exception:
                    reward_int = 0

                reward_total += reward_int
                success_count += 1

                real_task_name = response_data.get("taskName", task_name)
                real_task_code = response_data.get("taskCode", "")

                print(f"[+] 自动任务完成: {real_task_name}")
                print(f"    taskCode: {real_task_code}")
                print(f"    获得积分: {reward}")
                print(f"    接口返回总积分: {total if total != '' else '未知'}")

                details.append({
                    "task_id": task_id,
                    "task_name": real_task_name,
                    "task_type": task_type,
                    "task_code": real_task_code,
                    "status": "success",
                    "success": True,
                    "reward": reward_int,
                    "total_points": total,
                    "response": json.dumps(data, ensure_ascii=False)
                })

            elif is_already_completed_response(data):
                already_count += 1

                print(f"[=] 自动任务已完成，跳过: {task_name} | {task_id}")

                details.append({
                    "task_id": task_id,
                    "task_name": task_name,
                    "task_type": task_type,
                    "task_code": "",
                    "status": "already_completed",
                    "success": True,
                    "reward": 0,
                    "total_points": "",
                    "response": json.dumps(data, ensure_ascii=False)
                })

            else:
                fail_count += 1
                status = "failed"

                if is_retryable_onchain_verification_failure(data):
                    status = "retryable_verification_failed"
                    print(f"[-] 链上任务验证暂时失败: {task_name} | {task_id}")
                    print("    链上交易可能已完成，但后端 The Graph 数据库暂时不可用，稍后重试模式 2 验证即可。")
                else:
                    print(f"[-] 自动任务失败: {task_name} | {task_id}")

                print(f"    返回: {data}")

                details.append({
                    "task_id": task_id,
                    "task_name": task_name,
                    "task_type": task_type,
                    "task_code": "",
                    "status": status,
                    "success": False,
                    "reward": 0,
                    "total_points": "",
                    "response": json.dumps(data, ensure_ascii=False)
                })

        except Exception as e:
            fail_count += 1

            print(f"[!] 自动任务请求异常: {task_name} | {e}")

            details.append({
                "task_id": task_id,
                "task_name": task_name,
                "task_type": task_type,
                "task_code": "",
                "status": "exception",
                "success": False,
                "reward": 0,
                "total_points": "",
                "response": str(e)
            })

        time.sleep(TASK_INTERVAL)

    return {
        "success_count": success_count,
        "already_count": already_count,
        "fail_count": fail_count,
        "reward_total": reward_total,
        "details": details
    }
# =========================================================
# SimpleFlow 链上一次性任务逻辑
# =========================================================

def raw_tx_bytes(signed_tx):
    return getattr(signed_tx, "raw_transaction", None) or getattr(signed_tx, "rawTransaction", None)


def make_simple_web3():
    w3 = Web3(Web3.HTTPProvider(SIMPLE_RPC_URL, request_kwargs={"timeout": 30}))
    if not w3.is_connected():
        raise RuntimeError(f"无法连接 SimpleChain RPC: {SIMPLE_RPC_URL}")
    return w3


def checksum(w3, address):
    return w3.to_checksum_address(address)


def hex_bytes(data):
    return bytes.fromhex(data[2:] if data.startswith("0x") else data)


def send_simple_tx(w3, account, tx_func, value=0):
    address = account.address
    gas_price = w3.eth.gas_price
    tx = tx_func.build_transaction({
        "from": address,
        "value": value,
        "nonce": w3.eth.get_transaction_count(address, "pending"),
        "chainId": SIMPLE_CHAIN_ID,
        "maxFeePerGas": gas_price,
        "maxPriorityFeePerGas": gas_price,
    })
    tx.pop("gas", None)
    tx["gas"] = int(w3.eth.estimate_gas(tx) * 1.25)

    signed = account.sign_transaction(tx)
    tx_hash = w3.eth.send_raw_transaction(raw_tx_bytes(signed))
    tx_hash_hex = tx_hash.hex()
    if not tx_hash_hex.startswith("0x"):
        tx_hash_hex = "0x" + tx_hash_hex

    print(f"[+] 已广播交易: {tx_hash_hex}")
    receipt = w3.eth.wait_for_transaction_receipt(tx_hash, timeout=240, poll_latency=2)
    if receipt.status != 1:
        raise RuntimeError(f"交易回滚: {tx_hash_hex}")
    print(f"[+] 链上确认: block={receipt.blockNumber} gas={receipt.gasUsed}")
    return tx_hash_hex


def erc20_contract(w3, token_address):
    return w3.eth.contract(address=checksum(w3, token_address), abi=ERC20_ABI)


def ensure_approval(w3, account, token_address, spender, needed_amount):
    token = erc20_contract(w3, token_address)
    owner = account.address
    spender = checksum(w3, spender)
    allowance = token.functions.allowance(owner, spender).call()

    if allowance >= needed_amount:
        print("[=] 授权额度足够，跳过 approve。")
        return ""

    print("[*] 授权 Position Manager 使用代币...")
    return send_simple_tx(
        w3=w3,
        account=account,
        tx_func=token.functions.approve(spender, MAX_UINT256),
        value=0,
    )


def do_swap_token(w3, account, symbol, amount_in_wei):
    amount_in_wei = randomized_wei(amount_in_wei)
    token_out = checksum(w3, SIMPLE_TOKENS[symbol])
    router = w3.eth.contract(address=checksum(w3, SWAP_ROUTER_ADDRESS), abi=SWAP_ROUTER_ABI)
    deadline = int(time.time()) + 1200

    exact_data = router.functions.exactInputSingle((
        checksum(w3, WRAPPED_SRW_ADDRESS),
        token_out,
        POOL_FEE,
        checksum(w3, SWAP_RECIPIENT_MSG_SENDER),
        amount_in_wei,
        0,
        0,
    ))._encode_transaction_data()

    print(f"[*] 执行兑换: SRW -> {symbol} | amountInWei={amount_in_wei}")
    return send_simple_tx(
        w3=w3,
        account=account,
        tx_func=router.functions.multicall(deadline, [hex_bytes(exact_data)]),
        value=amount_in_wei,
    )


def do_add_liquidity(w3, account, symbol, native_amount_wei, max_token_amount_wei=None):
    native_amount_wei = randomized_wei(native_amount_wei)
    if max_token_amount_wei:
        max_token_amount_wei = randomized_wei(max_token_amount_wei)
    token_address = checksum(w3, SIMPLE_TOKENS[symbol])
    wrapped_srw = checksum(w3, WRAPPED_SRW_ADDRESS)
    position_manager = w3.eth.contract(
        address=checksum(w3, POSITION_MANAGER_ADDRESS),
        abi=POSITION_MANAGER_ABI,
    )
    token = erc20_contract(w3, token_address)
    token_balance = token.functions.balanceOf(account.address).call()
    token_amount_wei = min(token_balance, max_token_amount_wei or token_balance)

    if token_amount_wei <= 0:
        raise RuntimeError(f"{symbol} 余额为 0，无法加池；请先完成兑换。")

    approve_hash = ensure_approval(
        w3=w3,
        account=account,
        token_address=token_address,
        spender=POSITION_MANAGER_ADDRESS,
        needed_amount=token_amount_wei,
    )
    if approve_hash:
        time.sleep(2)

    token0, token1 = sorted([wrapped_srw, token_address], key=lambda x: int(x, 16))
    if token0.lower() == wrapped_srw.lower():
        amount0_desired = native_amount_wei
        amount1_desired = token_amount_wei
    else:
        amount0_desired = token_amount_wei
        amount1_desired = native_amount_wei

    deadline = int(time.time()) + 1200
    mint_data = position_manager.functions.mint((
        token0,
        token1,
        POOL_FEE,
        TICK_LOWER,
        TICK_UPPER,
        amount0_desired,
        amount1_desired,
        0,
        0,
        account.address,
        deadline,
    ))._encode_transaction_data()
    refund_data = position_manager.functions.refundETH()._encode_transaction_data()

    print(f"[*] 执行加池: SRW + {symbol} | nativeWei={native_amount_wei} | tokenWei={token_amount_wei}")
    lp_hash = send_simple_tx(
        w3=w3,
        account=account,
        tx_func=position_manager.functions.multicall([hex_bytes(mint_data), hex_bytes(refund_data)]),
        value=native_amount_wei,
    )
    return {"approve_tx": approve_hash, "lp_tx": lp_hash}


def task_completed_in_list(task_list, task_id):
    completed_statuses = {"COMPLETED", "COMPLETED_TODAY", "CLAIMED"}
    for task in task_list or []:
        if task.get("taskId") == task_id:
            return task.get("completionStatus") in completed_statuses
    return False


def execute_onchain_once_actions(private_key, tasks, task_list):
    if not tasks:
        return []

    w3 = make_simple_web3()
    account = Account.from_key(private_key)
    details = []

    print(f"[*] SimpleChain RPC 已连接，chainId={w3.eth.chain_id}")

    for task in tasks:
        task_id = task.get("task_id", "")
        task_name = task.get("task_name", "")
        symbol = task.get("symbol", "")
        action = task.get("action", "")

        if task_completed_in_list(task_list, task_id):
            print(f"[=] 链上任务已完成，跳过链上动作: {task_name} | {task_id}")
            details.append({
                "stage": "chain",
                "task_id": task_id,
                "task_name": task_name,
                "status": "already_completed_by_task_list",
                "success": True,
                "tx_hash": "",
            })
            continue

        try:
            if action == "swap":
                tx_hash = do_swap_token(w3, account, symbol, task["amount_in_wei"])
                details.append({
                    "stage": "chain",
                    "task_id": task_id,
                    "task_name": task_name,
                    "status": "success",
                    "success": True,
                    "tx_hash": tx_hash,
                })
            elif action == "liquidity":
                txs = do_add_liquidity(w3, account, symbol, task["native_amount_wei"], task.get("max_token_amount_wei"))
                details.append({
                    "stage": "chain",
                    "task_id": task_id,
                    "task_name": task_name,
                    "status": "success",
                    "success": True,
                    "tx_hash": txs.get("lp_tx", ""),
                    "approve_tx": txs.get("approve_tx", ""),
                })
            else:
                raise RuntimeError(f"未知链上动作: {action}")
        except Exception as e:
            print(f"[-] 链上任务失败: {task_name} | {e}")
            details.append({
                "stage": "chain",
                "task_id": task_id,
                "task_name": task_name,
                "status": "exception",
                "success": False,
                "tx_hash": "",
                "response": str(e),
            })

        time.sleep(TASK_INTERVAL)

    return details


def do_onchain_once_tasks(auth_token, private_key, tasks, task_list):
    chain_details = execute_onchain_once_actions(private_key, tasks, task_list)
    verify_result = do_auto_complete_tasks(auth_token=auth_token, tasks=tasks)
    verify_details = verify_result.get("details", [])
    for item in verify_details:
        item["stage"] = "verify"
    verify_result["details"] = chain_details + verify_details
    return verify_result
# =========================================================
# 最终总结
# =========================================================
def safe_load_json_list(value):
    """
    安全解析 JSON 字符串列表。
    daily_task_details / once_task_details / task_list_xxx_details 都是 JSON 字符串。
    """
    if not value:
        return []

    if isinstance(value, list):
        return value

    try:
        data = json.loads(value)
        if isinstance(data, list):
            return data
        return []
    except Exception:
        return []


def format_task_status_text(status):
    """
    把接口状态转成更容易看的中文。
    """
    status = str(status or "").strip()

    status_map = {
        "success": "新完成",
        "already_completed": "已完成",
        "failed": "失败",
        "exception": "异常",
        "COMPLETED": "已完成",
        "COMPLETED_TODAY": "今日已完成",
        "retryable_verification_failed": "索引失败待重试",
        "CLAIMED": "已领取",
        "NOT_STARTED": "未开始",
        "AVAILABLE": "可做",
    }

    return status_map.get(status, status or "未知")


def print_task_detail_block(title, details):
    """
    打印任务明细。
    """
    print(f"    {title}:")

    if not details:
        print("      - 无")
        return

    for task in details:
        task_id = (
            task.get("task_id")
            or task.get("taskId")
            or "-"
        )

        task_name = (
            task.get("task_name")
            or task.get("taskName")
            or "-"
        )

        status = (
            task.get("status")
            or task.get("completionStatus")
            or "-"
        )

        reward = (
            task.get("reward")
            if task.get("reward") not in [None, ""]
            else task.get("rewardPoints", 0)
        )

        print(
            f"      - {task_name} | "
            f"ID: {task_id} | "
            f"状态: {format_task_status_text(status)} | "
            f"积分: {reward}"
        )


def print_final_summary(final_results):
    print("\n每个账号总览：")
    print("-" * 150)
    print("序号 | 账号 | 钱包 | 登录 | 每日任务 | 一次性任务 | 总任务 | 总积分 | 可用积分 | 等级 | 绑定状态")
    print("-" * 150)

    for i, r in enumerate(final_results, start=1):
        name = r.get("name", "")
        address = r.get("address", "")

        if address and len(address) > 12:
            short_address = address[:6] + "..." + address[-4:]
        else:
            short_address = address or "-"

        login_text = "成功" if r.get("login_success") else "失败"

        daily_progress = r.get("daily_task_progress", "0/0")
        once_progress = r.get("once_task_progress", "0/0")

        # 优先使用 /api/v1/user/get/info 返回的真实任务进度
        total_progress = (
            r.get("real_task_progress")
            or r.get("task_list_progress")
            or "未知"
        )

        total_points = r.get("total_points", "未知")
        available_points = r.get("available_points_real", "未知")
        level_name = r.get("level_name", "未知")

        bind_text = (
            f"X:{'Y' if r.get('is_bind_twitter') else 'N'} "
            f"DC:{'Y' if r.get('is_bind_discord') else 'N'} "
            f"TG:{'Y' if r.get('is_bind_telegram') else 'N'}"
        )

        print(
            f"{i} | "
            f"{name} | "
            f"{short_address} | "
            f"{login_text} | "
            f"每日 {daily_progress} | "
            f"一次性 {once_progress} | "
            f"总任务 {total_progress} | "
            f"总积分 {total_points} | "
            f"可用 {available_points} | "
            f"{level_name} | "
            f"{bind_text}"
        )

    print("-" * 150)
def process_one_account(idx: int, total: int, item: dict, run_mode: str = "1"):
    """
    单个账号的完整执行流程：
    登录 -> 获取任务列表 -> Check In -> 每日任务 -> 一次性任务 -> task/list 统计
    """

    # 每个线程启动前随机等待，避免同时打接口
    start_delay = random.uniform(THREAD_START_DELAY_MIN, THREAD_START_DELAY_MAX)
    time.sleep(start_delay)

    name = item["name"]
    private_key = item["private_key"]
    account_type = item.get("account_type", "auto")
    invite_code = item.get("invite_code", "")
    mode_tasks = get_tasks_for_mode(run_mode)
    daily_tasks = mode_tasks["daily"]
    onchain_once_tasks = mode_tasks["onchain_once"]

    print("\n" + "#" * 70)
    print(f"[*] 线程启动：第 {idx}/{total} 个账号: {name}")
    print(f"[*] 启动延迟: {start_delay:.2f} 秒")
    print("#" * 70)

    row_result = {
        "name": name,
        "address": "",
        "account_type": account_type,
        "invite_code": invite_code,
        "login_mode": "",
        "login_success": False,

        # 每日任务统计
        "checkin_success": False,
        "checkin_status": "",
        "daily_auto_success_count": 0,
        "daily_auto_already_count": 0,
        "daily_auto_fail_count": 0,
        "daily_task_done_count": 0,
        "daily_task_total_count": len(daily_tasks),
        "daily_task_progress": f"0/{len(daily_tasks)}",

        # 一次性任务统计
        "once_success_count": 0,
        "once_already_count": 0,
        "once_fail_count": 0,
        "once_task_done_count": 0,
        "once_task_total_count": len(onchain_once_tasks),
        "once_task_progress": f"0/{len(onchain_once_tasks)}",

        # task/list 总任务进度
        "task_list_progress": "未知",
        "task_list_completed_count": 0,
        "task_list_total_count": 0,

        # 积分统计
        "daily_reward_total": 0,
        "once_reward_total": 0,
        "reward_total": 0,
        "points": "未知",
        "total_points": "未知",
        "available_points_real": "未知",
        "used_points": "未知",
        "completed_tasks_real": "未知",
        "total_tasks_real": "未知",
        "real_task_progress": "未知",
        "level": "未知",
        "level_name": "未知",
        "is_bind_twitter": False,
        "is_bind_discord": False,
        "is_bind_telegram": False,

        # 其他记录
        "token": "",
        "login_error": "",
        "checkin_response": "",
        "daily_task_details": "",
        "once_task_details": "",
        "task_list_completed_details": "",
        "task_list_not_completed_details": "",
    }

    try:
        # =====================================================
        # 1. 新老号兼容登录
        # =====================================================
        login_result = login_account(
            private_key=private_key,
            account_name=name,
            account_type=account_type,
            invite_code=invite_code
        )

        row_result["address"] = login_result.get("address", "")
        row_result["login_success"] = login_result.get("success", False)
        row_result["login_mode"] = login_result.get("login_mode", "")
        row_result["token"] = login_result.get("token", "")
        row_result["login_error"] = login_result.get("error", "")

        if not login_result.get("success"):
            print(f"[-] [{name}] 登录失败，跳过任务。")
            return row_result

        auth_token = login_result["token"]

        # =====================================================
        # 2. 登录后先获取任务列表
        # =====================================================
        tasks_before = fetch_task_list(auth_token)
        task_summary_before = analyze_tasks(tasks_before)
        print_task_summary(task_summary_before)

        row_result["all_tasks_count"] = task_summary_before.get("all_tasks_count", 0)
        row_result["completed_today_count"] = len(task_summary_before.get("completed_today_tasks", []))
        row_result["not_started_count"] = len(task_summary_before.get("not_started_tasks", []))
        row_result["completed_today_points"] = task_summary_before.get("completed_today_points", 0)
        row_result["available_points"] = task_summary_before.get("available_points", 0)

        # =====================================================
        # 3. Check In 签到 + 每日自动任务（模式 1）
        # =====================================================
        if daily_tasks:
            daily_checkin_task = task_summary_before.get("daily_checkin")

            if daily_checkin_task and daily_checkin_task.get("completionStatus") == "COMPLETED_TODAY":
                print(f"\n[=] [{name}] 任务列表显示今日已签到，跳过 Check In 请求。")
                checkin_result = {
                    "success": True,
                    "status": "already_completed_by_task_list",
                    "method": "task_list",
                    "response": json.dumps(daily_checkin_task, ensure_ascii=False)
                }
            else:
                checkin_result = do_daily_checkin(auth_token=auth_token)

            row_result["checkin_success"] = checkin_result.get("success", False)
            row_result["checkin_status"] = checkin_result.get("status", "")
            row_result["checkin_response"] = checkin_result.get("response", "")

            time.sleep(TASK_INTERVAL)

            daily_auto_tasks = [
                t for t in daily_tasks
                if t["task_id"] != "CHECK_IN"
            ]

            daily_auto_result = do_auto_complete_tasks(
                auth_token=auth_token,
                tasks=daily_auto_tasks
            )

            row_result["daily_auto_success_count"] = daily_auto_result.get("success_count", 0)
            row_result["daily_auto_already_count"] = daily_auto_result.get("already_count", 0)
            row_result["daily_auto_fail_count"] = daily_auto_result.get("fail_count", 0)
            row_result["daily_reward_total"] = daily_auto_result.get("reward_total", 0)
            row_result["daily_task_details"] = json.dumps(
                daily_auto_result.get("details", []),
                ensure_ascii=False
            )

            daily_done_count = 0

            if row_result["checkin_success"]:
                daily_done_count += 1

            daily_done_count += row_result["daily_auto_success_count"]
            daily_done_count += row_result["daily_auto_already_count"]

            row_result["daily_task_done_count"] = daily_done_count
            row_result["daily_task_progress"] = f"{daily_done_count}/{len(daily_tasks)}"

            time.sleep(TASK_INTERVAL)
        else:
            print(f"\n[*] [{name}] 模式 {run_mode} 不执行每日签到任务。")
            row_result["daily_task_progress"] = "0/0"

        # =====================================================
        # 5. 一次性链上任务（模式 2）
        # =====================================================
        if onchain_once_tasks:
            once_result = do_onchain_once_tasks(
                auth_token=auth_token,
                private_key=private_key,
                tasks=onchain_once_tasks,
                task_list=tasks_before,
            )
        else:
            print(f"\n[*] [{name}] 模式 {run_mode} 不执行一次性链上任务。")
            once_result = {
                "success_count": 0,
                "already_count": 0,
                "fail_count": 0,
                "reward_total": 0,
                "details": []
            }

        row_result["once_success_count"] = once_result.get("success_count", 0)
        row_result["once_already_count"] = once_result.get("already_count", 0)
        row_result["once_fail_count"] = once_result.get("fail_count", 0)
        row_result["once_reward_total"] = once_result.get("reward_total", 0)
        row_result["once_task_details"] = json.dumps(
            once_result.get("details", []),
            ensure_ascii=False
        )

        once_done_count = (
            row_result["once_success_count"]
            + row_result["once_already_count"]
        )

        row_result["once_task_done_count"] = once_done_count
        row_result["once_task_progress"] = f"{once_done_count}/{len(onchain_once_tasks)}"

        # =====================================================
        # 6. 执行后再次获取 task/list，统计总进度，例如 4/10
        # =====================================================
        tasks_after = fetch_task_list(auth_token)
        task_progress = analyze_task_progress(tasks_after)

        row_result["task_list_progress"] = task_progress.get("progress_text", "未知")
        row_result["task_list_completed_count"] = task_progress.get("completed_count", 0)
        row_result["task_list_total_count"] = task_progress.get("total_count", 0)
        # 7. 获取主页真实用户信息：总积分 / 真实任务进度 / 绑定状态
        user_info = fetch_user_info(auth_token)

        if user_info:
            row_result["total_points"] = user_info.get("totalPoints", "未知")
            row_result["available_points_real"] = user_info.get("availablePoints", "未知")
            row_result["used_points"] = user_info.get("usedPoints", "未知")

            completed_tasks = user_info.get("completedTasks", "未知")
            total_tasks = user_info.get("totalTasks", "未知")

            row_result["completed_tasks_real"] = completed_tasks
            row_result["total_tasks_real"] = total_tasks
            row_result["real_task_progress"] = f"{completed_tasks}/{total_tasks}"

            row_result["level"] = user_info.get("level", "未知")

            level_info = user_info.get("levelInfo", {}) or {}
            row_result["level_name"] = level_info.get("levelName", "未知")

            row_result["is_bind_twitter"] = user_info.get("isBindTwitter", False)
            row_result["is_bind_discord"] = user_info.get("isBindDiscord", False)
            row_result["is_bind_telegram"] = user_info.get("isBindTelegram", False)
        else:
            row_result["real_task_progress"] = row_result.get("task_list_progress", "未知")

        row_result["task_list_completed_details"] = json.dumps(
            task_progress.get("completed_tasks", []),
            ensure_ascii=False
        )

        row_result["task_list_not_completed_details"] = json.dumps(
            task_progress.get("not_completed_tasks", []),
            ensure_ascii=False
        )

        row_result["reward_total"] = (
            row_result["daily_reward_total"]
            + row_result["once_reward_total"]
        )

        row_result["points"] = f"任务进度 {row_result['task_list_progress']}"

        print(f"[+] [{name}] 执行完成。每日任务 {row_result['daily_task_progress']}，总进度 {row_result['task_list_progress']}")

        return row_result

    except Exception as e:
        print(f"[!] [{name}] 执行异常: {e}")
        row_result["login_error"] = f"执行异常: {e}"
        return row_result

# =========================================================
# 主流程
# =========================================================

def main():
    print("========== SimpleChain 签到 / 一次性链上任务脚本启动 ==========")

    accounts = load_accounts_from_excel()

    if not accounts:
        print("[-] Excel 中没有可用账号。")
        return

    run_mode = choose_run_mode()
    mode_tasks = get_tasks_for_mode(run_mode)
    total_accounts = len(accounts)

    print(f"[+] 已读取账号数量: {total_accounts}")
    print(f"[*] 当前模式: {run_mode}")
    print(f"[*] 模式任务: 每日 {len(mode_tasks['daily'])} 个，一次性链上 {len(mode_tasks['onchain_once'])} 个")
    print(f"[*] 当前线程数 MAX_WORKERS = {MAX_WORKERS}")

    final_results = []

    worker_count = min(MAX_WORKERS, total_accounts)

    with ThreadPoolExecutor(max_workers=worker_count) as executor:
        future_map = {}

        for idx, item in enumerate(accounts, start=1):
            future = executor.submit(
                process_one_account,
                idx,
                total_accounts,
                item,
                run_mode
            )
            future_map[future] = {
                "idx": idx,
                "name": item.get("name", f"wallet_{idx}")
            }

        for future in as_completed(future_map):
            info = future_map[future]
            idx = info["idx"]
            name = info["name"]

            try:
                result = future.result()
                final_results.append(result)
                print(f"[+] 主线程收到结果: 第 {idx} 个账号 {name}")
            except Exception as e:
                print(f"[!] 主线程捕获异常: 第 {idx} 个账号 {name} | {e}")

                final_results.append({
                    "name": name,
                    "address": "",
                    "account_type": "",
                    "invite_code": "",
                    "login_mode": "",
                    "login_success": False,
                    "checkin_success": False,
                    "checkin_status": "",
                    "daily_auto_success_count": 0,
                    "daily_auto_already_count": 0,
                    "daily_auto_fail_count": 0,
                    "daily_task_done_count": 0,
                    "daily_task_total_count": len(mode_tasks["daily"]),
                    "daily_task_progress": f"0/{len(mode_tasks['daily'])}",
                    "once_success_count": 0,
                    "once_already_count": 0,
                    "once_fail_count": 0,
                    "once_task_done_count": 0,
                    "once_task_total_count": len(mode_tasks["onchain_once"]),
                    "once_task_progress": f"0/{len(mode_tasks['onchain_once'])}",
                    "task_list_progress": "未知",
                    "task_list_completed_count": 0,
                    "task_list_total_count": 0,
                    "daily_reward_total": 0,
                    "once_reward_total": 0,
                    "reward_total": 0,
                    "points": "未知",
                    "total_points": "未知",
                    "available_points_real": "未知",
                    "used_points": "未知",
                    "completed_tasks_real": "未知",
                    "total_tasks_real": "未知",
                    "real_task_progress": "未知",
                    "level": "未知",
                    "level_name": "未知",
                    "is_bind_twitter": False,
                    "is_bind_discord": False,
                    "is_bind_telegram": False,
                    "token": "",
                    "login_error": f"线程异常: {e}",
                    "checkin_response": "",
                    "daily_task_details": "",
                    "once_task_details": "",
                    "task_list_completed_details": "",
                    "task_list_not_completed_details": "",
                })

    # 按 Excel 原始顺序排序，避免多线程完成顺序乱掉
    name_order = {
        item.get("name", f"wallet_{i + 1}"): i
        for i, item in enumerate(accounts)
    }

    final_results.sort(
        key=lambda r: name_order.get(r.get("name", ""), 999999)
    )

    if SAVE_RESULT_EXCEL:
        save_results(final_results)
    else:
        print("\n[*] 已关闭结果文件保存，仅控制台输出。")

    print_final_summary(final_results)

    accounts = []
    for result in final_results:
        login_ok = bool(result.get("login_success"))
        error = result.get("login_error", "") or ""
        accounts.append(
            {
                "address": result.get("address", ""),
                "name": result.get("name", ""),
                "status": "success" if login_ok and not error else "failed",
                "message": result.get("points", ""),
                "error": error,
            }
        )
    emit_summary("Simple", accounts)


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        print("\n[!] 脚本发生未捕获异常：")
        print(e)
    finally:
        if not is_airdrop():
            input("\n程序已执行完毕，按回车键退出...")



