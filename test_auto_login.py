import json
import os
import sys
import tempfile
import threading
import unittest
from datetime import datetime, timedelta
from unittest.mock import Mock, patch

import auto_login as app


class AutoLoginTests(unittest.TestCase):
    def setUp(self):
        self.config = dict(app.DEFAULT_CONFIG, username="student", password="test")
        self.log_patch = patch.object(app, "log")
        self.log_patch.start()
        self.addCleanup(self.log_patch.stop)
        # Every test must explicitly mock network access.
        self.network_patch = patch.object(app.urllib.request, "urlopen", side_effect=AssertionError("Unexpected network request"))
        self.network_patch.start()
        self.addCleanup(self.network_patch.stop)

    def portal_response(self, body, redirect=True):
        probe = Mock()
        probe.geturl.return_value = self.config["check_url"]
        probe.read.return_value = (b"location.href='/eportal/index.jsp?wlanuserip=10.0.0.1'" if redirect else b"online")
        page = Mock()
        page.geturl.return_value = self.config["portal_url"] + "/eportal/index.jsp?wlanuserip=10.0.0.1"
        page.read.return_value = b""
        result = Mock(status=200)
        result.read.return_value = body
        opener = Mock()
        opener.open.side_effect = [probe, page, result]
        with patch.object(app.urllib.request, "build_opener", return_value=opener):
            ok = app.do_auth_portal_post(self.config)
        return ok, opener

    def test_portal_requires_explicit_success(self):
        for body in (b'{"result": "fail"}', b'{}', b'[]', b'null', b'<html>Error</html>'):
            with self.subTest(body=body):
                self.assertFalse(self.portal_response(body)[0])
        self.assertTrue(self.portal_response(b'{"result": "success"}')[0])

    def test_relative_redirect_resolved(self):
        _, opener = self.portal_response(b'{"result":"success"}')
        self.assertEqual(opener.open.call_args_list[1].args[0].full_url,
                         self.config["portal_url"] + "/eportal/index.jsp?wlanuserip=10.0.0.1")

    def test_no_redirect_does_not_logout(self):
        _, opener = self.portal_response(b'{"result":"success"}', redirect=False)
        self.assertFalse(any("logout" in c.args[0].full_url or "offline" in c.args[0].full_url
                             for c in opener.open.call_args_list))

    def test_auth_requires_network_recovery(self):
        with patch.object(app, "do_auth_portal_post", return_value=True), \
             patch.object(app, "check_network", return_value=(False, "offline")), \
             patch.object(app.time, "sleep"):
            self.assertFalse(app.do_auth(self.config, None))

    def test_delayed_network_recovery(self):
        with patch.object(app, "do_auth_portal_post", return_value=True), \
             patch.object(app, "check_network", side_effect=[(False, "offline"), (True, "OK")]), \
             patch.object(app.time, "sleep"):
            self.assertTrue(app.do_auth(self.config, None))

    def test_http_error_not_success(self):
        self.config["auth_method"] = "http"
        with patch.object(app, "do_auth_http", return_value=(403, "Forbidden", "HTTP 403")):
            self.assertFalse(app.do_auth(self.config, None))

    def test_auth_flag_without_console(self):
        for success in (True, False):
            with patch.object(app.sys, "argv", ["auto_login.py", "--auth"]), \
                 patch.object(app, "INTERACTIVE", False), \
                 patch.object(app, "load_config", return_value=self.config), \
                 patch.object(app, "clean_old_logs"), \
                 patch.object(app, "do_auth", return_value=success), \
                 patch.object(app, "run_detection_loop") as loop:
                self.assertEqual(app.main(), 0 if success else 1)
                loop.assert_not_called()

    def test_placeholder_requires_setup(self):
        self.config["username"] = "你的学号"
        self.assertTrue(app._need_setup(self.config))

    def _probe(self, final_url, body=b"real baidu page"):
        resp = Mock()
        resp.geturl.return_value = final_url
        resp.read.return_value = body
        return patch.object(app.urllib.request, "urlopen", return_value=resp)

    def test_same_site_https_upgrade_is_not_portal(self):
        # baidu.com 301s http -> https on a healthy network; must not read as down.
        for final in ("https://www.baidu.com/", "https://www.baidu.com"):
            with self.subTest(final=final), self._probe(final):
                self.assertEqual(
                    app.check_network("http://www.baidu.com", 5, "baidu"),
                    (True, "OK"),
                )

    def test_cross_host_redirect_is_portal(self):
        with self._probe("http://10.10.200.102/eportal/index.jsp?wlanuserip=10.0.0.1"):
            ok, detail = app.check_network("http://www.baidu.com", 5, "baidu")
        self.assertFalse(ok)
        self.assertIn("portal redirect", detail)

    def test_same_host_path_change_is_portal(self):
        with self._probe("http://www.baidu.com/redirect?to=login"):
            ok, _ = app.check_network("http://www.baidu.com", 5, "baidu")
        self.assertFalse(ok)

    def test_proxied_body_without_keyword_is_portal(self):
        with self._probe("https://www.baidu.com/", body=b"<html>Please log in</html>"):
            ok, detail = app.check_network("http://www.baidu.com", 5, "baidu")
        self.assertFalse(ok)
        self.assertIn("missing", detail)

    def test_resident_tray_does_not_mutate_config(self):
        tray = app.TrayApp(self.config, start_hidden=True)
        self.assertEqual(tray._config["run_duration_minutes"], 0)
        self.assertEqual(self.config["run_duration_minutes"], 60)

    def test_worker_completion_notifies_ui(self):
        tray = app.TrayApp(self.config)
        tray._hwnd = 123
        with patch.object(app, "run_detection_loop"), \
             patch.object(app.ctypes.windll.user32, "PostMessageW") as post:
            tray._detection_worker()
            post.assert_called_once_with(123, app.WM_USER + 2, 0, 0)

    def test_failed_auth_backoff_and_fast_confirmation(self):
        stop = threading.Event()
        clock = [0.0]
        attempts = []
        sleeps = []
        def sleep(seconds):
            sleeps.append(seconds)
            clock[0] += seconds
            if clock[0] >= 100:
                stop.set()
        def auth(*args):
            attempts.append(clock[0])
            return False
        with patch.object(app, "check_network", return_value=(False, "offline")), \
             patch.object(app, "do_auth", side_effect=auth), \
             patch.object(app.time, "monotonic", side_effect=lambda: clock[0]), \
             patch.object(app.time, "sleep", side_effect=sleep):
            app.run_detection_loop(self.config, stop_event=stop)
        self.assertEqual(attempts[0], 2)
        self.assertEqual(len(attempts), 6)
        for index, delay in enumerate((3, 5, 10, 20, 40), start=1):
            elapsed = attempts[index] - attempts[index - 1]
            self.assertGreaterEqual(elapsed, delay)
            self.assertLess(elapsed, delay + 4)

    def test_retry_delay_progression_and_cap(self):
        self.assertEqual([app.auth_retry_delay(self.config, n) for n in range(1, 10)],
                         [3, 5, 10, 20, 40, 80, 160, 300, 300])
        self.assertEqual(app.auth_retry_delay(self.config, 0), 3)
        self.assertEqual(app.auth_retry_delay(dict(self.config, auth_cooldown_seconds=30), 2), 30)


class SilentModeTests(unittest.TestCase):
    """Scenario B is pure clock arithmetic — exercise it without a GUI."""

    def setUp(self):
        self.config = dict(app.DEFAULT_CONFIG, username="student", password="test")
        self.log_patch = patch.object(app, "log")
        self.log_patch.start()
        self.addCleanup(self.log_patch.stop)

    def test_parse_hhmm_accepts_valid_and_rejects_junk(self):
        self.assertEqual(app._parse_hhmm("07:05").hour, 7)
        self.assertEqual(app._parse_hhmm("7:05").minute, 5)
        self.assertEqual(app._parse_hhmm("23:59").minute, 59)
        for bad in ("", None, "  ", "24:00", "12-30", "abc", "12:60"):
            with self.subTest(bad=bad):
                self.assertIsNone(app._parse_hhmm(bad))

    def test_waits_until_configured_start_today(self):
        now = datetime(2026, 1, 1, 22, 30, 0)
        cfg = dict(self.config, silent_start_time="22:50", silent_run_minutes=30)
        start, end, wait = app.plan_silent_window(cfg, now=now)
        self.assertEqual(start, datetime(2026, 1, 1, 22, 50))
        self.assertEqual(end, datetime(2026, 1, 1, 23, 20))
        self.assertEqual(wait, 20 * 60)

    def test_start_already_passed_probes_immediately(self):
        now = datetime(2026, 1, 1, 22, 55, 0)
        cfg = dict(self.config, silent_start_time="22:50", silent_run_minutes=30)
        start, end, wait = app.plan_silent_window(cfg, now=now)
        self.assertEqual(wait, 0.0)
        self.assertEqual(end, datetime(2026, 1, 1, 23, 20))

    def test_elapsed_window_rolls_to_tomorrow_for_scheduled_runs(self):
        now = datetime(2026, 1, 1, 23, 59, 0)
        cfg = dict(self.config, silent_start_time="22:50", silent_run_minutes=30)
        start, end, wait = app.plan_silent_window(cfg, now=now, allow_tomorrow=True)
        self.assertEqual(start, datetime(2026, 1, 2, 22, 50))
        self.assertGreater(wait, 0)

    def test_manual_launch_never_waits_a_whole_day(self):
        now = datetime(2026, 1, 1, 23, 59, 0)
        cfg = dict(self.config, silent_start_time="22:50", silent_run_minutes=30)
        start, end, wait = app.plan_silent_window(cfg, now=now, allow_tomorrow=False)
        self.assertEqual(wait, 0.0)
        self.assertEqual(start, now)
        self.assertEqual(end, now + timedelta(minutes=30))

    def test_unset_or_malformed_time_disables_silent_mode(self):
        for bad in ("", "  ", "25:00", "abc", None):
            with self.subTest(bad=bad):
                cfg = dict(self.config, silent_start_time=bad)
                self.assertIsNone(app.plan_silent_window(cfg))

    def test_silent_mode_falls_back_when_unconfigured(self):
        with patch.object(app, "load_config", return_value=self.config), \
             patch.object(app, "clean_old_logs"), \
             patch.object(app, "run_detection_loop") as loop:
            self.assertEqual(app.run_silent_mode(run_minutes=5), 0)
        loop.assert_called_once_with(self.config, duration_override=5)

    @staticmethod
    def _frozen_datetime(when):
        """A datetime stand-in whose now() is fixed.

        `datetime.datetime` is immutable, so patch.object(app.datetime, "now")
        raises TypeError — the whole reference has to be swapped for a subclass.
        Subclassing keeps combine/strptime/timedelta working, which
        plan_silent_window relies on.
        """
        real = datetime

        class _Frozen(real):
            @classmethod
            def now(cls, tz=None):
                return when

        return _Frozen

    def test_silent_mode_waits_when_scheduled(self):
        """A scheduled launch must stand by until the configured time.

        The wait loop is driven by time.monotonic, so it is faked as well as
        time.sleep — mocking only sleep leaves the real clock frozen and spins
        the loop forever.
        """
        cfg = dict(self.config, silent_start_time="22:00", silent_run_minutes=30)
        slept = []
        steps = iter([0.0, 0.0, 99999.0, 99999.0])   # one pass through the loop

        def fake_sleep(seconds):
            slept.append(seconds)
            steps.__next__()                          # stop the loop on re-entry

        with patch.object(app, "load_config", return_value=cfg), \
             patch.object(app, "clean_old_logs"), \
             patch.object(app, "run_detection_loop") as loop, \
             patch.object(app, "datetime",
                          self._frozen_datetime(datetime(2026, 1, 1, 12, 0))), \
             patch.object(app.time, "monotonic", side_effect=lambda: next(steps, 99999.0)), \
             patch.object(app.time, "sleep", side_effect=fake_sleep):
            app.run_silent_mode(run_minutes=1)
        self.assertTrue(slept, "scheduled run should stand by until the window opens")
        loop.assert_called_once_with(cfg, duration_override=1)

    def test_now_never_waits_even_before_todays_time(self):
        """--now must start immediately even when today's HH:MM is still ahead.

        Regression guard: --now used to only stop the window rolling over to
        tomorrow, so at 00:15 with a 23:00 target it still blocked ~23 hours —
        the opposite of what passing --now means. `now` is a few seconds past
        the target here so the wait loop can only exit via the --now collapse
        (it must not spin for hours), and any sleep means the fix regressed.
        """
        cfg = dict(self.config, silent_start_time="22:00", silent_run_minutes=30)
        slept = []
        with patch.object(app, "load_config", return_value=cfg), \
             patch.object(app, "clean_old_logs"), \
             patch.object(app, "run_detection_loop") as loop, \
             patch.object(app, "datetime",
                          self._frozen_datetime(datetime(2026, 1, 1, 22, 0, 5))), \
             patch.object(app.time, "sleep", side_effect=lambda s: slept.append(s)):
            app.run_silent_mode(run_minutes=1, interactive_launch=True)
        self.assertEqual(slept, [],
                         f"--now must not sleep, but slept {len(slept)} time(s)")
        loop.assert_called_once_with(cfg, duration_override=1)

    def test_duration_override_beats_config(self):
        stop = threading.Event()
        seen = []
        with patch.object(app, "check_network", return_value=(True, "OK")), \
             patch.object(app, "log",
                          side_effect=lambda m, level="INFO": seen.append(m)), \
             patch.object(app.time, "sleep", side_effect=lambda s: stop.set()):
            app.run_detection_loop(dict(self.config, run_duration_minutes=999),
                                   stop_event=stop, duration_override=7)
        self.assertTrue(any("duration=7min" in m for m in seen), seen)


class ConfigWriteTests(unittest.TestCase):
    def setUp(self):
        self.log_patch = patch.object(app, "log")
        self.log_patch.start()
        self.addCleanup(self.log_patch.stop)

    def test_save_config_is_atomic_and_round_trips(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "cfg.json")
            cfg = dict(app.DEFAULT_CONFIG, username="u", password="p")
            self.assertTrue(app.save_config(cfg, path=path))
            with open(path, encoding="utf-8") as f:
                self.assertEqual(json.load(f)["username"], "u")
            self.assertFalse(os.path.exists(path + ".tmp"))

    def test_save_config_overwrites_previous_contents(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "cfg.json")
            app.save_config(dict(app.DEFAULT_CONFIG, username="old"), path=path)
            app.save_config(dict(app.DEFAULT_CONFIG, username="new"), path=path)
            with open(path, encoding="utf-8") as f:
                self.assertEqual(json.load(f)["username"], "new")

    def test_save_config_failure_returns_false(self):
        with tempfile.TemporaryDirectory() as d:
            self.assertFalse(app.save_config(dict(app.DEFAULT_CONFIG),
                                             path=os.path.join(d, "missing", "\0bad")))


class GuiLogicTests(unittest.TestCase):
    """Only the pure translation layer — never instantiate a Tk window."""

    def test_humanize_translates_real_log_lines(self):
        import gui_app
        cases = [
            ("[2026-01-01 21:03:57] [RECOVER] Network restored — outage: 1m2s, "
             "checks: 3, auth_attempts: 1", "1m2s"),
            ("[2026-01-01 21:03:55] [AUTH] Auth OK [HTTP 200, 120ms] body: success",
             "认证请求已提交"),
            ("[2026-01-01 21:03:50] [DOWN] Network DOWN (reason: portal redirect), "
             "starting auth", "检测到断网"),
            ("[2026-01-01 21:03:50] [WARN] Check #1 failed: timed out", "继续尝试"),
            ("[2026-01-01 21:03:55] [START] Service started [tray]", "开始守护"),
        ]
        for raw, expected in cases:
            with self.subTest(raw=raw):
                out = gui_app.humanize(raw)
                self.assertIsNotNone(out)
                self.assertIn(expected, out)

    def test_humanize_drops_noisy_config_lines(self):
        import gui_app
        noisy = ("[2026-01-01 21:03:55] [INFO] Config: auth=portal_post "
                 "check=http://www.baidu.com interval=5s threshold=2 duration=60min")
        self.assertIsNone(gui_app.humanize(noisy))

    def test_every_state_has_label_and_colour(self):
        import gui_app
        for state in (gui_app.STATE_IDLE, gui_app.STATE_PENDING, gui_app.STATE_RUNNING,
                      gui_app.STATE_AUTHING, gui_app.STATE_FAILED, gui_app.STATE_EXITING):
            with self.subTest(state=state):
                self.assertIn(state, gui_app.STATE_TEXT)
                self.assertIn(state, gui_app.STATE_COLOR)


class GuiLifecycleTests(unittest.TestCase):
    def setUp(self):
        import gui_app
        import queue
        self.gui = gui_app
        self.ui = gui_app.GuiApp.__new__(gui_app.GuiApp)
        self.ui._config = dict(app.DEFAULT_CONFIG, username="student", password="test")
        self.ui._q = queue.Queue()
        self.ui._monitor = None
        self.ui._monitor_stop = None
        self.ui._state = gui_app.STATE_IDLE
        self.ui._set_state = Mock()
        self.ui._log = Mock()
        self.ui._on_status_line = Mock()

    def test_primary_click_agrees_with_start_monitor_guard(self):
        """点击处理必须和 start_monitor 用同一个判断（_monitor is not None）。

        回归守卫：这里如果用 _monitor.is_alive()，一旦 worker 线程已经结束、
        而它的完成消息还躺在队列里没被处理，两者就会给出相反答案 ——
        按钮显示可再启动，点下去却在 start_monitor 的守卫里被静默拒绝，
        用户以为程序坏了。
        """
        dead = Mock()
        dead.is_alive.return_value = False          # 线程已结束
        self.ui._monitor = dead                     # 但完成消息尚未消费
        self.ui.stop_monitor = Mock()
        self.ui.start_monitor = Mock()

        self.ui._on_primary_click()

        self.ui.stop_monitor.assert_called_once()
        self.ui.start_monitor.assert_not_called()

    def test_stop_blocks_restart_until_completion_is_consumed(self):
        entered = threading.Event()
        release = threading.Event()

        def blocked_probe(config, **kwargs):
            entered.set()
            release.wait(5)

        with patch.object(app, "run_detection_loop", side_effect=blocked_probe) as run:
            self.ui.start_monitor()
            worker = self.ui._monitor
            try:
                self.assertTrue(entered.wait(2))
                self.ui.stop_monitor()
                self.ui.start_monitor()
                self.assertIs(self.ui._monitor, worker)
                self.assertEqual(run.call_count, 1)
            finally:
                release.set()
                worker.join(2)
            self.assertFalse(worker.is_alive())
            self.ui.start_monitor()
            self.assertIs(self.ui._monitor, worker)
            self.ui._handle_message(*self.ui._q.get_nowait())
            self.assertIsNone(self.ui._monitor)
            self.ui.start_monitor()
            self.ui._monitor.join(2)
            self.assertEqual(run.call_count, 2)
            self.assertEqual(run.call_args.kwargs["duration_override"], 0)

    def test_stale_worker_messages_cannot_change_current_session(self):
        old = threading.Event()
        current = threading.Event()
        self.ui._monitor_stop = current
        worker = self.ui._monitor = Mock()
        self.ui._handle_message("monitor_done", old)
        self.ui._handle_message("monitor_status", (old, "old status"))
        self.assertIs(self.ui._monitor, worker)
        self.ui._set_state.assert_not_called()
        self.ui._on_status_line.assert_not_called()
        current.set()
        self.ui._handle_message("monitor_status", (current, "stopping"))
        self.ui._on_status_line.assert_not_called()

    def test_failed_task_update_does_not_accept_existing_task(self):
        self.ui.mode_var = Mock()
        self.ui.mode_var.get.return_value = "silent"
        self.ui._config.update(silent_start_time="23:00", silent_run_minutes=30)
        for name in ("_write_config", "_require_credentials", "_explain_elevation",
                     "_task_exists"):
            setattr(self.ui, name, Mock(return_value=True))
        self.ui._task_register_command = Mock(return_value="command")
        self.ui._run_elevated = Mock(return_value=(1, "command", "failed"))
        self.ui.mode_note = Mock()
        self.ui.root = Mock()
        self.ui._report_task_failure = Mock()
        with patch.object(self.gui.messagebox, "showinfo") as success:
            self.ui._deploy_silent_task()
        success.assert_not_called()
        self.ui._report_task_failure.assert_called_once()

    def test_task_command_quotes_paths_and_checks_registered_settings(self):
        import base64
        with patch.object(sys, "executable", "C:\\O'Brien\\CampusNet.exe"), \
             patch.object(sys, "frozen", True, create=True), \
             patch.object(app, "exe_dir", return_value="C:\\O'Brien"):
            command = self.ui._task_register_command("23:00", 30)
        script = base64.b64decode(command.split()[-1]).decode("utf-16-le")
        self.assertIn("O''Brien", script)
        self.assertIn("--silent --now --run-minutes 30", script)
        self.assertIn("-Force -ErrorAction Stop", script)
        self.assertIn("$task.Actions[0].Arguments -ne $expectedArgs", script)
        self.assertIn(".ToString('HH:mm') -ne '23:00'", script)

    def test_generated_registration_verification_in_powershell(self):
        import base64
        import subprocess
        command = self.ui._task_register_command("23:00", 30)
        script = base64.b64decode(command.split()[-1]).decode("utf-16-le")
        # Replace all task cmdlets: this never reads or writes actual tasks.
        stubs = """
function New-ScheduledTaskAction { param($Execute, $Argument, $WorkingDirectory) }
function New-ScheduledTaskTrigger { param([switch]$Daily, $At) }
function New-ScheduledTaskSettingsSet {
 param([switch]$AllowStartIfOnBatteries, [switch]$DontStopIfGoingOnBatteries,
 [switch]$StartWhenAvailable, $MultipleInstances, $ExecutionTimeLimit, $RestartCount)
}
function Register-ScheduledTask { param($TaskName, $Action, $Trigger, $Settings,
 [switch]$Force, $ErrorAction) }
function Get-ScheduledTask {
 param($TaskName, $ErrorAction)
 [pscustomobject]@{ Actions = @([pscustomobject]@{
 Execute = $expectedExe; Arguments = $expectedArgs; WorkingDirectory = $expectedDir
 }); Triggers = @([pscustomobject]@{ StartBoundary = '2026-01-01T23:00:00' }) }
}
"""
        for matches in (True, False):
            with self.subTest(matches=matches):
                body = stubs if matches else stubs.replace("23:00:00", "22:00:00")
                encoded = base64.b64encode((body + script).encode("utf-16-le")).decode()
                result = subprocess.run(
                    ["powershell", "-NoProfile", "-EncodedCommand", encoded],
                    capture_output=True, timeout=20, creationflags=0x08000000)
                self.assertEqual(result.returncode == 0, matches, result.stderr)


class ReleaseManifestTests(unittest.TestCase):
    def test_release_includes_tracked_instructions_and_excludes_credentials(self):
        import contextlib
        import io
        import runpy
        import zipfile
        root = os.path.dirname(os.path.abspath(__file__))
        with patch.object(sys, "argv", ["make_release_zip.py", "1.7.2"]):
            module = runpy.run_path(os.path.join(root, "packaging", "make_release_zip.py"))
        with tempfile.TemporaryDirectory() as staging:
            for src, _ in module["MANIFEST"]:
                path = os.path.join(staging, src)
                os.makedirs(os.path.dirname(path), exist_ok=True)
                if src.endswith(".exe"):
                    data = b"test fixture, not an executable"
                else:
                    with open(os.path.join(root, src), "rb") as source:
                        data = source.read()
                with open(path, "wb") as target:
                    target.write(data)
            with open(os.path.join(staging, "dist", "auto_login_config.json"), "w") as f:
                f.write('test private data')
            module["main"].__globals__["ROOT"] = staging
            with contextlib.redirect_stdout(io.StringIO()):
                module["main"]()
            with zipfile.ZipFile(os.path.join(staging, "dist", "auto_login_v1.7.2.zip")) as z:
                names = z.namelist()
            self.assertTrue(any(n.endswith("使用说明.txt") for n in names))
            self.assertFalse(any(n.endswith("auto_login_config.json") for n in names))


class PowerShellScriptTests(unittest.TestCase):
    """setup_task.ps1 must stay loadable by Windows PowerShell 5.1.

    Regression guard for a real bug: the script contains Chinese text, and
    Windows PowerShell 5.1 decodes a BOM-less .ps1 using the system ANSI code
    page (GBK on a Chinese install). The UTF-8 bytes then decode into garbage
    that swallows quotes and braces, so the whole script fails to parse — every
    mode, not just silent. It shipped that way once after the file was rewritten
    without its BOM, and the failure surfaced only as a bare exit code 1.
    """

    SCRIPT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "setup_task.ps1")

    def test_has_utf8_bom(self):
        with open(self.SCRIPT, "rb") as f:
            head = f.read(3)
        self.assertEqual(head, b"\xef\xbb\xbf",
                         "setup_task.ps1 lost its UTF-8 BOM — PowerShell 5.1 will "
                         "mis-decode the Chinese text and refuse to parse the script")

    def test_param_block_is_first_statement(self):
        """Anything before param() makes PowerShell treat the whole file as a
        bare command and silently ignore the parameters."""
        with open(self.SCRIPT, "r", encoding="utf-8-sig") as f:
            first = f.readline()
        self.assertTrue(first.lstrip().startswith("param("),
                        f"setup_task.ps1 must start with param(...), got {first!r}")


if __name__ == "__main__":
    unittest.main()
