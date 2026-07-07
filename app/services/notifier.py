# app/services/notifier.py
import smtplib
import logging
from email.mime.text import MIMEText
from email.header import Header

from app.config import settings

logger = logging.getLogger(__name__)

# 从配置读取邮件参数
SMTP_HOST = settings.smtp_host
SMTP_PORT = settings.smtp_port
SMTP_USER = settings.smtp_user
SMTP_PASSWORD = settings.smtp_password
RECEIVER = settings.smtp_receiver


async def send_failure_notification(
    script_id: int, 
    execution_id: int | None, 
    error: str
):
    """发送执行失败通知邮件"""
    if not SMTP_USER or not SMTP_PASSWORD:
        logger.warning("邮件未配置，跳过发送通知")
        return
    
    subject = f"[AgentOps] 脚本 {script_id} 执行失败"
    body = f"""
脚本 ID: {script_id}
执行 ID: {execution_id if execution_id else '未记录'}
错误信息:
{error}

请及时查看详情。
"""
    import asyncio
    loop = asyncio.get_event_loop()
    await loop.run_in_executor(None, _send_mail, subject, body)


def _send_mail(subject: str, body: str):
    """同步发送邮件"""
    try:
        msg = MIMEText(body, 'plain', 'utf-8')
        msg['Subject'] = Header(subject, 'utf-8')
        msg['From'] = SMTP_USER
        msg['To'] = RECEIVER

        with smtplib.SMTP_SSL(SMTP_HOST, SMTP_PORT) as server:
            server.login(SMTP_USER, SMTP_PASSWORD)
            server.sendmail(SMTP_USER, [RECEIVER], msg.as_string())
        logger.info(f"失败通知邮件已发送至 {RECEIVER}")
    except Exception as e:
        logger.error(f"发送邮件失败: {e}")