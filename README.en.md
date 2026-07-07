# AgentOps — Automation Script Management Platform

![Python](https://img.shields.io/badge/Python-3.12-blue.svg)
![FastAPI](https://img.shields.io/badge/FastAPI-0.104.1-green.svg)
![Docker](https://img.shields.io/badge/Docker-✔-blue.svg)
![License](https://img.shields.io/badge/License-MIT-yellow.svg)

English | [简体中文](./README.md)

> **AgentOps** is a lightweight automation script management platform for managing, executing, and monitoring Python automation scripts (e.g., Web3 check-ins, task claiming, on-chain interactions). It transforms the daily routine from "manually running commands" to "one-click execution with visualized results."

---

## 📖 Table of Contents

- [Project Background](#project-background)
- [Core Features](#core-features)
- [Tech Stack](#tech-stack)
- [Quick Start](#quick-start)
- [Project Structure](#project-structure)
- [Architecture Design](#architecture-design)
- [Deployment Options](#deployment-options)
- [Roadmap](#roadmap)
- [License](#license)

---

## 🎯 Project Background

While participating in Web3 projects, I had to manually run a dozen scripts daily for check-ins, claiming, and swaps. These scripts were scattered across different directories, requiring commands like `python daily_checkin.py` or `python claim.py`—repetitive and mechanical work.

**AgentOps solves this problem** by unifying script management, automatic execution, failure notifications, and clear result visualization.

---

## ✨ Core Features

| Feature | Description |
| :--- | :--- |
| **📁 Project Management** | Project → Task → Script three-level hierarchy |
| **⚡ Script Execution** | Asynchronous Python script execution with auto-retry |
| **📊 Account-level Monitoring** | Track success/failure for each account, not just the whole script |
| **📧 Failure Notifications** | Automatic email alerts on execution failure |
| **📈 Dashboard** | Today's overview + execution history + expandable account details (auto-refresh every 30s) |
| **🐳 Containerized Deployment** | Docker + docker-compose for one-command startup |
| **🔧 RESTful API** | Auto-generated Swagger documentation (`/docs`) |

---

## 🛠️ Tech Stack

| Category | Technology |
| :--- | :--- |
| **Backend** | FastAPI (async) |
| **Database** | SQLite (with context manager) |
| **Configuration** | Pydantic Settings + .env |
| **Frontend** | Vanilla HTML + CSS + JavaScript (Fetch API) |
| **Process Management** | asyncio + subprocess |
| **Deployment** | Docker + docker-compose (systemd optional) |
| **Notifications** | SMTP Email |

---

## 🚀 Quick Start

### Option 1: Docker (Recommended)

```bash
# Clone the repository
git clone https://github.com/lucas045057-eng/AgentOps.git
cd AgentOps

# Create environment file
cp .env.example .env
# Edit .env with your configuration (email, etc.)

# Start the service
docker-compose up -d

# Open dashboard
# http://localhost:8000/static/index.html
```

### Option 2: Local Development (Python 3.12 required)

```bash
# Create virtual environment
python3.12 -m venv venv
source venv/bin/activate  # Windows: venv\Scripts\activate

# Install dependencies
pip install -r requirements.txt

# Start the service
uvicorn main:app --reload
```

### Environment Variables (`.env`)

```ini
# Script IDs to run on startup (comma-separated, leave empty to disable)
STARTUP_SCRIPT_IDS=1,2,3

# Email notification (QQ Mail example)
SMTP_HOST=smtp.qq.com
SMTP_PORT=465
SMTP_USER=your_email@qq.com
SMTP_PASSWORD=your_authorization_code
SMTP_RECEIVER=receiver@qq.com

# Service config
HOST=0.0.0.0
PORT=8000
DEBUG=false
```

---

## 📂 Project Structure

```
AgentOps/
├── app/
│   ├── config.py              # Configuration management
│   ├── database/
│   │   └── database.py        # SQLite CRUD + context manager
│   ├── models/                # Pydantic data models
│   │   ├── project.py
│   │   ├── task.py
│   │   └── script.py
│   └── services/
│       ├── execution_service.py  # Async execution engine + retry
│       └── notifier.py           # Email notification
├── scripts/                   # User automation scripts
│   ├── Simple/
│   │   └── Simple.py          # SimpleChain check-in example
│   └── Gency/
│       └── Gency.py           # GencyAI check-in example
├── static/
│   └── index.html             # Management dashboard
├── logs/                      # Log directory
├── main.py                    # FastAPI entry point
├── Dockerfile
├── docker-compose.yml
└── .env                       # Environment variables (not committed)
```

---

## 🏛️ Architecture Design

### Three-Layer Architecture

```
┌─────────────────────────────────────────────────────┐
│  Presentation Layer                                │
│  - Dashboard (static/index.html)                   │
│  - FastAPI Routes (main.py)                       │
└─────────────────────────────────────────────────────┘
                         ▼
┌─────────────────────────────────────────────────────┐
│  Business Logic Layer                              │
│  - Script Execution Engine (execution_service)     │
│  - Retry Mechanism (max_retries)                  │
│  - Notification Service (notifier)                │
└─────────────────────────────────────────────────────┘
                         ▼
┌─────────────────────────────────────────────────────┐
│  Data Access Layer                                 │
│  - SQLite Database (database.py)                  │
│  - Context Manager for Connection Management       │
│  - Foreign Key Constraints + Indexes              │
└─────────────────────────────────────────────────────┘
```

### Database ER Diagram

```
projects
    │
    ├─ 1 : N ── tasks
    │               │
    │               ├─ 1 : N ── scripts
    │                               │
    │                               ├─ 1 : N ── executions
    │                                               │
    │                                               ├─ 1 : N ── account_results
```

### Data Flow

```
User clicks execute → FastAPI route → execution_service
    ↓
Launch async subprocess to run script
    ↓
Capture stdout/stderr, parse last line JSON
    ↓
Determine business status (success/failed)
    ↓
Save to executions table + accounts to account_results
    ↓
Send email notification on failure
    ↓
Dashboard displays results (expandable account details)
```

---

## 🐳 Deployment Options

| Method | Use Case | Command |
| :--- | :--- | :--- |
| **Docker Compose** | Production / Cross-platform | `docker-compose up -d` |
| **Systemd (Linux)** | Native Linux service | `sudo systemctl start agentops` |
| **Local Dev** | Development | `uvicorn main:app --reload` |

---

## 🗺️ Roadmap

- [ ] WebSocket for real-time log streaming
- [ ] Telegram / DingTalk / Feishu notifications
- [ ] PostgreSQL migration
- [ ] Celery + Redis for distributed execution
- [ ] User authentication and permissions
- [ ] AI Agent research + auto script generation

---

## 📄 License

MIT © 2026 Lucas

---

## 🤝 Contributing

Issues and Pull Requests are welcome!

---

## 📬 Contact

- GitHub: [@lucas045057-eng](https://github.com/lucas045057-eng)
- Email:lucas045057@gmail.com
