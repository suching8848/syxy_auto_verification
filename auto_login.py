import urllib.request
import urllib.error
from urllib.parse import urlparse, urlencode
import http.cookiejar
import webbrowser
import json
import time
import os
import sys
import glob
import ctypes
import argparse
import re
from datetime import datetime, timedelta

if getattr(sys, "frozen", False):
    SCRIPT_DIR = os.path.dirname(sys.executable)
else:
    SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
VERSION = "v1.1"
DISCLAIMER = (
    f"Campus Network Auto-Login {VERSION}\n"
    "仅供学习研究使用，请勿用于非法用途。\n"
    "For educational purposes only. Do not use for illegal activities.\n"
    "项目地址: https://github.com/suching8848/syxy_auto_verification\n"
    "免费开源，如付费获取请立即退款举报。\n"
    "Free and open source. If you paid for this, request a refund.\n"
)

CONFIG_FILE = os.path.join(SCRIPT_DIR, "auto_login_config.json")
LOG_DIR = os.path.join(SCRIPT_DIR, "logs")
MAX_LOG_DAYS = 7

DEFAULT_CONFIG = {
    "portal_url": "",
    "check_url": "http://www.baidu.com",
    "check_interval_ok": 30,
    "check_interval_fail": 10,
    "fail_threshold": 2,
    "request_timeout": 5,
    "auth_method": "http",
    "run_duration_minutes": 60,
    "browser_wait_seconds": 3,
    "auth_cooldown_seconds": 30,
    "check_expected_body": "baidu",
    # portal_post mode fields (if your portal requires POST with credentials)
    "portal_host": "",
    "username": "",
    "password": "",
}

# True when user runs `python auto_login.py` directly (has a console)
INTERACTIVE = sys.stdout.isatty()


def get_log_path():
    os.makedirs(LOG_DIR, exist_ok=True)
    today = datetime.now().strftime("%Y-%m-%d")
    return os.path.join(LOG_DIR, f"auto_login_{today}.log")


def clean_old_logs():
    cutoff = datetime.now() - timedelta(days=MAX_LOG_DAYS)
    for f in glob.glob(os.path.join(LOG_DIR, "auto_login_*.log")):
        try:
            ftime = datetime.fromtimestamp(os.path.getmtime(f))
            if ftime < cutoff:
                os.remove(f)
        except OSError:
            pass


def load_config():
    if os.path.exists(CONFIG_FILE):
        try:
            with open(CONFIG_FILE, "r", encoding="utf-8") as f:
                cfg = json.load(f)
            merged = DEFAULT_CONFIG.copy()
            merged.update(cfg)
            return merged
        except (json.JSONDecodeError, IOError) as e:
            log(f"Config load failed: {e}, using defaults", "WARN")
    return DEFAULT_CONFIG.copy()


def log(msg, level="INFO"):
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    line = f"[{timestamp}] [{level}] {msg}"
    print(line, flush=True)

    try:
        with open(get_log_path(), "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except IOError:
        pass


def format_duration(seconds):
    if seconds < 60:
        return f"{seconds:.0f}s"
    m, s = divmod(int(seconds), 60)
    if m < 60:
        return f"{m}m{s}s"
    h, m = divmod(m, 60)
    return f"{h}h{m}m{s}s"


def check_network(url, timeout, expected_body=None):
    """Returns (ok: bool, detail: str).
    Checks for captive portal via URL redirect AND response body content."""
    expected_host = urlparse(url).hostname
    try:
        req = urllib.request.Request(url, method="GET")
        resp = urllib.request.urlopen(req, timeout=timeout)
        final_url = resp.geturl()
        final_host = urlparse(final_url).hostname
        if final_host and expected_host and final_host != expected_host:
            return False, f"portal redirect to {final_host}"
        if final_url != url:
            return False, f"redirected to {final_url[:100]}"
        if expected_body:
            body = resp.read(102400).decode("utf-8", errors="ignore")
            if expected_body.lower() not in body.lower():
                return False, f"response missing '{expected_body}' (portal injected?)"
        return True, "OK"
    except urllib.error.HTTPError as e:
        return False, f"HTTP {e.code}"
    except urllib.error.URLError as e:
        reason = str(e.reason)
        return False, reason
    except OSError as e:
        return False, str(e)
    except Exception as e:
        return False, type(e).__name__ + ": " + str(e)


def simulate_enter():
    VK_RETURN = 0x0D
    KEYEVENTF_KEYUP = 0x0002
    ctypes.windll.user32.keybd_event(VK_RETURN, 0, 0, 0)
    time.sleep(0.05)
    ctypes.windll.user32.keybd_event(VK_RETURN, 0, KEYEVENTF_KEYUP, 0)


BROWSER_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/147.0.0.0 Safari/537.36"
)

BROWSER_HEADERS = {
    "User-Agent": BROWSER_UA,
    "Accept": (
        "text/html,application/xhtml+xml,application/xml;"
        "q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8"
    ),
    "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
    "Accept-Encoding": "gzip, deflate",
    "Upgrade-Insecure-Requests": "1",
    "Connection": "keep-alive",
}


def do_auth_portal_post(config):
    """POST-based campus portal auth.
    Step 0: try to trigger portal redirect by accessing check_url (full browser headers)
    Step 1: fallback — access portal index.jsp directly, then try API
    Step 2: POST credentials to InterFace.do?method=login
    """
    portal_host = config.get("portal_host", "")
    username = config.get("username", "")
    password = config.get("password", "")
    check_url = config["check_url"]
    timeout = config["request_timeout"]

    if not username or not password:
        log("username or password not configured", "ERROR")
        return False

    cj = http.cookiejar.CookieJar()
    opener = urllib.request.build_opener(
        urllib.request.HTTPCookieProcessor(cj),
    )

    index_url = None

    # Step 0: probe check_url — portal may respond with JS redirect containing index.jsp URL
    try:
        req = urllib.request.Request(check_url, headers=BROWSER_HEADERS)
        resp = opener.open(req, timeout=timeout)
        body_text = resp.read(204800).decode("utf-8", errors="ignore")
        final_url = resp.geturl()

        # check for HTTP redirect
        if final_url != check_url and "index.jsp" in final_url:
            index_url = final_url
            log(f"Portal HTTP redirect: {index_url[:150]}...", "AUTH")
        else:
            # check for JavaScript redirect: top.self.location.href='...index.jsp?...'
            m = re.search(r"location\.href\s*=\s*['\"]([^'\"]*index\.jsp[^'\"]*)", body_text)
            if m:
                index_url = m.group(1)
                log(f"Portal JS redirect: {index_url[:150]}...", "AUTH")
    except Exception as e:
        log(f"Probe {check_url}: {e}", "AUTH")

    # Step 1: if no JS redirect found, try accessing portal directly
    if not index_url:
        index_url = f"{portal_host}/eportal/index.jsp"
        log(f"No JS redirect, accessing portal directly: {index_url}", "AUTH")

    # GET index.jsp to obtain JSESSIONID cookie (needed for both paths)
    try:
        req = urllib.request.Request(index_url, headers={"User-Agent": BROWSER_UA})
        resp = opener.open(req, timeout=timeout)
        resp.read(204800)
        final_url = resp.geturl()
        if final_url != index_url:
            index_url = final_url
            log(f"Portal responded with: {index_url[:150]}...", "AUTH")
    except Exception as e:
        log(f"Failed to fetch index page: {e}", "ERROR")
        return False

    # Step 1.5: if we still have no query params, try portal APIs to get them
    query_string = urlparse(index_url).query
    if not query_string:
        log("No query params, trying portal API to get device info...", "AUTH")
        for api_method in ("pageInfo", "getServices"):
            try:
                api_url = f"{portal_host}/eportal/InterFace.do?method={api_method}"
                req = urllib.request.Request(api_url, headers={
                    "User-Agent": BROWSER_UA,
                    "Referer": index_url,
                })
                resp = opener.open(req, timeout=timeout)
                api_body = resp.read(204800).decode("utf-8", errors="ignore")
                log(f"API {api_method} response: {api_body[:200]}", "AUTH")
                # Some portals return queryString in JSON or as redirect
                final_url = resp.geturl()
                if final_url != api_url and "index.jsp" in final_url:
                    index_url = final_url
                    query_string = urlparse(index_url).query
                    log(f"Got redirect with params: {index_url[:150]}...", "AUTH")
                    break
            except Exception as e:
                log(f"API {api_method} failed: {e}", "AUTH")

    if not query_string:
        log("Still no query parameters — portal may not support this flow", "AUTH")

    form_data = urlencode({
        "userId": username,
        "password": password,
        "service": "",
        "queryString": query_string,
        "operatorPwd": "",
        "operatorUserId": "",
        "validcode": "",
        "passwordEncrypt": "false",
    }).encode()

    login_url = f"{portal_host}/eportal/InterFace.do?method=login"

    start = time.time()
    try:
        req = urllib.request.Request(login_url, data=form_data, headers={
            "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
            "Referer": index_url,
            "Origin": portal_host,
            "User-Agent": BROWSER_UA,
        })
        resp = opener.open(req, timeout=timeout)
        body = resp.read().decode("utf-8", errors="ignore")
        elapsed = (time.time() - start) * 1000
        snippet = body[:120].replace("\n", " ").strip()
        log(f"Auth OK [HTTP {resp.status}, {elapsed:.0f}ms] body: {snippet}", "AUTH")
        return True
    except Exception as e:
        elapsed = (time.time() - start) * 1000
        log(f"Auth FAIL [{e}, {elapsed:.0f}ms]", "ERROR")
        return False


def do_auth_http(url, timeout):
    try:
        req = urllib.request.Request(url, headers={
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        })
        resp = urllib.request.urlopen(req, timeout=timeout)
        final_url = resp.geturl()
        body = resp.read().decode("utf-8", errors="ignore")
        info = f"HTTP {resp.status}"
        if final_url != url:
            info += f" (redirected to {final_url[:80]})"
        return resp.status, body, info
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", errors="ignore") if e.fp else ""
        return e.code, body, f"HTTP error {e.code}"
    except Exception as e:
        return None, str(e), str(e)


def do_auth_browser(url, wait_seconds):
    log("Opening portal URL in browser", "AUTH")
    webbrowser.open(url)
    time.sleep(wait_seconds)
    simulate_enter()


def do_auth(config, last_auth_time):
    now = datetime.now()
    cooldown = config.get("auth_cooldown_seconds", 30)
    if last_auth_time and (now - last_auth_time).total_seconds() < cooldown:
        remaining = cooldown - int((now - last_auth_time).total_seconds())
        log(f"Auth cooldown ({remaining}s remaining), skip", "INFO")
        return False

    method = config.get("auth_method", "http")
    url = config.get("portal_url", "")
    timeout = config["request_timeout"]

    if method == "portal_post":
        return do_auth_portal_post(config)
    elif method == "browser":
        do_auth_browser(url, config.get("browser_wait_seconds", 3))
        log("Browser auth completed (Enter sent)", "AUTH")
    else:
        start = time.time()
        status, body, info = do_auth_http(url, timeout)
        elapsed = (time.time() - start) * 1000
        if status:
            snippet = body[:150].replace("\n", " ").strip() if body else "(empty)"
            log(f"Auth OK [{info}, {elapsed:.0f}ms] body: {snippet}", "AUTH")
        else:
            log(f"Auth FAIL [{info}, {elapsed:.0f}ms]", "ERROR")
            return False

    return True


def main():
    parser = argparse.ArgumentParser(
        description=f"Campus Network Auto-Login {VERSION} — 校园网自动认证工具",
    )
    parser.add_argument("--auth", action="store_true", help="Test auth once and exit (skip network detection)")
    parser.add_argument("--version", action="version", version=f"auto_login {VERSION}")
    args = parser.parse_args()

    print(DISCLAIMER, flush=True)
    print()

    clean_old_logs()

    config = load_config()
    portal_url = config["portal_url"]
    check_url = config["check_url"]
    interval_ok = config["check_interval_ok"]
    interval_fail = config["check_interval_fail"]
    fail_threshold = config["fail_threshold"]
    timeout = config["request_timeout"]
    run_duration = config.get("run_duration_minutes", 0)

    if not portal_url and config.get("auth_method") != "portal_post":
        log("portal_url is empty, please set it in auto_login_config.json", "ERROR")
        return

    if args.auth:
        method = config.get("auth_method", "http")
        if method == "portal_post":
            log("Auth test mode: method=portal_post", "START")
        else:
            log(f"Auth test mode: method={method} url={portal_url[:60]}...", "START")
        ok = do_auth(config, None)
        log(f"Auth test {'PASSED' if ok else 'FAILED'}", "STOP")
        return

    log("Service started" + (" [interactive]" if INTERACTIVE else " [background]"), "START")
    log(f"Config: auth={config.get('auth_method','http')} check={check_url} interval={interval_ok}s threshold={fail_threshold} duration={run_duration}min", "INFO")

    start_time = datetime.now()
    end_time = start_time + timedelta(minutes=run_duration) if run_duration > 0 else None
    if end_time:
        log(f"Scheduled mode: will exit at {end_time.strftime('%H:%M:%S')}", "INFO")
    else:
        log("Continuous mode", "INFO")

    fail_count = 0
    total_checks = 0        # all checks since start
    outage_checks = 0       # checks during current outage
    outage_start = None
    auth_attempts = 0
    last_auth_time = None
    was_down = False

    while True:
        try:
            if end_time and datetime.now() > end_time:
                log("Run duration reached, exiting", "STOP")
                break

            total_checks += 1
            ok, detail = check_network(check_url, timeout, config.get("check_expected_body"))

            if ok:
                if was_down and outage_start:
                    duration = (datetime.now() - outage_start).total_seconds()
                    log(
                        f"Network restored — outage: {format_duration(duration)}, "
                        f"checks: {outage_checks}, auth_attempts: {auth_attempts}",
                        "RECOVER",
                    )
                    was_down = False
                    outage_start = None
                    outage_checks = 0
                    auth_attempts = 0
                fail_count = 0
            else:
                fail_count += 1
                if not outage_start:
                    outage_start = datetime.now()
                outage_checks += 1

                if fail_count >= fail_threshold:
                    if not was_down:
                        log(f"Network DOWN (reason: {detail}), starting auth", "DOWN")
                        was_down = True
                        outage_start = datetime.now()
                        outage_checks = 0
                        auth_attempts = 0

                    auth_attempts += 1
                    if do_auth(config, last_auth_time):
                        last_auth_time = datetime.now()
                    fail_count = 0
                else:
                    log(f"Check #{fail_count} failed: {detail}", "WARN")

            # --- interactive status line ---
            if INTERACTIVE:
                uptime = format_duration((datetime.now() - start_time).total_seconds())
                sleep = interval_fail if was_down else interval_ok

                if was_down and outage_start:
                    state = f"[DOWN] outage {format_duration((datetime.now() - outage_start).total_seconds())} | auth #{auth_attempts + 1}"
                elif fail_count > 0:
                    state = f"[WARN] check failed {fail_count}/{fail_threshold}"
                else:
                    state = "[OK]  reachable"

                log(
                    f"{state} | next in {sleep}s | uptime {uptime} | checks #{total_checks}",
                    "STATUS",
                )

            time.sleep(interval_fail if was_down else interval_ok)

        except KeyboardInterrupt:
            log("Service stopped by user", "STOP")
            break
        except Exception as e:
            log(f"Unexpected error: {e}", "ERROR")
            time.sleep(interval_fail)


if __name__ == "__main__":
    try:
        main()
    finally:
        if INTERACTIVE:
            input("\nPress Enter to exit...")
