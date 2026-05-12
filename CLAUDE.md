# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Overview

Campus network auto-login tool. Detects captive portal via HTTP content inspection and re-authenticates in the background. Runs as a Windows scheduled task (daily at 17:55, exits after 60 min).

## Commands

```bash
# Test auth once and exit (skip network detection loop)
python auto_login.py --auth

# Manual run (interactive mode — STATUS lines printed to console)
python auto_login.py

# Deploy as scheduled task (PowerShell, as Administrator)
.\setup_task.ps1

# Remove scheduled task
Unregister-ScheduledTask -TaskName CampusNetAutoLogin -Confirm:$false
```

No build, lint, or test steps. Python 3 standard library only, no pip dependencies.

## Architecture

Single-file script (`auto_login.py`) with JSON config (`auto_login_config.json`).

**Two run modes, auto-detected:**
- `INTERACTIVE = sys.stdout.isatty()` — when run from a terminal (`python auto_login.py`), prints a STATUS line every check cycle
- Background mode — when run via scheduled task with `pythonw.exe`, only logs events (START/DOWN/AUTH/RECOVER/STOP), no per-cycle output

**Three auth methods, configured via `auth_method` in config:**

1. **`portal_post`** (for portals requiring username/password POST):
   - GET check_url → portal returns JS redirect (`location.href='...index.jsp?...'`)
   - Regex-extract index.jsp URL (carries connection params: wlanuserip, nasip, mac, etc.)
   - GET index.jsp → obtain JSESSIONID cookie
   - POST to `InterFace.do?method=login` with username, password, queryString, and cookie
   - Requires config: `portal_host`, `username`, `password`

2. **`http`** (simple GET-based auth):
   - Background GET to `portal_url` with browser User-Agent header
   - No browser opened. Requires config: `portal_url`

3. **`browser`** (interactive fallback):
   - Opens `portal_url` in default browser via `webbrowser.open()`
   - Simulates Enter key via `ctypes.windll.user32.keybd_event`
   - Requires `LogonType Interactive` in scheduled task

**Captive portal detection (`check_network`):**
1. GET request to `check_url` with full browser headers
2. Check if URL changed (HTTP redirect to different host/path)
3. Check response body contains `check_expected_body` keyword (default: "baidu")
4. If body is missing the keyword, portal is transparently proxying — return False

**Detection loop:**
1. `check_network()` on `check_url` at configured interval
2. On failure, retry at `check_interval_fail` interval; after `fail_threshold` consecutive failures, trigger auth
3. On success, wait `check_interval_ok` seconds before next check
4. Auth cooldown (`auth_cooldown_seconds`, default 30s) prevents repeated auth attempts

**Logging:** Dual output — always prints to stdout/stderr; also appends to `logs/auto_login_YYYY-MM-DD.log`. Logs older than 7 days are auto-cleaned at startup.

**Scheduled task setup (`setup_task.ps1`):**
- Prefers `pythonw.exe` (no console window), falls back to `python.exe`
- Searches PATH first, then common Python install locations
- Creates task with `LogonType Interactive` (required for browser mode + `keybd_event`), `RunLevel Limited`
- 2-hour execution time limit, ignores new instances if already running

## Key dependencies in stdlib

- `urllib.request` + `urllib.error` — all HTTP requests
- `http.cookiejar` — cookie persistence for portal_post multi-step auth
- `re` — extract JS redirect URL from portal response
- `ctypes.windll.user32.keybd_event` — simulate Enter key (browser mode only)
