import urllib.request
import urllib.error
from urllib.parse import urlparse, urlencode, urljoin
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
import socket
import threading
from datetime import datetime, timedelta

if getattr(sys, "frozen", False):
    SCRIPT_DIR = os.path.dirname(sys.executable)
else:
    SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
VERSION = "v1.7.5"

# Required on Windows 11 for tray icon to appear — set before any window creation
try:
    ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(
        "CampusNetwork.AutoLogin.Tray"
    )
except Exception:
    pass

DISCLAIMER = (
    f"Campus Network Auto-Login {VERSION}\n"
    "仅供学习研究使用，请勿用于非法用途。\n"
    "For educational purposes only. Do not use for illegal activities.\n"
    "项目地址: https://github.com/suching8848/syxy_auto_verification\n"
    "免费开源，如付费获取请立即退款举报。\n"
    "Free and open source. If you paid for this, request a refund.\n"
)

CONFIG_FILE = os.path.join(SCRIPT_DIR, "auto_login_config.json")
CONFIG_FILENAME = "auto_login_config.json"
LOG_DIR = os.path.join(SCRIPT_DIR, "logs")
MAX_LOG_DAYS = 7


def exe_dir():
    """Directory the user actually launched from: the exe's folder when frozen
    (sys.executable), the script's folder when running from source."""
    if getattr(sys, "frozen", False):
        return os.path.dirname(os.path.abspath(sys.executable))
    return os.path.dirname(os.path.abspath(__file__))


# 程序目录能不能写（进程内缓存）。get_log_path() 会经 get_runtime_dir() 频繁走到
# 这里，所以探测结果必须缓存，不能每写一行日志就建删一次探针文件。
_EXE_DIR_WRITABLE = None


def _dir_writable(directory):
    """能不能往 directory 里创建文件。

    两种常见情况会让它为 False，而且都跟"权限不足"无关：

    * Windows 的受控文件夹访问（勒索软件防护）会拦住未签名程序往「桌面 / 文档 /
      图片 / 视频 / 音乐 / 收藏夹」这类受保护目录写文件 —— 而本程序经常就摆在
      桌面上。管理员身份也拦，因为拦的是路径不是权限。
    * 安装到只读目录（Program Files）时同理。

    探针文件建完立刻删掉；建不了就说明写不进去。
    """
    probe = os.path.join(directory or ".", f".campusnet_probe_{os.getpid()}")
    try:
        with open(probe, "w", encoding="utf-8") as f:
            f.write("")
    except OSError:
        return False
    try:
        os.remove(probe)
    except OSError:
        pass
    return True


def _exe_dir_writable():
    global _EXE_DIR_WRITABLE
    if _EXE_DIR_WRITABLE is None:
        _EXE_DIR_WRITABLE = _dir_writable(exe_dir())
    return _EXE_DIR_WRITABLE


def _reset_path_cache():
    """Clear the cached writability probe. Tests only."""
    global _EXE_DIR_WRITABLE, _RUNTIME_DIR
    _EXE_DIR_WRITABLE = None
    _RUNTIME_DIR = None


def _appdata_config_path():
    appdata = os.environ.get("APPDATA")
    if not appdata:
        return None
    return os.path.join(appdata, "CampusNet", CONFIG_FILENAME)


def find_config_file():
    """Locate auto_login_config.json.

    The rule is simple and deliberate: the config lives beside the exe (or, when
    running from source, in the project directory). A portable tool must be
    self-contained — a fresh folder should get its own config, never silently
    inherit one from %APPDATA% or from whoever's project happened to be the
    working directory.

    %APPDATA% is the fallback for the case where the program folder is not
    writable: installed under Program Files, or sitting in a folder that Windows'
    Controlled Folder Access protects (桌面/文档/图片…). It is consulted first in
    that case — otherwise the program would write settings into %APPDATA% and
    keep reading the stale file beside the exe, so the user's edits would look
    like they never took effect. See config_write_path().
    """
    beside = os.path.join(exe_dir(), CONFIG_FILENAME)
    installed = _appdata_config_path()
    if installed and os.path.exists(installed) and not _exe_dir_writable():
        return installed
    if os.path.exists(beside):
        return beside
    if not getattr(sys, "frozen", False) and os.path.exists(CONFIG_FILE):
        return CONFIG_FILE
    if installed and os.path.exists(installed):
        return installed
    return beside


# 日志/运行期文件放哪，同样进程内缓存：get_log_path() 每写一行日志都会问到它。
_RUNTIME_DIR = None


def get_runtime_dir():
    """Directory to keep logs in — beside the active config, so a portable
    folder stays self-contained and an installed copy never writes to a
    read-only program directory.

    When that directory cannot be written to (Controlled Folder Access on the
    Desktop, read-only install folder), logs go to %APPDATA%\\CampusNet instead.
    Without this the silent guard would run every night and leave no trace at
    all — and the log file is the only evidence silent mode ever produces.
    """
    global _RUNTIME_DIR
    if _RUNTIME_DIR is None:
        home = os.path.dirname(find_config_file()) or SCRIPT_DIR
        if not _dir_writable(home):
            installed = _appdata_config_path()
            if installed:
                candidate = os.path.dirname(installed)
                try:
                    os.makedirs(candidate, exist_ok=True)
                except OSError:
                    pass
                if _dir_writable(candidate):
                    home = candidate
        _RUNTIME_DIR = home
    return _RUNTIME_DIR

DEFAULT_CONFIG = {
    "portal_url": "http://10.10.200.102",
    "check_url": "http://www.baidu.com",
    "check_interval_ok": 5,
    "check_interval_fail": 2,
    "fail_threshold": 2,
    "request_timeout": 5,
    "auth_method": "portal_post",
    "run_duration_minutes": 60,
    "browser_wait_seconds": 3,
    "auth_cooldown_seconds": 3,
    "auth_retry_backoff_seconds": 5,
    "check_expected_body": "baidu",
    # portal_post mode fields (POST credentials to portal)
    "username": "",
    "password": "",
    # scheduled task trigger time (24h format)
    "schedule_time": "20:30",
    # ── GUI options ──
    # Which usage mode the GUI shows first: "silent" (每天自动守护, the common
    # case) or "manual" (window + tray, for temporary use). UI preference only.
    "mode": "silent",
    # Scenario A: closing the window hides to tray (True) or exits (False)
    "close_to_tray": True,
    # Scenario B: the clock time (24h "HH:MM") at which the silent watcher
    # starts. Empty string = silent mode disabled.
    "silent_start_time": "",
    # how long the silent watcher keeps probing before it quits, in minutes
    "silent_run_minutes": 30,
}

# True when user runs `python auto_login.py` directly (has a console)
# sys.stdout can be None under Task Scheduler with pythonw.exe
try:
    INTERACTIVE = sys.stdout.isatty()
except Exception:
    INTERACTIVE = False

# Optional log consumer (GUI). Called with (line, level) for every log record.
# The sink runs on whatever thread produced the record — a sink that touches
# tkinter widgets will crash. GUI sinks must only enqueue, never draw.
_LOG_SINK = None


def set_log_sink(fn):
    """Register a callable(line, level) receiving every log record. None clears it."""
    global _LOG_SINK
    _LOG_SINK = fn


def get_log_sink():
    return _LOG_SINK


def get_log_path():
    log_dir = os.path.join(get_runtime_dir(), "logs")
    os.makedirs(log_dir, exist_ok=True)
    today = datetime.now().strftime("%Y-%m-%d")
    return os.path.join(log_dir, f"auto_login_{today}.log")


def ensure_config_file():
    """Create auto_login_config.json with defaults if none exists yet.

    Makes the tool self-contained: a fresh folder needs nothing but the exe, and
    the GUI can then say "settings live in this file" truthfully. Returns the
    path (existing or newly written), or None if it could not be created.
    """
    existing = find_config_file()
    if os.path.exists(existing):
        return existing
    target = config_write_path()
    if save_config(DEFAULT_CONFIG.copy(), path=target):
        return target
    return None


def clean_old_logs():
    cutoff = datetime.now() - timedelta(days=MAX_LOG_DAYS)
    log_dir = os.path.join(get_runtime_dir(), "logs")
    for f in glob.glob(os.path.join(log_dir, "auto_login_*.log")):
        try:
            ftime = datetime.fromtimestamp(os.path.getmtime(f))
            if ftime < cutoff:
                os.remove(f)
        except OSError:
            pass


def load_config():
    path = find_config_file()
    if os.path.exists(path):
        try:
            with open(path, "r", encoding="utf-8") as f:
                cfg = json.load(f)
            merged = DEFAULT_CONFIG.copy()
            merged.update(cfg)
            return merged
        except (json.JSONDecodeError, IOError) as e:
            log(f"Config load failed: {e}, using defaults", "WARN")
    return DEFAULT_CONFIG.copy()


def config_write_path():
    """Where to persist config on first run / on every save.

    Prefer the folder the user launched from, so a portable deployment stays
    self-contained. Fall back to %APPDATA% when that folder is not writable —
    an installed copy under Program Files, or a copy inside a folder that
    Windows' Controlled Folder Access protects (桌面/文档/图片…). Note that the
    check is on the *directory*, not on the file: a config that exists but sits
    in an unwritable folder used to be returned as-is, and saving it then failed
    with "写不进配置" even though %APPDATA% was available.
    """
    beside = os.path.join(exe_dir(), CONFIG_FILENAME)
    existing = find_config_file()
    if os.path.exists(existing) and _dir_writable(os.path.dirname(existing)):
        return existing
    if _exe_dir_writable():
        return beside
    installed = _appdata_config_path()
    if installed:
        try:
            os.makedirs(os.path.dirname(installed), exist_ok=True)
        except OSError:
            pass
        if _dir_writable(os.path.dirname(installed)):
            return installed
    return existing if os.path.exists(existing) else beside


def save_config(config, path=None):
    """Write config atomically so a crash mid-write cannot corrupt the file.

    Writes to a sibling .tmp file first and then os.replace()s it into place,
    which is atomic on Windows for same-volume renames.
    """
    path = path or CONFIG_FILE
    tmp = path + ".tmp"
    try:
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(config, f, ensure_ascii=False, indent=2)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
        return True
    except (IOError, OSError, TypeError, ValueError) as e:
        log(f"Config save failed: {e}", "ERROR")
        try:
            if os.path.exists(tmp):
                os.remove(tmp)
        except OSError:
            pass
        return False


def log(msg, level="INFO"):
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    line = f"[{timestamp}] [{level}] {msg}"
    try:
        print(line, flush=True)
    except Exception:
        pass  # stdout may be broken (pythonw.exe)
    try:
        with open(get_log_path(), "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except IOError:
        pass
    if _LOG_SINK is not None:
        try:
            _LOG_SINK(line, level)
        except Exception:
            pass  # a broken sink must never break the detection loop


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
    Checks for captive portal via URL redirect AND response body content.

    Same-site http->https upgrades (e.g. baidu.com 301 to https) are normal
    and must NOT be treated as a portal redirect, otherwise a healthy network
    is reported as down — which both triggers bogus auth attempts and makes
    every post-auth verification fail."""
    expected = urlparse(url)
    try:
        req = urllib.request.Request(url, method="GET")
        resp = urllib.request.urlopen(req, timeout=timeout)
        final_url = resp.geturl()
        final = urlparse(final_url)
        same_site = (
            final.hostname == expected.hostname
            and (final.path or "/") == (expected.path or "/")
            and final.query == expected.query
        )
        if not same_site:
            if final.hostname and expected.hostname and final.hostname != expected.hostname:
                return False, f"portal redirect to {final.hostname}"
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
    "Accept-Encoding": "identity",
    "Upgrade-Insecure-Requests": "1",
    "Connection": "keep-alive",
}


# ── Windows System Tray API (ctypes, no external deps) ──────────────────────

class _POINT(ctypes.Structure):
    _fields_ = [("x", ctypes.c_long), ("y", ctypes.c_long)]

class _MSG(ctypes.Structure):
    _fields_ = [("hwnd", ctypes.c_void_p), ("message", ctypes.c_uint),
                ("wParam", ctypes.c_void_p), ("lParam", ctypes.c_void_p),
                ("time", ctypes.c_uint), ("pt", _POINT)]

class _NOTIFYICONDATAW(ctypes.Structure):
    _fields_ = [
        ("cbSize", ctypes.c_uint), ("hwnd", ctypes.c_void_p), ("uID", ctypes.c_uint),
        ("uFlags", ctypes.c_uint), ("uCallbackMessage", ctypes.c_uint),
        ("hIcon", ctypes.c_void_p), ("szTip", ctypes.c_wchar * 128),
        ("dwState", ctypes.c_uint), ("dwStateMask", ctypes.c_uint),
        ("szInfo", ctypes.c_wchar * 256), ("uTimeout", ctypes.c_uint),
        ("szInfoTitle", ctypes.c_wchar * 64), ("dwInfoFlags", ctypes.c_uint),
        ("guidItem", ctypes.c_ubyte * 16), ("hBalloonIcon", ctypes.c_void_p),
    ]

class _WNDCLASSEXW(ctypes.Structure):
    _fields_ = [
        ("cbSize", ctypes.c_uint), ("style", ctypes.c_uint),
        ("lpfnWndProc", ctypes.c_void_p), ("cbClsExtra", ctypes.c_int),
        ("cbWndExtra", ctypes.c_int), ("hInstance", ctypes.c_void_p),
        ("hIcon", ctypes.c_void_p), ("hCursor", ctypes.c_void_p),
        ("hbrBackground", ctypes.c_void_p), ("lpszMenuName", ctypes.c_wchar_p),
        ("lpszClassName", ctypes.c_wchar_p), ("hIconSm", ctypes.c_void_p),
    ]

# Shell_NotifyIcon actions
NIM_ADD        = 0x00000000
NIM_MODIFY     = 0x00000001
NIM_DELETE     = 0x00000002
NIM_SETVERSION = 0x00000004
NOTIFYICON_VERSION_4 = 4

# NIF flags
NIF_MESSAGE = 0x00000001
NIF_ICON    = 0x00000002
NIF_TIP     = 0x00000004

# System icon IDs
IDI_INFORMATION = 32513

# Menu flags
TPM_RETURNCMD  = 0x0100
TPM_RIGHTBUTTON = 0x0002
MF_STRING      = 0x0000
MF_GRAYED      = 0x0003
MF_SEPARATOR   = 0x0800

# Custom window messages
WM_USER              = 0x0400
WM_APP               = 0x8000
WM_TRAY_CALLBACK     = WM_APP + 1
WM_USER_TRAY_UPDATE  = WM_USER + 1

# Menu command IDs
CMD_STATUS_SHOW   = 1
CMD_EXIT          = 2
CMD_TOGGLE_CONSOLE = 3
TRAY_ICON_ID      = 1

# ── Set explicit 64-bit argtypes for Win32 functions used by TrayApp ─────────
_user32 = ctypes.windll.user32
_user32.DefWindowProcW.argtypes = [ctypes.c_void_p, ctypes.c_uint, ctypes.c_ulonglong, ctypes.c_ulonglong]
_user32.DefWindowProcW.restype = ctypes.c_longlong
_user32.GetMessageW.argtypes = [ctypes.c_void_p, ctypes.c_void_p, ctypes.c_uint, ctypes.c_uint]
_user32.GetMessageW.restype = ctypes.c_int
_user32.DispatchMessageW.argtypes = [ctypes.c_void_p]
_user32.DispatchMessageW.restype = ctypes.c_longlong
_user32.PostMessageW.argtypes = [ctypes.c_void_p, ctypes.c_uint, ctypes.c_ulonglong, ctypes.c_ulonglong]
_user32.PostMessageW.restype = ctypes.c_int

# ── End of Tray API definitions ─────────────────────────────────────────────


def do_auth_portal_post(config):
    """POST-based campus portal auth.
    Step 0: try to trigger portal redirect by accessing check_url (full browser headers)
    Step 1: fallback — access portal index.jsp directly, then try API
    Step 2: POST credentials to InterFace.do?method=login
    """
    portal_host = config.get("portal_url", "")
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

    index_url = urljoin(portal_host + "/eportal/", index_url)
    # GET index.jsp to obtain JSESSIONID cookie (needed for both paths)
    index_body = ""
    try:
        req = urllib.request.Request(index_url, headers={"User-Agent": BROWSER_UA})
        resp = opener.open(req, timeout=timeout)
        index_body = resp.read(204800).decode("utf-8", errors="ignore")
        final_url = resp.geturl()
        if final_url != index_url:
            index_url = final_url
            log(f"Portal responded with: {index_url[:150]}...", "AUTH")
        # scan body for JS redirect (same as Step 0, but for index page)
        if not urlparse(index_url).query:
            m = re.search(r"location\.href\s*=\s*['\"]([^'\"]*index\.jsp[^'\"]*)", index_body)
            if m:
                index_url = m.group(1)
                log(f"Found JS redirect in index page: {index_url[:150]}...", "AUTH")
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
                final_url = resp.geturl()
                if final_url != api_url and "index.jsp" in final_url:
                    index_url = final_url
                    query_string = urlparse(index_url).query
                    log(f"Got redirect with params: {index_url[:150]}...", "AUTH")
                    break
            except Exception as e:
                log(f"API {api_method} failed: {e}", "AUTH")

    # Step 1.6: extract params from index page body (hidden inputs / JS vars)
    if not query_string and index_body:
        # try hidden inputs: <input name="wlanuserip" value="10.1.2.3"/>
        params = re.findall(r'<input[^>]*name=["\'](wlan\w+|\w+ip|nas\w*|mac)["\'][^>]*value=["\']([^"\']*)["\']',
                            index_body, re.IGNORECASE)
        if params:
            query_string = "&".join(f"{k}={v}" for k, v in params)
            log(f"Extracted params from page body: {query_string[:200]}", "AUTH")
        else:
            # try JS vars: var wlanuserip = "10.1.2.3";
            js_params = re.findall(r'(?:var|let|const)\s+(wlan\w+|\w+ip|nas\w*|mac)\s*=\s*["\']([^"\']+)["\']',
                                   index_body, re.IGNORECASE)
            if js_params:
                query_string = "&".join(f"{k}={v}" for k, v in js_params)
                log(f"Extracted JS params from page body: {query_string[:200]}", "AUTH")

    # Step 1.7: last resort — construct minimal queryString from local IP
    if not query_string:
        try:
            parsed = urlparse(portal_host)
            host = parsed.hostname or portal_host
            s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            s.settimeout(2)
            s.connect((host, 80))
            local_ip = s.getsockname()[0]
            s.close()
            query_string = f"wlanuserip={local_ip}"
            log(f"Fallback queryString: {query_string}", "AUTH")
        except Exception as e:
            log(f"Could not construct queryString: {e}", "AUTH")

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
        snippet = body[:200].replace("\n", " ").strip()
        try:
            result = json.loads(body)
        except (ValueError, TypeError):
            result = {}
        if not isinstance(result, dict) or result.get("result") != "success":
            log(f"Auth FAIL [HTTP {resp.status}, {elapsed:.0f}ms] body: {snippet}", "ERROR")
            return False
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
    cooldown = config.get("auth_cooldown_seconds", 3)
    if last_auth_time and (now - last_auth_time).total_seconds() < cooldown:
        remaining = cooldown - int((now - last_auth_time).total_seconds())
        log(f"Auth cooldown ({remaining}s remaining), skip", "INFO")
        return False

    method = config.get("auth_method", "http")
    url = config.get("portal_url", "")
    timeout = config["request_timeout"]

    if method == "portal_post":
        if not do_auth_portal_post(config):
            return False
    elif method == "browser":
        do_auth_browser(url, config.get("browser_wait_seconds", 3))
        log("Browser auth completed (Enter sent)", "AUTH")
    else:
        start = time.time()
        status, body, info = do_auth_http(url, timeout)
        elapsed = (time.time() - start) * 1000
        snippet = body[:200].replace("\n", " ").strip() if body else "(empty)"
        if status and 200 <= status < 300:
            body_lower = body.lower() if body else ""
            if "fail" in body_lower or "error" in body_lower:
                log(f"Auth FAIL [{info}, {elapsed:.0f}ms] body: {snippet}", "ERROR")
                return False
            log(f"Auth OK [{info}, {elapsed:.0f}ms] body: {snippet}", "AUTH")
            log("NOTE: http 模式无法100%确认认证成功，建议改用 portal_post 模式", "WARN")
        else:
            log(f"Auth FAIL [{info}, {elapsed:.0f}ms]", "ERROR")
            return False

    # The portal response alone does not establish Internet connectivity.
    for attempt in range(3):
        if attempt:
            time.sleep(1)
        ok, detail = check_network(config["check_url"], timeout,
                                   config.get("check_expected_body"))
        if ok:
            return True
    log(f"Auth response received but network is unavailable: {detail}", "ERROR")
    return False


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
    stime = config.get("schedule_time", "20:30")
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
    current_dir = os.path.dirname(sys.executable) if getattr(sys, "frozen", False) else SCRIPT_DIR
    print("  ┌──────────────────────────────────────────────────┐")
    print("  │ 第三步：部署到 Windows 计划任务（需要管理员权限） │")
    print("  └──────────────────────────────────────────────────┘")
    print()
    print("  计划任务需要管理员权限，无法在程序内直接创建。")
    print()
    print("  ▸ 你的程序目录（复制这行）：")
    print(f"    {current_dir}")
    print()
    print("  ▸ 操作步骤：")
    print("    1. 按 Win 键 → 输入 PowerShell")
    print("    2. 右键 Windows PowerShell → 以管理员身份运行")
    print("    3. 在蓝色窗口输入 cd 空格，右键粘贴上面的路径：")
    print(f"        cd \"{current_dir}\"")
    print("    4. 运行部署脚本：")
    print("        .\\setup_task.ps1")
    print(f"    5. 看到绿色提示即成功！每天 {stime} 自动启动。")
    print()
    print("  ▸ 修改触发时间：")
    print("    在菜单 [3] 修改 schedule_time，或编辑配置文件")
    print("    使用 HH:mm 格式（如 17:58），")
    print("    保存后重新运行 .\\setup_task.ps1 即可。")
    print()
    print("  ▸ 替代方案：开机自启动（不需要每天同一时间触发）：")
    print("    以管理员身份运行: .\\setup_task.ps1 -Boot")
    print("    系统启动时自动开始监控，全天候断网即重连。")
    print()
    print("  ┌──────────────────────────────────────────────────┐")
    print("  │ 第四步：验证是否生效                             │")
    print("  └──────────────────────────────────────────────────┘")
    print()
    print("  第二天打开 logs/ 目录，看当天日志文件。")
    print("  如果有 [DOWN] → [AUTH] → [RECOVER] 的记录，")
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
        ("username", "学号/用户名", "你的学号"),
        ("password", "校园网密码", "你的密码"),
        ("portal_url", "校园网认证地址 (不知道可以不填)", "http://10.10.200.102"),
        ("schedule_time", "定时触发时间 (24h制)", "20:30"),
        ("check_url", "网络检测地址", "http://www.baidu.com"),
    ]

    for key, label, default_val in fields:
        current = config.get(key, "")
        if current:
            prompt = f"  {label} [{current}]: "
        else:
            prompt = f"  {label}: "
        val = _input(prompt).strip()
        if val:
            config[key] = val
        elif not current and default_val:
            config[key] = default_val

    print()
    print("  配置完成！正在保存...")

    target = config_write_path()
    if save_config(config, path=target):
        print(f"  已保存到: {target}")
    else:
        print(f"  保存失败: {target}")

    print()
    return config


def show_menu():
    """Display menu and return user choice."""
    current_dir = os.path.dirname(sys.executable) if getattr(sys, "frozen", False) else SCRIPT_DIR
    print()
    print("=" * 62)
    print("  校园网自动认证工具 " + VERSION)
    print("  项目: https://github.com/suching8848/syxy_auto_verification")
    print("-" * 62)
    print(f"  程序目录: {current_dir}")
    print("-" * 62)
    print("  [1] 启动自动认证")
    print("      持续检测网络状态，断网时自动重新登录")
    print("      适合：临时使用，关闭窗口即停止")
    print()
    print("  [2] 测试认证")
    print("      发送一次认证请求，验证账号密码是否正确")
    print("      适合：首次使用前确认配置无误")
    print()
    print("  [3] 修改基本配置")
    print("      修改学号、密码、认证地址等常用项")
    print("      如需修改检测间隔等高级参数，直接编辑配置文件：")
    print(f"      {os.path.join(current_dir, 'auto_login_config.json')}")
    print()
    print("  [4] 定时部署")
    print("      已知每天断网时间 → 设置 Windows 计划任务定时触发")
    print("      适合：校园网每天固定时间断网（如每晚 20:30）")
    print()
    print("  [5] 后台常驻")
    print("      不知道断网时间 → 托盘图标常驻后台，断网自动重连")
    print("      终端窗口消失，仅通知区域留一个蓝色 i 图标")
    print("      资源占用极低（CPU ~0%，内存 ~20MB），不影响电脑性能")
    print("      适合：断网时间不固定，或需要全天候自动认证")
    print()
    print("  [6] 使用帮助")
    print("      FAQ 常见问题：认证失败、闪退、日志在哪等")
    print()
    print("  [q] 退出")
    print("-" * 62)
    choice = _input("  请选择: ").strip().lower()
    return choice or "1"


# ── System Tray Application Class ────────────────────────────────────────────

class TrayApp:
    """Hidden window + notification area icon for background operation.
    Uses ctypes to call Win32 Shell_NotifyIcon API — zero external dependencies."""

    def __init__(self, config, start_hidden=False, status_text="Initializing...",
                 on_error=None, on_done=None, start_worker=True):
        """`start_worker=False` 只建图标和消息循环，不自己起探测线程。

        GUI（场景 A）必须传 False：那里的守护由 `GuiApp.start_monitor()` /
        `stop_monitor()` 拥有，托盘再自带一路就会变成两个 `run_detection_loop`
        同时探测，而点「停止守护」只停得掉界面那一路 —— 界面显示"待命中"，后台还在
        重连。控制台/托盘模式（`--tray`、菜单 [5]）继续用默认的 True，它们的守护
        线程本来就是托盘自己的。
        """
        self._config = config.copy()
        if start_hidden:
            self._config["run_duration_minutes"] = 0
        self._stop_event = threading.Event()
        self._worker = None
        self._start_worker = start_worker
        self._status = status_text
        self._on_error = on_error
        self._on_done = on_done
        self._hwnd = None
        self._hinst = ctypes.windll.kernel32.GetModuleHandleW(None)
        self._wnd_proc_cb = None
        self._title = f"Campus Network Auto-Login {VERSION}"
        self._start_hidden = start_hidden

    # ── public API ────────────────────────────────────────────────────────

    def run(self):
        """Entry point. Creates window + tray icon, starts worker, enters message loop.

        Returns True only when the tray really came up. False means "no tray at
        all" (window/icon creation failed) — the GUI uses that to drop back to
        window-only mode instead of exiting the program.
        """
        try:
            self._create_window()
            self._create_tray_icon()
        except Exception as e:
            if not self._start_worker:
                # GUI：没有托盘就回到无托盘窗口模式，调用方会自己保证"没点就不跑"。
                log(f"Tray init failed: {e}; no tray, caller keeps its own guard",
                    "ERROR")
                return False
            log(f"Tray init failed: {e}, falling back to console", "ERROR")
            # 回退也要能停：把托盘的停止事件交给探测循环，否则 Ctrl+C 之外
            # 没有任何办法让它退出。
            run_detection_loop(self._config, stop_event=self._stop_event)
            return False

        # hide console initially if requested
        if self._start_hidden:
            self._hide_console()

        if self._start_worker:
            self._worker = threading.Thread(target=self._detection_worker, daemon=True)
            self._worker.start()
        self._message_loop()
        self._cleanup()
        return True

    # ── console show/hide ─────────────────────────────────────────────────

    def _hide_console(self):
        """Hide the console/terminal window — use EnumWindows to catch parent terminal."""
        console = ctypes.windll.kernel32.GetConsoleWindow()
        if console:
            ctypes.windll.user32.ShowWindow(console, 0)

        pid = os.getpid()
        @ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.c_void_p, ctypes.c_void_p)
        def _enum_cb(hwnd, _lp):
            wpid = ctypes.c_ulong()
            ctypes.windll.user32.GetWindowThreadProcessId(hwnd, ctypes.byref(wpid))
            if wpid.value == pid and ctypes.windll.user32.IsWindowVisible(hwnd):
                ctypes.windll.user32.ShowWindow(hwnd, 0)
            return True
        ctypes.windll.user32.EnumWindows(_enum_cb, 0)

    def _show_console(self):
        """Restore the console/terminal window."""
        console = ctypes.windll.kernel32.GetConsoleWindow()
        if console:
            ctypes.windll.user32.ShowWindow(console, 5)  # SW_SHOW
            ctypes.windll.user32.SetForegroundWindow(console)

    # ── window creation ───────────────────────────────────────────────────

    def _create_window(self):
        # WPARAM / LPARAM are pointer-sized
        WNDPROC = ctypes.WINFUNCTYPE(
            ctypes.c_longlong, ctypes.c_void_p, ctypes.c_uint,
            ctypes.c_ulonglong, ctypes.c_ulonglong,
        )

        # set explicit 64-bit argtypes for DefWindowProcW (default c_int is 32-bit)
        _DefWndProc = ctypes.windll.user32.DefWindowProcW
        _DefWndProc.argtypes = [ctypes.c_void_p, ctypes.c_uint,
                                ctypes.c_ulonglong, ctypes.c_ulonglong]
        _DefWndProc.restype = ctypes.c_longlong

        def wnd_proc(hwnd, msg, wparam, lparam):
            if msg == WM_TRAY_CALLBACK:
                if lparam == 0x0205:  # WM_RBUTTONUP
                    self._show_context_menu()
                    return 0
            elif msg == WM_USER_TRAY_UPDATE:
                self._update_tooltip(self._status)
                return 0
            elif msg == WM_USER + 2:
                self._request_exit()
                return 0
            elif msg == 0x0002:  # WM_DESTROY
                ctypes.windll.user32.PostQuitMessage(0)
                return 0
            return _DefWndProc(hwnd, msg, wparam, lparam)

        self._wnd_proc_cb = WNDPROC(wnd_proc)

        wcx = _WNDCLASSEXW()
        wcx.cbSize = ctypes.sizeof(_WNDCLASSEXW)
        wcx.lpfnWndProc = ctypes.cast(self._wnd_proc_cb, ctypes.c_void_p)
        wcx.hInstance = self._hinst
        wcx.lpszClassName = "AutoLoginTrayClass"

        ctypes.windll.user32.RegisterClassExW(ctypes.byref(wcx))
        self._hwnd = ctypes.windll.user32.CreateWindowExW(
            0, "AutoLoginTrayClass", None, 0,
            0, 0, 0, 0, None, None, self._hinst, None,
        )
        if not self._hwnd:
            raise RuntimeError("CreateWindowExW failed")

    # ── tray icon ─────────────────────────────────────────────────────────

    def _create_tray_icon(self):
        nid = _NOTIFYICONDATAW()
        nid.cbSize = ctypes.sizeof(_NOTIFYICONDATAW)
        nid.hwnd = self._hwnd
        nid.uID = TRAY_ICON_ID
        nid.uFlags = NIF_MESSAGE | NIF_ICON | NIF_TIP
        nid.uCallbackMessage = WM_TRAY_CALLBACK
        nid.hIcon = ctypes.windll.user32.LoadIconW(0, ctypes.c_void_p(IDI_INFORMATION))
        nid.szTip = self._title

        if not ctypes.windll.shell32.Shell_NotifyIconW(NIM_ADD, ctypes.byref(nid)):
            raise RuntimeError("Shell_NotifyIcon NIM_ADD failed")

        # Keep the default callback format: lParam is the mouse message.

    def _update_tooltip(self, text):
        nid = _NOTIFYICONDATAW()
        nid.cbSize = ctypes.sizeof(_NOTIFYICONDATAW)
        nid.hwnd = self._hwnd
        nid.uID = TRAY_ICON_ID
        nid.uFlags = NIF_TIP
        nid.szTip = text[:127]
        ctypes.windll.shell32.Shell_NotifyIconW(NIM_MODIFY, ctypes.byref(nid))

    def _remove_tray_icon(self):
        nid = _NOTIFYICONDATAW()
        nid.cbSize = ctypes.sizeof(_NOTIFYICONDATAW)
        nid.hwnd = self._hwnd
        nid.uID = TRAY_ICON_ID
        ctypes.windll.shell32.Shell_NotifyIconW(NIM_DELETE, ctypes.byref(nid))

    # ── context menu ──────────────────────────────────────────────────────

    def _show_context_menu(self):
        console_visible = ctypes.windll.kernel32.GetConsoleWindow() and \
                          ctypes.windll.user32.IsWindowVisible(
                              ctypes.windll.kernel32.GetConsoleWindow())

        menu = ctypes.windll.user32.CreatePopupMenu()
        ctypes.windll.user32.AppendMenuW(menu, MF_GRAYED, CMD_STATUS_SHOW,
                                         f"Status: {self._status[:60]}")
        ctypes.windll.user32.AppendMenuW(menu, MF_SEPARATOR, 0, None)
        if console_visible:
            ctypes.windll.user32.AppendMenuW(menu, MF_STRING, CMD_TOGGLE_CONSOLE,
                                             "Hide to Tray")
        else:
            ctypes.windll.user32.AppendMenuW(menu, MF_STRING, CMD_TOGGLE_CONSOLE,
                                             "Show Console")
        ctypes.windll.user32.AppendMenuW(menu, MF_SEPARATOR, 0, None)
        ctypes.windll.user32.AppendMenuW(menu, MF_STRING, CMD_EXIT, "Exit")

        pt = _POINT()
        ctypes.windll.user32.GetCursorPos(ctypes.byref(pt))
        ctypes.windll.user32.SetForegroundWindow(self._hwnd)

        cmd = ctypes.windll.user32.TrackPopupMenu(
            menu, TPM_RETURNCMD | TPM_RIGHTBUTTON,
            pt.x, pt.y, 0, self._hwnd, None,
        )
        ctypes.windll.user32.PostMessageW(self._hwnd, WM_USER, 0, 0)
        ctypes.windll.user32.DestroyMenu(menu)

        if cmd == CMD_EXIT:
            self._request_exit()
        elif cmd == CMD_TOGGLE_CONSOLE:
            if console_visible:
                self._hide_console()
            else:
                self._show_console()

    # ── message loop ──────────────────────────────────────────────────────

    def _message_loop(self):
        msg = _MSG()
        lp_msg = ctypes.byref(msg)
        while ctypes.windll.user32.GetMessageW(lp_msg, None, 0, 0) > 0:
            ctypes.windll.user32.TranslateMessage(lp_msg)
            ctypes.windll.user32.DispatchMessageW(lp_msg)

    # ── lifecycle ─────────────────────────────────────────────────────────

    def _detection_worker(self):
        try:
            run_detection_loop(
                self._config,
                stop_event=self._stop_event,
                status_callback=self._on_status,
            )
        except Exception as e:
            log(f"Tray worker crashed: {e}", "ERROR")
            if self._on_error:
                try:
                    self._on_error(e)
                except Exception:
                    pass
        finally:
            if self._on_done:
                try:
                    self._on_done()
                except Exception:
                    pass
            if self._hwnd:
                ctypes.windll.user32.PostMessageW(self._hwnd, WM_USER + 2, 0, 0)

    def _on_status(self, status_line):
        """Called from worker thread. Post message to main thread for tooltip update."""
        self._status = status_line
        if self._hwnd:
            ctypes.windll.user32.PostMessageW(self._hwnd, WM_USER_TRAY_UPDATE, 0, 0)

    def _request_exit(self):
        self._stop_event.set()
        if self._hwnd:
            ctypes.windll.user32.DestroyWindow(self._hwnd)

    def _cleanup(self):
        self._stop_event.set()
        if self._worker and self._worker.is_alive():
            self._worker.join(timeout=5)
        if self._hwnd:
            self._remove_tray_icon()


# ── End of TrayApp ───────────────────────────────────────────────────────────


def tcp_reachable(host, port=80, timeout=2):
    """True if a TCP connection to host:port succeeds. Distinguishes
    'no connectivity at all' from 'reachable but content looks wrong'."""
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.settimeout(timeout)
    try:
        s.connect((host, port))
        return True
    except OSError:
        return False
    finally:
        s.close()


def log_failure_context(url, detail):
    """When the online check fails, record whether the host was at least
    reachable. A reachable host with a failed check means either the portal is
    transparently proxying (no redirect) or a redirect we refuse to follow —
    useful for telling a real outage apart from a portal that never hijacked."""
    host = urlparse(url).hostname
    if not host:
        return
    ok = tcp_reachable(host)
    if ok:
        log(
            f"Detail: {detail} — remote host is TCP-reachable, so this is NOT a "
            f"plain outage (portal may be proxying without redirecting, or a "
            f"redirect we treat as portal)",
            "WARN",
        )
    else:
        log(f"Detail: {detail} — remote host unreachable (genuine outage or DNS failure)", "WARN")


def auth_retry_delay(config, failures):
    """First retry is quick; subsequent failures back off from a separate base."""
    first = max(1, config.get("auth_cooldown_seconds", 3))
    base = max(first, config.get("auth_retry_backoff_seconds", 5))
    if failures <= 1:
        return first
    return min(max(300, base), base * 2 ** min(failures - 2, 10))


def run_detection_loop(config, stop_event=None, status_callback=None,
                       duration_override=None):
    """Core detection loop — runs until stopped or duration exceeded.

    Keeps probing check_url and re-authenticates as soon as the portal blocks
    traffic. This is the behaviour both GUI scenarios reuse: scenario A calls it
    from the window/tray, scenario B calls it with no UI at all.

    Args:
        stop_event: threading.Event — when set, loop exits gracefully
        status_callback: callable(str) — called with status line for tray tooltip
        duration_override: int minutes — overrides config run_duration_minutes
            (0 = run forever)
    """
    check_url = config["check_url"]
    interval_ok = config["check_interval_ok"]
    interval_fail = config["check_interval_fail"]
    fail_threshold = config["fail_threshold"]
    timeout = config["request_timeout"]
    if duration_override is None:
        run_duration = config.get("run_duration_minutes", 0)
    else:
        run_duration = duration_override
    is_tray = status_callback is not None

    log("Service started" + (" [tray]" if is_tray else " [interactive]" if INTERACTIVE else " [background]"), "START")
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
    next_auth_at = 0.0
    auth_failures = 0
    was_down = False

    while not (stop_event and stop_event.is_set()):
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
                auth_failures = 0
            else:
                fail_count += 1
                if not outage_start:
                    outage_start = datetime.now()
                outage_checks += 1

                if fail_count >= fail_threshold:
                    if not was_down:
                        log(f"Network DOWN (reason: {detail}), starting auth", "DOWN")
                        log_failure_context(check_url, detail)
                        was_down = True
                        outage_start = datetime.now()
                        outage_checks = 0
                        auth_attempts = 0

                    if time.monotonic() >= next_auth_at:
                        auth_attempts += 1
                        authenticated = do_auth(config, None)
                        auth_failures = 0 if authenticated else auth_failures + 1
                        delay = auth_retry_delay(config, auth_failures)
                        next_auth_at = time.monotonic() + delay
                    fail_count = 0
                else:
                    log(f"Check #{fail_count} failed: {detail}", "WARN")

            # build status line for tray tooltip / interactive console
            uptime = format_duration((datetime.now() - start_time).total_seconds())
            sleep = interval_fail if was_down or fail_count else interval_ok

            if was_down and outage_start:
                state = f"[DOWN] outage {format_duration((datetime.now() - outage_start).total_seconds())} | auth #{auth_attempts + 1}"
            elif fail_count > 0:
                state = f"[WARN] check failed {fail_count}/{fail_threshold}"
            else:
                state = "[OK]  reachable"

            status_line = f"{state} | next in {sleep}s | uptime {uptime} | checks #{total_checks}"

            if status_callback:
                status_callback(status_line)
            if INTERACTIVE:
                log(status_line, "STATUS")
            elif total_checks % 2 == 0:
                log(status_line, "STATUS")

            # interruptible sleep — check stop_event every 0.5s
            _sleep = sleep
            for _ in range(int(_sleep * 2)):
                if stop_event and stop_event.is_set():
                    break
                time.sleep(0.5)

        except KeyboardInterrupt:
            log("Service stopped by user", "STOP")
            break
        except Exception as e:
            log(f"Unexpected error: {e}", "ERROR")
            for _ in range(int(interval_fail * 2)):
                if stop_event and stop_event.is_set():
                    break
                time.sleep(0.5)


def _need_setup(config):
    """Check if config needs first-time setup."""
    method = config.get("auth_method", "http")
    if method == "portal_post":
        return (not config.get("username") or not config.get("password")
                or config.get("username") == "你的学号"
                or config.get("password") == "你的密码")
    return not config.get("portal_url")


def _stdin_available():
    """True when a usable stdin exists. Under --windowed (or pythonw.exe)
    sys.stdin is None, so any input() call would raise and kill the process."""
    try:
        return sys.stdin is not None and sys.stdin.readable()
    except Exception:
        return False


def notify_setup_required(config, gui=True):
    """The config is incomplete and no terminal is available to fix it.
    Logs an actionable message and, when a GUI is possible, pops a dialog.

    Returns True when the notice reached a human, False when there was nowhere
    to show it (silent/scheduled runs) — callers should then just abort.
    """
    missing = ("username/password" if config.get("auth_method") == "portal_post"
               else "portal_url")
    log(f"Config incomplete (missing: {missing}) and no console is available", "ERROR")
    log("Open the program's Settings panel once to fill it in", "ERROR")
    if not gui:
        return False
    try:
        import tkinter
        from tkinter import messagebox
        root = tkinter.Tk()
        root.withdraw()
        messagebox.showerror(
            f"Campus Network Auto-Login {VERSION}",
            "配置文件还没填完整，程序无法启动。\n\n"
            f"缺少：{missing}\n\n"
            "请双击本程序打开主窗口，展开「设置」填好后再启动。",
        )
        root.destroy()
        return True
    except Exception as e:
        log(f"Could not show setup dialog: {e}", "WARN")
        return False


def _parse_hhmm(text):
    """Parse 'HH:MM' (24h) into a datetime.time, or None when unset/invalid."""
    if not text or not isinstance(text, str):
        return None
    m = re.match(r"^\s*([01]?\d|2[0-3]):([0-5]\d)\s*$", text)
    if not m:
        return None
    return datetime.min.replace(hour=int(m.group(1)), minute=int(m.group(2))).time()


def plan_silent_window(config, now=None, allow_tomorrow=True):
    """Scenario B scheduling math, pure and testable.

    The watcher is launched ahead of the expected outage and then probes
    continuously, so all this has to decide is how long to stand by before
    probing begins, and how long to keep probing.

    Returns (start_at, end_at, wait_seconds) where wait_seconds is how long to
    idle before probing begins (0 = start probing right now), or None when
    silent_start_time is unset or malformed.

    allow_tomorrow=False means "the user launched this by hand": if today's
    window has already elapsed we start immediately instead of sleeping a whole
    day, because waiting 24h is never what someone clicking the app wants.
    """
    now = now or datetime.now()
    target = _parse_hhmm(config.get("silent_start_time"))
    if target is None:
        return None
    try:
        run_minutes = int(config.get("silent_run_minutes", 30))
    except (TypeError, ValueError):
        run_minutes = 30

    start_at = datetime.combine(now.date(), target)
    end_at = start_at + timedelta(minutes=run_minutes)
    if end_at <= now:
        if allow_tomorrow:
            start_at += timedelta(days=1)
            end_at = start_at + timedelta(minutes=run_minutes)
        else:
            # Launching by hand: today's window already went by, so just run now.
            start_at = now
            end_at = now + timedelta(minutes=run_minutes)
    wait_seconds = max(0.0, (start_at - now).total_seconds())
    return start_at, end_at, wait_seconds


def run_silent_mode(run_minutes=None, interactive_launch=False):
    """Scenario B: no console, no window, no tray icon. Never touches tkinter.

    Launched ahead of the expected outage, it waits quietly for the scheduled
    start, then probes continuously — the very same loop scenario A uses — and
    re-authenticates the moment the portal starts blocking traffic. Quits on its
    own after silent_run_minutes so nothing lingers on the machine.

    interactive_launch=True (used by --silent --now) means a human started it on
    purpose right now, so never stand by for a whole day.
    """
    clean_old_logs()
    config = load_config()

    if _need_setup(config):
        notify_setup_required(config, gui=False)
        return 1

    window = plan_silent_window(config, allow_tomorrow=not interactive_launch)
    if window is None:
        log("Silent mode requested but silent_start_time is not set "
            "(expected \"HH:MM\") — falling back to a plain background run", "WARN")
        run_detection_loop(config, duration_override=run_minutes)
        return 0

    start_at, end_at, wait_seconds = window
    if interactive_launch:
        # --now means "start right now, do not wait for the configured time".
        # Blocking for hours because today's HH:MM hasn't arrived yet is never
        # what someone running --now wants, so collapse the window to now.
        now = datetime.now()
        log(f"--now given: skipping the wait until "
            f"{start_at.strftime('%H:%M')}, starting immediately", "INFO")
        start_at = now
        end_at = now + timedelta(
            minutes=run_minutes if run_minutes is not None
            else max(1, int(config.get("silent_run_minutes", 30))))
        wait_seconds = 0.0
    log(f"Silent mode armed: probing starts at {start_at.strftime('%Y-%m-%d %H:%M:%S')}, "
        f"service will exit at {end_at.strftime('%H:%M:%S')}", "START")

    if wait_seconds > 0:
        log(f"Standing by silently for {format_duration(wait_seconds)} "
            f"(no window, no tray icon)", "INFO")
        deadline = time.monotonic() + wait_seconds
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            time.sleep(min(1.0, remaining))

    if run_minutes is None:
        run_minutes = max(1, int(round((end_at - start_at).total_seconds() / 60)))
    log("Silent window open — probing for connectivity, will re-auth on outage", "START")
    run_detection_loop(config, duration_override=run_minutes)
    log("Silent window ended, exiting", "STOP")
    return 0


def main():
    parser = argparse.ArgumentParser(
        description=f"Campus Network Auto-Login {VERSION} — 校园网自动认证工具",
    )
    parser.add_argument("--auth", action="store_true", help="Test auth once and exit")
    parser.add_argument("--tray", action="store_true", help="System tray mode (background) — hidden window + notification area icon")
    parser.add_argument("--background", action="store_true", help="Background mode — no console output, detection loop only")
    parser.add_argument("--boot", action="store_true", help="Boot auto-start mode — continuous monitoring (run_duration_minutes=0), Session 0 safe")
    parser.add_argument("--version", action="version", version=f"auto_login {VERSION}")
    parser.add_argument("--silent", action="store_true",
                        help="Scenario B: no window, no tray — start quietly at silent_start_time, probe until the portal drops traffic, re-auth instantly, then exit after silent_run_minutes")
    parser.add_argument("--now", action="store_true",
                        help="With --silent: probe immediately instead of waiting for silent_start_time (for manual testing)")
    parser.add_argument("--run-minutes", type=int, default=None, metavar="N",
                        help="Override how many minutes the service runs (0 = forever)")
    args = parser.parse_args()

    # Scenario B ignores all terminal paths — never touch input()/print()
    if args.silent:
        return run_silent_mode(args.run_minutes, interactive_launch=args.now)

    # --auth flag: test auth and exit (works in any mode)
    if args.auth:
        clean_old_logs()
        config = load_config()
        if _need_setup(config) and not INTERACTIVE:
            notify_setup_required(config, gui=False)
            log("Config incomplete", "ERROR")
            return 1
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
        return 0 if ok else 1

    # --tray: start tray in current process (no subprocess — tray icon created first)
    if args.tray:
        clean_old_logs()
        config = load_config()
        if _need_setup(config):
            if not _stdin_available():
                notify_setup_required(config)
                return 1
            log("Config incomplete — running setup first...", "WARN")
            config = interactive_setup(config)
        TrayApp(config, start_hidden=True).run()
        return

    # Boot auto-start mode (开机自启动, Session 0 via scheduled task)
    if args.boot:
        clean_old_logs()
        config = load_config()
        config["run_duration_minutes"] = 0
        log("Boot mode: continuous monitoring (run_duration=0)", "START")
        if config.get("auth_method") == "browser":
            log("WARNING: browser auth requires user session — boot mode runs in Session 0, it will fail. Use portal_post or http.", "WARN")
        run_detection_loop(config)
        return

    # Background mode (scheduled task or --background flag)
    # Must come before print() — pythonw.exe has no stdout
    if not INTERACTIVE or args.background:
        clean_old_logs()
        config = load_config()
        run_detection_loop(config, duration_override=args.run_minutes)
        return

    print(DISCLAIMER, flush=True)
    print()
    clean_old_logs()

    config = load_config()

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
            print("  ║  自动认证已启动                           ║")
            print("  ║  通知区域已出现托盘图标，右键可：         ║")
            print("  ║  · Hide to Tray — 隐藏终端到托盘          ║")
            print("  ║  · Exit — 完全退出                        ║")
            print("  ║  隐藏后可通过右键图标 → Show Console 恢复 ║")
            print("  ╚════════════════════════════════════════════╝")
            print()
            _input("  按 Enter 开始...")
            TrayApp(config, start_hidden=False).run()

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
            print("  ╔════════════════════════════════════════════╗")
            print("  ║  后台常驻模式                             ║")
            print("  ║  终端窗口即将消失，通知区域出现蓝色 i 图标║")
            print("  ║  断网时自动重连，无需任何操作。           ║")
            print("  ║  右键图标 → Show Console 可恢复终端       ║")
            print("  ║  资源占用极低，不影响电脑性能。           ║")
            print("  ╚════════════════════════════════════════════╝")
            print()
            _input("  按 Enter 开始...")
            TrayApp(config, start_hidden=True).run()

        elif choice == "6":
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
            print("  Q: 校园网认证地址在哪看？")
            print("  A: 打开浏览器手动登录校园网，看地址栏。")
            print("     通常是 http://10.10.xxx.xxx 这样的 IP。")
            print("     三亚学院默认为 http://10.10.200.102")
            print("     不知道的话不用改，默认就能用。")
            print()
            print("  Q: 认证失败怎么办？")
            print("  A: 检查三样：学号密码是否正确、校园网认证地址")
            print("     是否写对、校园网是否换了认证方式。打开日志看")
            print("     [ERROR] 行的具体原因。")
            print()
            print("  Q: 我想让它在后台一直跑，怎么办？")
            print("  A: 两种方式：")
            print("     [5] 系统托盘模式 — 隐藏窗口，右下角图标常驻。")
            print("     [4] 无感部署指南 — 设计划任务，每天定时启动。")
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
    _tray_mode = "--tray" in sys.argv
    try:
        sys.exit(main())
    finally:
        if INTERACTIVE and not any(flag in sys.argv for flag in ("--tray", "--background", "--boot", "--auth", "--version", "--help", "-h")):
            input("\nPress Enter to exit...")
