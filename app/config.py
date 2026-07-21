# app/config.py
from pydantic_settings import BaseSettings
from typing import List


class Settings(BaseSettings):
    # 数据库
    database_url: str = "projects.db"
    
    # 开机自动执行的脚本ID列表（环境变量中为逗号分隔的字符串）
    startup_script_ids_raw: str = ""
    
    # 邮件配置
    smtp_host: str = "smtp.qq.com"
    smtp_port: int = 465
    smtp_user: str = ""
    smtp_password: str = ""
    smtp_receiver: str = ""
    
    # 服务配置
    host: str = "0.0.0.0"
    port: int = 8000
    debug: bool = False
    script_timeout_seconds: float = 300.0

    class Config:
        env_file = ".env"
        env_file_encoding = "utf-8"

    @property
    def startup_script_ids(self) -> List[int]:
        """解析逗号分隔的字符串为整数列表"""
        if not self.startup_script_ids_raw:
            return []
        return [int(x.strip()) for x in self.startup_script_ids_raw.split(",") if x.strip()]


settings = Settings()
