"""
全自动 OAuth2 Token 获取 + 导入
无需任何手动输入
"""
import hashlib
import json
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

# ===== 用户凭据 =====
EMAIL = "elisonhuge@outlook.com"
PASSWORD = "9030bill666888"

# ===== OAuth2 配置 =====
# Thunderbird client_id + Graph API scope（读取邮件）
CLIENT_ID = "9e5f94bc-e8a4-4e73-b8be-63364c29d753"
SCOPE = "https://graph.microsoft.com/Mail.Read offline_access"
AUTH_URL = "https://login.microsoftonline.com/common/oauth2/v2.0/authorize"
TOKEN_URL = "https://login.microsoftonline.com/common/oauth2/v2.0/token"

auth_code_result = {"code": None, "error": None}


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        params = parse_qs(urlparse(self.path).query)
        if "code" in params:
            auth_code_result["code"] = params["code"][0]
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.end_headers()
            self.wfile.write("<h1 style='text-align:center;color:green;padding-top:100px'>OK! 可以关闭此页面</h1>".encode("utf-8"))
        elif "error" in params:
            auth_code_result["error"] = params.get("error_description", params.get("error", ["?"]))[0]
            self.send_response(400)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.end_headers()
            self.wfile.write(f"<h1 style='color:red'>{auth_code_result['error']}</h1>".encode("utf-8"))
        else:
            self.send_response(404)
            self.end_headers()

    def log_message(self, *a):
        pass


def main():
    # 1. 启动本地服务器
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("", 0))
        port = s.getsockname()[1]
    redirect_uri = f"http://localhost:{port}"
    server = HTTPServer(("127.0.0.1", port), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    print(f"[1/4] 回调服务: {redirect_uri}")

    # 2. 构建授权 URL（带 PKCE）
    verifier = base64.urlsafe_b64encode(secrets.token_bytes(32)).rstrip(b"=").decode()
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    url = f"{AUTH_URL}?{urlencode({'client_id': CLIENT_ID, 'response_type': 'code', 'redirect_uri': redirect_uri, 'scope': SCOPE, 'state': 'x', 'code_challenge': challenge, 'code_challenge_method': 'S256'})}"

    import webbrowser
    webbrowser.open(url)
    print("[2/4] 浏览器已打开，请登录授权...")

    # 3. 等待回调
    for _ in range(300):
        if auth_code_result["code"] or auth_code_result["error"]:
            break
        time.sleep(1)
    server.shutdown()

    if auth_code_result["error"]:
        print(f"授权失败: {auth_code_result['error']}")
        return
    if not auth_code_result["code"]:
        print("超时")
        return

    print("[3/4] 收到授权码，换取 Token...")

    # 4. 换取 token
    resp = _requests.post(TOKEN_URL, data={
        "client_id": CLIENT_ID,
        "grant_type": "authorization_code",
        "code": auth_code_result["code"],
        "redirect_uri": redirect_uri,
        "code_verifier": verifier,
        "scope": SCOPE,
    }, headers={"Content-Type": "application/x-www-form-urlencoded"}, impersonate="chrome110", timeout=30)

    if resp.status_code != 200:
        print(f"Token 失败: {resp.text[:300]}")
        return

    data = resp.json()
    refresh_token = data.get("refresh_token", "")
    if not refresh_token:
        print(f"无 refresh_token: {json.dumps(data, indent=2)}")
        return

    print("[4/4] Token 获取成功!")
    print()

    # 生成导入行
    import_line = f"{EMAIL}----{PASSWORD}----{CLIENT_ID}----{refresh_token}"

    print("=" * 60)
    print("导入格式（已自动保存）:")
    print("=" * 60)
    print(import_line)
    print("=" * 60)

    # 保存备份
    with open("outlook_token_backup.txt", "w", encoding="utf-8") as f:
        f.write(f"email: {EMAIL}\n")
        f.write(f"password: {PASSWORD}\n")
        f.write(f"client_id: {CLIENT_ID}\n")
        f.write(f"refresh_token: {refresh_token}\n")
        f.write(f"\nimport_line:\n{import_line}\n")
    print("\n已保存到 outlook_token_backup.txt")

    # 尝试通过 API 直接导入
    print("\n正在通过 API 自动导入...")
    try:
        api_resp = _requests.post(
            "http://127.0.0.1:8000/api/email-services/outlook/batch-import",
            json={"data": import_line, "enabled": True, "priority": 0},
            timeout=10,
        )
        if api_resp.status_code == 200:
            result = api_resp.json()
            print(f"自动导入成功! {json.dumps(result, ensure_ascii=False)}")
        else:
            print(f"自动导入失败 (HTTP {api_resp.status_code}): {api_resp.text[:200]}")
            print("请手动在邮箱服务页面导入上面的格式")
    except Exception as e:
        print(f"API 调用失败: {e}")
        print("请手动在邮箱服务页面导入上面的格式")


if __name__ == "__main__":
    main()
