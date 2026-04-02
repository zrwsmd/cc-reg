from src.core.anyauto.chatgpt_client import ChatGPTClient
from src.core.anyauto.utils import FlowState


class FakeSkyMailClient:
    def __init__(self, codes):
        self.codes = list(codes)
        self.calls = []

    def wait_for_verification_code(self, email, timeout=60, otp_sent_at=None, exclude_codes=None):
        self.calls.append(
            {
                "email": email,
                "timeout": timeout,
                "otp_sent_at": otp_sent_at,
                "exclude_codes": set(exclude_codes or []),
            }
        )
        if not self.codes:
            return None
        return self.codes.pop(0)


def _build_client():
    client = ChatGPTClient(verbose=False)
    client._log = lambda _msg: None
    client._browser_pause = lambda *args, **kwargs: None
    client.visit_homepage = lambda: True
    client.get_csrf_token = lambda: "csrf-token"
    client.signin = lambda email, csrf_token: "https://auth.openai.com/create-account/password"
    client.authorize = lambda _url: "https://auth.openai.com/create-account/password"
    client.send_email_otp = lambda: True
    client.create_account = lambda first_name, last_name, birthdate, return_state=False: (
        (True, FlowState(page_type="chatgpt_home", current_url="https://chatgpt.com/"))
        if return_state
        else (True, "ok")
    )
    return client


def test_register_complete_flow_retries_same_code_after_network_timeout():
    client = _build_client()
    skymail = FakeSkyMailClient(["123456"])
    verify_calls = []

    client.register_user = lambda email, password: (True, "注册成功")

    def fake_verify_email_otp(code, return_state=False):
        verify_calls.append(code)
        if len(verify_calls) < 3:
            client._last_otp_validation_outcome = "network_timeout"
            return False, "timeout"

        client._last_otp_validation_outcome = "success"
        next_state = FlowState(page_type="about_you", current_url="https://auth.openai.com/about-you")
        return (True, next_state) if return_state else (True, "验证成功")

    client.verify_email_otp = fake_verify_email_otp

    success, message = client.register_complete_flow(
        "tester@example.com",
        "Password123!",
        "Test",
        "User",
        "2000-01-01",
        skymail,
    )

    assert success is True
    assert message == "注册成功"
    assert verify_calls == ["123456", "123456", "123456"]
    assert len(skymail.calls) == 1


def test_register_complete_flow_retries_register_user_after_network_timeout():
    client = _build_client()
    skymail = FakeSkyMailClient(["654321"])
    register_attempts = []

    def fake_register_user(email, password):
        register_attempts.append((email, password))
        if len(register_attempts) == 1:
            client._last_register_outcome = "network_timeout"
            return False, "timeout"

        client._last_register_outcome = "success"
        return True, "注册成功"

    def fake_verify_email_otp(code, return_state=False):
        client._last_otp_validation_outcome = "success"
        next_state = FlowState(page_type="about_you", current_url="https://auth.openai.com/about-you")
        return (True, next_state) if return_state else (True, "验证成功")

    client.register_user = fake_register_user
    client.verify_email_otp = fake_verify_email_otp

    success, message = client.register_complete_flow(
        "tester@example.com",
        "Password123!",
        "Test",
        "User",
        "2000-01-01",
        skymail,
    )

    assert success is True
    assert message == "注册成功"
    assert len(register_attempts) == 2
    assert register_attempts[0] == register_attempts[1]
