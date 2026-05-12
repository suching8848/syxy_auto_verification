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
VERSION = "v1.2"
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
    # portal_post mode fields (POST credentials to portal)
    "username": "",
    "password": "",
    # scheduled task trigger time (24h format)
    "schedule_time": "17:55",
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
    portal_host = config.get("portal_url") or config.get("portal_host", "")
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


def _input(prompt, default=""):
    """Read a line from stdin. Uses direct sys.stdin.readline for exe compat."""
    sys.stdout.write(prompt)
    sys.stdout.flush()
    try:
        line = sys.stdin.readline()
        if not line:  # EOF
            return default
        return line.strip()
    except (EOFError, KeyboardInterrupt):
        return default


def show_seamless_guide(config):
    """Print guide for achieving fully seamless (no-popup) auth."""
    stime = config.get("schedule_time", "17:55")
    stime_early = _shift_time(stime, -2)
    print()
    print("=" * 66)
    print("  如何实现完全【无感】认证")
    print("=" * 66)
    print()
    print('  所谓"无感"：断网 → 后台自动认证 → 恢复联网，')
    print("  整个过程不弹任何窗口，不影响你打游戏或看视频。")
    print()
    print("  ┌──────────────────────────────────────────────────┐")
    print("  │ 第一步：关闭 Windows 自带的弹窗（必须做！）       │")
    print("  └──────────────────────────────────────────────────┘")
    print()
    print("  Windows 检测到没网时，会自己弹浏览器窗口。")
    print("  不关掉它，即使脚本认证成功了，浏览器还是会弹出来。")
    print()
    print("  ▸ 关闭弹窗（以管理员身份打开 PowerShell，运行）：")
    print()
    print("    Set-ItemProperty -Path \"HKLM:\\SYSTEM\\")
    print("    CurrentControlSet\\Services\\NlaSvc\\")
    print("    Parameters\\Internet\" -Name")
    print("    \"EnableActiveProbing\" -Value 0 -Type DWord")
    print()
    print("  ▸ 恢复弹窗（如果以后需要恢复，运行下面这个）：")
    print()
    print("    Set-ItemProperty -Path \"HKLM:\\SYSTEM\\")
    print("    CurrentControlSet\\Services\\NlaSvc\\")
    print("    Parameters\\Internet\" -Name")
    print("    \"EnableActiveProbing\" -Value 1 -Type DWord")
    print()
    print("  运行后重启电脑即生效（只需一次，永久有效）。")
    print()
    print("  ┌──────────────────────────────────────────────────┐")
    print("  │ 第二步：确认定时触发时间                         │")
    print("  └──────────────────────────────────────────────────┘")
    print()
    print(f"  当前设定的触发时间：每天 {stime}")
    print(f"  建议设到校园网断网前 1-2 分钟，如 {stime_early}。")
    print("  如需修改，到菜单选 [3] 修改配置 → schedule_time。")
    print()
    print("  ┌──────────────────────────────────────────────────┐")
    print("  │ 第三步：部署到 Windows 计划任务（需要管理员）     │")
    print("  └──────────────────────────────────────────────────┘")
    print()
    print("  计划任务需要管理员权限，无法在程序内直接创建。")
    print("  请按以下步骤操作：")
    print()
    print("  1. 打开文件管理器，找到你放 auto_login.exe 的文件夹")
    print("  2. 在地址栏点击，复制完整路径（比如 C:\\Users\\xxx\\Desktop\\校园网）")
    print("  3. 按 Win 键 → 输入 PowerShell")
    print("  4. 右键 Windows PowerShell → 以管理员身份运行")
    print("  5. 在蓝色窗口里输入 cd 空格，然后右键粘贴路径，回车：")
    print("       cd \"你复制的文件夹路径\"")
    print("  6. 然后运行部署脚本：")
    print("       .\\setup_task.ps1")
    print(f"  7. 看到绿色提示即成功！任务会在每天 {stime} 自动启动。")
    print()
    print("  ⚠ 修改触发时间：")
    print("    用记事本打开 setup_task.ps1，找到 -Daily -At 这一行，")
    print("    把时间改成你想要的（24 小时制，如 17:58），保存后")
    print("    重新运行 .\\setup_task.ps1 即可。")
    print()
    print("  ┌──────────────────────────────────────────────────┐")
    print("  │ 验证是否生效                                     │")
    print("  └──────────────────────────────────────────────────┘")
    print()
    print("  第二天打开 logs/ 目录，看当天日志文件。")
    print("  如果里面有 [DOWN] → [AUTH] → [RECOVER] 的记录，")
    print("  说明无感认证已生效。你什么都没感觉到，网就恢复了。")
    print()
    print("  项目地址: https://github.com/suching8848/syxy_auto_verification")
    print("  有详细 README、版本历史、抓包教程、常见问题。")
    print()
    print("=" * 66)
    print()


def _shift_time(time_str, minutes_offset):
    """Shift a HH:MM time string by N minutes, returning HH:MM."""
    try:
        parts = time_str.strip().split(":")
        h, m = int(parts[0]), int(parts[1])
        total = h * 60 + m + minutes_offset
        total = total % (24 * 60)
        return f"{total // 60:02d}:{total % 60:02d}"
    except (ValueError, IndexError):
        return time_str


def interactive_setup(config):
    """Walk user through basic config setup, returns updated config dict."""
    print()
    print("=" * 58)
    print("  首次配置向导")
    print("  按 Enter 保留当前值，输入新值后按 Enter 保存")
    print("=" * 58)
    print()

    fields = [
        ("username", "学号/用户名", "2312505051"),
        ("password", "校园网密码", "身份证后6位"),
        ("portal_url", "校园网认证地址", "http://10.10.200.102"),
        ("schedule_time", "定时触发时间 (24h制)", "17:55"),
        ("check_url", "网络检测地址", "http://www.baidu.com"),
    ]

    for key, label, example in fields:
        current = config.get(key, "")
        if current:
            prompt = f"  {label} [{current}]: "
        else:
            prompt = f"  {label} (如 {example}): "
        val = _input(prompt).strip()
        if val:
            config[key] = val
        elif not current and example:
            config[key] = example

    print()
    print("  配置完成！正在保存...")

    try:
        with open(CONFIG_FILE, "w", encoding="utf-8") as f:
            json.dump(config, f, ensure_ascii=False, indent=4)
        print(f"  已保存到: {CONFIG_FILE}")
    except IOError as e:
        print(f"  保存失败: {e}")

    print()
    return config


def show_menu():
    """Display menu and return user choice."""
    print()
    print("=" * 62)
    print("  校园网自动认证工具 " + VERSION)
    print("  项目地址: https://github.com/suching8848/syxy_auto_verification")
    print("=" * 62)
    print("  [1] 启动自动认证  — 后台检测 + 断网自动重连")
    print("  [2] 测试认证      — 发一次请求验证配置是否正确")
    print("  [3] 修改配置      — 学号 / 密码 / Portal 地址")
    print("  [4] 无感部署指南  — 关弹窗 + 计划任务（实现完全无感）")
    print("  [5] 使用帮助      — 完整说明和常见问题")
    print("  [q] 退出")
    print("-" * 62)
    choice = _input("  请选择: ").strip().lower()
    return choice or "1"


def run_detection_loop(config):
    """Core detection loop — runs until stopped or duration exceeded."""
    check_url = config["check_url"]
    interval_ok = config["check_interval_ok"]
    interval_fail = config["check_interval_fail"]
    fail_threshold = config["fail_threshold"]
    timeout = config["request_timeout"]
    run_duration = config.get("run_duration_minutes", 0)

    log("Service started" + (" [interactive]" if INTERACTIVE else " [background]"), "START")
    log(f"Config: auth={config.get('auth_method','http')} check={check_url} interval={interval_ok}s threshold={fail_threshold} duration={run_duration}min", "INFO")

    start_time = datetime.now()
    end_time = start_time + timedelta(minutes=run_duration) if run_duration > 0 else None
    if end_time:
        log(f"Scheduled mode: will exit at {end_time.strftime('%H:%M:%S')}", "INFO")
    else:
        log("Continuous mode", "INFO")

    fail_count = 0
    total_checks = 0
    outage_checks = 0
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


def _need_setup(config):
    """Check if config needs first-time setup."""
    method = config.get("auth_method", "http")
    if method == "portal_post":
        return not config.get("username") or not config.get("password")
    return not config.get("portal_url")


def main():
    parser = argparse.ArgumentParser(
        description=f"Campus Network Auto-Login {VERSION} — 校园网自动认证工具",
    )
    parser.add_argument("--auth", action="store_true", help="Test auth once and exit")
    parser.add_argument("--version", action="version", version=f"auto_login {VERSION}")
    args = parser.parse_args()

    print(DISCLAIMER, flush=True)
    print()
    clean_old_logs()

    config = load_config()

    # --auth flag: test auth and exit (works in any mode)
    if args.auth:
        if _need_setup(config):
            log("Config incomplete — running setup first...", "WARN")
            config = interactive_setup(config)
        method = config.get("auth_method", "http")
        if method == "portal_post":
            log("Auth test mode: method=portal_post", "START")
        else:
            log(f"Auth test mode: method={method}", "START")
        ok = do_auth(config, None)
        log(f"Auth test {'PASSED' if ok else 'FAILED'}", "STOP")
        return

    # Background mode (scheduled task): run detection loop directly, no menu
    if not INTERACTIVE:
        run_detection_loop(config)
        return

    # Interactive mode with no flags: show menu
    while True:
        # If config is incomplete on first run, prompt setup
        if _need_setup(config):
            print("检测到配置文件未完成。")
            print("如果是从 .example.json 复制来的，请先重命名为 auto_login_config.json")
            print()
            do_setup = _input("是否现在配置? [Y/n]: ").strip().lower()
            if do_setup in ("", "y", "yes"):
                config = interactive_setup(config)
            else:
                print("已跳过。可在菜单选 [3] 修改配置。")
                print()

        choice = show_menu()

        if choice == "1":
            print()
            print("  ╔════════════════════════════════════════════╗")
            print("  ║  提示：本窗口关闭后自动认证即停止。       ║")
            print("  ║  如需长期后台静默运行（不开窗口），       ║")
            print("  ║  请返回菜单选 [4] 无感部署指南。          ║")
            print("  ╚════════════════════════════════════════════╝")
            print()
            _input("  按 Enter 开始...")
            run_detection_loop(config)

        elif choice == "2":
            if _need_setup(config):
                log("Config incomplete — running setup first...", "WARN")
                config = interactive_setup(config)
            method = config.get("auth_method", "http")
            log(f"Auth test mode: method={method}", "START")
            ok = do_auth(config, None)
            log(f"Auth test {'PASSED' if ok else 'FAILED'}", "STOP")

        elif choice == "3":
            config = interactive_setup(config)

        elif choice == "4":
            show_seamless_guide(config)

        elif choice == "5":
            print()
            print("=" * 62)
            print("  使用帮助")
            print("=" * 62)
            print()
            print('  Q: 为什么提示“配置文件未完成”？')
            print("  A: 把 auto_login_config.example.json 重命名为")
            print("     auto_login_config.json，删除 .example 后缀即可。")
            print()
            print("  Q: 怎么知道认证成功了？")
            print("  A: 先选 [2] 测试认证，看到 PASSED 就是成功。")
            print("     正式运行选 [1]，断网时日志会显示 [AUTH] 记录。")
            print()
            print("  Q: 日志在哪里？")
            print("  A: 程序所在目录的 logs/ 文件夹，按日期命名。")
            print()
            print("  Q: 认证失败怎么办？")
            print("  A: 检查三样：学号密码是否正确、校园网认证地址")
            print("     是否写对、校园网是否换了认证方式。打开日志看")
            print("     [ERROR] 行的具体原因。")
            print()
            print("  Q: 我想让它在后台一直跑，怎么办？")
            print("  A: 选 [4] 无感部署指南，按步骤操作。")
            print()
            print("  Q: 为什么我双击 exe 闪退？")
            print("  A: 新版不会闪退。如果遇到闪退，右键→在终端中打开。")
            print()
            print("  项目地址: https://github.com/suching8848/syxy_auto_verification")
            print("  有详细 README 说明、版本历史、常见问题。")
            print("  免费开源！如付费获取请立即退款。")
            print()
            print("=" * 62)

        elif choice == "q":
            print("再见！")
            break

        else:
            print(f"无效选项: {choice}")

        print()


if __name__ == "__main__":
    try:
        main()
    finally:
        if INTERACTIVE:
            input("\nPress Enter to exit...")
