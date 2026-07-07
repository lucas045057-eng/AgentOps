import os
import time
import json
import random
import sys
import pandas as pd
import requests
from concurrent.futures import ThreadPoolExecutor, as_completed
from eth_account import Account
from eth_account.messages import encode_defunct

Account.enable_unaudited_hdwallet_features()

# =========================================================
# 配置区
# =========================================================

ACCOUNTS_EXCEL = "simple账号.xlsx"
RESULT_EXCEL = "run_result.xlsx"

# 是否保存结果文件
SAVE_RESULT_EXCEL = False

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
    {"task_id": "CHECK_IN", "task_name": "Daily Check In", "task_type": "DAILY_CHECKIN", "reward": 60},
    {"task_id": "TK-202604-DT-0007", "task_name": "Visit Official Website", "task_type": "DAILY_BROWSE", "reward": 30},
]

# 一次性任务：做过一次后以后只会显示已完成
ONCE_TASKS = [
    {"task_id": "TK-202604-CT-0006", "task_name": "Check Out the Block Explorer", "task_type": "ONCE", "reward": 100},
]

# 实际需要提交到 /task/complete 的任务
AUTO_COMPLETE_TASKS = [t for t in DAILY_TASKS + ONCE_TASKS if t["task_id"] != "CHECK_IN"]

# 每个任务之间的间隔
ACCOUNT_INTERVAL = 2
TASK_INTERVAL = 2
BROWSE_WAIT_SECONDS = 3

# =========================================================
# 多线程配置
# =========================================================
MAX_WORKERS = 10
THREAD_START_DELAY_MIN = 1
THREAD_START_DELAY_MAX = 5

COMMON_HEADERS = {
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
    "Origin": "https://task.simplechain.com",
    "Referer": "https://task.simplechain.com/",
    "Content-Type": "application/json",
    "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"),
}


# =========================================================
# Excel 工具
# =========================================================

def create_accounts_template():
    if os.path.exists(ACCOUNTS_EXCEL):
        return
    df = pd.DataFrame([{"name": "wallet1", "private_key": "0x你的私钥", "account_type": "auto", "invite_code": "", "api_key": "", "checkin_task_id": ""}])
    df.to_excel(ACCOUNTS_EXCEL, index=False)
    print(f"[!] 未找到 {ACCOUNTS_EXCEL}，已自动创建模板。")


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

        account_type = clean_cell(row.get("account_type", "auto")) if "account_type" in df.columns else "auto"
        account_type = account_type.lower().strip()
        if account_type not in ["old", "new", "auto"]:
            account_type = "auto"

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
        return {"code": -1, "message": "响应不是 JSON", "raw_text": response.text[:500]}


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
    return (data.get("code") == 20101 or data.get("reason") == "TASK_ALREADY_COMPLETED" or
            "task already completed" in text or "already completed" in text or "已完成" in text or "已签到" in text)


def fetch_user_info(auth_token: str):
    headers = make_auth_headers(auth_token)
    try:
        res = requests.get(USER_INFO_URL, headers=headers, timeout=20)
        data = safe_json_response(res)
        if not is_success_response(data):
            return {}
        return data.get("data", {}) or {}
    except Exception as e:
        print(f"[!] 获取 user info 异常: {e}")
        return {}


def fetch_task_list(auth_token: str):
    headers = make_auth_headers(auth_token)
    try:
        res = requests.get(TASK_LIST_URL, headers=headers, timeout=20)
        data = safe_json_response(res)
        if not is_success_response(data):
            return []
        tasks = data.get("data", {}).get("tasks", [])
        return tasks if isinstance(tasks, list) else []
    except Exception as e:
        print(f"[!] 获取 task/list 异常: {e}")
        return []


def analyze_task_progress(tasks: list):
    total_count = 0
    completed_count = 0
    completed_tasks = []
    not_completed_tasks = []
    completed_statuses = ["COMPLETED", "COMPLETED_TODAY", "CLAIMED"]

    for task in tasks:
        task_id = task.get("taskId", "")
        task_name = task.get("taskName", "")
        task_code = task.get("taskCode", "")
        task_type = task.get("taskType", "")
        status = task.get("completionStatus", "")
        reward = task.get("rewardPoints", 0)

        total_count += 1
        item = {"task_id": task_id, "task_name": task_name, "task_code": task_code, "task_type": task_type, "status": status, "reward": reward}

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
    session = requests.Session()
    session.headers.update(COMMON_HEADERS)

    try:
        account = Account.from_key(private_key)
        address = account.address
    except Exception as e:
        return {"success": False, "address": "", "token": "", "login_mode": "", "error": f"私钥解析失败: {e}", "server_response": ""}

    print("\n" + "=" * 60)
    print(f"[*] 当前账号: {account_name}")
    print(f"[*] 当前钱包: {address}")
    print(f"[*] 账号类型: {account_type}")
    print(f"[*] 邀请码: {invite_code if invite_code else '未填写'}")

    try:
        nonce_res = session.post(NONCE_URL, json={"address": address}, timeout=20)
        nonce_data = safe_json_response(nonce_res)

        if nonce_res.status_code != 200 or nonce_data.get("code") != 0:
            print(f"[!] 获取 Nonce 失败: {nonce_data}")
            return {"success": False, "address": address, "token": "", "login_mode": "", "error": "获取 Nonce 失败", "server_response": json.dumps(nonce_data, ensure_ascii=False)}

        message_text = nonce_data.get("data", {}).get("message")
        if not message_text:
            print(f"[!] Nonce 返回缺少 message: {nonce_data}")
            return {"success": False, "address": address, "token": "", "login_mode": "", "error": "Nonce 返回缺少 message", "server_response": json.dumps(nonce_data, ensure_ascii=False)}

        print("[+] 成功拿到 Nonce 签名模板。")
    except Exception as e:
        print(f"[!] Nonce 接口异常: {e}")
        return {"success": False, "address": address, "token": "", "login_mode": "", "error": f"Nonce 接口异常: {e}", "server_response": ""}

    try:
        signable_message = encode_defunct(text=message_text)
        signed_message = Account.sign_message(signable_message, private_key=private_key)
        signature = normalize_signature(signed_message.signature.hex())
        print("[+] 签名完成。")
        print(f"[*] signature 长度: {len(signature)}")
    except Exception as e:
        print(f"[!] 签名失败: {e}")
        return {"success": False, "address": address, "token": "", "login_mode": "", "error": f"签名失败: {e}", "server_response": ""}

    def submit_login(payload: dict, mode_name: str):
        print(f"[*] 正在发送登录请求：{mode_name}")
        try:
            login_res = session.post(LOGIN_URL, json=payload, timeout=20)
            login_data = safe_json_response(login_res)

            if login_res.status_code == 200 and login_data.get("code") == 0:
                token = login_data.get("data", {}).get("token", "")
                if token and not token.startswith("Bearer "):
                    token = f"Bearer {token}"
                print(f"[+] 登录成功。模式: {mode_name}")
                return {"success": True, "address": address, "token": token, "login_mode": mode_name, "error": "", "server_response": json.dumps(login_data, ensure_ascii=False)}

            print(f"[-] 登录未通过。模式: {mode_name}")
            print(f"[*] HTTP 状态码: {login_res.status_code}")
            print(f"[*] 服务端返回: {login_data}")
            return {"success": False, "address": address, "token": "", "login_mode": mode_name, "error": "登录失败", "server_response": json.dumps(login_data, ensure_ascii=False)}
        except Exception as e:
            print(f"[!] 登录接口异常。模式: {mode_name} | {e}")
            return {"success": False, "address": address, "token": "", "login_mode": mode_name, "error": f"登录接口异常: {e}", "server_response": ""}

    old_payload = {"address": address, "message": message_text, "signature": signature}
    new_payload = {"address": address, "inviteCode": invite_code, "message": message_text, "signature": signature}

    account_type = (account_type or "auto").lower().strip()

    if account_type == "old":
        return submit_login(old_payload, "old_no_invite")

    if account_type == "new":
        if not invite_code:
            return {"success": False, "address": address, "token": "", "login_mode": "new_with_invite", "error": "新号缺少 invite_code", "server_response": ""}
        return submit_login(new_payload, "new_with_invite")

    # auto：先老号，失败再新号
    old_result = submit_login(old_payload, "auto_old_no_invite")
    if old_result.get("success"):
        return old_result

    if not invite_code:
        old_result["error"] = "老号登录失败，且未填写 invite_code"
        return old_result

    return submit_login(new_payload, "auto_new_with_invite")


# =========================================================
# 任务列表逻辑
# =========================================================

def fetch_task_list(auth_token: str):
    headers = make_auth_headers(auth_token)
    print("\n[*] 正在获取任务列表...")
    try:
        res = requests.get(TASK_LIST_URL, headers=headers, timeout=20)
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

        if completion_status == "COMPLETED_TODAY":
            summary["completed_today_tasks"].append(task)
            summary["completed_today_points"] += reward_points
            summary["completed_total_visible_points"] += reward_points

        if completion_status in ["COMPLETED", "CLAIMED"]:
            summary["completed_once_tasks"].append(task)
            summary["completed_total_visible_points"] += reward_points

        if completion_status == "NOT_STARTED":
            summary["not_started_tasks"].append(task)
            summary["not_started_points"] += reward_points

        if completion_status in ["AVAILABLE", "NOT_STARTED"]:
            summary["available_tasks"].append(task)
            summary["available_points"] += reward_points

        if task_code == "DAILY_CHECK_IN":
            summary["daily_checkin"] = task

        if (task_code == "ACCESS_LINK" or "visit" in task_name.lower() or "website" in task_name.lower()):
            summary["daily_browse_tasks"].append(task)

    return summary


def print_task_summary(task_summary):
    print("\n[*] 当前账号任务摘要：")
    print(f"    任务总数: {task_summary['all_tasks_count']}")
    print(f"    今日已完成任务数: {len(task_summary['completed_today_tasks'])}")
    print(f"    未开始任务数: {len(task_summary['not_started_tasks'])}")
    print(f"    可用/未开始任务潜在积分: {task_summary['available_points']}")
    print(f"    今日已完成任务积分: {task_summary['completed_today_points']}")

    daily_checkin = task_summary.get("daily_checkin")
    if daily_checkin:
        print(f"    每日签到: {daily_checkin.get('taskName')} | 状态: {daily_checkin.get('completionStatus')} | 奖励: {daily_checkin.get('rewardPoints')}")

    if task_summary.get("daily_browse_tasks"):
        print("    浏览/访问任务:")
        for t in task_summary["daily_browse_tasks"]:
            print(f"      - {t.get('taskName')} | 状态: {t.get('completionStatus')} | 奖励: {t.get('rewardPoints')}")


# =========================================================
# 签到逻辑
# =========================================================

def do_daily_checkin(auth_token: str, checkin_task_id: str = ""):
    headers = make_auth_headers(auth_token)
    print("\n[*] 开始执行 Check In...")

    try:
        print("[*] 尝试方式 A：POST json={} ...")
        res = requests.post(CHECKIN_URL, json={}, headers=headers, timeout=20)
        data = safe_json_response(res)

        if is_success_response(data):
            print("[+] Check In 成功。")
            return {"success": True, "status": "success", "method": "POST json={}", "response": json.dumps(data, ensure_ascii=False)}

        if is_already_completed_response(data):
            print("[=] 今日已经 Check In，跳过。")
            return {"success": True, "status": "already_completed", "method": "POST json={}", "response": json.dumps(data, ensure_ascii=False)}
        print(f"[-] 方式 A 未成功: {data}")
    except Exception as e:
        print(f"[!] 方式 A 请求异常: {e}")

    try:
        print("[*] 尝试方式 B：POST 无 body ...")
        res = requests.post(CHECKIN_URL, headers=headers, timeout=20)
        data = safe_json_response(res)

        if is_success_response(data):
            print("[+] Check In 成功。")
            return {"success": True, "status": "success", "method": "POST no body", "response": json.dumps(data, ensure_ascii=False)}

        if is_already_completed_response(data):
            print("[=] 今日已经 Check In，跳过。")
            return {"success": True, "status": "already_completed", "method": "POST no body", "response": json.dumps(data, ensure_ascii=False)}

        print(f"[-] 方式 B 未成功: {data}")
        return {"success": False, "status": "failed", "method": "campaign_checkin", "response": json.dumps(data, ensure_ascii=False)}
    except Exception as e:
        print(f"[!] 方式 B 请求异常: {e}")
        return {"success": False, "status": "exception", "method": "campaign_checkin", "response": str(e)}


# =========================================================
# 浏览任务逻辑
# =========================================================

def do_auto_complete_tasks(auth_token: str, tasks: list):
    headers = make_auth_headers(auth_token)
    print("\n[*] 开始执行自动提交任务...")

    if not tasks:
        print("[*] 自动任务列表为空，跳过。")
        return {"success_count": 0, "already_count": 0, "fail_count": 0, "reward_total": 0, "details": []}

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
            res = requests.post(TASK_COMPLETE_URL, json={"taskId": task_id}, headers=headers, timeout=20)
            data = safe_json_response(res)

            if is_success_response(data):
                response_data = data.get("data", {}) or {}
                reward = response_data.get("rewardPoints", expect_reward) or 0
                try:
                    reward_int = int(reward)
                except Exception:
                    reward_int = 0

                reward_total += reward_int
                success_count += 1

                real_task_name = response_data.get("taskName", task_name)
                real_task_code = response_data.get("taskCode", "")

                print(f"[+] 自动任务完成: {real_task_name}")
                print(f"    获得积分: {reward}")

                details.append({
                    "task_id": task_id,
                    "task_name": real_task_name,
                    "task_type": task_type,
                    "task_code": real_task_code,
                    "status": "success",
                    "success": True,
                    "reward": reward_int,
                    "response": json.dumps(data, ensure_ascii=False)
                })

            elif is_already_completed_response(data):
                already_count += 1
                print(f"[=] 自动任务已完成，跳过: {task_name} | {task_id}")
                details.append({
                    "task_id": task_id,
                    "task_name": task_name,
                    "task_type": task_type,
                    "status": "already_completed",
                    "success": True,
                    "reward": 0,
                    "response": json.dumps(data, ensure_ascii=False)
                })
            else:
                fail_count += 1
                print(f"[-] 自动任务失败: {task_name} | {task_id}")
                details.append({
                    "task_id": task_id,
                    "task_name": task_name,
                    "task_type": task_type,
                    "status": "failed",
                    "success": False,
                    "reward": 0,
                    "response": json.dumps(data, ensure_ascii=False)
                })
        except Exception as e:
            fail_count += 1
            print(f"[!] 自动任务请求异常: {task_name} | {e}")
            details.append({
                "task_id": task_id,
                "task_name": task_name,
                "task_type": task_type,
                "status": "exception",
                "success": False,
                "reward": 0,
                "response": str(e)
            })

        time.sleep(TASK_INTERVAL)

    return {"success_count": success_count, "already_count": already_count, "fail_count": fail_count, "reward_total": reward_total, "details": details}


# =========================================================
# 执行单个账号
# =========================================================

def process_one_account(idx: int, total: int, item: dict):
    start_delay = random.uniform(THREAD_START_DELAY_MIN, THREAD_START_DELAY_MAX)
    time.sleep(start_delay)

    name = item["name"]
    private_key = item["private_key"]
    account_type = item.get("account_type", "auto")
    invite_code = item.get("invite_code", "")

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
        "checkin_success": False,
        "checkin_status": "",
        "daily_auto_success_count": 0,
        "daily_auto_already_count": 0,
        "daily_auto_fail_count": 0,
        "daily_task_done_count": 0,
        "daily_task_total_count": len(DAILY_TASKS),
        "daily_task_progress": f"0/{len(DAILY_TASKS)}",
        "once_success_count": 0,
        "once_already_count": 0,
        "once_fail_count": 0,
        "once_task_done_count": 0,
        "once_task_total_count": len(ONCE_TASKS),
        "once_task_progress": f"0/{len(ONCE_TASKS)}",
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
        "login_error": "",
        "checkin_response": "",
        "daily_task_details": "",
        "once_task_details": "",
        "task_list_completed_details": "",
        "task_list_not_completed_details": "",
    }

    try:
        login_result = login_account(private_key=private_key, account_name=name, account_type=account_type, invite_code=invite_code)

        row_result["address"] = login_result.get("address", "")
        row_result["login_success"] = login_result.get("success", False)
        row_result["login_mode"] = login_result.get("login_mode", "")
        row_result["token"] = login_result.get("token", "")
        row_result["login_error"] = login_result.get("error", "")

        if not login_result.get("success"):
            print(f"[-] [{name}] 登录失败，跳过任务。")
            return row_result

        auth_token = login_result["token"]

        tasks_before = fetch_task_list(auth_token)
        task_summary_before = analyze_tasks(tasks_before)
        print_task_summary(task_summary_before)

        daily_checkin_task = task_summary_before.get("daily_checkin")

        if daily_checkin_task and daily_checkin_task.get("completionStatus") == "COMPLETED_TODAY":
            print(f"\n[=] [{name}] 任务列表显示今日已签到，跳过 Check In 请求。")
            checkin_result = {"success": True, "status": "already_completed_by_task_list", "method": "task_list", "response": json.dumps(daily_checkin_task, ensure_ascii=False)}
        else:
            checkin_result = do_daily_checkin(auth_token=auth_token)

        row_result["checkin_success"] = checkin_result.get("success", False)
        row_result["checkin_status"] = checkin_result.get("status", "")
        row_result["checkin_response"] = checkin_result.get("response", "")

        time.sleep(TASK_INTERVAL)

        daily_auto_tasks = [t for t in DAILY_TASKS if t["task_id"] != "CHECK_IN"]
        daily_auto_result = do_auto_complete_tasks(auth_token=auth_token, tasks=daily_auto_tasks)

        row_result["daily_auto_success_count"] = daily_auto_result.get("success_count", 0)
        row_result["daily_auto_already_count"] = daily_auto_result.get("already_count", 0)
        row_result["daily_auto_fail_count"] = daily_auto_result.get("fail_count", 0)
        row_result["daily_reward_total"] = daily_auto_result.get("reward_total", 0)
        row_result["daily_task_details"] = json.dumps(daily_auto_result.get("details", []), ensure_ascii=False)

        daily_done_count = 0
        if row_result["checkin_success"]:
            daily_done_count += 1
        daily_done_count += row_result["daily_auto_success_count"]
        daily_done_count += row_result["daily_auto_already_count"]
        row_result["daily_task_done_count"] = daily_done_count
        row_result["daily_task_progress"] = f"{daily_done_count}/{len(DAILY_TASKS)}"

        time.sleep(TASK_INTERVAL)

        once_result = do_auto_complete_tasks(auth_token=auth_token, tasks=ONCE_TASKS)

        row_result["once_success_count"] = once_result.get("success_count", 0)
        row_result["once_already_count"] = once_result.get("already_count", 0)
        row_result["once_fail_count"] = once_result.get("fail_count", 0)
        row_result["once_reward_total"] = once_result.get("reward_total", 0)
        row_result["once_task_details"] = json.dumps(once_result.get("details", []), ensure_ascii=False)

        once_done_count = row_result["once_success_count"] + row_result["once_already_count"]
        row_result["once_task_done_count"] = once_done_count
        row_result["once_task_progress"] = f"{once_done_count}/{len(ONCE_TASKS)}"

        tasks_after = fetch_task_list(auth_token)
        task_progress = analyze_task_progress(tasks_after)

        row_result["task_list_progress"] = task_progress.get("progress_text", "未知")
        row_result["task_list_completed_count"] = task_progress.get("completed_count", 0)
        row_result["task_list_total_count"] = task_progress.get("total_count", 0)

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

        row_result["task_list_completed_details"] = json.dumps(task_progress.get("completed_tasks", []), ensure_ascii=False)
        row_result["task_list_not_completed_details"] = json.dumps(task_progress.get("not_completed_tasks", []), ensure_ascii=False)

        row_result["reward_total"] = row_result["daily_reward_total"] + row_result["once_reward_total"]
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
    final_status = "failed"
    final_message = "脚本执行完成"
    all_account_results = []

    try:
        print("========== 新老号兼容登录 + 多线程任务脚本启动 ==========")

        accounts = load_accounts_from_excel()

        if not accounts:
            print(json.dumps({"status": "failed", "message": "没有可用账号", "total": 0, "success_count": 0, "failed_count": 0, "accounts": []}))
            return

        total_accounts = len(accounts)
        print(f"[+] 已读取账号数量: {total_accounts}")
        print(f"[*] 当前线程数 MAX_WORKERS = {MAX_WORKERS}")

        final_results = []
        worker_count = min(MAX_WORKERS, total_accounts)

        with ThreadPoolExecutor(max_workers=worker_count) as executor:
            future_map = {}

            for idx, item in enumerate(accounts, start=1):
                future = executor.submit(process_one_account, idx, total_accounts, item)
                future_map[future] = {"idx": idx, "name": item.get("name", f"wallet_{idx}")}

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
                        "login_success": False,
                        "login_error": f"线程异常: {e}",
                        "daily_task_progress": "0/0",
                        "once_task_progress": "0/0",
                        "task_list_progress": "未知",
                        "total_points": "未知",
                        "available_points_real": "未知",
                        "level_name": "未知",
                        "reward_total": 0,
                    })

        # 按 Excel 原始顺序排序
        name_order = {item.get("name", f"wallet_{i + 1}"): i for i, item in enumerate(accounts)}
        final_results.sort(key=lambda r: name_order.get(r.get("name", ""), 999999))

        if SAVE_RESULT_EXCEL:
            save_results(final_results)
        else:
            print("\n[*] 已关闭结果文件保存，仅控制台输出。")

        # 打印总结（保持原有功能）
        print("\n每个账号总览：")
        print("-" * 150)
        print("序号 | 账号 | 钱包 | 登录 | 每日任务 | 一次性任务 | 总任务 | 总积分 | 可用积分 | 等级 | 绑定状态")
        print("-" * 150)

        for i, r in enumerate(final_results, start=1):
            name = r.get("name", "")
            address = r.get("address", "")
            short_address = address[:6] + "..." + address[-4:] if address and len(address) > 12 else address or "-"
            login_text = "成功" if r.get("login_success") else "失败"
            daily_progress = r.get("daily_task_progress", "0/0")
            once_progress = r.get("once_task_progress", "0/0")
            total_progress = r.get("real_task_progress") or r.get("task_list_progress") or "未知"
            total_points = r.get("total_points", "未知")
            available_points = r.get("available_points_real", "未知")
            level_name = r.get("level_name", "未知")
            bind_text = f"X:{'Y' if r.get('is_bind_twitter') else 'N'} DC:{'Y' if r.get('is_bind_discord') else 'N'} TG:{'Y' if r.get('is_bind_telegram') else 'N'}"

            print(f"{i} | {name} | {short_address} | {login_text} | 每日 {daily_progress} | 一次性 {once_progress} | 总任务 {total_progress} | 总积分 {total_points} | 可用 {available_points} | {level_name} | {bind_text}")

        print("-" * 150)

        # ========== 构建账号明细 ==========
        for r in final_results:
            address = r.get("address", "")
            login_success = r.get("login_success", False)

            # 判断每个账号的整体状态
            if login_success:
                account_status = "success"
                error_msg = ""
            else:
                account_status = "failed"
                error_msg = r.get("login_error", "登录失败")

            all_account_results.append({
                "address": address,
                "name": r.get("name", ""),
                "status": account_status,
                "message": f"每日 {r.get('daily_task_progress', '0/0')}，总进度 {r.get('task_list_progress', '未知')}",
                "points": r.get("reward_total", 0),
                "error": error_msg
            })

        total = len(all_account_results)
        success_accounts = sum(1 for a in all_account_results if a["status"] == "success")
        failed_accounts = total - success_accounts

        final_status = "failed" if failed_accounts > 0 else "success"
        final_message = f"执行完成：{total}个账号，{success_accounts}成功，{failed_accounts}失败"

    except Exception as e:
        final_status = "failed"
        final_message = f"脚本执行异常: {str(e)}"
        print(f"[!] 捕获到顶层异常: {e}")

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