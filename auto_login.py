import urllib.request
import urllib.error
from urllib.parse import urlparse, urlsplit, urlencode, urljoin
import http.cookiejar
import webbrowser
import json
import time
import os
import sys
import glob
import hashlib
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
VERSION = "v1.7.6-b-candidate.20260922.1"


def runtime_identity_line():
    """Identify the running artifact in its own log, even after it is replaced.

    A file hash checked hours later does not identify an earlier process. Log
    both the launch path and bytes loaded at startup so a future incident can
    be attributed to the actual executable instead of a similarly named copy.
    """
    path = os.path.abspath(sys.executable if getattr(sys, "frozen", False)
                           else __file__)
    try:
        digest = hashlib.sha256()
        with open(path, "rb") as source:
            for block in iter(lambda: source.read(1024 * 1024), b""):
                digest.update(block)
        sha256 = digest.hexdigest()
    except OSError:
        sha256 = "unavailable"
    return f"Runtime identity: version={VERSION} path={path} sha256={sha256}"

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


# ── Attempt timing instrumentation (observation only) ────────────────────────
#
# Records how long each phase of one auth attempt took, so retry parameters can
# be chosen from samples instead of guesses. A TimingRecord never influences a
# decision: every field is written for logging only. Callers pass one in
# optionally, so the existing boolean auth path is unchanged when it is absent.
#
# Two timing sources are deliberately kept apart:
#   * `_now()` uses time.monotonic() — the only clock safe for intervals
#     (wall time can jump when the network or clock changes).
#   * the durations printed as ms derive from monotonic deltas.
# This is wall-clock-agnostic and stays correct across DST/clock corrections.

_METRIC_UNIT = "ms"


def _now():
    """Monotonic seconds — used for every interval in an attempt."""
    return time.monotonic()


# How many auth attempts are executing right now, in this process. Kept for
# telemetry; the separate submit lock below now prevents actual overlap.
_AUTH_IN_FLIGHT = 0
_AUTH_IN_FLIGHT_LOCK = threading.Lock()
# Protect the entire request/verification path, not just the observation counter.
# A GUI click and the guard must never submit to the portal at the same time.
_AUTH_SUBMIT_LOCK = threading.Lock()


def _metric_ms(started_at, finished_at):
    """Milliseconds between two monotonic stamps; None when either is missing
    or the result is negative (which can only happen with a broken clock)."""
    if started_at is None or finished_at is None:
        return None
    delta = (finished_at - started_at) * 1000
    return None if delta < 0 else delta


def _fmt_metric(value, label=None):
    """Render one metric for the log; None values simply disappear."""
    if value is None:
        return None
    text = f"{value / 1000:.1f}s" if value >= 1000 else f"{value:.0f}ms"
    return f"{label} {text}" if label else text


def _timing_suffix(t):
    """Compact per-phase summary appended to the existing Auth OK/FAIL line.

    Returns "" when no record was supplied, so log lines produced by other
    callers (tests, --auth) stay byte-identical to before. Phases are labelled
    separately and never merged, so a reader can always tell where the time
    went: discover = portal discovery/params, post = the credential POST,
    verify = the post-auth connectivity check.
    """
    if t is None:
        return ""
    parts = []
    for label, value in (("discover", t.discovery_ms),
                         ("post", t.login_post_ms),
                         ("verify", t.verify_ms),
                         ("total", t.total_ms),
                         ("unaccounted", t.unaccounted_ms)):
        rendered = _fmt_metric(value, label)
        if rendered:
            parts.append(rendered)
    if t.http_requests or t.socket_probes:
        parts.append(f"http_requests={t.http_requests} socket_probes={t.socket_probes}")
    return f" | {' '.join(parts)}" if parts else ""


# ── Log redaction ────────────────────────────────────────────────────────────
#
# Portal responses are external input that routinely embeds session material:
# the observed body carries a `userIndex` token, and a request/response can
# carry a session id or a credential. Logs are shared for troubleshooting, so
# anything that looks like a secret is masked before it is written, and the
# raw body is never logged verbatim.

_SECRET_KEYS = (
    "password", "passwd", "pwd", "token", "secret", "sessionid", "session_id",
    "jsessionid", "authorization", "userindex", "ticket", "validcode",
    "querystring", "userId",
    # Portal session parameters: these identify the client's session on the
    # access controller and are bound into the login form, so they belong with
    # the rest of the session material even though they are not credentials.
    "wlanuserip", "wlanacname", "nasip", "wlanacip", "mac",
)

_REDACT_PATTERNS = (
    # JSON form:  "password":"FAKE_SECRET"
    (re.compile(
        r'(?i)"(' + "|".join(_SECRET_KEYS) + r')"\s*:\s*"[^"]*"'),
     r'"\1":"***"'),
    # Form/query form:  password=FAKE_SECRET&token=FAKE_TOKEN
    (re.compile(
        r'(?i)\b(' + "|".join(_SECRET_KEYS) + r')\s*=\s*([^"\s&,;]+)'),
     r'\1=***'),
    # long opaque blobs: hex/base64-ish runs that are almost never diagnostics
    (re.compile(r'\b[0-9a-fA-F]{24,}\b'), '***'),
    (re.compile(r'\b[A-Za-z0-9+/]{32,}={0,2}\b'), '***'),
)


def redact_for_log(text, limit=200):
    """Mask *recognised* secrets and bound the length of free-form text.

    Defence in depth, NOT a guarantee: an unforeseen field name holding a short
    opaque value (`{"accesstoken":"Ab3xK9mQ2pL7v"}`) passes straight through,
    because a blacklist cannot know a name it has never seen and the value is
    too short to look like an opaque blob. Measured, not assumed.

    Therefore: never hand a raw network payload to this function. Values taken
    from the network go through `describe_portal_body`, `url_for_log` or
    `query_param_names`, which emit structure only. This function is for text
    a human wrote (the portal's own error message) and for defence in depth on
    paths that already avoid raw values.
    """
    if not isinstance(text, str):
        return ""
    cleaned = text.replace("\r", " ").replace("\n", " ").strip()
    if not cleaned:
        return ""
    for pattern, replacement in _REDACT_PATTERNS:
        cleaned = pattern.sub(replacement, cleaned)
    if limit and len(cleaned) > limit:
        cleaned = cleaned[:limit] + "..."
    return cleaned


def network_error_for_log(error):
    """Describe transport failures without echoing exception-supplied URLs.

    urllib exceptions may include the full request URL (including portal
    session parameters), so their string form is not suitable for any log or
    GUI detail. The class and OS error number retain useful diagnostics.
    """
    if isinstance(error, urllib.error.HTTPError):
        return f"HTTP {error.code}"
    if isinstance(error, urllib.error.URLError):
        error = error.reason
    if isinstance(error, TimeoutError):
        return "timeout"
    if isinstance(error, OSError):
        number = getattr(error, "errno", None)
        return (f"{type(error).__name__} errno={number}"
                if number is not None else type(error).__name__)
    return type(error).__name__


def describe_portal_body(body):
    """A log-safe one-liner describing a portal response.

    Whitelist, not blacklist. Masking known secret *names* cannot work: a field
    this code has never seen (`accesstoken`, a device fingerprint, the next
    portal release's new token) is unknown by definition, and a short opaque
    value slips past the long-blob rules too. Measured on a realistic token,
    `{"accesstoken":"Ab3xK9mQ2pL7v"}` passed through unchanged.

    So no network-sourced VALUE is ever emitted:
      * the verdict and the portal's own message are allow-listed fields, and
        the message still goes through the redaction pass;
      * any other JSON field contributes only its NAME;
      * a response that is not a JSON object contributes only its size.
    The raw body is never included — a success response carries a session token
    in `userIndex`, which would otherwise land in every log file.
    """
    if not body:
        return "(empty)"
    result, message = parse_portal_reply(body)
    if message:
        return f"result={result or '?'} msg={redact_for_log(message)}"
    if result:
        return f"result={result}"
    return f"(unparsed {len(body)} bytes) {json_field_names(body)}"


def json_field_names(body):
    """The field names of a JSON-object body, or a size note for anything else.

    Names are structural, never values, so this cannot echo a secret even when
    the field is one nobody anticipated.
    """
    try:
        parsed = json.loads(body)
    except (ValueError, TypeError):
        return "non-JSON"
    if not isinstance(parsed, dict):
        return f"json {type(parsed).__name__}"
    names = sorted(str(key) for key in parsed.keys())
    if not names:
        return "json object (no fields)"
    return "fields " + ",".join(names[:12])


def _safe_portal_message(value, limit=160):
    """Backwards-compatible alias: redact + bound a single portal message."""
    return redact_for_log(value, limit)


# Portal discovery carries session material in its URLs and bodies just as the
# login response does. Rather than masking known-bad names (which cannot cover
# a field nobody anticipated), discovery logging emits structure only: URL
# parameter names, and JSON field names — never a value taken from the network.
def url_for_log(url):
    """A portal URL reduced to the parts that are safe to keep.

    The host and path identify the portal (already known from config); the query
    string is where session material lives and where an unforeseen parameter
    name would leak, so the values are never emitted. Parameter NAMES are kept,
    which is what makes the line diagnostically useful.
    """
    if not isinstance(url, str) or not url:
        return ""
    split = urlsplit(url)
    base = f"{split.scheme}://{split.netloc}{split.path}" if split.scheme else split.path
    if not split.query:
        return base
    names = []
    for pair in split.query.split("&"):
        name = pair.split("=", 1)[0]
        if name:
            names.append(name)
    return f"{base}?{','.join(names)}" if names else base


def query_param_names(query_string):
    """Only the parameter names of a form/query string, never the values.

    Used where a query string is itself the diagnostic artefact (extracted
    params, fallback queryString): seeing which parameters were recovered is
    the useful part, and the values are exactly the session material that must
    not be written down.
    """
    if not isinstance(query_string, str) or not query_string:
        return "(none)"
    names = [pair.split("=", 1)[0] for pair in query_string.split("&") if pair]
    names = [name for name in names if name]
    return ",".join(names) if names else "(none)"


def body_for_log(body, limit=120):
    """A response body as log-safe structure only (see describe_portal_body)."""
    if not isinstance(body, str) or not body:
        return "(empty)"
    return f"({len(body)} bytes) {json_field_names(body)}"


def parse_portal_reply(body):
    """Extract (result, message) from a portal JSON body.

    Pure and defensive: non-JSON, arrays, null and oversized messages all come
    back as empty strings rather than raising. Stage A only records what the
    portal said — classifying it into a failure kind is deliberately left to a
    later step, so nothing here can steer a retry.
    """
    try:
        parsed = json.loads(body)
    except (ValueError, TypeError):
        return "", ""
    if not isinstance(parsed, dict):
        return "", ""
    result = parsed.get("result")
    result_text = result if isinstance(result, str) else ""
    return result_text[:32], _safe_portal_message(parsed.get("message"))


class TimingRecord:
    """Per-attempt phase timings plus the clue fields from the plan's A.5.

    A.5 insists these are *clues*, never proof: a conflict on the first attempt
    does not establish a portal-side leftover, and a conflict appearing only on
    later attempts does not prove this tool created one. The fields below are
    therefore stored verbatim and never turned into a verdict; any conclusion
    has to come from correlating several independent samples.
    """

    __slots__ = (
        "started_at", "discovery_finished_at", "login_started_at",
        "login_finished_at", "verify_finished_at", "finished_at",
        "http_requests", "socket_probes", "verify_attempts", "portal_http_status",
        "portal_result", "portal_message",
        "attempt_index", "overlapping_auth", "peer_device_state",
        "retry_planned_wait_ms", "retry_lateness_ms", "_retry_due_at",
    )

    def __init__(self):
        self.started_at = _now()
        self.discovery_finished_at = None
        self.login_started_at = None
        self.login_finished_at = None
        self.verify_finished_at = None
        self.finished_at = None
        self.http_requests = 0
        self.socket_probes = 0
        self.verify_attempts = 0
        self.portal_http_status = None
        self.portal_result = ""
        self.portal_message = ""
        # ── A.5 clue fields ──
        # Which attempt inside this outage this is (1 = the first one).
        self.attempt_index = 0
        # Kept for log compatibility. With the submit lock, actual in-process
        # overlap must remain False; lock contention is not a portal submit.
        self.overlapping_auth = False
        # User-supplied note about other devices on the same account. Usually
        # empty: it depends on the user reporting it, so absences prove nothing.
        self.peer_device_state = ""
        # Scheduling quality: how long we planned to wait vs how late we were.
        self.retry_planned_wait_ms = None
        self.retry_lateness_ms = None
        self._retry_due_at = None

    def count_request(self):
        """Count one HTTP request actually issued for this attempt."""
        self.http_requests += 1

    def mark_discovery_done(self):
        """Portal discovery, index.jsp and parameter completion are finished —
        i.e. everything before the credential POST is built.

        Must be called on every exit path, including failures, so the discovery
        phase is always closed and never silently absorbs time that belongs to
        a later phase.
        """
        self.discovery_finished_at = _now()

    def reboot_timeline(self):
        """Restart the phase timeline from now, discarding every stamp.

        Called once the submit lock has been acquired. `started_at` is set when
        the record is constructed, so lock waiting must be excluded from portal
        discovery and the unaccounted residual.
        """
        self.started_at = _now()
        self.discovery_finished_at = None
        self.login_started_at = None
        self.login_finished_at = None
        self.verify_finished_at = None
        self.finished_at = None
        if self._retry_due_at is not None:
            self.retry_lateness_ms = max(0.0,
                                         (self.started_at - self._retry_due_at) * 1000)

    def mark_login_started(self):
        """The credential POST is about to be sent."""
        self.login_started_at = _now()
        if self.discovery_finished_at is None:
            # Callers that skip explicit phase marks must not yield a bogus
            # negative POST duration; treat the gap as discovery.
            self.discovery_finished_at = self.login_started_at

    def mark_login_done(self):
        self.login_finished_at = _now()

    def mark_verify_done(self):
        self.verify_finished_at = _now()

    def mark_due(self, scheduled_at, planned_wait_ms=None):
        """Record how the retry schedule performed for this attempt.

        `scheduled_at` is the monotonic deadline the loop planned to auth at;
        `planned_wait_ms` is the interval it intended to wait. Keeping them
        apart matters: a slipped deadline shows up as lateness, while a long
        planned wait is a deliberate parameter — conflating the two would let a
        scheduling bug masquerade as a conservative setting.
        """
        if planned_wait_ms is not None:
            self.retry_planned_wait_ms = planned_wait_ms
        # A falsy deadline means "no scheduled retry yet" — the first attempt of
        # an outage starts with next_auth_at = 0.0, and measuring against that
        # would report the machine's uptime as lateness. Only a real deadline
        # says anything about scheduling quality.
        if not scheduled_at:
            return
        self._retry_due_at = scheduled_at
        self.retry_lateness_ms = max(0.0, (self.started_at - scheduled_at) * 1000)

    def mark_finished(self):
        self.finished_at = _now()

    @property
    def total_ms(self):
        """Whole attempt, from record creation to the terminal mark.

        Deliberately does NOT fall back to an earlier mark: a phase that never
        ran must report None, not 0.0, so "not measured" and "instant" stay
        distinguishable when the samples are reviewed. Every exit path is
        responsible for calling mark_finished().
        """
        return _metric_ms(self.started_at, self.finished_at)

    @property
    def unaccounted_ms(self):
        """Time inside total_ms that no named phase claims.

        Kept explicit rather than folded into a phase: record construction, the
        return trip out of do_auth and the final mark are real but tiny, and
        making the residual visible is what keeps a future regression (a phase
        whose boundary was forgotten) from hiding inside total_ms.

        Returns None until the record is closed, so an open record never looks
        like a perfectly accounted one.
        """
        total = self.total_ms
        if total is None:
            return None
        measured = sum(value for value in
                       (self.discovery_ms, self.login_post_ms, self.verify_ms)
                       if value is not None)
        return total - measured

    @property
    def discovery_ms(self):
        """Portal discovery, index.jsp, the cookie exchange and parameter
        completion — everything up to (not including) the credential POST.

        Kept separate from login_post_ms because the existing AUTH log line only
        reports the POST, which made the pre-POST work invisible; that invisible
        part is what the 13s outages were actually spent on.
        """
        return _metric_ms(self.started_at, self.discovery_finished_at)

    @property
    def login_post_ms(self):
        """The credential POST and its response read."""
        return _metric_ms(self.login_started_at, self.login_finished_at)

    @property
    def verify_ms(self):
        """Post-auth connectivity confirmation (empty when no POST succeeded)."""
        if self.verify_finished_at is None:
            return None
        started = self.login_finished_at or self.started_at
        return _metric_ms(started, self.verify_finished_at)

    def clue_line(self):
        """One log line of A.5 clue fields, or "" when there is nothing to say.

        Deliberately reports raw observations ("attempt #2", "1.8s") instead of
        any interpretation, because the plan forbids drawing a cause from these.
        """
        if self.attempt_index <= 0:
            return ""
        parts = [f"attempt #{self.attempt_index}"]
        if self.portal_result or self.portal_message:
            described = self.portal_message or self.portal_result
            parts.append(f"portal={self.portal_result or '?'} msg={described}")
        for label, value in (("discover", self.discovery_ms),
                             ("post", self.login_post_ms),
                             ("verify", self.verify_ms),
                             ("total", self.total_ms),
                             ("unaccounted", self.unaccounted_ms)):
            rendered = _fmt_metric(value, label)
            if rendered:
                parts.append(rendered)
        if self.verify_attempts:
            parts.append(f"verify_attempts={self.verify_attempts}")
        if self.http_requests or self.socket_probes:
            parts.append(f"http_requests={self.http_requests} "
                         f"socket_probes={self.socket_probes}")
        if self.overlapping_auth:
            parts.append("overlapping_auth=yes")
        if self.peer_device_state:
            parts.append(f"peer_devices={self.peer_device_state}")
        if self.retry_planned_wait_ms is not None:
            parts.append(f"planned_wait={_fmt_metric(self.retry_planned_wait_ms) or '-'}")
        if self.retry_lateness_ms is not None:
            parts.append(f"lateness={_fmt_metric(self.retry_lateness_ms) or '-'}")
        return " | ".join(parts)



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
                # The detail text is printed by callers and shown in the GUI, and
                # this URL is network-sourced: a portal redirect carries its
                # query string (session parameters) with it. Parameter names are
                # kept, values are not.
                return False, f"redirected to {url_for_log(final_url)}"
        if expected_body:
            body = resp.read(102400).decode("utf-8", errors="ignore")
            if expected_body.lower() not in body.lower():
                return False, f"response missing '{expected_body}' (portal injected?)"
        return True, "OK"
    except urllib.error.HTTPError as e:
        return False, f"HTTP {e.code}"
    except urllib.error.URLError as e:
        return False, network_error_for_log(e)
    except OSError as e:
        return False, network_error_for_log(e)
    except Exception as e:
        return False, network_error_for_log(e)


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


def do_auth_portal_post(config, timings=None, cancelled=None):
    """POST-based campus portal auth.
    Step 0: try to trigger portal redirect by accessing check_url (full browser headers)
    Step 1: fallback — access portal index.jsp directly, then try API
    Step 2: POST credentials to InterFace.do?method=login

    Returns `(ok, result)`: the boolean is what every existing caller uses, and
    the AuthResult carries *why* it failed so the detection loop can pick a
    retry policy. Returning a tuple rather than a bare truthy object is
    deliberate — a two-element tuple is always truthy, so callers that still do
    `if do_auth_portal_post(...)` would be silently wrong; callers are updated
    explicitly instead.

    `timings` is an optional TimingRecord used purely for observation; passing
    None keeps the original behaviour and log format exactly as before.
    """
    portal_host = config.get("portal_url", "")
    username = config.get("username", "")
    password = config.get("password", "")
    check_url = config["check_url"]
    timeout = config["request_timeout"]

    def result_for(ok, kind, result_text="", message="", status=None):
        requests = timings.http_requests if timings is not None else 0
        return AuthResult(ok, kind, portal_result=result_text,
                          portal_message=message, http_status=status,
                          http_requests_issued=requests, timings=timings)

    def abort_if_needed():
        if cancelled is None or not cancelled():
            return None
        if timings is not None and timings.discovery_finished_at is None:
            timings.mark_discovery_done()
        return False, result_for(False, RESULT_CANCELLED, message="attempt cancelled")

    if not username or not password:
        log("username or password not configured", "ERROR")
        return False, result_for(False, RESULT_CREDENTIAL_ERROR)

    cj = http.cookiejar.CookieJar()
    opener = urllib.request.build_opener(
        urllib.request.HTTPCookieProcessor(cj),
    )

    def _count():
        if timings is not None:
            timings.count_request()

    index_url = None

    # Step 0: probe check_url — portal may respond with JS redirect containing index.jsp URL
    aborted = abort_if_needed()
    if aborted is not None:
        return aborted
    try:
        req = urllib.request.Request(check_url, headers=BROWSER_HEADERS)
        _count()
        resp = opener.open(req, timeout=timeout)
        body_text = resp.read(204800).decode("utf-8", errors="ignore")
        final_url = resp.geturl()

        # check for HTTP redirect
        if final_url != check_url and "index.jsp" in final_url:
            index_url = final_url
            log(f"Portal HTTP redirect: {url_for_log(index_url)}", "AUTH")
        else:
            # check for JavaScript redirect: top.self.location.href='...index.jsp?...'
            m = re.search(r"location\.href\s*=\s*['\"]([^'\"]*index\.jsp[^'\"]*)", body_text)
            if m:
                index_url = m.group(1)
                log(f"Portal JS redirect: {url_for_log(index_url)}", "AUTH")
    except Exception as e:
        log(f"Probe {url_for_log(check_url)}: {network_error_for_log(e)}", "AUTH")

    # Step 1: if no JS redirect found, try accessing portal directly
    if not index_url:
        index_url = f"{portal_host}/eportal/index.jsp"
        log(f"No JS redirect, accessing portal directly: {index_url}", "AUTH")

    index_url = urljoin(portal_host + "/eportal/", index_url)
    # GET index.jsp to obtain JSESSIONID cookie (needed for both paths)
    index_body = ""
    aborted = abort_if_needed()
    if aborted is not None:
        return aborted
    try:
        req = urllib.request.Request(index_url, headers={"User-Agent": BROWSER_UA})
        _count()
        resp = opener.open(req, timeout=timeout)
        index_body = resp.read(204800).decode("utf-8", errors="ignore")
        final_url = resp.geturl()
        if final_url != index_url:
            index_url = final_url
            log(f"Portal responded with: {url_for_log(index_url)}", "AUTH")
        # scan body for JS redirect (same as Step 0, but for index page)
        if not urlparse(index_url).query:
            m = re.search(r"location\.href\s*=\s*['\"]([^'\"]*index\.jsp[^'\"]*)", index_body)
            if m:
                index_url = m.group(1)
                log(f"Found JS redirect in index page: {url_for_log(index_url)}", "AUTH")
    except Exception as e:
        log(f"Failed to fetch index page: {network_error_for_log(e)}", "ERROR")
        if timings is not None:
            # Discovery aborted here, and it must still be closed: otherwise the
            # phase would stay None and its elapsed time would be silently
            # attributed to nothing (or, worse, counted again later).
            timings.mark_discovery_done()
        return False, result_for(False, RESULT_NETWORK_ERROR,
                                 message=network_error_for_log(e))

    # Step 1.5: if we still have no query params, try portal APIs to get them
    query_string = urlparse(index_url).query
    if not query_string:
        log("No query params, trying portal API to get device info...", "AUTH")
        for api_method in ("pageInfo", "getServices"):
            aborted = abort_if_needed()
            if aborted is not None:
                return aborted
            try:
                api_url = f"{portal_host}/eportal/InterFace.do?method={api_method}"
                req = urllib.request.Request(api_url, headers={
                    "User-Agent": BROWSER_UA,
                    "Referer": index_url,
                })
                _count()
                resp = opener.open(req, timeout=timeout)
                api_body = resp.read(204800).decode("utf-8", errors="ignore")
                log(f"API {api_method} response: {body_for_log(api_body)}", "AUTH")
                final_url = resp.geturl()
                if final_url != api_url and "index.jsp" in final_url:
                    index_url = final_url
                    query_string = urlparse(index_url).query
                    log(f"Got redirect with params: {url_for_log(index_url)}", "AUTH")
                    break
            except Exception as e:
                log(f"API {api_method} failed: {network_error_for_log(e)}", "AUTH")

    # Step 1.6: extract params from index page body (hidden inputs / JS vars)
    if not query_string and index_body:
        # try hidden inputs: <input name="wlanuserip" value="10.1.2.3"/>
        params = re.findall(r'<input[^>]*name=["\'](wlan\w+|\w+ip|nas\w*|mac)["\'][^>]*value=["\']([^"\']*)["\']',
                            index_body, re.IGNORECASE)
        if params:
            query_string = "&".join(f"{k}={v}" for k, v in params)
            log(f"Extracted params from page body: {query_param_names(query_string)}", "AUTH")
        else:
            # try JS vars: var wlanuserip = "10.1.2.3";
            js_params = re.findall(r'(?:var|let|const)\s+(wlan\w+|\w+ip|nas\w*|mac)\s*=\s*["\']([^"\']+)["\']',
                                   index_body, re.IGNORECASE)
            if js_params:
                query_string = "&".join(f"{k}={v}" for k, v in js_params)
                log(f"Extracted JS params from page body: {query_param_names(query_string)}", "AUTH")

    # Step 1.7: last resort — construct minimal queryString from local IP
    if not query_string:
        aborted = abort_if_needed()
        if aborted is not None:
            return aborted
        try:
            parsed = urlparse(portal_host)
            host = parsed.hostname or portal_host
            s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            s.settimeout(2)
            s.connect((host, 80))
            local_ip = s.getsockname()[0]
            s.close()
            if timings is not None:
                # A raw socket probe, not an HTTP request: counted separately so
                # the HTTP request count stays an honest number.
                timings.socket_probes += 1
            query_string = f"wlanuserip={local_ip}"
            log(f"Fallback queryString: {query_param_names(query_string)}", "AUTH")
        except Exception as e:
            log(f"Could not construct queryString: {network_error_for_log(e)}", "AUTH")

    if timings is not None:
        timings.mark_discovery_done()

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
    aborted = abort_if_needed()
    if aborted is not None:
        return aborted
    try:
        req = urllib.request.Request(login_url, data=form_data, headers={
            "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
            "Referer": index_url,
            "Origin": portal_host,
            "User-Agent": BROWSER_UA,
        })
        if timings is not None:
            timings.mark_login_started()
        _count()
        resp = opener.open(req, timeout=timeout)
        body = resp.read().decode("utf-8", errors="ignore")
        elapsed = (time.time() - start) * 1000
        result_text, message_text = parse_portal_reply(body)
        if timings is not None:
            timings.mark_login_done()
            timings.portal_http_status = resp.status
            timings.portal_result = result_text
            timings.portal_message = message_text
        if not result_text or result_text != "success":
            log(f"Auth FAIL [HTTP {resp.status}, {elapsed:.0f}ms]{_timing_suffix(timings)} "
                f"body: {describe_portal_body(body)}", "ERROR")
            return False, _result_for_post_failure(
                timings, message_text, result_text, resp.status,
                timings.http_requests if timings is not None else 0)
        log(f"Auth OK [HTTP {resp.status}, {elapsed:.0f}ms]{_timing_suffix(timings)} "
            f"body: {describe_portal_body(body)}", "AUTH")
        return True, result_for(True, RESULT_UNKNOWN, result_text="success",
                                message=message_text, status=resp.status)
    except Exception as e:
        elapsed = (time.time() - start) * 1000
        if timings is not None:
            # No status/result exists for a transport-level failure, so the
            # fields stay empty rather than being filled with a guess.
            timings.mark_login_done()
        safe_error = network_error_for_log(e)
        log(f"Auth FAIL [{safe_error}, {elapsed:.0f}ms]{_timing_suffix(timings)}", "ERROR")
        # A transport-level failure is a network error: never eligible for the
        # conflict fast path, so it takes the regular backoff.
        return False, result_for(False, RESULT_NETWORK_ERROR, message=safe_error)


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
            info += f" (redirected to {url_for_log(final_url)})"
        return resp.status, body, info
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", errors="ignore") if e.fp else ""
        return e.code, body, f"HTTP error {e.code}"
    except Exception as e:
        safe_error = network_error_for_log(e)
        return None, "", safe_error


def do_auth_browser(url, wait_seconds, cancelled=None):
    if cancelled is not None and cancelled():
        return False
    log("Opening portal URL in browser", "AUTH")
    webbrowser.open(url)
    end = time.monotonic() + wait_seconds
    while True:
        if cancelled is not None and cancelled():
            return False
        remaining = end - time.monotonic()
        if remaining <= 0:
            break
        time.sleep(min(0.1, remaining))
    if cancelled is not None and cancelled():
        return False
    simulate_enter()
    return True


def do_auth(config, last_auth_time, timings=None, result_out=None,
            stop_event=None, deadline=None, recheck_after_lock=False,
            submit_before=None):
    """Attempt authentication and confirm real connectivity.

    Returns a plain bool — every existing caller (CLI, GUI, tray) depends on
    that, and a truthy non-bool would silently read as success. Callers that
    need to know *why* it failed pass a one-element list as `result_out` and
    read `result_out[0]` afterwards; an AuthResult is truthy only via its own
    `__bool__`, so it is never returned from here by accident.

    `timings` is an optional TimingRecord filled in for observation only; it
    never changes what happens. The record is closed on every exit path,
    including the early returns, so a failed attempt can never report
    total_ms = None (which would be indistinguishable from "not measured").
    """
    def publish(ok, result):
        if result_out is not None:
            result_out.append(result)
        return ok

    def cancelled():
        return ((stop_event is not None and stop_event.is_set())
                or (deadline is not None and datetime.now() >= deadline))

    def cancelled_return(reason="attempt cancelled"):
        if timings is not None:
            timings.mark_finished()
        return publish(False, AuthResult(False, RESULT_CANCELLED,
                                         portal_message=reason,
                                         timings=timings))

    if cancelled():
        return cancelled_return()

    now = datetime.now()
    cooldown = config.get("auth_cooldown_seconds", 3)
    if last_auth_time and (now - last_auth_time).total_seconds() < cooldown:
        remaining = cooldown - int((now - last_auth_time).total_seconds())
        log(f"Auth cooldown ({remaining}s remaining), skip", "INFO")
        if timings is not None:
            # Skipped before doing anything: phases stay None, and the record is
            # closed so total_ms is a real (tiny) number rather than missing.
            timings.mark_finished()
        # Not an auth failure at all: nothing was attempted, so it must not
        # consume any retry budget.
        return publish(False, AuthResult(False, RESULT_UNKNOWN,
                                         portal_message="cooldown skip",
                                         timings=timings))

    method = config.get("auth_method", "http")
    url = config.get("portal_url", "")
    timeout = config["request_timeout"]

    # Acquire interruptibly: a GUI click may be waiting behind the guard while
    # the user stops it, or a scheduled run may reach its end time.
    acquired = _AUTH_SUBMIT_LOCK.acquire(blocking=False)
    waited_for_lock = not acquired
    while not acquired:
        if cancelled():
            return cancelled_return()
        if submit_before is not None and time.monotonic() >= submit_before:
            return cancelled_return("fast reservation expired")
        acquired = _AUTH_SUBMIT_LOCK.acquire(timeout=0.1)

    global _AUTH_IN_FLIGHT
    try:
        if cancelled():
            return cancelled_return()
        if submit_before is not None and time.monotonic() >= submit_before:
            return cancelled_return("fast reservation expired")
        # The other attempt may have restored connectivity while we waited.
        if recheck_after_lock and waited_for_lock:
            healthy, _detail = check_network(
                config["check_url"], timeout, config.get("check_expected_body"))
            if cancelled() or healthy:
                return cancelled_return()
        if submit_before is not None and time.monotonic() >= submit_before:
            return cancelled_return("fast reservation expired")
        with _AUTH_IN_FLIGHT_LOCK:
            # Lock waiting is not part of portal discovery or its residual.
            if timings is not None:
                timings.reboot_timeline()
            _AUTH_IN_FLIGHT += 1
        try:
            ok, result = _do_auth_inner(config, method, url, timeout, timings,
                                        cancelled)
            return publish(ok, result)
        finally:
            with _AUTH_IN_FLIGHT_LOCK:
                _AUTH_IN_FLIGHT -= 1
    finally:
        _AUTH_SUBMIT_LOCK.release()
        if timings is not None and timings.finished_at is None:
            timings.mark_finished()


def _do_auth_inner(config, method, url, timeout, timings, cancelled=None):
    """The auth body, split out so do_auth can always close the timing record.

    Returns `(ok, result)`. `result` explains the outcome; for the http and
    browser modes the portal gives us nothing to classify, so the kind stays
    `unknown` and those modes take the regular backoff.
    """
    if cancelled is not None and cancelled():
        return False, AuthResult(False, RESULT_CANCELLED,
                                 portal_message="attempt cancelled", timings=timings)
    if method == "browser":
        if not do_auth_browser(url, config.get("browser_wait_seconds", 3), cancelled):
            return False, AuthResult(False, RESULT_CANCELLED,
                                     portal_message="attempt cancelled", timings=timings)
        log("Browser auth completed (Enter sent)", "AUTH")
        return _finish_by_verification(config, method, timings,
                                       AuthResult(True, RESULT_UNKNOWN), cancelled)
    elif method != "portal_post":
        return _do_auth_http_mode(config, url, timeout, timings, cancelled)

    ok, result = do_auth_portal_post(config, timings, cancelled)
    if not ok:
        return False, result

    # The portal accepted the credentials; connectivity still has to be proven.
    return _finish_by_verification(config, method, timings, result, cancelled)


def _do_auth_http_mode(config, url, timeout, timings, cancelled=None):
    if cancelled is not None and cancelled():
        return False, AuthResult(False, RESULT_CANCELLED,
                                 portal_message="attempt cancelled", timings=timings)
    start = time.time()
    if timings is not None:
        timings.mark_login_started()
    status, body, info = do_auth_http(url, timeout)
    elapsed = (time.time() - start) * 1000
    if timings is not None:
        timings.mark_login_done()
        timings.portal_http_status = status
    # Structural description, not a masked excerpt: http mode is just as
    # likely to echo a session token, and a blacklist pass over a raw body
    # cannot be trusted (a short unforeseen value slips through).
    snippet = describe_portal_body(body)
    requests = timings.http_requests if timings is not None else 0
    if status and 200 <= status < 300:
        body_lower = body.lower() if body else ""
        if "fail" in body_lower or "error" in body_lower:
            log(f"Auth FAIL [{info}, {elapsed:.0f}ms]{_timing_suffix(timings)} "
                f"body: {snippet}", "ERROR")
            # http mode gives no structured portal verdict, so it cannot claim a
            # conflict: it stays unknown and takes the regular backoff.
            return False, AuthResult(False, RESULT_UNKNOWN, portal_result="fail",
                                     http_status=status,
                                     http_requests_issued=requests, timings=timings)
        log(f"Auth OK [{info}, {elapsed:.0f}ms]{_timing_suffix(timings)} "
            f"body: {snippet}", "AUTH")
        log("NOTE: http 模式无法100%确认认证成功，建议改用 portal_post 模式", "WARN")
        return _finish_by_verification(
            config, "http", timings,
            AuthResult(True, RESULT_UNKNOWN, http_status=status,
                       http_requests_issued=requests, timings=timings), cancelled)
    log(f"Auth FAIL [{info}, {elapsed:.0f}ms]{_timing_suffix(timings)}", "ERROR")
    kind = RESULT_NETWORK_ERROR if status is None else RESULT_UNKNOWN
    return False, AuthResult(False, kind, portal_message=info,
                             http_status=status, http_requests_issued=requests,
                             timings=timings)


def _finish_by_verification(config, method, timings, result, cancelled=None):
    """Shared tail: the portal's answer is not proof, so confirm connectivity.

    A portal that reports success while the network stays down is
    `verification_failed` — distinct from a rejected login, and still not ok.
    """
    timeout = config["request_timeout"]

    # The portal response alone does not establish Internet connectivity.
    for attempt in range(3):
        if cancelled is not None and cancelled():
            return False, AuthResult(False, RESULT_CANCELLED,
                                     portal_message="attempt cancelled", timings=timings)
        if attempt:
            time.sleep(1)
            if cancelled is not None and cancelled():
                return False, AuthResult(False, RESULT_CANCELLED,
                                         portal_message="attempt cancelled", timings=timings)
        if timings is not None:
            timings.verify_attempts += 1
        ok, detail = check_network(config["check_url"], timeout,
                                   config.get("check_expected_body"))
        if ok:
            if timings is not None:
                timings.mark_verify_done()
                timings.mark_finished()
            return True, result
    if timings is not None:
        # The portal accepted the credentials but connectivity still failed.
        # Recorded so a "portal accepted / network not up" case is not confused
        # with a rejected login when the samples are reviewed later.
        timings.mark_verify_done()
        timings.mark_finished()
    log(f"Auth response received but network is unavailable: {detail}", "ERROR")
    # Distinct from a rejected login: the credentials were accepted, so retrying
    # the login itself is not the fix — this takes the regular backoff too.
    return False, AuthResult(False, RESULT_VERIFICATION_FAILED,
                             portal_result=result.portal_result,
                             portal_message=detail,
                             http_status=result.http_status,
                             http_requests_issued=result.http_requests_issued,
                             timings=timings)


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
    """First retry is quick; subsequent failures back off from a separate base.

    Kept exactly as it was: it is the regular backoff, and the samples showed it
    is what the outage time is actually spent on. RetryPolicy below decides
    *whether* to use it; it never changes what this function returns.
    """
    first = max(1, config.get("auth_cooldown_seconds", 3))
    base = max(first, config.get("auth_retry_backoff_seconds", 5))
    if failures <= 1:
        return first
    return min(max(300, base), base * 2 ** min(failures - 2, 10))


# ── Auth result classification ───────────────────────────────────────────────
#
# The detection loop needs to know *why* a login failed, because the right
# response differs: a portal that is up and rejecting the session is worth
# retrying in a second, while a timeout or a portal-side configuration error is
# not worth retrying faster at all. Classification is deliberately narrow — it
# only claims what a sample actually supports, and everything else stays
# `unknown` and takes the regular backoff.

RESULT_SESSION_CONFLICT = "session_conflict"
RESULT_CREDENTIAL_ERROR = "credential_error"
RESULT_NETWORK_ERROR = "network_error"
RESULT_PORTAL_CONFIG_ERROR = "portal_config_error"
RESULT_VERIFICATION_FAILED = "verification_failed"
RESULT_UNKNOWN = "unknown"
RESULT_CANCELLED = "cancelled"  # Internal stop/deadline, never a portal classification.

_AUTH_KINDS = (
    RESULT_SESSION_CONFLICT, RESULT_CREDENTIAL_ERROR, RESULT_NETWORK_ERROR,
    RESULT_PORTAL_CONFIG_ERROR, RESULT_VERIFICATION_FAILED, RESULT_UNKNOWN,
    RESULT_CANCELLED,
)

# A fast retry needs an observed, specific conflict signature. Generic session
# failure wording must stay unknown and use the regular backoff.
_SESSION_CONFLICT_MARKERS = (
    "mac collision",
)
_CREDENTIAL_ERROR_MARKERS = (
    "密码错误", "密码不正确", "用户不存在", "用户名不存在", "账号不存在",
    "password error", "invalid password", "wrong password",
    "认证失败,失败原因[密码",
)
_PORTAL_CONFIG_MARKERS = (
    "设备未注册", "未注册", "sam+", "portal/设备", "参数配置",
)


def classify_portal_failure(message):
    """Map a portal rejection message to an auth result kind.

    Order matters: the configuration wording is checked before the conflict
    wording, because the observed "device not registered" message also arrives
    as a generic auth failure. Anything unrecognised stays `unknown`.
    """
    text = (message or "").lower()
    if not text:
        return RESULT_UNKNOWN
    if any(marker in text for marker in _PORTAL_CONFIG_MARKERS):
        return RESULT_PORTAL_CONFIG_ERROR
    if any(marker in text for marker in _SESSION_CONFLICT_MARKERS):
        return RESULT_SESSION_CONFLICT
    if any(marker in text for marker in _CREDENTIAL_ERROR_MARKERS):
        return RESULT_CREDENTIAL_ERROR
    return RESULT_UNKNOWN


class AuthResult:
    """Why an auth attempt ended the way it did.

    `ok` keeps the meaning the whole program already relies on: the portal
    accepted the credentials *and* connectivity was confirmed. A portal that
    says "success" while the network stays down is NOT ok — that is precisely
    the `verification_failed` case.
    """

    __slots__ = ("ok", "kind", "portal_result", "portal_message", "http_status",
                 "http_requests_issued", "timings")

    def __init__(self, ok, kind, portal_result="", portal_message="",
                 http_status=None, http_requests_issued=0, timings=None):
        self.ok = ok
        self.kind = kind if kind in _AUTH_KINDS else RESULT_UNKNOWN
        self.portal_result = portal_result
        self.portal_message = portal_message
        self.http_status = http_status
        self.http_requests_issued = http_requests_issued
        self.timings = timings

    def describe(self):
        """One short line for the log; never includes a raw network value."""
        parts = [f"kind={self.kind}", f"ok={self.ok}"]
        if self.portal_result:
            parts.append(f"portal={self.portal_result}")
        if self.portal_message:
            parts.append(f"msg={redact_for_log(self.portal_message)}")
        return " | ".join(parts)

    def __bool__(self):
        return bool(self.ok)

    def __repr__(self):
        return f"AuthResult(ok={self.ok}, kind={self.kind})"


def _result_for_post_failure(timings, message, result_text, status, requests):
    """Build the AuthResult for a rejected login POST."""
    return AuthResult(False, classify_portal_failure(message),
                      portal_result=result_text, portal_message=message,
                      http_status=status, http_requests_issued=requests,
                      timings=timings)


# ── Retry policy ─────────────────────────────────────────────────────────────

RETRY_FAST = "fast"
RETRY_BACKOFF = "backoff"


class RetryDecision:
    """The delay to wait plus *why*, so the log can state the reason."""

    __slots__ = ("delay", "reason", "is_fast")

    def __init__(self, delay, reason, is_fast=False):
        self.delay = delay
        self.reason = reason
        self.is_fast = is_fast

    def __repr__(self):
        return f"RetryDecision(delay={self.delay}, reason={self.reason!r})"


# The candidate fast-retry parameters. They are NOT production defaults: the
# whole fast path is off unless `auth_conflict_fast_enabled` is explicitly true,
# because the samples only show that the third attempt succeeded, not that an
# earlier one would have. Enable it per deployment, watch the request count, and
# turn it off if conflicts grow.
DEFAULT_CONFLICT_FAST_RETRY_SECONDS = 1
DEFAULT_CONFLICT_FAST_MAX_RETRIES = 15
DEFAULT_CONFLICT_FAST_WINDOW_SECONDS = 25


class RetryPolicy:
    """Decides the wait after a failed auth attempt.

    Only an explicitly enabled, bounded conflict fast path can produce a short
    delay. Every other combination — fast path disabled, another failure kind, a
    budget spent, or the window closed — falls through to the regular backoff,
    which is guaranteed to span the whole backoff chain rather than jumping to
    its cap.
    """

    def __init__(self, config):
        self.config = config
        enabled = config.get("auth_conflict_fast_enabled", False)
        self.fast_enabled = enabled is True
        self.config_error = ("auth_conflict_fast_enabled"
                             if type(enabled) is not bool else None)

        def bounded_int(name, default, minimum, maximum):
            value = config.get(name, default)
            # bool is an int in Python, but not a valid duration or count.
            if type(value) is not int or not minimum <= value <= maximum:
                if self.config_error is None:
                    self.config_error = name
                return default
            return value

        self.fast_seconds = bounded_int("auth_conflict_retry_seconds",
                                        DEFAULT_CONFLICT_FAST_RETRY_SECONDS, 1, 60)
        self.max_fast = bounded_int("auth_conflict_max_fast_retries",
                                    DEFAULT_CONFLICT_FAST_MAX_RETRIES, 0, 50)
        self.window = bounded_int("auth_conflict_fast_window_seconds",
                                  DEFAULT_CONFLICT_FAST_WINDOW_SECONDS, 0, 300)
        if self.config_error:
            self.fast_enabled = False

    def fast_budget(self):
        """How many fast retries fit inside the window, and their total wait.

        None when the fast path is off (or configured to nothing usable). Kept
        as its own method so the log can state the budget explicitly before it
        is ever spent, and so tests can pin it without running the loop.
        """
        if not (self.fast_enabled and self.max_fast > 0 and self.window > 0):
            return None
        # A request exactly on the deadline is forbidden. This is only an
        # ideal upper bound; request durations and probe cadence reduce it.
        count = min(self.max_fast, (self.window - 1) // self.fast_seconds)
        if count <= 0:
            return None
        return {"count": count, "each": self.fast_seconds,
                "total": count * self.fast_seconds, "window": self.window}

    def describe_budget(self, backoff_step=1):
        """A one-line, human-readable account of what will happen after a
        failure — logged so a misconfigured deployment is visible up front."""
        chain = ", ".join(f"{auth_retry_delay(self.config, backoff_step + offset)}s"
                          for offset in range(5))
        budget = self.fast_budget()
        if budget is None:
            why = (f"disabled: invalid {self.config_error}" if self.config_error
                   else "disabled" if not self.fast_enabled
                   else "configured to nothing usable")
            return f"conflict fast path {why}; regular backoff {chain}..."
        return (f"conflict fast path: up to {budget['count']} retries of "
                f"{budget['each']}s within {budget['window']}s "
                f"(max {budget['total']}s of waiting); then regular backoff {chain}...")

    def _backoff_chain(self, steps=6):
        return ", ".join(f"{auth_retry_delay(self.config, n)}s"
                         for n in range(1, steps + 1))

    def decide(self, result, backoff_step, fast_used, seconds_since_first_conflict,
               fast_budget_closed=False):
        """Return the RetryDecision for a failed attempt.

        `result.ok` False is assumed. `backoff_step` is the regular backoff
        level (1 = first regular retry), never the raw failure count — keeping
        them separate is what stops a burst of fast retries from shoving the
        regular backoff straight to its cap.
        """
        kind = getattr(result, "kind", RESULT_UNKNOWN)
        if result.ok:
            raise ValueError("decide() is for failed attempts only")

        if kind == RESULT_SESSION_CONFLICT and self.fast_enabled and not fast_budget_closed:
            if (fast_used < self.max_fast
                    and seconds_since_first_conflict is not None
                    and seconds_since_first_conflict + self.fast_seconds < self.window):
                return RetryDecision(
                    self.fast_seconds,
                    f"conflict fast retry {fast_used + 1}/{self.max_fast} "
                    f"within {self.window}s window",
                    is_fast=True)

        # Regular backoff, and the reason names the class so the log explains
        # why a conflict did not get the fast path when it did not.
        reason = self._backoff_reason(kind, fast_used, fast_budget_closed)
        return RetryDecision(auth_retry_delay(self.config, backoff_step), reason)

    def _backoff_reason(self, kind, fast_used, fast_budget_closed):
        if kind != RESULT_SESSION_CONFLICT:
            return f"regular backoff: kind={kind} is not eligible for fast retry"
        if not self.fast_enabled:
            return "regular backoff: conflict fast path is disabled"
        if fast_budget_closed:
            return "regular backoff: fast budget already closed for this outage"
        if fast_used >= self.max_fast:
            return f"regular backoff: fast budget spent ({fast_used}/{self.max_fast})"
        return "regular backoff: fast window closed"



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

    log(runtime_identity_line(), "START")
    log("Service started" + (" [tray]" if is_tray else " [interactive]" if INTERACTIVE else " [background]"), "START")
    log(f"Config: auth={config.get('auth_method','http')} check={check_url} interval={interval_ok}s threshold={fail_threshold} duration={run_duration}min", "INFO")

    start_time = datetime.now()
    end_time = start_time + timedelta(minutes=run_duration) if run_duration > 0 else None
    if end_time:
        log(f"Scheduled mode: will exit at {end_time.strftime('%H:%M:%S')}", "INFO")
    else:
        log("Continuous mode", "INFO")

    fail_count = 0
    confirm_count = 0
    total_checks = 0
    outage_checks = 0
    outage_start = None
    # Wall-clock stamps are kept only for the log text the user reads; every
    # interval below is measured on the monotonic clock so a clock correction
    # (or a suspend/resume) cannot distort the numbers we will tune on.
    first_failure_at = None
    first_failure_monotonic = None
    down_confirmed_monotonic = None
    auth_attempts = 0
    next_auth_at = 0.0
    auth_failures = 0
    was_down = False
    # ── retry state machine (plan §4.2) ──
    # `backoff_step` is the regular backoff level, kept separate from
    # `auth_failures` so a burst of fast retries cannot shove the backoff chain
    # to its 300s cap. `first_conflict_at` opens the conflict fast-retry window;
    # all three reset only when connectivity is actually confirmed.
    retry_policy = RetryPolicy(config)
    backoff_step = 1
    fast_used = 0
    first_conflict_at = None
    fast_budget_closed = False
    next_auth_is_fast = False
    last_auth_finished_at = None
    # ── observation state (never feeds a decision) ──
    last_probe_ms = None
    planned_wait_ms = None

    while not (stop_event and stop_event.is_set()):
        try:
            if end_time and datetime.now() > end_time:
                log("Run duration reached, exiting", "STOP")
                break

            total_checks += 1
            probe_started = _now()
            ok, detail = check_network(check_url, timeout, config.get("check_expected_body"))
            last_probe_ms = _metric_ms(probe_started, _now())

            if ok:
                if was_down and outage_start:
                    # Outage length from DOWN confirmation (what the log has
                    # always reported, kept for continuity).
                    duration = (datetime.now() - outage_start).total_seconds()
                    log(
                        f"Network restored — outage: {format_duration(duration)}, "
                        f"checks: {outage_checks}, auth_attempts: {auth_attempts}",
                        "RECOVER",
                    )
                    # The window above starts when DOWN was confirmed, so it
                    # under-reports what a user feels. The honest span starts at
                    # the first failed probe; both are logged because they answer
                    # different questions. Monotonic, so a clock correction
                    # cannot change the number.
                    if first_failure_monotonic is not None:
                        observed = _now() - first_failure_monotonic
                        confirmed = (down_confirmed_monotonic - first_failure_monotonic
                                     if down_confirmed_monotonic is not None else 0.0)
                        log(
                            f"Recovery observed: {format_duration(observed)} from first "
                            f"failed probe (DOWN confirmation added "
                            f"{format_duration(max(0.0, confirmed))}, "
                            f"auth_attempts {auth_attempts})",
                            "RECOVER",
                        )
                    was_down = False
                    outage_start = None
                    outage_checks = 0
                    auth_attempts = 0
                # Cleared on EVERY success, not only at the end of a confirmed
                # outage: a single failed probe followed by a healthy one must
                # not leave a stale stamp behind, or a much later outage would
                # inherit it and report a hugely inflated recovery time.
                fail_count = 0
                confirm_count = 0
                auth_failures = 0
                backoff_step = 1
                fast_used = 0
                first_conflict_at = None
                fast_budget_closed = False
                next_auth_is_fast = False
                last_auth_finished_at = None
                next_auth_at = 0.0
                first_failure_at = None
                first_failure_monotonic = None
                down_confirmed_monotonic = None
                outage_start = None
                planned_wait_ms = None
            else:
                fail_count += 1
                confirm_count += 1
                if not outage_start:
                    outage_start = datetime.now()
                if first_failure_monotonic is None:
                    first_failure_at = datetime.now()
                    first_failure_monotonic = _now()
                outage_checks += 1

                # Confirmation is only needed to *enter* the down state. Once
                # confirmed, a retry is scheduled by time alone: making each
                # retry re-accumulate the threshold added a full probe interval
                # of dead time per attempt (measured at 1.1-1.4s on 09-21).
                if was_down or confirm_count >= fail_threshold:
                    if confirm_count >= fail_threshold and not was_down:
                        log(f"Network DOWN (reason: {detail}), starting auth", "DOWN")
                        log_failure_context(check_url, detail)
                        was_down = True
                        outage_start = datetime.now()
                        down_confirmed_monotonic = _now()
                        if first_failure_monotonic is not None:
                            confirm_seconds = _now() - first_failure_monotonic
                            log(
                                f"Detection timing: first failure to DOWN confirmation "
                                f"{format_duration(confirm_seconds)} (threshold "
                                f"{fail_threshold}, last probe {_fmt_metric(last_probe_ms) or '-'})",
                                "DOWN",
                            )
                        outage_checks = 0
                        auth_attempts = 0

                    if time.monotonic() >= next_auth_at:
                        if stop_event and stop_event.is_set():
                            break
                        if end_time and datetime.now() >= end_time:
                            log("Run duration reached before auth, exiting", "STOP")
                            break

                        # A fast reservation is not permission to submit after
                        # its deadline. Convert it to the regular chain without
                        # consuming a fast attempt if a slow probe/lock made it
                        # late. A regular attempt may run now if its own delay
                        # has already elapsed since the last failure.
                        if next_auth_is_fast:
                            fast_deadline = (first_conflict_at + retry_policy.window
                                             if first_conflict_at is not None else 0.0)
                            if (fast_budget_closed or fast_used >= retry_policy.max_fast
                                    or time.monotonic() >= fast_deadline):
                                fast_budget_closed = True
                                next_auth_is_fast = False
                                delay = auth_retry_delay(config, backoff_step)
                                next_auth_at = (last_auth_finished_at
                                                if last_auth_finished_at is not None
                                                else time.monotonic()) + delay
                                planned_wait_ms = delay * 1000
                                backoff_step += 1
                                log(f"Fast reservation expired; regular backoff {delay}s", "AUTH")

                        if time.monotonic() >= next_auth_at:
                            dispatching_fast = next_auth_is_fast
                            auth_attempts += 1
                            timings = TimingRecord()
                            timings.attempt_index = auth_attempts
                            timings.mark_due(next_auth_at, planned_wait_ms)
                            result_out = []
                            authenticated = do_auth(
                                config, None, timings, result_out,
                                stop_event=stop_event, deadline=end_time,
                                recheck_after_lock=True,
                                submit_before=(first_conflict_at + retry_policy.window
                                               if dispatching_fast else None))
                            auth_result = result_out[0] if result_out else None
                            if (auth_result is not None
                                    and auth_result.kind == RESULT_CANCELLED
                                    and auth_result.portal_message == "fast reservation expired"):
                                auth_attempts -= 1
                                fast_budget_closed = True
                                next_auth_is_fast = False
                                delay = auth_retry_delay(config, backoff_step)
                                next_auth_at = (last_auth_finished_at
                                                if last_auth_finished_at is not None
                                                else time.monotonic()) + delay
                                planned_wait_ms = delay * 1000
                                backoff_step += 1
                                log(f"Fast reservation expired while waiting to submit; "
                                    f"regular backoff {delay}s", "AUTH")
                                continue
                            if (auth_result is not None
                                    and auth_result.kind == RESULT_CANCELLED):
                                continue
                            if dispatching_fast:
                                fast_used += 1  # Count actual dispatch, not reservation.
                                next_auth_is_fast = False
                            last_auth_finished_at = time.monotonic()
                            clue = timings.clue_line()
                            if clue:
                                log(f"Attempt {auth_attempts} {clue}", "AUTH")

                            # Only a regular decision advances backoff_step.
                            if authenticated:
                                auth_failures = 0
                                backoff_step = 1
                                fast_used = 0
                                first_conflict_at = None
                                fast_budget_closed = False
                                delay = auth_retry_delay(config, 1)
                                reason = "success"
                            else:
                                auth_failures += 1
                                kind = (auth_result.kind if auth_result is not None
                                        else RESULT_UNKNOWN)
                                if kind == RESULT_SESSION_CONFLICT:
                                    if first_conflict_at is None:
                                        first_conflict_at = time.monotonic()
                                    elapsed = time.monotonic() - first_conflict_at
                                else:
                                    elapsed = None
                                decision = retry_policy.decide(
                                    auth_result if auth_result is not None
                                    else AuthResult(False, RESULT_UNKNOWN),
                                    backoff_step, fast_used, elapsed,
                                    fast_budget_closed)
                                delay = decision.delay
                                reason = decision.reason
                                next_auth_is_fast = decision.is_fast
                                if not decision.is_fast:
                                    fast_budget_closed = True
                                    backoff_step += 1
                            log(f"Retry in {delay}s ({reason})", "AUTH")
                            planned_wait_ms = delay * 1000
                            next_auth_at = time.monotonic() + delay
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
            log(f"Unexpected error: {network_error_for_log(e)}", "ERROR")
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
