# AgentOps — 自动化脚本管理平台

![Python](https://img.shields.io/badge/Python-3.12-blue.svg)
![FastAPI](https://img.shields.io/badge/FastAPI-0.104.1-green.svg)
![Docker](https://img.shields.io/badge/Docker-✔-blue.svg)
![License](https://img.shields.io/badge/License-MIT-yellow.svg)

[English](./README.en.md) | 简体中文

> **AgentOps** 是一个轻量级的自动化脚本管理平台，用于统一管理、执行和监控各类 Python 自动化脚本（如 Web3 签到、任务领取、链上交互等）。让用户从"每天手动敲命令"变成"一键执行，打开网页看结果"。

---

## 📖 目录

- [项目背景](#项目背景)
- [核心功能](#核心功能)
- [技术栈](#技术栈)
- [快速开始](#快速开始)
- [项目结构](#项目结构)
- [架构设计](#架构设计)
- [部署方式](#部署方式)
- [未来规划](#未来规划)
- [License](#license)

---

## 🎯 项目背景

在参与 Web3 项目时，我每天需要手动执行十几个签到、领水、Swap 脚本。这些脚本散落在不同目录，有的需要 `python daily_checkin.py`，有的需要 `python claim.py`，每天重复操作，非常机械。

**AgentOps 就是为了解决这个问题而生的**：统一管理所有脚本，开机自动运行，失败时自动通知，执行结果一目了然。

---

## ✨ 核心功能

| 功能模块 | 说明 |
| :--- | :--- |
| **📁 项目管理** | Project → Task → Script 三级分类管理 |
| **⚡ 脚本执行** | 异步执行 Python 脚本，支持失败自动重试 |
| **📊 账号级监控** | 不仅知道整个脚本成功/失败，还能看到每个账号的明细 |
| **📧 失败通知** | 执行失败时自动发送邮件告警 |
| **📈 前端仪表板** | 今日概况 + 执行记录 + 账号明细展开，每 30 秒自动刷新 |
| **🐳 容器化部署** | Docker + docker-compose，一键启动 |
| **🔧 RESTful API** | 完整 Swagger 文档（`/docs`） |

---

## 🛠️ 技术栈

| 类别 | 技术 |
| :--- | :--- |
| **后端框架** | FastAPI (异步) |
| **数据库** | SQLite (支持上下文管理器) |
| **配置管理** | Pydantic Settings + .env |
| **前端** | 原生 HTML + CSS + JavaScript (Fetch API) |
| **进程管理** | asyncio + subprocess (异步执行) |
| **部署** | Docker + docker-compose (可选 systemd) |
| **通知** | SMTP 邮件 |

---

## 🚀 快速开始

### 方式一：Docker 一键启动（推荐）

```bash
# 克隆项目
git clone https://github.com/lucas045057-eng/AgentOps.git
cd AgentOps

# 创建环境变量文件
cp .env.example .env
# 编辑 .env 填入你的配置（邮箱等）

# 启动服务
docker-compose up -d

# 访问仪表板
# http://localhost:8000/static/index.html

## 📂 项目结构
AgentOps/
├── app/
│   ├── config.py              # 配置管理
│   ├── database/
│   │   └── database.py        # SQLite CRUD + 上下文管理器
│   ├── models/                # Pydantic 数据模型
│   │   ├── project.py
│   │   ├── task.py
│   │   └── script.py
│   └── services/
│       ├── execution_service.py  # 异步执行引擎 + 重试
│       └── notifier.py           # 邮件通知
├── scripts/                   # 用户自动化脚本
│   ├── Simple/
│   │   └── Simple.py          # SimpleChain 签到示例
│   └── Gency/
│       └── Gency.py           # GencyAI 签到示例
├── static/
│   └── index.html             # 管理仪表板
├── logs/                      # 日志目录
├── main.py                    # FastAPI 入口
├── Dockerfile
├── docker-compose.yml
└── .env                       # 环境变量（不提交）
## 🏛️ 架构设计
三层架构
┌─────────────────────────────────────────────────────┐
│  表现层 (Presentation)                             │
│  - 前端仪表板 (static/index.html)                  │
│  - FastAPI 路由 (main.py)                         │
└─────────────────────────────────────────────────────┘
                         ▼
┌─────────────────────────────────────────────────────┐
│  业务逻辑层 (Business Logic)                       │
│  - 脚本执行引擎 (execution_service.py)             │
│  - 失败重试机制 (max_retries)                     │
│  - 通知服务 (notifier.py)                         │
└─────────────────────────────────────────────────────┘
                         ▼
┌─────────────────────────────────────────────────────┐
│  数据访问层 (Data Access)                         │
│  - SQLite 数据库 (database.py)                    │
│  - 上下文管理器自动管理连接                        │
│  - 外键约束 + 索引优化                            │
└─────────────────────────────────────────────────────┘
## 数据库ER图
projects (项目)
    │
    ├─ 1 : N ── tasks (任务)
    │               │
    │               ├─ 1 : N ── scripts (脚本)
    │                               │
    │                               ├─ 1 : N ── executions (执行记录)
    │                                               │
    │                                               ├─ 1 : N ── account_results (账号明细)
## 数据流向
用户点击执行 → FastAPI 路由 → execution_service
    ↓
启动异步子进程执行脚本
    ↓
捕获 stdout/stderr，解析最后一行 JSON
    ↓
判断业务状态 (success/failed)
    ↓
存入 executions 表 + 解析 accounts 存入 account_results
    ↓
失败时发送邮件通知
    ↓
前端仪表板展示结果（可展开账号明细）
## 🐳 部署方式对比
方式	适用场景	命令
Docker Compose	生产环境 / 跨平台迁移	docker-compose up -d
Systemd (Linux)	原生 Linux 服务	sudo systemctl start agentops
本地开发	开发调试	uvicorn main:app --reload
## 🗺️ 未来规划
WebSocket 实时日志推送

Telegram / 钉钉 / 飞书通知

PostgreSQL 数据库迁移

Celery + Redis 分布式执行

用户认证与权限管理

AiAgent调研项目 + 自动编写脚本执行
## 📄 License
MIT © 2026 Lucas
## 🤝 贡献
欢迎提交 Issue 和 Pull Request！

如果你有好的想法或者发现了 Bug，请告诉我。
