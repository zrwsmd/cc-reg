import base64
import json

import src.core.register as register_module
from src.config.constants import EmailServiceType, OPENAI_API_ENDPOINTS, OPENAI_PAGE_TYPES
from src.core.http_client import OpenAIHTTPClient
from src.core.openai.oauth import OAuthStart
from src.core.register import RegistrationEngine
from src.services.base import BaseEmailService


class DummyResponse:
    def __init__(self, status_code=200, payload=None, text="", headers=None, on_return=None):
        self.status_code = status_code
        self._payload = payload
        self.text = text
        self.headers = headers or {}
        self.on_return = on_return

    def json(self):
        if self._payload is None:
            raise ValueError("no json payload")
        return self._payload


class QueueSession:
    def __init__(self, steps):
        self.steps = list(steps)
        self.calls = []
        self.cookies = {}

    def get(self, url, **kwargs):
        return self._request("GET", url, **kwargs)

    def post(self, url, **kwargs):
        return self._request("POST", url, **kwargs)

    def request(self, method, url, **kwargs):
        return self._request(method.upper(), url, **kwargs)

    def close(self):
        return None

    def _request(self, method, url, **kwargs):
        self.calls.append({
            "method": method,
            "url": url,
            "kwargs": kwargs,
        })
        if not self.steps:
            raise AssertionError(f"unexpected request: {method} {url}")
        expected_method, expected_url, response = self.steps.pop(0)
        assert method == expected_method
        assert url == expected_url
        if callable(response):
            response = response(self)
        if response.on_return:
            response.on_return(self)
        return response


class FakeEmailService(BaseEmailService):
    def __init__(self, codes):
        super().__init__(EmailServiceType.TEMPMAIL)
        self.codes = list(codes)
        self.otp_requests = []

    def create_email(self, config=None):
        return {
            "email": "tester@example.com",
            "service_id": "mailbox-1",
        }

    def get_verification_code(self, email, email_id=None, timeout=120, pattern=r"(?<!\d)(\d{6})(?!\d)", otp_sent_at=None):
        self.otp_requests.append({
            "email": email,
            "email_id": email_id,
            "otp_sent_at": otp_sent_at,
        })
        if not self.codes:
            raise AssertionError("no verification code queued")
        return self.codes.pop(0)

    def list_emails(self, **kwargs):
        return []

    def delete_email(self, email_id):
        return True

    def check_health(self):
        return True


class FakeOAuthManager:
    def __init__(self):
        self.start_calls = 0
        self.callback_calls = []

    def start_oauth(self):
        self.start_calls += 1
        return OAuthStart(
            auth_url=f"https://auth.example.test/flow/{self.start_calls}",
            state=f"state-{self.start_calls}",
            code_verifier=f"verifier-{self.start_calls}",
            redirect_uri="http://localhost:1455/auth/callback",
        )

    def handle_callback(self, callback_url, expected_state, code_verifier):
        self.callback_calls.append({
            "callback_url": callback_url,
            "expected_state": expected_state,
            "code_verifier": code_verifier,
        })
        return {
            "account_id": "acct-1",
            "access_token": "access-1",
            "refresh_token": "refresh-1",
            "id_token": "id-1",
        }


class FakeOpenAIClient:
    def __init__(self, sessions, sentinel_tokens):
        self._sessions = list(sessions)
        self._session_index = 0
        self._session = self._sessions[0]
        self._sentinel_tokens = list(sentinel_tokens)

    @property
    def session(self):
        return self._session

    def check_ip_location(self):
        return True, "US"

    def check_sentinel(self, did):
        if not self._sentinel_tokens:
            raise AssertionError("no sentinel token queued")
        return self._sentinel_tokens.pop(0)

    def close(self):
        if self._session_index + 1 < len(self._sessions):
            self._session_index += 1
            self._session = self._sessions[self._session_index]


def _workspace_cookie(workspace_id):
    payload = base64.urlsafe_b64encode(
        json.dumps({"workspaces": [{"id": workspace_id}]}).encode("utf-8")
    ).decode("ascii").rstrip("=")
    return f"{payload}.sig"


def _response_with_did(did):
    return DummyResponse(
        status_code=200,
        text="ok",
        on_return=lambda session: session.cookies.__setitem__("oai-did", did),
    )


def _response_with_login_cookies(workspace_id="ws-1", session_token="session-1"):
    def setter(session):
        session.cookies["oai-client-auth-session"] = _workspace_cookie(workspace_id)
        session.cookies["__Secure-next-auth.session-token"] = session_token

    return DummyResponse(status_code=200, payload={}, on_return=setter)


def test_check_sentinel_sends_non_empty_pow(monkeypatch):
    session = QueueSession([
        ("POST", OPENAI_API_ENDPOINTS["sentinel"], DummyResponse(payload={"token": "sentinel-token"})),
    ])
    client = OpenAIHTTPClient()
    client._session = session

    monkeypatch.setattr(
        "src.core.http_client.build_sentinel_pow_token",
        lambda user_agent: "gAAAAACpow-token",
    )

    token = client.check_sentinel("device-1")

    assert token == "sentinel-token"
    body = json.loads(session.calls[0]["kwargs"]["data"])
    assert body["id"] == "device-1"
    assert body["flow"] == "authorize_continue"
    assert body["p"] == "gAAAAACpow-token"


def test_run_registers_then_relogs_to_fetch_token():
    session_one = QueueSession([
        ("GET", "https://auth.example.test/flow/1", _response_with_did("did-1")),
        (
            "POST",
            OPENAI_API_ENDPOINTS["signup"],
            DummyResponse(payload={"page": {"type": OPENAI_PAGE_TYPES["PASSWORD_REGISTRATION"]}}),
        ),
        ("POST", OPENAI_API_ENDPOINTS["register"], DummyResponse(payload={})),
        ("GET", OPENAI_API_ENDPOINTS["send_otp"], DummyResponse(payload={})),
        ("POST", OPENAI_API_ENDPOINTS["validate_otp"], DummyResponse(payload={})),
        ("POST", OPENAI_API_ENDPOINTS["create_account"], DummyResponse(payload={})),
    ])
    session_two = QueueSession([
        ("GET", "https://auth.example.test/flow/2", _response_with_did("did-2")),
        (
            "POST",
            OPENAI_API_ENDPOINTS["signup"],
            DummyResponse(payload={"page": {"type": OPENAI_PAGE_TYPES["LOGIN_PASSWORD"]}}),
        ),
        (
            "POST",
            OPENAI_API_ENDPOINTS["password_verify"],
            DummyResponse(payload={"page": {"type": OPENAI_PAGE_TYPES["EMAIL_OTP_VERIFICATION"]}}),
        ),
        ("POST", OPENAI_API_ENDPOINTS["validate_otp"], _response_with_login_cookies()),
        (
            "POST",
            OPENAI_API_ENDPOINTS["select_workspace"],
            DummyResponse(payload={"continue_url": "https://auth.example.test/continue"}),
        ),
        (
            "GET",
            "https://auth.example.test/continue",
            DummyResponse(
                status_code=302,
                headers={"Location": "http://localhost:1455/auth/callback?code=code-2&state=state-2"},
            ),
        ),
    ])

    email_service = FakeEmailService(["123456", "654321"])
    engine = RegistrationEngine(email_service)
    fake_oauth = FakeOAuthManager()
    engine.http_client = FakeOpenAIClient([session_one, session_two], ["sentinel-1", "sentinel-2"])
    engine.oauth_manager = fake_oauth

    result = engine.run()

    assert result.success is True
    assert result.source == "register"
    assert result.workspace_id == "ws-1"
    assert result.session_token == "session-1"
    assert fake_oauth.start_calls == 2
    assert len(email_service.otp_requests) == 2
    assert all(item["otp_sent_at"] is not None for item in email_service.otp_requests)
    assert sum(1 for call in session_one.calls if call["url"] == OPENAI_API_ENDPOINTS["send_otp"]) == 1
    assert sum(1 for call in session_two.calls if call["url"] == OPENAI_API_ENDPOINTS["send_otp"]) == 0
    assert sum(1 for call in session_one.calls if call["url"] == OPENAI_API_ENDPOINTS["select_workspace"]) == 0
    assert sum(1 for call in session_two.calls if call["url"] == OPENAI_API_ENDPOINTS["select_workspace"]) == 1
    relogin_start_body = json.loads(session_two.calls[1]["kwargs"]["data"])
    assert relogin_start_body["screen_hint"] == "login"
    assert relogin_start_body["username"]["value"] == "tester@example.com"
    password_verify_body = json.loads(session_two.calls[2]["kwargs"]["data"])
    assert password_verify_body == {"password": result.password}
    assert result.metadata["token_acquired_via_relogin"] is True


def test_existing_account_login_uses_auto_sent_otp_without_manual_send():
    session = QueueSession([
        ("GET", "https://auth.example.test/flow/1", _response_with_did("did-1")),
        (
            "POST",
            OPENAI_API_ENDPOINTS["signup"],
            DummyResponse(payload={"page": {"type": OPENAI_PAGE_TYPES["EMAIL_OTP_VERIFICATION"]}}),
        ),
        ("POST", OPENAI_API_ENDPOINTS["validate_otp"], _response_with_login_cookies("ws-existing", "session-existing")),
        (
            "POST",
            OPENAI_API_ENDPOINTS["select_workspace"],
            DummyResponse(payload={"continue_url": "https://auth.example.test/continue-existing"}),
        ),
        (
            "GET",
            "https://auth.example.test/continue-existing",
            DummyResponse(
                status_code=302,
                headers={"Location": "http://localhost:1455/auth/callback?code=code-1&state=state-1"},
            ),
        ),
    ])

    email_service = FakeEmailService(["246810"])
    engine = RegistrationEngine(email_service)
    fake_oauth = FakeOAuthManager()
    engine.http_client = FakeOpenAIClient([session], ["sentinel-1"])
    engine.oauth_manager = fake_oauth

    result = engine.run()

    assert result.success is True
    assert result.source == "login"
    assert fake_oauth.start_calls == 1
    assert sum(1 for call in session.calls if call["url"] == OPENAI_API_ENDPOINTS["send_otp"]) == 0
    assert len(email_service.otp_requests) == 1
    assert email_service.otp_requests[0]["otp_sent_at"] is not None
    assert result.metadata["token_acquired_via_relogin"] is False


def test_verify_email_otp_retries_same_code_after_network_timeout():
    email_service = FakeEmailService(["112233"])
    engine = RegistrationEngine(email_service)

    fetched_codes = []
    validated_codes = []

    def fake_get_verification_code(timeout=None):
        fetched_codes.append(timeout)
        if len(fetched_codes) > 1:
            raise AssertionError("should retry the same OTP before fetching a new email")
        return "112233"

    def fake_validate_verification_code(code):
        validated_codes.append(code)
        engine._last_otp_validation_code = code
        engine._last_otp_validation_status_code = None
        if len(validated_codes) == 1:
            engine._last_otp_validation_outcome = "network_timeout"
            return False
        engine._last_otp_validation_outcome = "success"
        return True

    original_sleep = register_module.time.sleep
    engine._get_verification_code = fake_get_verification_code
    engine._validate_verification_code = fake_validate_verification_code
    register_module.time.sleep = lambda _seconds: None

    try:
        result = engine._verify_email_otp_with_retry(stage_label="注册验证码", max_attempts=3)
    finally:
        register_module.time.sleep = original_sleep

    assert result is True
    assert fetched_codes == [None]
    assert validated_codes == ["112233", "112233"]


def test_verify_email_otp_reuses_last_code_across_attempts_after_network_timeout():
    email_service = FakeEmailService(["445566"])
    engine = RegistrationEngine(email_service)

    fetched_codes = []
    validated_codes = []

    def fake_get_verification_code(timeout=None):
        fetched_codes.append(timeout)
        if len(fetched_codes) > 1:
            raise AssertionError("should reuse the previous OTP instead of waiting for a new email")
        return "445566"

    def fake_validate_verification_code(code):
        validated_codes.append(code)
        engine._last_otp_validation_code = code
        engine._last_otp_validation_status_code = None
        if len(validated_codes) < 3:
            engine._last_otp_validation_outcome = "network_timeout"
            engine._last_otp_validation_reason = "状态探测也超时，暂时无法判断服务端是否已处理验证码"
            return False
        engine._last_otp_validation_outcome = "success"
        engine._last_otp_validation_reason = "服务端已接受验证码"
        return True

    original_sleep = register_module.time.sleep
    engine._get_verification_code = fake_get_verification_code
    engine._validate_verification_code = fake_validate_verification_code
    register_module.time.sleep = lambda _seconds: None

    try:
        result = engine._verify_email_otp_with_retry(stage_label="注册验证码", max_attempts=3)
    finally:
        register_module.time.sleep = original_sleep

    assert result is True
    assert fetched_codes == [None]
    assert validated_codes == ["445566", "445566", "445566"]
    assert any("继续复用上一枚验证码 445566" in log for log in engine.logs)


def test_validate_verification_code_reports_probe_reason_after_timeout():
    email_service = FakeEmailService([])
    engine = RegistrationEngine(email_service)

    timeout_error = Exception(
        "Failed to perform, curl: (28) Operation timed out after 60006 milliseconds with 0 bytes received."
    )
    probe_response = DummyResponse(status_code=200, payload={})
    probe_response.url = "https://auth.openai.com/email-verification"

    session = QueueSession([
        ("POST", OPENAI_API_ENDPOINTS["validate_otp"], lambda _session: (_ for _ in ()).throw(timeout_error)),
        ("GET", "https://auth.openai.com/email-verification", probe_response),
    ])
    engine.session = session

    result = engine._validate_verification_code("123456")

    assert result is False
    assert engine._last_otp_validation_outcome == "network_timeout"
    assert "email-verification" in engine._last_otp_validation_reason
    assert any("验证码校验未推进" in log for log in engine.logs)
    assert all("curl: (28)" not in log for log in engine.logs)


def test_validate_verification_code_treats_timeout_as_success_when_probe_progressed():
    email_service = FakeEmailService([])
    engine = RegistrationEngine(email_service)

    timeout_error = Exception(
        "Failed to perform, curl: (28) Operation timed out after 60006 milliseconds with 0 bytes received."
    )
    probe_response = DummyResponse(status_code=200, payload={})
    probe_response.url = "https://auth.openai.com/about-you"

    session = QueueSession([
        ("POST", OPENAI_API_ENDPOINTS["validate_otp"], lambda _session: (_ for _ in ()).throw(timeout_error)),
        ("GET", "https://auth.openai.com/email-verification", probe_response),
    ])
    engine.session = session

    result = engine._validate_verification_code("654321")

    assert result is True
    assert engine._last_otp_validation_outcome == "success"
    assert engine._last_validate_otp_continue_url == "https://auth.openai.com/about-you"
    assert "about-you" in engine._last_otp_validation_reason


def test_create_user_account_treats_timeout_as_success_when_probe_has_progress():
    email_service = FakeEmailService([])
    engine = RegistrationEngine(email_service)

    timeout_error = Exception(
        "Failed to perform, curl: (28) Operation timed out after 30001 milliseconds with 0 bytes received."
    )
    probe_response = DummyResponse(status_code=200, payload={})
    probe_response.url = "https://auth.openai.com/add-phone"

    session = QueueSession([
        ("POST", OPENAI_API_ENDPOINTS["create_account"], lambda _session: (_ for _ in ()).throw(timeout_error)),
        ("GET", "https://auth.openai.com/about-you", probe_response),
    ])

    original_generate_user_info = register_module.generate_random_user_info
    original_build_sentinel_token = register_module.build_sentinel_token
    original_generate_datadog_trace = register_module.generate_datadog_trace
    engine.session = session
    register_module.generate_random_user_info = lambda: {
        "name": "Alice Example",
        "birthdate": "1994-06-15",
    }
    register_module.build_sentinel_token = lambda *args, **kwargs: "sentinel-token"
    register_module.generate_datadog_trace = lambda: {"traceparent": "trace-1"}

    try:
        result = engine._create_user_account()
    finally:
        register_module.generate_random_user_info = original_generate_user_info
        register_module.build_sentinel_token = original_build_sentinel_token
        register_module.generate_datadog_trace = original_generate_datadog_trace

    assert result is True
    assert engine._create_account_page_type == "add_phone"
    assert len([call for call in session.calls if call["method"] == "POST"]) == 1
    headers = session.calls[0]["kwargs"]["headers"]
    assert headers["oai-device-id"] == engine.device_id
    assert headers["openai-sentinel-token"] == "sentinel-token"
    assert headers["traceparent"] == "trace-1"


def test_create_user_account_retries_same_payload_after_timeout_without_progress():
    email_service = FakeEmailService([])
    engine = RegistrationEngine(email_service)

    timeout_error = Exception(
        "Failed to perform, curl: (28) Operation timed out after 30001 milliseconds with 0 bytes received."
    )
    probe_response = DummyResponse(status_code=200, payload={})
    probe_response.url = "https://auth.openai.com/about-you"

    create_response = DummyResponse(
        status_code=200,
        payload={
            "continue_url": "https://auth.openai.com/add-phone",
            "page": {"type": "add_phone"},
        },
    )
    create_response.url = "https://auth.openai.com/add-phone"

    session = QueueSession([
        ("POST", OPENAI_API_ENDPOINTS["create_account"], lambda _session: (_ for _ in ()).throw(timeout_error)),
        ("GET", "https://auth.openai.com/about-you", probe_response),
        ("POST", OPENAI_API_ENDPOINTS["create_account"], create_response),
    ])

    original_generate_user_info = register_module.generate_random_user_info
    original_build_sentinel_token = register_module.build_sentinel_token
    original_generate_datadog_trace = register_module.generate_datadog_trace
    original_sleep = register_module.time.sleep
    engine.session = session
    register_module.generate_random_user_info = lambda: {
        "name": "Bob Example",
        "birthdate": "1996-12-03",
    }
    register_module.build_sentinel_token = lambda *args, **kwargs: "sentinel-token"
    register_module.generate_datadog_trace = lambda: {"traceparent": "trace-2"}
    register_module.time.sleep = lambda _seconds: None

    try:
        result = engine._create_user_account()
    finally:
        register_module.generate_random_user_info = original_generate_user_info
        register_module.build_sentinel_token = original_build_sentinel_token
        register_module.generate_datadog_trace = original_generate_datadog_trace
        register_module.time.sleep = original_sleep

    assert result is True
    post_calls = [call for call in session.calls if call["method"] == "POST"]
    assert len(post_calls) == 2
    assert post_calls[0]["kwargs"]["json"] == post_calls[1]["kwargs"]["json"]
    assert post_calls[0]["kwargs"]["headers"]["openai-sentinel-token"] == "sentinel-token"
    assert engine._create_account_page_type == "add_phone"


def test_complete_token_exchange_native_backup_stops_on_add_phone_gate():
    email_service = FakeEmailService([])
    engine = RegistrationEngine(email_service)
    result = register_module.RegistrationResult(success=False, logs=engine.logs)

    engine.password = "secret-1"
    engine.device_id = "device-1"
    engine._verify_email_otp_with_retry = lambda *args, **kwargs: True
    engine._get_workspace_id = lambda: ""
    engine._last_validate_otp_continue_url = "https://auth.openai.com/add-phone"
    engine._create_account_page_type = "add_phone"
    engine._follow_redirects = lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("should not follow redirects"))

    complete_ok = engine._complete_token_exchange_native_backup(result)

    assert complete_ok is False
    assert "add-phone" in result.error_message
    assert result.password == "secret-1"
    assert result.device_id == "device-1"


def test_register_password_treats_timeout_as_success_when_probe_has_progress():
    email_service = FakeEmailService([])
    engine = RegistrationEngine(email_service)
    engine.email = "tester@example.com"

    timeout_error = Exception(
        "Failed to perform, curl: (28) Operation timed out after 30001 milliseconds with 0 bytes received."
    )
    probe_response = DummyResponse(status_code=200, payload={})
    probe_response.url = "https://auth.openai.com/email-verification"

    session = QueueSession([
        ("POST", OPENAI_API_ENDPOINTS["register"], lambda _session: (_ for _ in ()).throw(timeout_error)),
        ("GET", "https://auth.openai.com/email-verification", probe_response),
    ])

    original_generate_datadog_trace = register_module.generate_datadog_trace
    original_sleep = register_module.time.sleep
    engine.session = session
    register_module.generate_datadog_trace = lambda: {"traceparent": "trace-register-1"}
    register_module.time.sleep = lambda _seconds: None

    try:
        success, password = engine._register_password()
    finally:
        register_module.generate_datadog_trace = original_generate_datadog_trace
        register_module.time.sleep = original_sleep

    assert success is True
    assert password == engine.password
    post_headers = session.calls[0]["kwargs"]["headers"]
    assert post_headers["traceparent"] == "trace-register-1"
    assert session.calls[0]["kwargs"]["json"]["username"] == "tester@example.com"


def test_register_password_retries_same_payload_after_timeout_without_progress():
    email_service = FakeEmailService([])
    engine = RegistrationEngine(email_service)
    engine.email = "tester@example.com"

    timeout_error = Exception(
        "Failed to perform, curl: (28) Operation timed out after 30001 milliseconds with 0 bytes received."
    )
    probe_response = DummyResponse(status_code=200, payload={})
    probe_response.url = "https://auth.openai.com/create-account/password"
    success_response = DummyResponse(status_code=200, payload={})

    session = QueueSession([
        ("POST", OPENAI_API_ENDPOINTS["register"], lambda _session: (_ for _ in ()).throw(timeout_error)),
        ("GET", "https://auth.openai.com/email-verification", probe_response),
        ("POST", OPENAI_API_ENDPOINTS["register"], success_response),
    ])

    original_generate_datadog_trace = register_module.generate_datadog_trace
    original_sleep = register_module.time.sleep
    engine.session = session
    register_module.generate_datadog_trace = lambda: {"traceparent": "trace-register-2"}
    register_module.time.sleep = lambda _seconds: None

    try:
        success, password = engine._register_password()
    finally:
        register_module.generate_datadog_trace = original_generate_datadog_trace
        register_module.time.sleep = original_sleep

    assert success is True
    assert password == engine.password
    post_calls = [call for call in session.calls if call["method"] == "POST"]
    assert len(post_calls) == 2
    assert post_calls[0]["kwargs"]["json"] == post_calls[1]["kwargs"]["json"]


def test_send_verification_code_treats_timeout_as_assumed_sent():
    email_service = FakeEmailService([])
    engine = RegistrationEngine(email_service)

    timeout_error = Exception(
        "Failed to perform, curl: (28) Operation timed out after 45015 milliseconds with 0 bytes received."
    )
    session = QueueSession([
        ("GET", OPENAI_API_ENDPOINTS["send_otp"], lambda _session: (_ for _ in ()).throw(timeout_error)),
    ])
    engine.session = session

    result = engine._send_verification_code()

    assert result is True
    assert engine._last_send_otp_outcome == "timeout_assumed_sent"
    assert any("继续等待邮箱" in log for log in engine.logs)
    assert all("curl: (28)" not in log for log in engine.logs)
