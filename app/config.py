from pathlib import Path
from typing import List

from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    database_url: str = "projects.db"
    progress_database_url: str = "progress.db"
    api_key: str = ""
    script_root: str = "scripts"
    external_script_root: str = "/mnt/e/项目脚本/日签/自动日签项目"
    runner_catalog_path: str = "config/runner_catalog.json"
    runner_workspace_root: str = "/tmp/airdrop-runs"
    execution_log_root: str = "logs/executions"
    execution_timeout: int = 900
    allow_interactive: bool = False
    execution_platform: str = "linux"
    keep_runner_workspace: bool = False
    sync_runner_catalog_on_startup: bool = True
    auto_run_on_startup: bool = False
    auto_run_interval_minutes: int = 0
    auto_run_catalog_ids_raw: str = ""
    windows_worker_url: str = ""
    windows_worker_token: str = ""
    windows_worker_timeout: int = 930

    startup_script_ids_raw: str = ""

    smtp_host: str = "smtp.qq.com"
    smtp_port: int = 465
    smtp_user: str = ""
    smtp_password: str = ""
    smtp_receiver: str = ""

    host: str = "0.0.0.0"
    port: int = 8000
    debug: bool = False

    class Config:
        env_file = ".env"
        env_file_encoding = "utf-8"

    @property
    def startup_script_ids(self) -> List[int]:
        if not self.startup_script_ids_raw:
            return []
        return [int(x.strip()) for x in self.startup_script_ids_raw.split(",") if x.strip()]

    @property
    def auto_run_catalog_ids(self) -> List[str]:
        if not self.auto_run_catalog_ids_raw:
            return []
        return [x.strip() for x in self.auto_run_catalog_ids_raw.split(",") if x.strip()]


settings = Settings()
