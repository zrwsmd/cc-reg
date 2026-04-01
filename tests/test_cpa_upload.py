from datetime import datetime
from types import SimpleNamespace

from src.core.upload import cpa_upload


class FakeResponse:
    def __init__(self, status_code=200, payload=None, text=""):
        self.status_code = status_code
        self._payload = payload
        self.text = text

    def json(self):
        if self._payload is None:
            raise ValueError("no json payload")
        return self._payload


class FakeMime:
    def __init__(self):
        self.parts = []

    def addpart(self, **kwargs):
        self.parts.append(kwargs)


def make_jwt(payload):
    return ".".join(
        [
            cpa_upload._base64url_encode_json({"alg": "none", "typ": "JWT"}),
            cpa_upload._base64url_encode_json(payload),
            "signature",
        ]
    )


def make_account(**overrides):
    data = {
        "email": "tester@example.com",
        "expires_at": datetime(2026, 6, 1, 12, 0, 0),
        "id_token": "",
        "account_id": "acct-123",
        "workspace_id": "ws-456",
        "access_token": make_jwt(
            {
                "client_id": "app_EMoamEEZ73f0CkXaXp7hrann",
                "https://api.openai.com/auth": {
                    "chatgpt_account_id": "acct-123",
                    "workspace_id": "ws-456",
                },
            }
        ),
        "last_refresh": datetime(2026, 4, 1, 8, 0, 0),
        "refresh_token": "refresh-token",
        "subscription_type": "plus",
        "client_id": "app_EMoamEEZ73f0CkXaXp7hrann",
        "session_token": "session-token",
    }
    data.update(overrides)
    return SimpleNamespace(**data)


def test_generate_token_json_backfills_codex_id_token_when_missing():
    token_data = cpa_upload.generate_token_json(make_account())

    assert token_data["account_id"] == "acct-123"
    assert token_data["chatgpt_account_id"] == "acct-123"
    assert token_data["workspace_id"] == "ws-456"
    assert token_data["plan_type"] == "plus"
    assert token_data["client_id"] == "app_EMoamEEZ73f0CkXaXp7hrann"
    assert token_data["session_token"] == "session-token"

    assert token_data["id_token"].count(".") == 2
    id_token_payload = cpa_upload._decode_jwt_payload(token_data["id_token"])
    auth_claim = id_token_payload["https://api.openai.com/auth"]
    assert auth_claim["chatgpt_account_id"] == "acct-123"
    assert auth_claim["workspace_id"] == "ws-456"
    assert auth_claim["chatgpt_plan_type"] == "plus"


def test_generate_token_json_preserves_existing_id_token():
    existing_id_token = ".".join(
        [
            cpa_upload._base64url_encode_json({"alg": "none", "typ": "JWT"}),
            cpa_upload._base64url_encode_json(
                {
                    "email": "tester@example.com",
                    "https://api.openai.com/auth": {
                        "chatgpt_account_id": "acct-from-token",
                        "chatgpt_plan_type": "team",
                    },
                }
            ),
            "signature",
        ]
    )

    token_data = cpa_upload.generate_token_json(
        make_account(
            id_token=existing_id_token,
            account_id="",
            workspace_id="",
            subscription_type="",
        )
    )

    assert token_data["id_token"] == existing_id_token
    assert token_data["account_id"] == "acct-from-token"
    assert token_data["chatgpt_account_id"] == "acct-from-token"
    assert token_data["workspace_id"] == "acct-from-token"
    assert token_data["plan_type"] == "team"


def test_upload_to_cpa_accepts_management_root_url(monkeypatch):
    calls = []

    def fake_post(url, **kwargs):
        calls.append({"url": url, "kwargs": kwargs})
        return FakeResponse(status_code=201)

    monkeypatch.setattr(cpa_upload, "CurlMime", FakeMime)
    monkeypatch.setattr(cpa_upload.cffi_requests, "post", fake_post)

    success, message = cpa_upload.upload_to_cpa(
        {"email": "tester@example.com"},
        api_url="https://cpa.example.com/v0/management",
        api_token="token-123",
    )

    assert success is True
    assert message == "上传成功"
    assert calls[0]["url"] == "https://cpa.example.com/v0/management/auth-files"
    assert calls[0]["kwargs"]["headers"]["Authorization"] == "Bearer token-123"
    assert calls[0]["kwargs"]["headers"]["X-Management-Key"] == "token-123"


def test_upload_to_cpa_accepts_management_html_hash_url(monkeypatch):
    calls = []

    def fake_post(url, **kwargs):
        calls.append({"url": url, "kwargs": kwargs})
        return FakeResponse(status_code=201)

    monkeypatch.setattr(cpa_upload, "CurlMime", FakeMime)
    monkeypatch.setattr(cpa_upload.cffi_requests, "post", fake_post)

    success, message = cpa_upload.upload_to_cpa(
        {"email": "tester@example.com"},
        api_url="https://cpa.example.com:8317/management.html#/auth-files",
        api_token="token-123",
    )

    assert success is True
    assert message == "上传成功"
    assert calls[0]["url"] == "https://cpa.example.com:8317/v0/management/auth-files"


def test_upload_to_cpa_does_not_double_append_full_endpoint(monkeypatch):
    calls = []

    def fake_post(url, **kwargs):
        calls.append({"url": url, "kwargs": kwargs})
        return FakeResponse(status_code=201)

    monkeypatch.setattr(cpa_upload, "CurlMime", FakeMime)
    monkeypatch.setattr(cpa_upload.cffi_requests, "post", fake_post)

    success, _ = cpa_upload.upload_to_cpa(
        {"email": "tester@example.com"},
        api_url="https://cpa.example.com/v0/management/auth-files",
        api_token="token-123",
    )

    assert success is True
    assert calls[0]["url"] == "https://cpa.example.com/v0/management/auth-files"


def test_upload_to_cpa_falls_back_to_raw_json_when_multipart_returns_404(monkeypatch):
    calls = []
    responses = [
        FakeResponse(status_code=404, text="404 page not found"),
        FakeResponse(status_code=200, payload={"status": "ok"}),
    ]

    def fake_post(url, **kwargs):
        calls.append({"url": url, "kwargs": kwargs})
        return responses.pop(0)

    monkeypatch.setattr(cpa_upload, "CurlMime", FakeMime)
    monkeypatch.setattr(cpa_upload.cffi_requests, "post", fake_post)

    token_data = cpa_upload.generate_token_json(make_account(email="tester@example.com"))

    success, message = cpa_upload.upload_to_cpa(
        token_data,
        api_url="https://cpa.example.com",
        api_token="token-123",
    )

    assert success is True
    assert message == "上传成功"
    assert calls[0]["kwargs"]["multipart"] is not None
    assert calls[1]["url"] == "https://cpa.example.com/v0/management/auth-files?name=tester%40example.com.json"
    assert calls[1]["kwargs"]["headers"]["Content-Type"] == "application/json"
    assert calls[1]["kwargs"]["data"].startswith(b"{")


def test_upload_to_cpa_rejects_incomplete_codex_bundle_before_network_call(monkeypatch):
    called = {"post": False}

    def fake_post(url, **kwargs):
        called["post"] = True
        return FakeResponse(status_code=201)

    monkeypatch.setattr(cpa_upload, "CurlMime", FakeMime)
    monkeypatch.setattr(cpa_upload.cffi_requests, "post", fake_post)

    token_data = cpa_upload.generate_token_json(make_account(refresh_token=""))

    success, message = cpa_upload.upload_to_cpa(
        token_data,
        api_url="https://cpa.example.com",
        api_token="token-123",
    )

    assert success is False
    assert "refresh_token" in message
    assert called["post"] is False


def test_test_cpa_connection_uses_get_and_normalized_url(monkeypatch):
    calls = []

    def fake_get(url, **kwargs):
        calls.append({"url": url, "kwargs": kwargs})
        return FakeResponse(status_code=200, payload={"files": []})

    monkeypatch.setattr(cpa_upload.cffi_requests, "get", fake_get)

    success, message = cpa_upload.test_cpa_connection(
        "https://cpa.example.com/v0/management",
        "token-123",
    )

    assert success is True
    assert message == "CPA 连接测试成功"
    assert calls[0]["url"] == "https://cpa.example.com/v0/management/auth-files"
    assert calls[0]["kwargs"]["headers"]["Authorization"] == "Bearer token-123"
    assert calls[0]["kwargs"]["headers"]["X-Management-Key"] == "token-123"
