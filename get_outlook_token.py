"""
Outlook OAuth2 Token 获取工具
通过 Authorization Code Flow 获取 client_id 和 refresh_token
支持个人 Outlook.com 账户
"""

import hashlib
import json
import os
import secrets
import socket
import sys
import time
import threading
import base64
from http.server import HTTPServer, BaseHTTPRequestHandler
from urllib.parse import urlencode, urlparse, parse_qs

try:
    from curl_cffi import requests as _requests
except ImportError:
    import subprocess
    subprocess.check_call([sys.executable, "-m", "pip", "install", "curl_cffi"])
    from curl_cffi import requests as _requests


# ==================== 配置 ====================
# Thunderbird 公共 client_id（支持个人 Outlook.com 账户）
CLIENT_ID = "9e5f94bc-e8a4-4e73-b8be-63364c29d753"

# IMAP scope
SCOPE = "https://outlook.office.com/IMAP.AccessAsUser.All offline_access"

# OAuth2 站点
AUTH_URL = "https://login.microsoftonline.com/common/oauth2/v2.0/authorize"
TOKEN_URL = "https://login.microsoftonline.com/common/oauth2/v2.0/token"

# 回调配置（运行时填充实际端口）
REDIRECT_URI = ""

# 全局变量，用于接收回调
auth_code_result = {"code": None, "error": None}


class OAuthCallbackHandler(BaseHTTPRequestHandler):
    """处理 OAuth2 回调"""
    def do_GET(self):
        parsed = urlparse(self.path)
        params = parse_qs(parsed.query)
        if "code" in params:
            auth_code_result["code"] = params["code"][0]
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.end_headers()
            self.wfile.write(
                "<html><body style='background:#1a1a2e;color:#0f0;text-align:center;"
                "padding-top:100px;font-size:24px;'>"
                "<h1>\u2705 \u6388\u6743\u6210\u529f\uff01</h1>"
                "<p>\u8bf7\u8fd4\u56de\u7ec8\u7aef\u67e5\u770b\u7ed3\u679c\uff0c\u53ef\u4ee5\u5173\u95ed\u6b64\u9875\u9762\u3002</p>"
                "</body></html>".encode("utf-8")
            )
        elif "error" in params:
            desc = params.get("error_description", params.get("error", ["unknown"]))[0]
            auth_code_result["error"] = desc
            self.send_response(400)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.end_headers()
            self.wfile.write(f"<html><body><h1>\u6388\u6743\u5931\u8d25: {desc}</h1></body></html>".encode("utf-8"))
        else:
            self.send_response(404)
            self.end_headers()

    def log_message(self, format, *args):
        pass


def find_free_port():
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("", 0))
        return s.getsockname()[1]


def generate_pkce():
    """生成 PKCE code_verifier 和 code_challenge"""
    code_verifier = base64.urlsafe_b64encode(secrets.token_bytes(32)).rstrip(b"=").decode("ascii")
    code_challenge = base64.urlsafe_b64encode(
        hashlib.sha256(code_verifier.encode("ascii")).digest()
    ).rstrip(b"=").decode("ascii")
    return code_verifier, code_challenge


def exchange_code_for_token(code, redirect_uri, code_verifier):
    """用授权码换取 token"""
    resp = _requests.post(
        TOKEN_URL,
        data={
            "client_id": CLIENT_ID,
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": redirect_uri,
            "code_verifier": code_verifier,
            "scope": SCOPE,
        },
        headers={
            "Content-Type": "application/x-www-form-urlencoded",
            "Accept": "application/json",
        },
        impersonate="chrome110",
        timeout=30,
    )

    if resp.status_code != 200:
        print(f"换取 token 失败: HTTP {resp.status_code}")
        print(resp.text[:500])
        return None

    return resp.json()


def main():
    global REDIRECT_URI

    print("=" * 60)
    print("  Outlook OAuth2 Token 获取工具")
    print("  支持个人 Outlook.com 账户")
    print("=" * 60)
    print()

    # 步骤 1: 启动本地回调服务器
    port = find_free_port()
    REDIRECT_URI = f"http://localhost:{port}"
    server = HTTPServer(("127.0.0.1", port), OAuthCallbackHandler)
    server_thread = threading.Thread(target=server.serve_forever, daemon=True)
    server_thread.start()
    print(f"[1/3] 本地回调服务已启动: {REDIRECT_URI}")

    # 步骤 2: 生成 PKCE 并打开浏览器
    code_verifier, code_challenge = generate_pkce()
    state = secrets.token_hex(16)

    auth_params = {
        "client_id": CLIENT_ID,
        "response_type": "code",
        "redirect_uri": REDIRECT_URI,
        "scope": SCOPE,
        "state": state,
        "code_challenge": code_challenge,
        "code_challenge_method": "S256",
        "response_mode": "query",
    }
    auth_full_url = f"{AUTH_URL}?{urlencode(auth_params)}"

    print("[2/3] 正在打开浏览器...请登录并同意授权")
    print()

    try:
        import webbrowser
        webbrowser.open(auth_full_url)
        print("      （已自动打开浏览器）")
    except Exception:
        print(f"      请手动打开: {auth_full_url}")

    print()
    print("等待授权回调...", end="", flush=True)

    # 等待回调（最多 5 分钟）
    timeout = 300
    start = time.time()
    while time.time() - start < timeout:
        if auth_code_result["code"] or auth_code_result["error"]:
            break
        print(".", end="", flush=True)
        time.sleep(1)

    server.shutdown()
    print()

    if auth_code_result["error"]:
        print(f"授权失败: {auth_code_result['error']}")
        sys.exit(1)

    if not auth_code_result["code"]:
        print("等待授权超时")
        sys.exit(1)

    code = auth_code_result["code"]
    print("收到授权码！")
    print()

    # 步骤 3: 换取 Token
    print("[3/3] 正在换取 Token...")
    token_data = exchange_code_for_token(code, REDIRECT_URI, code_verifier)

    if not token_data:
        print("换取 token 失败")
        sys.exit(1)

    refresh_token = token_data.get("refresh_token", "")
    if not refresh_token:
        print("错误: 未获取到 refresh_token")
        print(f"响应: {json.dumps(token_data, indent=2)}")
        sys.exit(1)

    print()
    print("=" * 60)
    print("  授权成功！")
    print("=" * 60)
    print(f"  client_id:     {CLIENT_ID}")
    print(f"  refresh_token: {refresh_token[:50]}...")
    print()

    # 让用户输入邮箱和密码
    print("-" * 60)
    email = input("请输入你的 Outlook 邮箱地址: ").strip()
    password = input("请输入你的 Outlook 密码: ").strip()
    print()

    # 生成导入格式
    import_line = f"{email}----{password}----{CLIENT_ID}----{refresh_token}"

    print("=" * 60)
    print("  请复制下面这行，粘贴到「邮箱服务」→「Outlook 批量导入」中")
    print("=" * 60)
    print()
    print(import_line)
    print()
    print("=" * 60)
    print()
    print("操作步骤：")
    print("  1. 先在邮箱服务页面删除旧的 Outlook 账户")
    print("  2. 在 Outlook 批量导入中粘贴上面的内容")
    print("  3. 点击导入")
    print("  4. 回到注册页面重新注册")
    print()

    # 保存到文件备份
    try:
        with open("outlook_token_backup.txt", "w", encoding="utf-8") as f:
            f.write(f"email: {email}\n")
            f.write(f"client_id: {CLIENT_ID}\n")
            f.write(f"refresh_token: {refresh_token}\n")
            f.write(f"\n导入格式:\n{import_line}\n")
        print("（凭据已备份到 outlook_token_backup.txt）")
    except Exception:
        pass


if __name__ == "__main__":
    main()
