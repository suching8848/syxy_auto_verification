# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Overview

Campus network auto-login tool (v1.7.0). Detects captive portal via HTTP content inspection and re-authenticates in the background. Runs as a Windows scheduled task or as a system tray app with notification area icon.

Two user-facing scenarios drive the design, and both are the **same behaviour** (probe continuously, re-auth the moment the portal drops traffic) — they differ only in how they start and what shell they wear:

- **Scenario A** — `CampusNet.exe` (GUI). User clicks 「开始守护」, gets a window + tray icon, and it guards until they stop it. Closing the window hides to tray.
- **Scenario B** — `CampusNet.exe --silent` (no UI at all). A scheduled task starts it at `silent_start_time`; it probes with no window, no tray icon, no popup, then exits after `silent_run_minutes`. Only `CampusNet.exe` can do this: it is built `--windowed` (GUI subsystem, no console), whereas `--console` builds always flash a black window that code can only hide *after* it appears.

## Commands

```bash
# GUI / scenario A (window + tray)
python gui_app.py

# Scenario B: silent guard — no window, no tray icon
python gui_app.py --silent                        # wait for silent_start_time
python gui_app.py --silent --now --run-minutes 1  # probe immediately (manual test)

# Build both exes
pyinstaller --clean --noconfirm build/CampusNet.spec   # dist\CampusNet.exe (--windowed)
pyinstaller --onefile --console --name auto_login --specpath build auto_login.py

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

# Deploy as silent daily guard task (scenario B, PowerShell as Administrator)
.\setup_task.ps1 -Silent
.\setup_task.ps1 -Silent -SilentAt 22:50 -RunMinutes 30

# Remove silent task
Unregister-ScheduledTask -TaskName CampusNetAutoLogin_Silent -Confirm:$false

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

### GUI layer (`gui_app.py`, ~1250 lines)

`gui_app.py` is presentation + orchestration only — it imports `auto_login as core` and never re-implements auth or detection.

**The window is three notebook tabs**, one per usage mode plus settings, because most users only ever want the daily-automatic one and should not have to understand the manual one:

| Tab | Contents |
|---|---|
| 手动守护 | 模式 A. status dot + 「开始守护 / 停止守护 / 立即认证一次」 + log pane (expanded by default) |
| 每天自动守护 | 模式 B. plain-language explanation, 每天 HH:MM / N 分钟 inputs, huge 「开启每天自动守护」 button, test / remove / open-logs links |
| 设置 | account, portal URL, check URL, auth method, the default-tab radio (`mode`), `close_to_tray`, reveal-config button |

`mode: "silent" | "manual"` (default `silent`) only chooses which tab is selected on launch; both tabs are always present, and switching the radio jumps to that tab.

**Deploying scenario B from the GUI** registers the task **itself** — `_task_register_command()` builds the whole `Register-ScheduledTask` call and passes it via `powershell -EncodedCommand` (base64 UTF-16LE), so there is no quoting or code-page hazard even when the exe path contains spaces or Chinese characters. `setup_task.ps1` is therefore optional: **a lone `CampusNet.exe` can deploy its own daily task** (verified with nothing but the exe in a folder). Delegating to the script was the earlier design, and it kept breaking — the script had to be *found* next to the exe, and it ran in whatever directory it was launched from, which is wrong when the user starts the exe by full path from elsewhere.

The flow deliberately does not trust the child's exit code alone:

- `_is_admin()` (`shell32.IsUserAnAdmin`) decides whether to show the "即将请求管理员权限" explainer *before* UAC appears — the app never needs to be started as admin, it elevates only this one operation.
- `_task_exists()` is the authoritative success check and must use the **same mechanism as registration** (a PowerShell `Get-ScheduledTask` probe via `-EncodedCommand`). It previously shelled out to `schtasks.exe`, which on this Chinese Windows returned "not found" for a task that plainly existed — so a successful create was reported as a failure and the delete path silently did nothing.
- A cancelled UAC surfaces as `ERROR_CANCELLED` (1223), which gets its own message instead of the generic failure.
- `_run_elevated()` redirects the child's stdout+stderr into a file and reads it back, because `CREATE_NO_WINDOW` makes pipes useless — the real error text is what the user needs, and it is shown in the failure dialog.
- `_report_task_failure()` always offers a way forward: relaunch self elevated via `ShellExecuteW(…, "runas", …)` (`_self_elevate`), or the parameter values for a manual `Register-ScheduledTask`.

**Icons work in a lone-exe deployment.** `assets/campusnet.ico` is preferred when present, and the same .ico is embedded as `TRAY_ICON_B64` so a single-file copy still shows the right tray icon (written to a temp file just for `LoadImageW`, which needs a path). `_icon_candidates()` must return absolute paths only: joining `getattr(sys, "_MEIPASS", "")` with a path yields the *relative* `assets\campusnet.ico` when `_MEIPASS` is absent, which silently resolves against the current working directory and picks up a stray icon.

`_test_silent_now` launches `--silent --now --run-minutes 1` so the user can see for themselves that nothing appears.

**Config self-generation:** `GuiApp.__init__` calls `core.ensure_config_file()`, which writes `auto_login_config.json` next to the exe with defaults if none exists, so a fresh folder needs nothing but the exe.

- **Scenario A** (`GuiApp`): window + tray. `start_monitor()` / `stop_monitor()` own a `core.run_detection_loop` thread that keeps probing (duration 0 = forever); the tray/close handlers only hide or exit the shell.
- **Scenario B** (`core.run_silent_mode`): `--silent` never constructs a `tkinter.Tk()` at all. `plan_silent_window()` (pure, unit-tested) decides how long to stand by, then the same `run_detection_loop` runs with `duration_override`. `--now` means *start immediately*: `run_silent_mode` collapses the whole window to `now`, because passing `--now` at 00:15 against a 23:00 target and still blocking ~23 hours is the opposite of what that flag means (it previously only suppressed the roll-over to tomorrow). Note `--run-minutes` sets the duration while `--now` sets the start; `_test_silent_now` passes both.

**Threading model — the one rule that matters:**

```
main thread    Win32 tray message loop (core.TrayApp hidden window + GetMessageW)
window thread  tkinter mainloop, root.after(POLL_MS) drains a queue.Queue
probe thread   core.run_detection_loop(stop_event, status_callback)
auth thread    one-shot core.do_auth for the 「立即认证一次」 button
```

Non-window threads may **only** put items on the queue (`QueueSink`, `StatusSink`, `GuiApp._queue_exit`). Touching tkinter widgets from another thread crashes or deadlocks it. All widget updates happen inside `GuiApp._tick` on the window thread.

`GUITray` extends `core.TrayApp`, replacing the console-oriented menu with 打开主窗口 / 隐藏到托盘 / 退出, and repaints the tray tooltip via `set_status()`.

**`GUITray._hide_console()` is deliberately overridden as a no-op, and `start_hidden=False` is forced.** `core.TrayApp` calls `_hide_console()` for its console-hosted modes; that helper also runs `EnumWindows` and hides *every* visible window owned by the process — which in GUI mode includes the tkinter main window. With it enabled the main window disappeared ~0.3s after `_show_window()` (`state` went `normal` → `withdrawn` with nothing calling `_hide_window`). `GUITray` must never hide windows.

`humanize(line)` is the translation layer that turns `[AUTH]/[RECOVER]/[DOWN]/...` records into plain-language lines for the log pane; `[STATUS]` rows are deliberately dropped there (they stay in the detailed view and in the log file).

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

Signature: `run_detection_loop(config, stop_event=None, status_callback=None, duration_override=None)`

- `stop_event` (`threading.Event`): when set, loop exits gracefully. Checked every 0.5s during sleep.
- `status_callback` (`callable(str)`): called with status line for tray tooltip updates.
- `duration_override` (int minutes): overrides `run_duration_minutes`; `None` keeps the config value. `--run-minutes` and `run_silent_mode` use it.
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

A GUI can attach a third output with `set_log_sink(fn)`; the sink receives `(line, level)` and is invoked **on whichever thread produced the record**, so it may only enqueue — never draw. `gui_app.QueueSink` is the reference implementation, and a sink that raises is swallowed so it can't break the detection loop.

### Config

`_need_setup(config)` checks whether core fields are missing (username+password for portal_post, portal_url for others). Triggers `interactive_setup()` wizard on first run if config is incomplete. `DEFAULT_CONFIG` also includes `schedule_time` (default `"20:30"`) for the scheduled task and `browser_wait_seconds` (default `3`) for browser auth mode.

GUI/silent additions to `DEFAULT_CONFIG`:

- `mode` (default `"silent"`) — which tab opens on launch: `"silent"` (每天自动守护, the common case) or `"manual"` (手动守护). UI preference only
- `close_to_tray` (default `True`) — scenario A: ✕ hides to tray instead of exiting
- `silent_start_time` (default `""`, "HH:MM") — scenario B; empty disables silent mode
- `silent_run_minutes` (default `30`) — how long the silent guard keeps probing before it exits

**Config location: beside the exe, always.** `find_config_file()` looks in exactly one meaningful place — `exe_dir()` (the exe's folder when frozen, the project folder from source). It deliberately does **not** consult the working directory or the source tree when frozen, because a portable tool must never inherit somebody else's config just because it was launched from that directory; that bug made `dist\CampusNet.exe` run on `DEFAULT_CONFIG` (no credentials, wrong intervals) and write logs into `dist\logs\`. `%APPDATA%\CampusNet` is consulted only as a last resort, and only for reading (installed copies where the program folder is read-only) — `config_write_path()` falls back to it when a write probe beside the exe fails. `ensure_config_file()` writes a default config when none exists, so a fresh folder ends up with `auto_login_config.json` + `logs\` created on first launch.

`_stdin_available()` + `notify_setup_required(config, gui=True)` replace the old bare `interactive_setup()` calls on non-console paths. Under `--windowed` builds `sys.stdin` is `None`, so `input()` would raise and kill the process; these helpers log the missing field and (when a GUI is possible) show a `messagebox` instead.

`save_config(config, path=None)` writes to `path + ".tmp"`, fsyncs, then `os.replace()`s it into place — atomic on Windows for same-volume renames. Both `gui_app` and the CLI use it, so a crash mid-write can't corrupt a config that contains credentials.

### `setup_task.ps1`

`param([switch]$Boot, [switch]$Silent, [string]$ScheduleTime, [string]$SilentAt, [int]$RunMinutes)`

- **Daily mode** (no flags): creates `CampusNetAutoLogin` task using config `schedule_time` (default 20:30), overridable with `-ScheduleTime HH:mm`
- **Boot mode** (`-Boot`): creates `CampusNetAutoLogin_Boot` task with `-AtStartup` trigger
- **Silent mode** (`-Silent`): creates `CampusNetAutoLogin_Silent`, a daily trigger at `$SilentAt` (falls back to config `silent_start_time`), running `CampusNet.exe --silent --run-minutes N` **directly** — no `Start-Process` wrapper is needed because a `--windowed` exe creates no console. `ExecutionTimeLimit` is `$RunMinutes + 10` minutes rather than the 2-hour default. Fails loudly if `CampusNet.exe` is missing, since `auto_login.exe` cannot do silent mode cleanly.
- Prefers `auto_login.exe` if present → launches via `powershell.exe Start-Process -WindowStyle Hidden` (fully hidden); boot mode appends `--boot` argument
- Falls back to `pythonw.exe` (no console window), then `python.exe`; searches PATH first, then common Python install locations
- Daily task: `LogonType Interactive` (required for browser mode + `keybd_event`), `RunLevel Limited`, 2-hour execution time limit, `RestartCount 0`
- Boot task: `LogonType ServiceAccount` with `UserId SYSTEM` (Session 0, no user logon required), `ExecutionTimeLimit 0` (unlimited), `RestartCount 3` with 1-minute interval
- `Hidden=$true` on task settings, ignores new instances if already running
- Trigger verification uses `$SilentAt` in silent mode and `$ScheduleTime` otherwise

## Key dependencies in stdlib

- `urllib.request` + `urllib.error` — all HTTP requests
- `http.cookiejar` — cookie persistence for portal_post multi-step auth
- `re` — extract JS redirect URL and form params from portal response
- `ctypes` + `ctypes.windll.*` — Win32 API: Shell_NotifyIcon, CreateWindowExW, RegisterClassExW, keybd_event, GetConsoleWindow, ShowWindow, EnumWindows, SetCurrentProcessExplicitAppUserModelID
- `threading` — `Thread` + `Event` for tray worker thread and clean shutdown
- `socket` — get local IP as fallback for queryString construction
- `argparse` — CLI flags (`--auth`, `--tray`, `--background`, `--boot`, `--silent`, `--now`, `--run-minutes`, `--version`)
- `tkinter` (+ `tkinter.messagebox`, `tkinter.ttk`) — the GUI layer only; standard library, so the zero-dependency rule holds
- `sys.frozen` (PyInstaller) — distinguishes exe vs script for path resolution and exit behavior

## Development gotchas

**Critical: `print()` ordering in `main()`.**
The background mode check (`if not INTERACTIVE or args.background:`) MUST execute before any `print()` call. Under `pythonw.exe` (Task Scheduler), `sys.stdout` is `None` and `print()` throws. If you add logging or output before this check you will break background mode. The `INTERACTIVE` flag is set at module level with a try/except: `try: INTERACTIVE = sys.stdout.isatty()` / `except: INTERACTIVE = False`.

**`TrayApp` has a built-in fallback.** If `_create_window()` or `_create_tray_icon()` raises (e.g., no desktop session), `TrayApp.run()` catches the exception and falls back to `run_detection_loop(config)` — this is a console-less fallback, not the tray app.

**`_need_setup` checks different fields per `auth_method`.**
`portal_post` requires `username` + `password`; `http` and `browser` require `portal_url`. This affects when the config wizard triggers.

**`__main__` finally block.** Interactive mode (non-tray) has a `finally:` block that prompts `input("\nPress Enter to exit...")`. This prevents the console from instantly vanishing when running as `auto_login.exe` (PyInstaller). Tray mode skips this via the `_tray_mode` flag.

**JSON config contains plaintext passwords.** `auto_login_config.json` is in `.gitignore` and must never be committed. The example config (`auto_login_config.example.json`) uses `_`-prefixed keys as pseudo-comments since JSON has no comment syntax.

**`setup_task.ps1` exe/script detection order.** The script checks `auto_login.exe` first → `pythonw.exe` (no window) → `python.exe` (fallback). If `auto_login.exe` exists, it's launched via `powershell.exe Start-Process -WindowStyle Hidden` for true zero-window execution. Python fallbacks pass `--background` (or `--boot`) to suppress console output. Silent mode (`-Silent`) bypasses this chain entirely and requires `CampusNet.exe`.

**Never edit UTF-8 repo files through Windows PowerShell text cmdlets.** `Get-Content`/`Set-Content` (and `-replace` pipelines) read and write using the ANSI code page on a Chinese Windows install, so round-tripping a UTF-8 source file silently corrupts every non-ASCII character — including the Chinese UI strings in `gui_app.py`. Use the file tools, or read/write explicitly with `[System.IO.File]::ReadAllText/WriteAllText(..., [Text.Encoding]::UTF8)`. The same trap makes `git diff`/console output of UTF-8 files look like mojibake even though the file is fine.

**`setup_task.ps1` must keep its UTF-8 BOM.** Windows PowerShell 5.1 decodes a BOM-less `.ps1` using the system ANSI code page, so the Chinese comments and error strings decode into garbage that swallows quotes and braces — the script then fails to parse *entirely* (every mode, not just the one being used) and exits 1 with no usable message. It shipped broken once after being rewritten without its BOM, and surfaced only as a bare exit code. `test_auto_login.PowerShellScriptTests` guards both the BOM and the `param()`-first rule; nothing in the default toolchain strips a BOM, so the real risk is a manual rewrite.

**`.bat` files must be pure ASCII.** cmd.exe has no reliable way to discover a batch file's encoding (a UTF-8 BOM makes it choke on the first line), so Chinese text in a `.bat` breaks under at least one code page. Keep launcher/helper scripts ASCII-only, or write them as `.ps1` with a BOM instead.

## Reliability updates

- Hidden resident tray mode overrides duration to 0 on a config copy. Worker completion posts a UI message to close the tray.
- EXE scheduled launcher passes --background/--boot, waits for completion, and propagates exit status; Python script paths are quoted.
- Explicit CLI modes do not wait for Enter on exit.
- Run offline tests before packaging; existing dist artifacts are not updated by source edits.
- GUI: `run_detection_loop` runs on its own thread while the window polls a queue; `stop_monitor()` clears the thread reference immediately so the button state flips without waiting for the thread to unwind.
- GUI: `close_to_tray` is honoured only when a tray icon actually exists; otherwise ✕ exits, so a tray-less environment can't leave an invisible, unstoppable process.
- Silent mode verified end to end on Windows: `CampusNet.exe --silent --now --run-minutes 1` exits with code 0 after ~62s with **no window handle at any point** and only log output.
- `--silent` never constructs `tkinter.Tk()`, so it is safe in Session 0 and under Task Scheduler.
- GUI: `_tick` checks an `_alive` flag and stops rescheduling once `_do_exit` tears the window down. A pending `after()` callback firing on a destroyed canvas raises `TclError: invalid command name` and spams tracebacks during shutdown.
- Scenario A verified end to end (all 8 checks): tray icon created → window `normal` → ✕ withdraws it while the probe thread keeps guarding → reopen restores `normal` → quit wakes the tray thread and exits cleanly.
