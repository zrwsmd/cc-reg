from __future__ import annotations

import os
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.config.constants import EmailServiceType, OPENAI_PAGE_TYPES
from src.config.settings import get_settings
from src.core.dynamic_proxy import get_proxy_url_for_task
from src.core.register import RegistrationEngine
from src.core.timezone_utils import apply_process_timezone
from src.database.init_db import initialize_database
from src.services.base import BaseEmailService


FIXED_EMAIL = "hhheinaisher80@rwsmd.eu.cc"


class FixedEmailService(BaseEmailService):
    def __init__(self, email: str):
        super().__init__(EmailServiceType.TEMPMAIL, name="fixed_email_debug")
        self.email = str(email or "").strip().lower()

    def create_email(self, config=None):
        return {
            "email": self.email,
            "service_id": self.email,
        }

    def get_verification_code(
        self,
        email,
        email_id=None,
        timeout=120,
        pattern=r"(?<!\d)(\d{6})(?!\d)",
        otp_sent_at=None,
    ):
        return None

    def list_emails(self, **kwargs):
        return []

    def delete_email(self, email_id: str) -> bool:
        return True

    def check_health(self) -> bool:
        return True


def _bootstrap_environment() -> None:
    apply_process_timezone()
    data_dir = ROOT / "data"
    logs_dir = ROOT / "logs"
    data_dir.mkdir(exist_ok=True)
    logs_dir.mkdir(exist_ok=True)
    os.environ.setdefault("APP_DATA_DIR", str(data_dir))
    os.environ.setdefault("APP_LOGS_DIR", str(logs_dir))
    initialize_database()


def main() -> int:
    _bootstrap_environment()

    settings = get_settings()
    proxy_url = get_proxy_url_for_task()
    print(f"[脚本] 固定邮箱: {FIXED_EMAIL}")
    print(f"[脚本] 当前入口流: {settings.registration_entry_flow}")
    print(f"[脚本] 当前代理: {proxy_url or 'direct'}")

    email_service = FixedEmailService(FIXED_EMAIL)
    engine = RegistrationEngine(
        email_service=email_service,
        proxy_url=proxy_url,
        callback_logger=lambda msg: print(msg),
    )

    engine.email = FIXED_EMAIL.lower()
    engine.inbox_email = engine.email
    engine.email_info = email_service.create_email()

    did, sen_token = engine._prepare_authorize_flow("固定邮箱触发验证码")
    if not did:
        print("[脚本] 失败: 没拿到 Device ID")
        return 1
    if not sen_token:
        print("[脚本] 警告: 没拿到 Sentinel token, 继续尝试")

    signup_result = engine._submit_signup_form(did, sen_token)
    if not signup_result.success:
        print(f"[脚本] 失败: 注册入口没走通: {signup_result.error_message}")
        return 2

    print(f"[脚本] 注册入口页面类型: {signup_result.page_type or 'unknown'}")

    if signup_result.page_type == OPENAI_PAGE_TYPES["EMAIL_OTP_VERIFICATION"]:
        print("[脚本] 当前邮箱直接进入验证码页, OpenAI 可能已经自动发过验证码")
        resend_ok = engine._send_verification_code(referer="https://auth.openai.com/email-verification")
        if engine._last_send_otp_reason:
            print(f"[脚本] {engine._last_send_otp_reason}")
        print(f"[脚本] 尝试补发验证码: {'成功' if resend_ok else '失败'}")
        return 0 if resend_ok else 3

    if signup_result.page_type != OPENAI_PAGE_TYPES["PASSWORD_REGISTRATION"]:
        print(
            f"[脚本] 失败: 当前页面不是 create_account_password, 而是 {signup_result.page_type or 'unknown'}"
        )
        return 4

    password_ok, password = engine._register_password(did, sen_token)
    if not password_ok:
        print(f"[脚本] 失败: 提交密码没走通: {engine._last_register_password_error or 'unknown'}")
        return 5

    print(f"[脚本] 本次固定邮箱生成密码: {password}")
    send_ok = engine._send_verification_code()
    if engine._last_send_otp_outcome == "timeout_assumed_sent":
        print("[脚本] 最终发送验证码结果: 接口超时, 但按已发送继续")
    else:
        print(f"[脚本] 最终发送验证码结果: {'成功' if send_ok else '失败'}")

    if engine._last_send_otp_reason:
        print(f"[脚本] {engine._last_send_otp_reason}")

    if send_ok:
        print(f"[脚本] 现在去邮箱 {FIXED_EMAIL} 看是否收到验证码")
        return 0
    return 6


if __name__ == "__main__":
    raise SystemExit(main())
