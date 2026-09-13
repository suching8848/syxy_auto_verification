# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Overview

Campus network auto-login tool (v1.6.1). Detects captive portal via HTTP content inspection and re-authenticates in the background. Runs as a Windows scheduled task or as a system tray app with notification area icon.

## Commands

```bash
# Test auth once and exit (skip network detection loop)
python auto_login.py --auth

# Manual run (interactive mode — shows menu with 6 options, then detection loop)
python auto_login.py

# System tray mode (hidden window + notification area icon, no terminal)
python auto_login.py --tray

# Background mode (no console, detection loop only — used by scheduled task)
pythonw.exe auto_login.py --background

# Boot auto-start mode (continuous monitoring, run_duration forced to 0, Session 0)
python auto_login.py --boot

# Print version
python auto_login.py --version

# Build standalone exe (no Python required to run)
pyinstaller --onefile --console --name auto_login --specpath build auto_login.py

# Deploy as daily scheduled task (PowerShell, as Administrator)
.\setup_task.ps1

# Deploy as boot auto-start task (PowerShell, as Administrator)
.\setup_task.ps1 -Boot

# Remove daily scheduled task
Unregister-ScheduledTask -TaskName CampusNetAutoLogin -Confirm:$false

# Remove boot auto-start task
Unregister-ScheduledTask -TaskName CampusNetAutoLogin_Boot -Confirm:$false
```

Offline regression tests: `python -m unittest -v test_auto_login` (Windows). Python 3 standard library only, no pip dependencies (except `pyinstaller` for exe packaging).

## Architecture

Single-file script (`auto_login.py`) with JSON config (`auto_login_config.json`).

**Five run modes:**
- `INTERACTIVE` (try/except on `sys.stdout.isatty()`, defaults False if stdout is None) — when run from a terminal, shows interactive menu (options 1-6, q), then runs detection loop inside a `TrayApp` (with visible console). Menu options: [1] 启动自动认证 (start detection loop with tray), [2] 测试认证 (test auth), [3] 修改基本配置 (edit config), [4] 定时部署 (scheduled task deployment guide), [5] 后台常驻 (background resident — hidden tray), [6] 使用帮助 (FAQ), [q] Quit.
- `--background` — forces background mode regardless of `isatty()`, calls `run_detection_loop` directly. Used by `setup_task.ps1`.
- `--tray` — starts `TrayApp` with `start_hidden=True` directly, no menu. Notification area icon only.
- `--boot` — overrides `run_duration_minutes` to 0, logs Session 0 warnings, calls `run_detection_loop` directly. Used by `setup_task.ps1 -Boot`.
- Auto-detected background — when `not INTERACTIVE` (pythonw.exe has no stdout), skips menu and tray, calls `run_detection_loop` directly. Logs key events (START/DOWN/AUTH/RECOVER/STOP) + STATUS every 2 checks (≈10s).

**`--auth` flag** bypasses all paths — does a single auth attempt and exits. Never logs out automatically; returns exit code 0 on verified connectivity and 1 on failure. Triggers config wizard if config is incomplete.

**exe vs script mode** detected via `sys.frozen` (PyInstaller sets this). Affects `SCRIPT_DIR` resolution and the exe shows a pre-exit "Press Enter" prompt so the console doesn't vanish.

### System tray architecture (`TrayApp` class, ~220 lines)

Uses ctypes to call Win32 APIs directly — zero external dependencies:
- `RegisterClassExW` + `CreateWindowExW` — invisible message-only window
- `Shell_NotifyIconW` (NIM_ADD/NIM_MODIFY/NIM_DELETE) — notification area icon
- `CreatePopupMenu` + `TrackPopupMenu` — right-click context menu (status, show/hide console, exit)
- `GetConsoleWindow` + `ShowWindow` — hide/restore terminal window
- Spawns a daemon `threading.Thread` running `run_detection_loop` with `stop_event` and `status_callback`
- `status_callback` posts `WM_USER_TRAY_UPDATE` to the main thread for tooltip updates
- `stop_event` enables clean shutdown: right-click Exit → `stop_event.set()` → `DestroyWindow` → `PostQuitMessage`

x86-64 safety: explicit 64-bit argtypes set on `DefWindowProcW`, `GetMessageW`, `DispatchMessageW`, `PostMessageW` (default `c_int` is 32-bit and would truncate pointers).

### `run_detection_loop` threading support

Signature: `run_detection_loop(config, stop_event=None, status_callback=None)`

- `stop_event` (`threading.Event`): when set, loop exits gracefully. Checked every 0.5s during sleep.
- `status_callback` (`callable(str)`): called with status line for tray tooltip updates.
- Sleep is interruptible via 0.5s sub-steps to allow responsive shutdown.

### Auth methods

1. **`portal_post`** (default, for portals requiring username/password POST):
   - GET check_url with full browser headers → portal returns JS redirect (`location.href='...index.jsp?...'`)
   - Regex-extract index.jsp URL (carries connection params: wlanuserip, nasip, mac, etc.)
   - No redirect: accesses the portal directly; never calls logout APIs
   - GET index.jsp → obtain JSESSIONID cookie
   - Fallback param extraction: hidden `<input>` fields, JS `var` declarations, local IP via socket
   - POST to `InterFace.do?method=login` with username, password, queryString, and cookie
   - Parses JSON and requires `result == "success"`; then verifies Internet connectivity
   - Requires config: `portal_url`, `username`, `password`

2. **`http`** (simple GET-based auth):
   - Background GET to `portal_url` with browser User-Agent header
   - Checks response body for fail/error keywords
   - No browser opened. Requires config: `portal_url`

3. **`browser`** (interactive fallback):
   - Opens `portal_url` in default browser via `webbrowser.open()`
   - Simulates Enter key via `ctypes.windll.user32.keybd_event`
   - Requires `LogonType Interactive` in scheduled task

### Captive portal detection (`check_network`)

1. GET request to `check_url` with full browser headers
2. Check if URL changed (HTTP redirect to different host/path)
3. Check response body contains `check_expected_body` keyword (default: "baidu")
4. If body is missing the keyword, portal is transparently proxying — return False

### Detection loop

1. `check_network()` on `check_url` at configured interval
2. On failure, retry at `check_interval_fail` interval; after `fail_threshold` consecutive failures, trigger auth
3. On success, wait `check_interval_ok` seconds before next check
4. Monotonic retry deadlines cover failed and successful attempts; consecutive failures exponentially back off from the configured cooldown (default 30s) up to max(300s, cooldown)
5. Runs until `run_duration_minutes` (default 60, `0` = infinite) elapses, `stop_event` is set, or Ctrl+C

### Logging

Dual output — always prints to stdout/stderr; also appends to `logs/auto_login_YYYY-MM-DD.log`. `clean_old_logs()` runs at startup, removing log files older than 7 days (based on mtime). `log()` function catches print exceptions for `pythonw.exe` (no stdout).

### Config

`_need_setup(config)` checks whether core fields are missing (username+password for portal_post, portal_url for others). Triggers `interactive_setup()` wizard on first run if config is incomplete. `DEFAULT_CONFIG` also includes `schedule_time` (default `"20:30"`) for the scheduled task and `browser_wait_seconds` (default `3`) for browser auth mode.

### `setup_task.ps1`

- **Daily mode** (no flags): creates `CampusNetAutoLogin` task using config `schedule_time` (default 20:30), overridable with `-ScheduleTime HH:mm`
- **Boot mode** (`-Boot`): creates `CampusNetAutoLogin_Boot` task with `-AtStartup` trigger
- Prefers `auto_login.exe` if present → launches via `powershell.exe Start-Process -WindowStyle Hidden` (fully hidden); boot mode appends `--boot` argument
- Falls back to `pythonw.exe` (no console window), then `python.exe`; searches PATH first, then common Python install locations
- Daily task: `LogonType Interactive` (required for browser mode + `keybd_event`), `RunLevel Limited`, 2-hour execution time limit, `RestartCount 0`
- Boot task: `LogonType ServiceAccount` with `UserId SYSTEM` (Session 0, no user logon required), `ExecutionTimeLimit 0` (unlimited), `RestartCount 3` with 1-minute interval
- `Hidden=$true` on task settings, ignores new instances if already running
- Uses `param([switch]$Boot)` to toggle between daily and boot mode

## Key dependencies in stdlib

- `urllib.request` + `urllib.error` — all HTTP requests
- `http.cookiejar` — cookie persistence for portal_post multi-step auth
- `re` — extract JS redirect URL and form params from portal response
- `ctypes` + `ctypes.windll.*` — Win32 API: Shell_NotifyIcon, CreateWindowExW, RegisterClassExW, keybd_event, GetConsoleWindow, ShowWindow, EnumWindows, SetCurrentProcessExplicitAppUserModelID
- `threading` — `Thread` + `Event` for tray worker thread and clean shutdown
- `socket` — get local IP as fallback for queryString construction
- `argparse` — CLI flags (`--auth`, `--tray`, `--background`, `--boot`, `--version`)
- `sys.frozen` (PyInstaller) — distinguishes exe vs script for path resolution and exit behavior

## Development gotchas

**Critical: `print()` ordering in `main()`.**
The background mode check (`if not INTERACTIVE or args.background:`) MUST execute before any `print()` call. Under `pythonw.exe` (Task Scheduler), `sys.stdout` is `None` and `print()` throws. If you add logging or output before this check you will break background mode. The `INTERACTIVE` flag is set at module level with a try/except: `try: INTERACTIVE = sys.stdout.isatty()` / `except: INTERACTIVE = False`.

**`TrayApp` has a built-in fallback.** If `_create_window()` or `_create_tray_icon()` raises (e.g., no desktop session), `TrayApp.run()` catches the exception and falls back to `run_detection_loop(config)` — this is a console-less fallback, not the tray app.

**`_need_setup` checks different fields per `auth_method`.**
`portal_post` requires `username` + `password`; `http` and `browser` require `portal_url`. This affects when the config wizard triggers.

**`__main__` finally block.** Interactive mode (non-tray) has a `finally:` block that prompts `input("\nPress Enter to exit...")`. This prevents the console from instantly vanishing when running as `auto_login.exe` (PyInstaller). Tray mode skips this via the `_tray_mode` flag.

**JSON config contains plaintext passwords.** `auto_login_config.json` is in `.gitignore` and must never be committed. The example config (`auto_login_config.example.json`) uses `_`-prefixed keys as pseudo-comments since JSON has no comment syntax.

**`setup_task.ps1` exe/script detection order.** The script checks `auto_login.exe` first → `pythonw.exe` (no window) → `python.exe` (fallback). If `auto_login.exe` exists, it's launched via `powershell.exe Start-Process -WindowStyle Hidden` for true zero-window execution. Python fallbacks pass `--background` (or `--boot`) to suppress console output.

## Reliability updates

- Hidden resident tray mode overrides duration to 0 on a config copy. Worker completion posts a UI message to close the tray.
- EXE scheduled launcher passes --background/--boot, waits for completion, and propagates exit status; Python script paths are quoted.
- Explicit CLI modes do not wait for Enter on exit.
- Run offline tests before packaging; existing dist artifacts are not updated by source edits.
