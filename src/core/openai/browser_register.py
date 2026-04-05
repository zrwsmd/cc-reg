"""
浏览器自动注册密码提交模块。

使用 Playwright + Chrome CDP 驱动真实浏览器，自动处理 Cloudflare Turnstile。
当纯 API 方式被 OpenAI 风控拦截（HTTP 400）时作为降级方案。
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


def _extract_cookies_for_playwright(session) -> List[dict]:
    """从 curl_cffi session 提取 cookie 并转为 Playwright 格式。"""
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
            cookies.append({
                "name": name,
                "value": value,
                "domain": domain,
                "path": str(c.path or "/"),
                "secure": bool(c.secure),
                "httpOnly": http_only,
                "sameSite": "Lax",
            })
    except Exception as e:
        logger.warning("提取 session cookies 失败: %s", e)
    return cookies


def _inject_browser_cookies_to_session(session, browser_cookies: List[dict]) -> int:
    """将 Playwright 浏览器 cookie 回注到 curl_cffi session。"""
    injected = 0
    if not session or not browser_cookies:
        return injected
    for bc in browser_cookies:
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


def _inject_pw_cookies(context, cookies: List[dict], _log) -> None:
    """注入 cookies 到 Playwright context，失败时逐个降级。"""
    try:
        context.add_cookies(cookies)
        _log(f"[浏览器注册] 批量注入 {len(cookies)} 个 cookie")
        return
    except Exception as e:
        _log(f"[浏览器注册] 批量注入失败，逐个降级: {e}", "warning")
    ok = 0
    for c in cookies:
        try:
            context.add_cookies([c])
            ok += 1
        except Exception:
            pass
    _log(f"[浏览器注册] 逐个注入 {ok}/{len(cookies)} 个 cookie")


def _find_password_input(page):
    for sel in ['input[type="password"]', 'input[name="password"]', '#password']:
        try:
            el = page.wait_for_selector(sel, timeout=8000)
            if el:
                return el
        except Exception:
            continue
    return None


def _find_submit_btn(page):
    selectors = [
        'button[type="submit"]',
        'button:has-text("Continue")',
        'button:has-text("Sign up")',
        'button:has-text("Create account")',
        '[data-testid*="submit"]',
        '[data-testid*="continue"]',
    ]
    for sel in selectors:
        try:
            btn = page.query_selector(sel)
            if btn and btn.is_visible():
                return btn
        except Exception:
            continue
    return None


def _wait_turnstile(page, max_wait: int = 20):
    """等待 Cloudflare Turnstile 自动完成验证。"""
    deadline = time.monotonic() + max_wait
    while time.monotonic() < deadline:
        try:
            frames = page.query_selector_all('iframe[src*="challenges.cloudflare.com"]')
            if not frames:
                return True
            result = page.evaluate("""() => {
                const inp = document.querySelector('[name="cf-turnstile-response"]');
                return (inp && inp.value) ? 'solved' : 'pending';
            }""")
            if result == "solved":
                return True
        except Exception:
            pass
        time.sleep(1)
    return False


def _wait_for_result(page, _log, max_wait: int = 30) -> Dict[str, Any]:
    """等待密码提交后的结果。"""
    deadline = time.monotonic() + max_wait
    while time.monotonic() < deadline:
        time.sleep(2)
        try:
            url = str(page.url or "").lower()
            body = _page_text(page, 800).lower()
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

    _log(f"[浏览器注册] 超时，当前: {page.url[:120]}", "warning")
    return {"success": False, "error": "timeout", "stage": "timeout"}


def _page_text(page, max_len: int = 1000) -> str:
    try:
        return str(page.evaluate("document.body ? document.body.innerText : ''") or "")[:max_len]
    except Exception:
        return ""


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

    Returns:
        {"success": bool, "error": str, "stage": str, "cookies_synced": int}
    """
    def _log(msg, level="info"):
        if log_callback:
            log_callback(msg, level)
        getattr(logger, level if level in ("info", "warning", "error") else "info")(msg)

    try:
        from playwright.sync_api import sync_playwright
    except Exception:
        return {"success": False, "error": "playwright not installed", "stage": "bootstrap", "cookies_synced": 0}

    from .browser_bind import _find_chrome_binary
    chrome_binary = _find_chrome_binary()
    if not chrome_binary:
        return {"success": False, "error": "chrome binary not found", "stage": "chrome_not_found", "cookies_synced": 0}

    cookies = _extract_cookies_for_playwright(session)
    _log(f"[浏览器注册] 提取 {len(cookies)} 个 cookie，启动 Chrome CDP...")

    timeout_seconds = max(int(timeout_seconds), 30)
    cdp_port = random.randint(9520, 9680)
    user_data_dir = tempfile.mkdtemp(prefix=f"codex-reg-{cdp_port}-")
    cdp_url = f"http://127.0.0.1:{cdp_port}"

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
    try:
        chrome_proc = subprocess.Popen(chrome_args, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

        cdp_ready = False
        for _ in range(22):
            try:
                with urllib.request.urlopen(f"{cdp_url}/json/version", timeout=2) as resp:
                    if json.loads(resp.read() or b"{}").get("Browser"):
                        cdp_ready = True
                        break
            except Exception:
                time.sleep(0.5)

        if not cdp_ready:
            return {"success": False, "error": "chrome cdp not responding", "stage": "cdp_unavailable", "cookies_synced": 0}

        _log("[浏览器注册] Chrome CDP 就绪")

        with sync_playwright() as p:
            browser = p.chromium.connect_over_cdp(cdp_url)
            context = None
            try:
                context = browser.new_context(viewport={"width": 1366, "height": 900})

                if cookies:
                    _inject_pw_cookies(context, cookies, _log)

                page = context.new_page()
                page.set_default_timeout(timeout_seconds * 1000)

                _log("[浏览器注册] 导航到密码设置页...")
                try:
                    page.goto(
                        "https://auth.openai.com/create-account/password",
                        wait_until="domcontentloaded",
                        timeout=30000,
                    )
                except Exception as nav_err:
                    return {"success": False, "error": f"navigate: {nav_err}", "stage": "navigate", "cookies_synced": 0}

                time.sleep(2)
                current_url = str(page.url or "")
                _log(f"[浏览器注册] 当前页面: {current_url[:120]}")

                if "create-account/password" not in current_url.lower():
                    body = _page_text(page, 300)
                    _log(f"[浏览器注册] 被重定向: {current_url[:100]}, body: {body[:150]}", "warning")
                    return {"success": False, "error": f"redirected: {current_url[:100]}", "stage": "wrong_page", "cookies_synced": 0}

                password_input = _find_password_input(page)
                if not password_input:
                    body = _page_text(page, 300)
                    _log(f"[浏览器注册] 未找到密码框, body: {body[:150]}", "warning")
                    return {"success": False, "error": "password input not found", "stage": "no_input", "cookies_synced": 0}

                _log("[浏览器注册] 填写密码...")
                password_input.click()
                time.sleep(random.uniform(0.3, 0.6))
                page.keyboard.type(password, delay=random.randint(25, 55))
                time.sleep(random.uniform(0.5, 1.0))

                _log("[浏览器注册] 等待 Turnstile...")
                _wait_turnstile(page, max_wait=20)

                submit_btn = _find_submit_btn(page)
                if not submit_btn:
                    _log("[浏览器注册] 未找到提交按钮", "warning")
                    return {"success": False, "error": "submit button not found", "stage": "no_button", "cookies_synced": 0}

                _log("[浏览器注册] 提交密码...")
                submit_btn.click()

                result = _wait_for_result(page, _log, max_wait=30)

                cookies_synced = 0
                try:
                    browser_cookies = context.cookies()
                    cookies_synced = _inject_browser_cookies_to_session(session, browser_cookies)
                    _log(f"[浏览器注册] 回注 {cookies_synced} 个 cookie")
                except Exception as ce:
                    _log(f"[浏览器注册] Cookie 回注失败: {ce}", "warning")

                result["cookies_synced"] = cookies_synced
                return result

            finally:
                try:
                    if context:
                        context.close()
                except Exception:
                    pass
                try:
                    browser.close()
                except Exception:
                    pass

    except Exception as exc:
        _log(f"[浏览器注册] 异常: {exc}", "error")
        return {"success": False, "error": f"exception: {exc}", "stage": "exception", "cookies_synced": 0}
    finally:
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
