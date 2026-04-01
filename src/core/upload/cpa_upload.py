"""
CPA (Codex Protocol API) 上传功能
"""

import base64
import json
import logging
from typing import List, Dict, Any, Tuple, Optional
from datetime import datetime
from urllib.parse import quote, urlsplit, urlunsplit

from curl_cffi import requests as cffi_requests
from curl_cffi import CurlMime

from ...database.session import get_db
from ...database.models import Account
from ...config.settings import get_settings

logger = logging.getLogger(__name__)


def _decode_jwt_payload(token: Optional[str]) -> Dict[str, Any]:
    if not token or not isinstance(token, str):
        return {}

    parts = token.split(".")
    if len(parts) < 2:
        return {}

    payload = parts[1]
    padding = "=" * (-len(payload) % 4)

    try:
        decoded = base64.urlsafe_b64decode(payload + padding).decode("utf-8")
        parsed = json.loads(decoded)
    except Exception:
        return {}

    return parsed if isinstance(parsed, dict) else {}


def _base64url_encode_json(payload: Dict[str, Any]) -> str:
    raw = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def _parse_json_object(value: Any) -> Dict[str, Any]:
    if isinstance(value, dict):
        return value

    if not isinstance(value, str):
        return {}

    text = value.strip()
    if not text:
        return {}

    try:
        parsed = json.loads(text)
    except Exception:
        return {}

    return parsed if isinstance(parsed, dict) else {}


def _extract_auth_claim(payload: Dict[str, Any]) -> Dict[str, Any]:
    auth = payload.get("https://api.openai.com/auth")
    if isinstance(auth, dict):
        return auth

    auth = payload.get("auth_data")
    if isinstance(auth, dict):
        return auth

    return {}


def _extract_chatgpt_account_id_from_token(token: Optional[str]) -> str:
    payload = _parse_json_object(token) or _decode_jwt_payload(token)
    if not payload:
        return ""

    auth = _extract_auth_claim(payload)
    for key in ("chatgpt_account_id", "chatgptAccountId", "account_id", "workspace_id"):
        value = str(auth.get(key) or payload.get(key) or "").strip()
        if value:
            return value

    return ""


def _extract_plan_type_from_token(token: Optional[str]) -> str:
    payload = _parse_json_object(token) or _decode_jwt_payload(token)
    if not payload:
        return ""

    auth = _extract_auth_claim(payload)
    for key in ("plan_type", "planType", "chatgpt_plan_type", "subscription_plan", "subscription_tier"):
        value = str(auth.get(key) or payload.get(key) or "").strip()
        if value:
            return value

    return ""


def _is_jwt_token(token: Optional[str]) -> bool:
    token_text = str(token or "").strip()
    return token_text.count(".") == 2 and bool(_decode_jwt_payload(token_text))


def _resolve_chatgpt_account_id(account: Account) -> str:
    for candidate in (
        getattr(account, "account_id", None),
        _extract_chatgpt_account_id_from_token(getattr(account, "id_token", None)),
        _extract_chatgpt_account_id_from_token(getattr(account, "access_token", None)),
        getattr(account, "workspace_id", None),
    ):
        value = str(candidate or "").strip()
        if value:
            return value

    return ""


def _resolve_plan_type(account: Account) -> str:
    for candidate in (
        getattr(account, "subscription_type", None),
        _extract_plan_type_from_token(getattr(account, "id_token", None)),
        _extract_plan_type_from_token(getattr(account, "access_token", None)),
    ):
        value = str(candidate or "").strip()
        if value:
            return value

    return ""


def _build_export_id_token(account: Account, chatgpt_account_id: str, workspace_id: str, plan_type: str) -> str:
    raw_id_token = str(getattr(account, "id_token", "") or "").strip()
    if raw_id_token and _is_jwt_token(raw_id_token):
        return raw_id_token

    synthetic_auth_claim: Dict[str, Any] = {}
    if chatgpt_account_id:
        synthetic_auth_claim["chatgpt_account_id"] = chatgpt_account_id
        synthetic_auth_claim["chatgpt_user_id"] = chatgpt_account_id
        synthetic_auth_claim["user_id"] = chatgpt_account_id
    if workspace_id:
        synthetic_auth_claim["workspace_id"] = workspace_id
        synthetic_auth_claim["organizations"] = [
            {
                "id": workspace_id,
                "is_default": True,
                "role": "owner",
                "title": "Default",
            }
        ]
    if plan_type:
        synthetic_auth_claim["chatgpt_plan_type"] = plan_type

    if not synthetic_auth_claim:
        return ""

    now_ts = int(datetime.utcnow().timestamp())
    payload: Dict[str, Any] = {
        "iss": "https://auth.openai.com",
        "aud": ["codex-cli"],
        "iat": now_ts,
        "exp": now_ts + 86400 * 365,
        "email": str(getattr(account, "email", "") or "").strip(),
        "sub": chatgpt_account_id or workspace_id or str(getattr(account, "email", "") or "").strip(),
        "https://api.openai.com/auth": synthetic_auth_claim,
    }
    header = {"alg": "none", "typ": "JWT"}

    return ".".join(
        (
            _base64url_encode_json(header),
            _base64url_encode_json(payload),
            "cli-proxy-api",
        )
    )


def _normalize_cpa_auth_files_url(api_url: str) -> str:
    """将用户填写的 CPA 地址规范化为 auth-files 接口地址。"""
    raw_url = (api_url or "").strip()
    if not raw_url:
        return ""

    parsed = urlsplit(raw_url)
    path = (parsed.path or "").rstrip("/")
    lower_path = path.lower()

    def _build_url(final_path: str) -> str:
        clean_path = final_path if final_path.startswith("/") else f"/{final_path}"
        return urlunsplit((parsed.scheme, parsed.netloc, clean_path, "", ""))

    if lower_path.endswith("/management.html") or lower_path.endswith("/management.htm"):
        base_path = path.rsplit("/", 1)[0] if "/" in path else ""
        return _build_url(f"{base_path}/v0/management/auth-files")

    if lower_path.endswith("/auth-files"):
        return _build_url(path)

    if lower_path.endswith("/v0/management") or lower_path.endswith("/management"):
        return _build_url(f"{path}/auth-files")

    if lower_path.endswith("/v0"):
        return _build_url(f"{path}/management/auth-files")

    if path:
        return _build_url(f"{path}/v0/management/auth-files")

    return _build_url("/v0/management/auth-files")


def _build_cpa_headers(api_token: str, content_type: Optional[str] = None) -> dict:
    headers = {
        "Authorization": f"Bearer {api_token}",
        "X-Management-Key": api_token,
    }
    if content_type:
        headers["Content-Type"] = content_type
    return headers


def _extract_cpa_error(response) -> str:
    error_msg = f"上传失败: HTTP {response.status_code}"
    try:
        error_detail = response.json()
        if isinstance(error_detail, dict):
            error_msg = error_detail.get("message", error_msg)
    except Exception:
        error_msg = f"{error_msg} - {response.text[:200]}"
    return error_msg


def _post_cpa_auth_file_multipart(upload_url: str, filename: str, file_content: bytes, api_token: str):
    mime = CurlMime()
    mime.addpart(
        name="file",
        data=file_content,
        filename=filename,
        content_type="application/json",
    )

    return cffi_requests.post(
        upload_url,
        multipart=mime,
        headers=_build_cpa_headers(api_token),
        proxies=None,
        timeout=30,
        impersonate="chrome110",
    )


def _post_cpa_auth_file_raw_json(upload_url: str, filename: str, file_content: bytes, api_token: str):
    raw_upload_url = f"{upload_url}?name={quote(filename)}"
    return cffi_requests.post(
        raw_upload_url,
        data=file_content,
        headers=_build_cpa_headers(api_token, content_type="application/json"),
        proxies=None,
        timeout=30,
        impersonate="chrome110",
    )


def generate_token_json(account: Account) -> dict:
    """
    生成 CPA 格式的 Token JSON

    Args:
        account: 账号模型实例

    Returns:
        CPA 格式的 Token 字典
    """
    chatgpt_account_id = _resolve_chatgpt_account_id(account)
    workspace_id = str(getattr(account, "workspace_id", "") or chatgpt_account_id or "").strip()
    plan_type = _resolve_plan_type(account)
    export_id_token = _build_export_id_token(account, chatgpt_account_id, workspace_id, plan_type)

    return {
        "type": "codex",
        "email": account.email,
        "expired": account.expires_at.strftime("%Y-%m-%dT%H:%M:%S+08:00") if account.expires_at else "",
        "id_token": export_id_token,
        "account_id": chatgpt_account_id,
        "chatgpt_account_id": chatgpt_account_id,
        "workspace_id": workspace_id,
        "plan_type": plan_type,
        "access_token": account.access_token or "",
        "last_refresh": account.last_refresh.strftime("%Y-%m-%dT%H:%M:%S+08:00") if account.last_refresh else "",
        "refresh_token": account.refresh_token or "",
    }


def upload_to_cpa(
    token_data: dict,
    proxy: str = None,
    api_url: str = None,
    api_token: str = None,
) -> Tuple[bool, str]:
    """
    上传单个账号到 CPA 管理平台（不走代理）

    Args:
        token_data: Token JSON 数据
        proxy: 保留参数，不使用（CPA 上传始终直连）
        api_url: 指定 CPA API URL（优先于全局配置）
        api_token: 指定 CPA API Token（优先于全局配置）

    Returns:
        (成功标志, 消息或错误信息)
    """
    settings = get_settings()

    # 优先使用传入的参数，否则退回全局配置
    effective_url = api_url or settings.cpa_api_url
    effective_token = api_token or (settings.cpa_api_token.get_secret_value() if settings.cpa_api_token else "")

    # 仅当未指定服务时才检查全局启用开关
    if not api_url and not settings.cpa_enabled:
        return False, "CPA 上传未启用"

    if not effective_url:
        return False, "CPA API URL 未配置"

    if not effective_token:
        return False, "CPA API Token 未配置"

    upload_url = _normalize_cpa_auth_files_url(effective_url)

    filename = f"{token_data['email']}.json"
    file_content = json.dumps(token_data, ensure_ascii=False, indent=2).encode("utf-8")

    try:
        response = _post_cpa_auth_file_multipart(
            upload_url,
            filename,
            file_content,
            effective_token,
        )

        if response.status_code in (200, 201):
            return True, "上传成功"

        if response.status_code in (404, 405, 415):
            logger.warning("CPA multipart 上传失败，尝试原始 JSON 回退: %s", response.status_code)
            fallback_response = _post_cpa_auth_file_raw_json(
                upload_url,
                filename,
                file_content,
                effective_token,
            )
            if fallback_response.status_code in (200, 201):
                return True, "上传成功"
            response = fallback_response

        return False, _extract_cpa_error(response)

    except Exception as e:
        logger.error(f"CPA 上传异常: {e}")
        return False, f"上传异常: {str(e)}"


def batch_upload_to_cpa(
    account_ids: List[int],
    proxy: str = None,
    api_url: str = None,
    api_token: str = None,
) -> dict:
    """
    批量上传账号到 CPA 管理平台

    Args:
        account_ids: 账号 ID 列表
        proxy: 可选的代理 URL
        api_url: 指定 CPA API URL（优先于全局配置）
        api_token: 指定 CPA API Token（优先于全局配置）

    Returns:
        包含成功/失败统计和详情的字典
    """
    results = {
        "success_count": 0,
        "failed_count": 0,
        "skipped_count": 0,
        "details": []
    }

    with get_db() as db:
        for account_id in account_ids:
            account = db.query(Account).filter(Account.id == account_id).first()

            if not account:
                results["failed_count"] += 1
                results["details"].append({
                    "id": account_id,
                    "email": None,
                    "success": False,
                    "error": "账号不存在"
                })
                continue

            # 检查是否已有 Token
            if not account.access_token:
                results["skipped_count"] += 1
                results["details"].append({
                    "id": account_id,
                    "email": account.email,
                    "success": False,
                    "error": "缺少 Token"
                })
                continue

            # 生成 Token JSON
            token_data = generate_token_json(account)

            # 上传
            success, message = upload_to_cpa(token_data, proxy, api_url=api_url, api_token=api_token)

            if success:
                # 更新数据库状态
                account.cpa_uploaded = True
                account.cpa_uploaded_at = datetime.utcnow()
                db.commit()

                results["success_count"] += 1
                results["details"].append({
                    "id": account_id,
                    "email": account.email,
                    "success": True,
                    "message": message
                })
            else:
                results["failed_count"] += 1
                results["details"].append({
                    "id": account_id,
                    "email": account.email,
                    "success": False,
                    "error": message
                })

    return results


def test_cpa_connection(api_url: str, api_token: str, proxy: str = None) -> Tuple[bool, str]:
    """
    测试 CPA 连接（不走代理）

    Args:
        api_url: CPA API URL
        api_token: CPA API Token
        proxy: 保留参数，不使用（CPA 始终直连）

    Returns:
        (成功标志, 消息)
    """
    if not api_url:
        return False, "API URL 不能为空"

    if not api_token:
        return False, "API Token 不能为空"

    test_url = _normalize_cpa_auth_files_url(api_url)
    headers = _build_cpa_headers(api_token)

    try:
        response = cffi_requests.get(
            test_url,
            headers=headers,
            proxies=None,
            timeout=10,
            impersonate="chrome110",
        )

        if response.status_code == 200:
            return True, "CPA 连接测试成功"
        if response.status_code == 401:
            return False, "连接成功，但 API Token 无效"
        if response.status_code == 403:
            return False, "连接成功，但服务端未启用远程管理或当前 Token 无权限"
        if response.status_code == 404:
            return False, "未找到 CPA auth-files 接口，请检查 API URL 是否填写为根地址、/v0/management 或完整 auth-files 地址"
        if response.status_code == 503:
            return False, "连接成功，但服务端认证管理器不可用"

        return False, f"服务器返回异常状态码: {response.status_code}"

    except cffi_requests.exceptions.ConnectionError as e:
        return False, f"无法连接到服务器: {str(e)}"
    except cffi_requests.exceptions.Timeout:
        return False, "连接超时，请检查网络配置"
    except Exception as e:
        return False, f"连接测试失败: {str(e)}"
