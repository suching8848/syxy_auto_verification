# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Overview

Campus network auto-login tool (v1.5). Detects captive portal via HTTP content inspection and re-authenticates in the background. Runs as a Windows scheduled task or as a system tray app with notification area icon.

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

# Print version
python auto_login.py --version

# Build standalone exe (no Python required to run)
pyinstaller --onefile --console --name auto_login auto_login.py

# Deploy as scheduled task (PowerShell, as Administrator)
.\setup_task.ps1

# Remove scheduled task
Unregister-ScheduledTask -TaskName CampusNetAutoLogin -Confirm:$false
```

No build, lint, or test steps. Python 3 standard library only, no pip dependencies (except `pyinstaller` for exe packaging).

## Architecture

Single-file script (`auto_login.py`) with JSON config (`auto_login_config.json`).

**Four run modes:**
- `INTERACTIVE` (try/except on `sys.stdout.isatty()`, defaults False if stdout is None) — when run from a terminal, shows interactive menu (options 1-6, q), then runs detection loop inside a `TrayApp` (with visible console). Menu options: [1] Launch with tray, [2] Test auth, [3] Edit config, [4] Scheduled task guide, [5] Background tray (hidden), [6] FAQ, [q] Quit.
- `--background` — forces background mode regardless of `isatty()`, calls `run_detection_loop` directly. Used by `setup_task.ps1`.
- `--tray` — starts `TrayApp` with `start_hidden=True` directly, no menu. Notification area icon only.
- Auto-detected background — when `not INTERACTIVE` (pythonw.exe has no stdout), skips menu and tray, calls `run_detection_loop` directly. Logs key events (START/DOWN/AUTH/RECOVER/STOP) + STATUS every 2 checks (≈10s).

**`--auth` flag** bypasses all paths — does a single auth attempt and exits. If already online, tries portal logout APIs first to trigger captive portal redirect and get real auth parameters. Triggers config wizard if config is incomplete.

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
   - If already online (no redirect), tries portal logout APIs (logout/offline/disconnect), then re-probes
   - GET index.jsp → obtain JSESSIONID cookie
   - Fallback param extraction: hidden `<input>` fields, JS `var` declarations, local IP via socket
   - POST to `InterFace.do?method=login` with username, password, queryString, and cookie
   - Checks response for `"result":"fail"` to detect auth failure
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
4. Auth cooldown (`auth_cooldown_seconds`, default 30s) prevents repeated auth attempts
5. Runs until `run_duration_minutes` (default 60, `0` = infinite) elapses, `stop_event` is set, or Ctrl+C

### Logging

Dual output — always prints to stdout/stderr; also appends to `logs/auto_login_YYYY-MM-DD.log`. `clean_old_logs()` runs at startup, removing log files older than 7 days (based on mtime). `log()` function catches print exceptions for `pythonw.exe` (no stdout).

### Config

`_need_setup(config)` checks whether core fields are missing (username+password for portal_post, portal_url for others). Triggers `interactive_setup()` wizard on first run if config is incomplete. `DEFAULT_CONFIG` also includes `schedule_time` (default `"17:55"`) for the scheduled task and `browser_wait_seconds` (default `3`) for browser auth mode.

### `setup_task.ps1`

- Prefers `auto_login.exe` if present → launches via `powershell.exe Start-Process -WindowStyle Hidden` (fully hidden)
- Falls back to `pythonw.exe` (no console window), then `python.exe`; searches PATH first, then common Python install locations
- Creates task with `LogonType Interactive` (required for browser mode + `keybd_event`), `RunLevel Limited`
- `Hidden=$true` on task settings, 2-hour execution time limit, ignores new instances if already running

## Key dependencies in stdlib

- `urllib.request` + `urllib.error` — all HTTP requests
- `http.cookiejar` — cookie persistence for portal_post multi-step auth
- `re` — extract JS redirect URL and form params from portal response
- `ctypes` + `ctypes.windll.*` — Win32 API: Shell_NotifyIcon, CreateWindowExW, RegisterClassExW, keybd_event, GetConsoleWindow, ShowWindow, EnumWindows, SetCurrentProcessExplicitAppUserModelID
- `threading` — `Thread` + `Event` for tray worker thread and clean shutdown
- `socket` — get local IP as fallback for queryString construction
- `argparse` — CLI flags (`--auth`, `--tray`, `--version`)
- `sys.frozen` (PyInstaller) — distinguishes exe vs script for path resolution and exit behavior
