"""
浏览器自动注册密码提交模块。

使用 websockets + Chrome CDP 驱动真实浏览器，自动处理 Cloudflare Turnstile。
当纯 API 方式被 OpenAI 风控拦截（HTTP 400）时作为降级方案。
不依赖 Playwright，仅需 websockets（已安装）和系统 Chrome/Edge。
"""

from __future__ import annotations

import json
import logging
import random
import shutil
import subprocess
import tempfile
import time
import urllib.request
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Minimal CDP client over websockets
# ---------------------------------------------------------------------------
class _CDPClient:
    """通过 WebSocket 与 Chrome DevTools Protocol 通信的极简客户端。"""

    def __init__(self, ws_url: str):
        from websockets.sync.client import connect
        self._ws = connect(ws_url, max_size=16 * 1024 * 1024)
        self._next_id = 0
        self._events: List[dict] = []

    def send(self, method: str, params: Optional[dict] = None, timeout: float = 30) -> dict:
        self._next_id += 1
        msg_id = self._next_id
        self._ws.send(json.dumps({"id": msg_id, "method": method, "params": params or {}}))
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            try:
                raw = self._ws.recv(timeout=max(deadline - time.monotonic(), 0.1))
            except Exception:
                break
            data = json.loads(raw)
            if data.get("id") == msg_id:
                return data
            # 缓存事件消息
            if "method" in data:
                self._events.append(data)
        return {}

    def drain_events(self, max_wait: float = 0.3) -> List[dict]:
        """读取缓冲区中的事件消息。"""
        collected: List[dict] = list(self._events)
        self._events.clear()
        deadline = time.monotonic() + max_wait
        while time.monotonic() < deadline:
            try:
                raw = self._ws.recv(timeout=max(deadline - time.monotonic(), 0.05))
                data = json.loads(raw)
                if "method" in data:
                    collected.append(data)
            except Exception:
                break
        return collected

    def evaluate(self, expression: str, timeout: float = 15) -> Any:
        """执行 JavaScript 并返回结果值。"""
        resp = self.send("Runtime.evaluate", {
            "expression": expression,
            "returnByValue": True,
            "awaitPromise": True,
        }, timeout=timeout)
        result = resp.get("result", {}).get("result", {})
        return result.get("value")

    def close(self):
        try:
            self._ws.close()
        except Exception:
            pass


# ---------------------------------------------------------------------------
# Cookie helpers
# ---------------------------------------------------------------------------
def _extract_cdp_cookies(session) -> List[dict]:
    """从 curl_cffi session 提取 cookie 并转为 CDP Network.setCookies 格式。"""
    cookies: List[dict] = []
    try:
        jar = getattr(session.cookies, "jar", None) if session else None
        if jar is None:
            return cookies
        for c in jar:
            name = str(c.name or "")
            value = str(c.value or "")
            domain = str(c.domain or "")
            if not name or not domain:
                continue
            http_only = False
            try:
                if hasattr(c, "_rest") and isinstance(c._rest, dict):
                    http_only = any(k.lower() == "httponly" for k in c._rest)
            except Exception:
                pass
            # CDP cookie 格式
            cdp_cookie: Dict[str, Any] = {
                "name": name,
                "value": value,
                "domain": domain,
                "path": str(c.path or "/"),
                "secure": bool(c.secure),
                "httpOnly": http_only,
            }
            cookies.append(cdp_cookie)
    except Exception as e:
        logger.warning("提取 session cookies 失败: %s", e)
    return cookies


def _inject_browser_cookies_to_session(session, cdp_cookies: List[dict]) -> int:
    """将 CDP 获取的浏览器 cookie 回注到 curl_cffi session。"""
    injected = 0
    if not session or not cdp_cookies:
        return injected
    for bc in cdp_cookies:
        name = str(bc.get("name") or "")
        value = str(bc.get("value") or "")
        domain = str(bc.get("domain") or "")
        path = str(bc.get("path") or "/")
        if not name or not value:
            continue
        try:
            session.cookies.set(name, value, domain=domain, path=path)
            injected += 1
        except Exception:
            pass
    return injected


# ---------------------------------------------------------------------------
# Main registration function
# ---------------------------------------------------------------------------
def register_password_via_browser(
    *,
    session,
    password: str,
    email: str = "",
    proxy: Optional[str] = None,
    device_id: str = "",
    timeout_seconds: int = 60,
    headless: bool = True,
    log_callback=None,
) -> Dict[str, Any]:
    """
    使用真实 Chrome 浏览器（CDP 模式）提交注册密码，自动处理 Turnstile。
    仅依赖 websockets + 系统 Chrome，不需要 Playwright。

    Returns:
        {"success": bool, "error": str, "stage": str, "cookies_synced": int}
    """
    def _log(msg, level="info"):
        if log_callback:
            log_callback(msg, level)
        getattr(logger, level if level in ("info", "warning", "error") else "info")(msg)

    # 检查 websockets 是否可用
    try:
        from websockets.sync.client import connect as _ws_check  # noqa: F401
    except Exception:
        return {"success": False, "error": "websockets not installed", "stage": "bootstrap", "cookies_synced": 0}

    # 查找 Chrome
    from .browser_bind import _find_chrome_binary
    chrome_binary = _find_chrome_binary()
    if not chrome_binary:
        return {"success": False, "error": "chrome binary not found", "stage": "chrome_not_found", "cookies_synced": 0}

    cookies = _extract_cdp_cookies(session)
    _log(f"[浏览器注册] 提取 {len(cookies)} 个 cookie，启动 Chrome CDP...")

    timeout_seconds = max(int(timeout_seconds), 30)
    cdp_port = random.randint(9520, 9680)
    user_data_dir = tempfile.mkdtemp(prefix=f"codex-reg-{cdp_port}-")
    cdp_http = f"http://127.0.0.1:{cdp_port}"

    chrome_args = [
        chrome_binary,
        f"--remote-debugging-port={cdp_port}",
        "--no-sandbox",
        "--disable-dev-shm-usage",
        "--no-first-run",
        "--no-default-browser-check",
        "--disable-extensions",
        "--disable-background-networking",
        "--disable-sync",
        "--window-size=1366,900",
        f"--user-data-dir={user_data_dir}",
        "about:blank",
    ]
    if headless:
        chrome_args.extend([
            "--headless=new", "--disable-gpu",
            "--use-gl=angle", "--use-angle=swiftshader-webgl",
            "--enable-unsafe-swiftshader",
        ])
    else:
        chrome_args.extend([
            "--use-gl=angle", "--use-angle=swiftshader-webgl",
            "--enable-unsafe-swiftshader",
        ])
    if proxy:
        chrome_args.append(f"--proxy-server={proxy}")

    chrome_proc = None
    cdp: Optional[_CDPClient] = None
    try:
        chrome_proc = subprocess.Popen(chrome_args, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

        # 等待 CDP 就绪
        ws_url = ""
        for _ in range(22):
            try:
                with urllib.request.urlopen(f"{cdp_http}/json/version", timeout=2) as resp:
                    data = json.loads(resp.read() or b"{}")
                    ws_url = data.get("webSocketDebuggerUrl", "")
                    if ws_url:
                        break
            except Exception:
                time.sleep(0.5)

        if not ws_url:
            return {"success": False, "error": "chrome cdp not responding", "stage": "cdp_unavailable", "cookies_synced": 0}

        _log("[浏览器注册] Chrome CDP 就绪，连接 WebSocket...")

        # 获取第一个 tab 的 WebSocket URL
        tab_ws_url = ""
        try:
            with urllib.request.urlopen(f"{cdp_http}/json/list", timeout=5) as resp:
                tabs = json.loads(resp.read() or b"[]")
                for tab in tabs:
                    if tab.get("type") == "page":
                        tab_ws_url = tab.get("webSocketDebuggerUrl", "")
                        break
        except Exception:
            pass
        if not tab_ws_url:
            tab_ws_url = ws_url  # fallback to browser-level

        cdp = _CDPClient(tab_ws_url)
        _log("[浏览器注册] CDP WebSocket 已连接")

        # 启用必要的 CDP 域
        cdp.send("Network.enable")
        cdp.send("Page.enable")

        # 注入 cookies
        if cookies:
            resp = cdp.send("Network.setCookies", {"cookies": cookies})
            err = resp.get("error")
            if err:
                _log(f"[浏览器注册] Cookie 注入部分失败: {err}", "warning")
                # 逐个注入
                ok = 0
                for c in cookies:
                    r = cdp.send("Network.setCookie", c)
                    if r.get("result", {}).get("success", False):
                        ok += 1
                _log(f"[浏览器注册] 逐个注入 {ok}/{len(cookies)} 个 cookie")
            else:
                _log(f"[浏览器注册] 批量注入 {len(cookies)} 个 cookie 成功")

        # 导航到密码页面
        _log("[浏览器注册] 导航到密码设置页...")
        nav_resp = cdp.send("Page.navigate", {"url": "https://auth.openai.com/create-account/password"}, timeout=30)
        if nav_resp.get("error"):
            return {"success": False, "error": f"navigate error: {nav_resp['error']}", "stage": "navigate", "cookies_synced": 0}

        # 等待页面加载
        time.sleep(4)
        cdp.drain_events(0.5)

        # 检查当前 URL
        current_url = cdp.evaluate("window.location.href") or ""
        _log(f"[浏览器注册] 当前页面: {current_url[:120]}")

        if "create-account/password" not in current_url.lower():
            body = cdp.evaluate("document.body ? document.body.innerText.slice(0, 300) : ''") or ""
            _log(f"[浏览器注册] 被重定向: {current_url[:100]}, body: {str(body)[:150]}", "warning")
            return {"success": False, "error": f"redirected: {current_url[:100]}", "stage": "wrong_page", "cookies_synced": 0}

        # 查找密码输入框
        has_input = cdp.evaluate("""(() => {
            const el = document.querySelector('input[type="password"]')
                || document.querySelector('input[name="password"]')
                || document.querySelector('#password');
            return !!el;
        })()""")
        if not has_input:
            body = cdp.evaluate("document.body ? document.body.innerText.slice(0, 300) : ''") or ""
            _log(f"[浏览器注册] 未找到密码框, body: {str(body)[:150]}", "warning")
            return {"success": False, "error": "password input not found", "stage": "no_input", "cookies_synced": 0}

        # 聚焦并填写密码（模拟逐字输入）
        _log("[浏览器注册] 填写密码...")
        cdp.evaluate("""(() => {
            const el = document.querySelector('input[type="password"]')
                || document.querySelector('input[name="password"]')
                || document.querySelector('#password');
            if (el) { el.focus(); el.click(); }
        })()""")
        time.sleep(0.5)

        # 使用 CDP Input.dispatchKeyEvent 逐字输入密码
        for ch in password:
            cdp.send("Input.dispatchKeyEvent", {
                "type": "keyDown",
                "text": ch,
                "key": ch,
            })
            cdp.send("Input.dispatchKeyEvent", {
                "type": "keyUp",
                "key": ch,
            })
            time.sleep(random.uniform(0.03, 0.08))
        time.sleep(0.5)

        # 等待 Turnstile
        _log("[浏览器注册] 等待 Turnstile 验证...")
        _wait_turnstile_cdp(cdp, max_wait=25)

        # 查找并点击提交按钮
        _log("[浏览器注册] 查找提交按钮...")
        clicked = cdp.evaluate("""(() => {
            // 优先找 type=submit 按钮
            let btn = document.querySelector('button[type="submit"]');
            if (!btn) {
                // 兜底：找包含 Continue 文字的按钮
                const btns = document.querySelectorAll('button');
                for (const b of btns) {
                    const txt = (b.textContent || '').trim().toLowerCase();
                    if (txt === 'continue' || txt === 'sign up' || txt === 'create account') {
                        btn = b;
                        break;
                    }
                }
            }
            if (btn) { btn.click(); return true; }
            return false;
        })()""")

        if not clicked:
            _log("[浏览器注册] 未找到提交按钮", "warning")
            return {"success": False, "error": "submit button not found", "stage": "no_button", "cookies_synced": 0}

        _log("[浏览器注册] 已点击提交，等待结果...")

        # 等待结果
        result = _wait_for_result_cdp(cdp, _log, max_wait=30)

        # 回注 cookies
        cookies_synced = 0
        try:
            cookie_resp = cdp.send("Network.getAllCookies")
            browser_cookies = cookie_resp.get("result", {}).get("cookies", [])
            cookies_synced = _inject_browser_cookies_to_session(session, browser_cookies)
            _log(f"[浏览器注册] 回注 {cookies_synced} 个 cookie")
        except Exception as ce:
            _log(f"[浏览器注册] Cookie 回注失败: {ce}", "warning")

        result["cookies_synced"] = cookies_synced
        return result

    except Exception as exc:
        _log(f"[浏览器注册] 异常: {exc}", "error")
        return {"success": False, "error": f"exception: {exc}", "stage": "exception", "cookies_synced": 0}
    finally:
        if cdp:
            cdp.close()
        if chrome_proc:
            try:
                chrome_proc.terminate()
                chrome_proc.wait(timeout=4)
            except Exception:
                try:
                    chrome_proc.kill()
                except Exception:
                    pass
        shutil.rmtree(user_data_dir, ignore_errors=True)


def _wait_turnstile_cdp(cdp: _CDPClient, max_wait: int = 20):
    """等待 Cloudflare Turnstile 自动完成验证。"""
    deadline = time.monotonic() + max_wait
    while time.monotonic() < deadline:
        try:
            result = cdp.evaluate("""(() => {
                // 检测 Turnstile iframe
                const frames = document.querySelectorAll('iframe[src*="challenges.cloudflare.com"]');
                if (!frames.length) return 'no_turnstile';
                // 检测是否已解决
                const inp = document.querySelector('[name="cf-turnstile-response"]');
                if (inp && inp.value) return 'solved';
                return 'pending';
            })()""")
            if result in ("no_turnstile", "solved"):
                return True
        except Exception:
            pass
        time.sleep(1)
    return False


def _wait_for_result_cdp(cdp: _CDPClient, _log, max_wait: int = 30) -> Dict[str, Any]:
    """等待密码提交后的页面跳转结果。"""
    deadline = time.monotonic() + max_wait
    while time.monotonic() < deadline:
        time.sleep(2)
        try:
            url = str(cdp.evaluate("window.location.href") or "").lower()
            body = str(cdp.evaluate("document.body ? document.body.innerText.slice(0, 800) : ''") or "").lower()
        except Exception:
            continue

        if "email-verification" in url or "verify-email" in url:
            _log("[浏览器注册] ✅ 成功 → 邮箱验证页")
            return {"success": True, "error": "", "stage": "email_verification"}

        if "about-you" in url:
            _log("[浏览器注册] ✅ 成功 → 个人信息页")
            return {"success": True, "error": "", "stage": "about_you"}

        if "add-phone" in url or "phone" in url:
            _log("[浏览器注册] ✅ 密码注册成功（需手机验证）")
            return {"success": True, "error": "phone_verification_required", "stage": "add_phone"}

        if "failed to create" in body:
            _log("[浏览器注册] ❌ 仍然失败: Failed to create account", "warning")
            return {"success": False, "error": "Failed to create account (browser)", "stage": "create_failed"}

        if ("already" in body and "exists" in body) or "already registered" in body:
            _log("[浏览器注册] 邮箱已注册", "warning")
            return {"success": False, "error": "email already exists", "stage": "already_exists"}

        if "create-account/password" in url:
            continue

    current_url = str(cdp.evaluate("window.location.href") or "")
    _log(f"[浏览器注册] 超时，当前: {current_url[:120]}", "warning")
    return {"success": False, "error": "timeout", "stage": "timeout"}
