import base64
import json
from types import SimpleNamespace

from src.core.anyauto import register_flow


def make_jwt(payload):
    body = base64.urlsafe_b64encode(json.dumps(payload).encode("utf-8")).decode("ascii").rstrip("=")
    return f"header.{body}.signature"


class FakeEmailService:
    def create_email(self):
        return {
            "email": "tester@example.com",
            "service_id": "mailbox-1",
        }

    def get_verification_code(self, email, email_id=None, timeout=60, otp_sent_at=None):
        return "123456"


class FakeChatGPTClient:
    def __init__(self, proxy=None, verbose=False, browser_mode="protocol"):
        self.proxy = proxy
        self.verbose = verbose
        self.browser_mode = browser_mode
        self.session = SimpleNamespace(cookies=SimpleNamespace(jar=[]))
        self.device_id = "device-12345678"
        self.ua = "test-ua"
        self.sec_ch_ua = '"Chromium";v="136"'
        self.impersonate = "chrome136"
        self.last_registration_state = SimpleNamespace(continue_url="", current_url="", page_type="")
        self._log = lambda _msg: None

    def register_complete_flow(self, email, password, first_name, last_name, birthdate, skymail_adapter):
        return True, "ok"

    def reuse_session_and_get_tokens(self):
        return True, {
            "access_token": make_jwt(
                {
                    "client_id": "app_web_session_only",
                    "https://api.openai.com/auth": {
                        "chatgpt_account_id": "acct-session",
                        "workspace_id": "ws-session",
                    },
                }
            ),
            "session_token": "session-only-token",
            "account_id": "acct-session",
            "workspace_id": "ws-session",
            "auth_provider": "openai",
        }


class FakeOAuthClient:
    instances = []

    def __init__(self, config, proxy=None, verbose=False, browser_mode="protocol"):
        self.config = dict(config or {})
        self.proxy = proxy
        self.verbose = verbose
        self.browser_mode = browser_mode
        self.last_error = ""
        self.session = SimpleNamespace(cookies=SimpleNamespace(jar=[]))
        self.login_calls = 0
        self._log = lambda _msg: None
        self.__class__.instances.append(self)

    def login_passwordless_and_get_tokens(self, *args, **kwargs):
        return None

    def login_and_get_tokens(self, *args, **kwargs):
        self.login_calls += 1
        self.session.cookies.jar.append(
            SimpleNamespace(name="__Secure-next-auth.session-token", value="oauth-session-cookie")
        )
        return {
            "access_token": make_jwt(
                {
                    "client_id": "app_EMoamEEZ73f0CkXaXp7hrann",
                    "https://api.openai.com/auth": {
                        "chatgpt_account_id": "acct-oauth",
                        "workspace_id": "ws-oauth",
                    },
                }
            ),
            "refresh_token": "refresh-oauth",
            "id_token": make_jwt(
                {
                    "client_id": "app_EMoamEEZ73f0CkXaXp7hrann",
                    "https://api.openai.com/auth": {
                        "chatgpt_account_id": "acct-oauth",
                        "workspace_id": "ws-oauth",
                    },
                }
            ),
        }

    def _decode_oauth_session_cookie(self):
        return {
            "workspaces": [
                {
                    "id": "ws-oauth",
                }
            ]
        }


def test_run_uses_oauth_fallback_for_session_only_tokens(monkeypatch):
    FakeOAuthClient.instances = []

    monkeypatch.setattr(register_flow, "ChatGPTClient", FakeChatGPTClient)
    monkeypatch.setattr(register_flow, "OAuthClient", FakeOAuthClient)
    monkeypatch.setattr(register_flow, "generate_random_name", lambda: ("Test", "User"))
    monkeypatch.setattr(register_flow, "generate_random_birthday", lambda: "2000-01-01")
    monkeypatch.setattr(
        register_flow,
        "get_settings",
        lambda: SimpleNamespace(
            registration_default_password_length=16,
            openai_auth_url="https://auth.openai.com",
            openai_client_id="app_EMoamEEZ73f0CkXaXp7hrann",
            openai_redirect_uri="http://localhost:1455/auth/callback",
        ),
    )

    engine = register_flow.AnyAutoRegistrationEngine(
        email_service=FakeEmailService(),
        max_retries=1,
    )

    result = engine.run()

    assert result["success"] is True
    assert result["refresh_token"] == "refresh-oauth"
    assert result["id_token"]
    assert result["account_id"] == "acct-oauth"
    assert result["workspace_id"] == "ws-oauth"
    assert result["session_token"] == "oauth-session-cookie"
    assert len(FakeOAuthClient.instances) == 1
    assert FakeOAuthClient.instances[0].login_calls == 1
