import json
import os
import re
import sys
import tempfile
import threading
import time
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


class AttemptTimingTests(unittest.TestCase):
    """Stage A is observation only: these timings must never steer a decision.

    The tests therefore check two separate things — that the numbers are
    correct, and that adding them left the auth path's behaviour and return
    types untouched.
    """

    def setUp(self):
        self.config = dict(app.DEFAULT_CONFIG, username="student", password="test")
        self.log_patch = patch.object(app, "log")
        self.log_patch.start()
        self.addCleanup(self.log_patch.stop)
        self.clock = [100.0]

    def _advance(self, seconds):
        self.clock[0] += seconds

    def _record(self):
        """A TimingRecord whose clock we control, so phase maths is exact."""
        with patch.object(app.time, "monotonic", side_effect=lambda: self.clock[0]):
            return app.TimingRecord()

    def test_absent_record_leaves_log_lines_unchanged(self):
        # Callers that pass no record (--auth, tests, older code) must keep
        # producing exactly the previous log text.
        self.assertEqual(app._timing_suffix(None), "")
        empty = app.TimingRecord()
        self.assertEqual(app._timing_suffix(empty), "")

    def test_phase_metrics_are_exact(self):
        # Three phases must stay separate: portal discovery/params, the
        # credential POST, and the post-auth verification. Merging discovery
        # with the POST would hide whichever half is actually slow.
        with patch.object(app.time, "monotonic", side_effect=lambda: self.clock[0]):
            t = app.TimingRecord()
            self._advance(0.4)
            t.mark_discovery_done()        # 发现 400ms
            self._advance(0.2)
            t.mark_login_started()         # POST 开始
            self._advance(0.2)
            t.mark_login_done()            # POST 200ms
            self._advance(1.5)
            t.mark_verify_done()
            t.mark_finished()
        self.assertAlmostEqual(t.discovery_ms, 400, delta=1)
        self.assertAlmostEqual(t.login_post_ms, 200, delta=1)
        self.assertAlmostEqual(t.verify_ms, 1500, delta=1)
        self.assertAlmostEqual(t.total_ms, 2300, delta=1)
        suffix = app._timing_suffix(t)
        self.assertIn("discover 400ms", suffix)
        self.assertIn("post 200ms", suffix)
        self.assertIn("verify 1.5s", suffix)
        self.assertIn("total 2.3s", suffix)

    def test_login_post_ms_is_absent_when_the_post_never_started(self):
        # Discovery failed before any POST: the POST phase is "not measured",
        # which must not be reported as 0ms.
        with patch.object(app.time, "monotonic", side_effect=lambda: self.clock[0]):
            t = app.TimingRecord()
            self._advance(0.3)
            t.mark_discovery_done()
            t.mark_finished()
        self.assertAlmostEqual(t.discovery_ms, 300, delta=1)
        self.assertIsNone(t.login_post_ms)

    def test_login_started_defaults_the_discovery_boundary(self):
        # A caller that only marks the POST must still yield a non-negative
        # discovery phase rather than a negative one.
        with patch.object(app.time, "monotonic", side_effect=lambda: self.clock[0]):
            t = app.TimingRecord()
            self._advance(0.25)
            t.mark_login_started()
            self._advance(0.1)
            t.mark_login_done()
        self.assertAlmostEqual(t.discovery_ms, 250, delta=1)
        self.assertAlmostEqual(t.login_post_ms, 100, delta=1)

    def test_total_ms_is_none_until_the_record_is_closed(self):
        # "not measured" and "instant" must stay distinguishable.
        with patch.object(app.time, "monotonic", side_effect=lambda: self.clock[0]):
            t = app.TimingRecord()
            t.mark_discovery_done()
            self.assertIsNone(t.total_ms)
            t.mark_finished()
            self.assertEqual(t.total_ms, 0)

    def test_verify_ms_is_absent_when_the_post_never_succeeded(self):
        with patch.object(app.time, "monotonic", side_effect=lambda: self.clock[0]):
            t = app.TimingRecord()
            self._advance(0.3)
            t.mark_discovery_done()
            self._advance(0.1)
            t.mark_login_started()
            t.mark_login_done()
            t.mark_finished()
        self.assertIsNone(t.verify_ms)
        self.assertIn("discover 300ms", app._timing_suffix(t))
        self.assertIn("post 0ms", app._timing_suffix(t))

    def test_lateness_is_separate_from_the_planned_wait(self):
        # A long planned wait is a deliberate parameter; lateness is a
        # scheduling defect. Conflating them would hide a slipped deadline.
        with patch.object(app.time, "monotonic", side_effect=lambda: self.clock[0]):
            t = app.TimingRecord()
            t.mark_due(scheduled_at=self.clock[0] - 0.05, planned_wait_ms=250)
        self.assertEqual(t.retry_planned_wait_ms, 250)
        self.assertAlmostEqual(t.retry_lateness_ms, 50, delta=1)

    def test_first_attempt_of_an_outage_reports_no_lateness(self):
        # next_auth_at starts at 0.0, so measuring against it would report the
        # machine's uptime (hours) as lateness and poison the sample.
        with patch.object(app.time, "monotonic", side_effect=lambda: self.clock[0]):
            t = app.TimingRecord()
            t.mark_due(scheduled_at=0.0, planned_wait_ms=None)
        self.assertIsNone(t.retry_lateness_ms)
        self.assertNotIn("lateness=", t.clue_line())

    def test_lateness_never_goes_negative_for_an_early_attempt(self):
        with patch.object(app.time, "monotonic", side_effect=lambda: self.clock[0]):
            t = app.TimingRecord()
            t.mark_due(scheduled_at=self.clock[0] + 10, planned_wait_ms=1000)
        self.assertEqual(t.retry_lateness_ms, 0)

    def test_missing_stamps_yield_none_instead_of_zero(self):
        t = app.TimingRecord()
        self.assertIsNone(t.verify_ms)
        self.assertEqual(app._metric_ms(None, None), None)

    def test_fmt_metric_switches_unit_at_one_second(self):
        self.assertEqual(app._fmt_metric(999), "999ms")
        self.assertEqual(app._fmt_metric(1000), "1.0s")
        self.assertEqual(app._fmt_metric(2540), "2.5s")
        self.assertIsNone(app._fmt_metric(None))

    def test_portal_message_is_bounded_and_flattened(self):
        self.assertEqual(app._safe_portal_message(None), "")
        self.assertEqual(app._safe_portal_message(123), "")
        self.assertEqual(app._safe_portal_message("  a\nb  "), "a b")
        # Long opaque runs are treated as secrets first, so the result is short;
        # the important property is the bound, not the exact text.
        self.assertLessEqual(len(app._safe_portal_message("x" * 500)), 163)

    def test_long_diagnostic_text_is_truncated_not_masked(self):
        # A genuinely long sentence must be truncated at the limit, otherwise
        # the bound is untested for the realistic case.
        message = ("运营商用户认证失败 " * 60).strip()
        bounded = app._safe_portal_message(message, limit=160)
        self.assertEqual(len(bounded), 163)
        self.assertTrue(bounded.endswith("..."))
        self.assertTrue(bounded.startswith("运营商用户认证失败"))

    def test_redaction_masks_labelled_secrets(self):
        cases = [
            "password=FAKE_SECRET token=FAKE_TOKEN",
            '{"password":"FAKE_SECRET","token":"FAKE_TOKEN"}',
            "sessionId=FAKE_SESSION jsessionid=FAKE_JSESSION",
            "userIndex=6632386361646666386164353832666639326137616365353931323434653332",
        ]
        for raw in cases:
            with self.subTest(raw=raw):
                cleaned = app.redact_for_log(raw)
                self.assertNotIn("FAKE_SECRET", cleaned)
                self.assertNotIn("FAKE_TOKEN", cleaned)
                self.assertNotIn("FAKE_SESSION", cleaned)
                self.assertNotIn("FAKE_JSESSION", cleaned)
                self.assertNotIn("6632386361646666", cleaned)

    def test_redaction_masks_unlabelled_opaque_blobs(self):
        hex_blob = "a1b2c3d4e5f6a7b8c9d0e1f2a3b4c5d6"
        self.assertNotIn(hex_blob, app.redact_for_log(f"index.jsp?wlan={hex_blob}"))
        self.assertEqual(app.redact_for_log(None), "")
        self.assertEqual(app.redact_for_log(42), "")

    def test_redaction_keeps_the_diagnostic_part_intact(self):
        # Over-redacting would make the logs useless for the analysis this work
        # exists to enable, so the portal's own error text must survive.
        message = ("运营商用户认证失败,失败原因[brac error: "
                   "create session failed(mac collision)!]")
        cleaned = app.redact_for_log(message)
        self.assertIn("mac collision", cleaned)
        self.assertIn("brac error", cleaned)

    def test_describe_portal_body_never_echoes_session_material(self):
        # The real success body carries a per-session userIndex token; logging
        # it verbatim would put session material in every log file.
        body = (b'{"userIndex":"6632386361646666386164353832666639326137616365353931'
                b'3234346533325f31302e3135302e37342e31315f32333132353035303531",'
                b'"result":"success","message":""}')
        described = app.describe_portal_body(body)
        self.assertNotIn("66323863", described)
        self.assertIn("result=success", described)

    def test_unknown_json_fields_contribute_names_only(self):
        # A blacklist cannot cover a field nobody anticipated, so no value from
        # the network is emitted at all — only the shape of the response.
        # These short mixed-case tokens defeat both the name list and the
        # opaque-blob length rules, which is exactly why the whitelist exists.
        body = json.dumps({
            "accesstoken": "Ab3xK9mQ2pL7v",
            "deviceFingerprint": "Zq8Wn4Rt6Yu2",
            "result": "",
            "message": "",
        }).encode()
        described = app.describe_portal_body(body)
        for secret in ("Ab3xK9mQ2pL7v", "Zq8Wn4Rt6yu2", "Zq8Wn4Rt6Yu2"):
            self.assertNotIn(secret, described)
        self.assertIn("accesstoken", described)
        self.assertIn("deviceFingerprint", described)

    def test_unparsed_body_contributes_neither_values_nor_text(self):
        described = app.describe_portal_body(
            b"<html>accesstoken=Ab3xK9mQ2pL7v</html>")
        self.assertNotIn("Ab3xK9mQ2pL7v", described)
        self.assertIn("non-JSON", described)
        self.assertIn("bytes", described)

    def test_json_array_body_reports_its_type_not_its_contents(self):
        described = app.describe_portal_body(b'["Ab3xK9mQ2pL7v"]')
        self.assertNotIn("Ab3xK9mQ2pL7v", described)
        self.assertIn("json list", described)

    def test_url_for_log_keeps_names_and_drops_values(self):
        url = ("http://10.10.200.102/eportal/index.jsp?wlanuserip=Ab3xK9mQ2pL7v"
               "&nasip=f28cadff&ssid=campus")
        rendered = app.url_for_log(url)
        self.assertIn("index.jsp", rendered)
        self.assertIn("wlanuserip", rendered)
        self.assertIn("nasip", rendered)
        for secret in ("Ab3xK9mQ2pL7v", "f28cadff"):
            self.assertNotIn(secret, rendered)
        self.assertEqual(app.url_for_log(""), "")
        self.assertEqual(app.url_for_log("http://h/p"), "http://h/p")

    def test_query_param_names_reports_names_only(self):
        rendered = app.query_param_names("wlanuserip=Ab3xK9mQ2pL7v&nasip=f28cadff")
        self.assertEqual(rendered, "wlanuserip,nasip")
        self.assertNotIn("Ab3xK9m", rendered)
        self.assertEqual(app.query_param_names(""), "(none)")

    def test_phase_buckets_never_exceed_the_total(self):
        # The named phases measure disjoint intervals, so they can never add up
        # to more than the whole attempt. This is a real assertion, unlike the
        # earlier version which compared a sum against a total defined as that
        # same sum (true by construction, so it could not fail).
        with patch.object(app.time, "monotonic", side_effect=lambda: self.clock[0]):
            t = app.TimingRecord()
            self._advance(0.3)
            t.mark_discovery_done()
            self._advance(0.1)
            t.mark_login_started()
            self._advance(0.2)
            t.mark_login_done()
            self._advance(0.4)
            t.mark_verify_done()
            t.mark_finished()
        phase_sum = t.discovery_ms + t.login_post_ms + t.verify_ms
        self.assertLessEqual(phase_sum, t.total_ms + 0.001)
        self.assertGreaterEqual(t.unaccounted_ms, -0.001)
        self.assertAlmostEqual(t.unaccounted_ms, t.total_ms - phase_sum, delta=0.001)
        suffix = app._timing_suffix(t)
        self.assertIn("unaccounted", suffix)
        # The lock is not a phase any more; its wait must not be reported.
        self.assertNotIn("lock", suffix)

    def test_concurrent_attempts_keep_the_accounting_sane(self):
        """Two overlapping attempts must leave the counter clean and every
        record measurable. Overlap detection itself is covered by the
        dedicated overlapping_auth tests; this one is about the accounting
        surviving concurrent use.
        """
        records = []
        holding = threading.Event()
        abort = threading.Event()

        def slow(cfg, timings=None):
            records.append(timings)
            if len(records) == 1:
                holding.set()
                abort.wait(timeout=5)
            return False

        def run(record):
            app.do_auth(self.config, None, record)

        first, second = app.TimingRecord(), app.TimingRecord()
        with patch.object(app, "do_auth_portal_post", side_effect=slow):
            threads = [threading.Thread(target=run, args=(r,))
                       for r in (first, second)]
            threads[0].start()
            self.assertTrue(holding.wait(timeout=5), "holder never entered")
            threads[1].start()
            time.sleep(0.02)
            abort.set()
            for thread in threads:
                thread.join(timeout=10)

        self.assertEqual(app._AUTH_IN_FLIGHT, 0)
        for label, record in (("holder", first), ("waiter", second)):
            with self.subTest(attempt=label):
                self.assertIsNotNone(record.total_ms)
                phase_sum = ((record.discovery_ms or 0) + (record.login_post_ms or 0)
                             + (record.verify_ms or 0))
                self.assertLessEqual(phase_sum, record.total_ms + 0.001)
                self.assertGreaterEqual(record.unaccounted_ms, -0.001)

    def test_lock_wait_is_not_a_reported_phase(self):
        # The auth mutex guards only a counter, so its wait has no analytical
        # value and must not appear as a phase. It is absorbed by the timeline
        # reboot instead, which is why the residual stays tiny in normal runs.
        self.assertFalse(hasattr(app.TimingRecord, "lock_wait_ms"))
        self.assertNotIn("lock", app._timing_suffix(app.TimingRecord()))

    def test_unaccounted_is_none_while_the_record_is_open(self):
        self.assertIsNone(app.TimingRecord().unaccounted_ms)

    def test_discovery_failure_still_closes_the_phase(self):
        # If discovery aborts (index.jsp unreachable), the phase must still be
        # closed — otherwise its elapsed time is reported as "not measured" and
        # is silently attributed to nothing.
        probe = Mock()
        probe.geturl.return_value = self.config["check_url"]
        probe.read.return_value = b"location.href='/eportal/index.jsp?wlanuserip=10.0.0.1'"
        opener = Mock()
        opener.open.side_effect = [probe, OSError("index.jsp unreachable")]
        t = self._record()
        with patch.object(app.urllib.request, "build_opener", return_value=opener):
            ok = app.do_auth_portal_post(self.config, t)
        self.assertFalse(ok)
        self.assertIsNotNone(t.discovery_ms)
        self.assertIsNone(t.login_post_ms)

    def test_discovery_urls_are_redacted(self):
        # index.jsp query strings carry wlanuserip/nasip session parameters, so
        # every discovery log line has to go through the redaction too.
        probe = Mock()
        probe.geturl.return_value = self.config["check_url"]
        probe.read.return_value = (
            b"location.href='/eportal/index.jsp?wlanuserip=b1ec05a1192f2f949193"
            b"c99f3e53a245&wlanacname=27dafb59c00c89cd&nasip=f28cadff8ad582ff'")
        page = Mock()
        page.geturl.return_value = (
            self.config["portal_url"] + "/eportal/index.jsp?wlanuserip="
            "b1ec05a1192f2f949193c99f3e53a245&nasip=f28cadff8ad582ff")
        page.read.return_value = b""
        result = Mock(status=200)
        result.read.return_value = b'{"result":"fail","message":"nope"}'
        opener = Mock()
        opener.open.side_effect = [probe, page, result]
        logged = []
        with patch.object(app, "log", side_effect=lambda msg, level="INFO":
                          logged.append(msg)), \
             patch.object(app.urllib.request, "build_opener", return_value=opener):
            app.do_auth_portal_post(self.config, app.TimingRecord())
        joined = "\n".join(logged)
        self.assertIn("Portal JS redirect", joined)
        self.assertNotIn("b1ec05a1192f2f949193c99f3e53a245", joined)
        self.assertNotIn("f28cadff8ad582ff", joined)
        self.assertNotIn("27dafb59c00c89cd", joined)

    def test_discovery_api_body_is_redacted(self):
        # The device APIs echo session state; it must not be dumped verbatim.
        probe = Mock()
        probe.geturl.return_value = self.config["check_url"]
        probe.read.return_value = b"location.href='/eportal/index.jsp'"
        page = Mock()
        page.geturl.return_value = self.config["portal_url"] + "/eportal/index.jsp"
        page.read.return_value = b""
        api = Mock()
        api.geturl.return_value = self.config["portal_url"] + "/eportal/InterFace.do?method=pageInfo"
        api.read.return_value = (b'{"userIndex":"66323863616466663861643538326666",'
                                b'"token":"FAKE_TOKEN","service":"ok"}')
        login = Mock(status=200)
        login.read.return_value = b'{"result":"fail","message":"nope"}'
        opener = Mock()
        opener.open.side_effect = [probe, page, api, api, login]
        logged = []
        with patch.object(app, "log", side_effect=lambda msg, level="INFO":
                          logged.append(msg)), \
             patch.object(app.urllib.request, "build_opener", return_value=opener):
            app.do_auth_portal_post(self.config, app.TimingRecord())
        joined = "\n".join(logged)
        self.assertNotIn("FAKE_TOKEN", joined)
        self.assertNotIn("66323863616466663861643538326666", joined)

    def test_overlapping_auth_is_measured_not_assumed(self):
        # Two attempts in flight at once is a *definite* self-inflicted cause,
        # so it has to be observed. A single attempt must not report it.
        solo = app.TimingRecord()
        with patch.object(app, "do_auth_portal_post", return_value=False):
            app.do_auth(self.config, None, solo)
        self.assertFalse(solo.overlapping_auth)

    def test_two_concurrent_attempts_are_flagged_as_overlapping(self):
        first = app.TimingRecord()
        second = app.TimingRecord()
        entered = threading.Barrier(2, timeout=5)

        def slow_attempt(cfg, timings=None):
            entered.wait()          # both attempts are now inside do_auth
            return False

        def run(record):
            app.do_auth(self.config, None, record)

        with patch.object(app, "do_auth_portal_post", side_effect=slow_attempt):
            threads = [threading.Thread(target=run, args=(r,))
                       for r in (first, second)]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join(timeout=10)

        # The barrier guarantees both are inside do_auth before either reads the
        # counter, so exactly one of them must observe the other.
        self.assertTrue(first.overlapping_auth or second.overlapping_auth)
        self.assertFalse(first.overlapping_auth and second.overlapping_auth)

    def test_in_flight_counter_returns_to_zero_after_failures(self):
        # A leaked counter would flag every later attempt as overlapping.
        with patch.object(app, "do_auth_portal_post", return_value=False):
            for _ in range(3):
                app.do_auth(self.config, None, app.TimingRecord())
        self.assertEqual(app._AUTH_IN_FLIGHT, 0)

    def test_parse_portal_reply_handles_every_shape(self):
        cases = [
            (b'{"result":"success"}', ("success", "")),
            (b'{"result":"fail","message":"mac collision"}', ("fail", "mac collision")),
            (b"{}", ("", "")),
            (b"[]", ("", "")),
            (b"null", ("", "")),
            (b"<html>not json</html>", ("", "")),
            (b'{"result":123}', ("", "")),
            (b'{"result":"fail","message":null}', ("fail", "")),
        ]
        for body, expected in cases:
            with self.subTest(body=body):
                self.assertEqual(app.parse_portal_reply(body), expected)

    def test_clue_line_reports_observations_not_a_verdict(self):
        with patch.object(app.time, "monotonic", side_effect=lambda: self.clock[0]):
            t = app.TimingRecord()
            t.attempt_index = 2
            t.portal_result = "fail"
            t.portal_message = "brac error: create session failed(mac collision)!"
            t.http_requests = 3
            t.socket_probes = 1
            t.mark_due(scheduled_at=self.clock[0], planned_wait_ms=5000)
            line = t.clue_line()
        self.assertIn("attempt #2", line)
        self.assertIn("portal=fail", line)
        self.assertIn("mac collision", line)
        self.assertIn("http_requests=3", line)
        self.assertIn("socket_probes=1", line)
        self.assertIn("planned_wait=5.0s", line)
        # The plan forbids inferring a cause from these fields, so the line must
        # not claim one. Guard the wording rather than trusting reviewers.
        for forbidden in ("portal_side", "self_inflicted", "残留", "工具制造"):
            self.assertNotIn(forbidden, line)

    def test_clue_line_is_empty_before_an_attempt_is_numbered(self):
        self.assertEqual(app.TimingRecord().clue_line(), "")

    def test_portal_post_records_what_the_portal_said(self):
        probe = Mock()
        probe.geturl.return_value = self.config["check_url"]
        probe.read.return_value = b"location.href='/eportal/index.jsp?wlanuserip=10.0.0.1'"
        page = Mock()
        page.geturl.return_value = self.config["portal_url"] + "/eportal/index.jsp?wlanuserip=10.0.0.1"
        page.read.return_value = b""
        result = Mock(status=200)
        result.read.return_value = (b'{"result":"fail","message":"create session failed'
                                    b'(mac collision)!"}')
        opener = Mock()
        opener.open.side_effect = [probe, page, result]
        t = self._record()
        with patch.object(app.urllib.request, "build_opener", return_value=opener):
            with patch.object(app.urllib.request, "urlopen",
                              side_effect=AssertionError("Unexpected network request")):
                ok = app.do_auth_portal_post(self.config, t)
        self.assertFalse(ok)
        self.assertEqual(t.portal_result, "fail")
        self.assertIn("mac collision", t.portal_message)
        self.assertEqual(t.portal_http_status, 200)
        self.assertEqual(t.http_requests, 3)
        self.assertEqual(t.socket_probes, 0)

    def test_instrumentation_keeps_do_auth_returning_a_plain_bool(self):
        # A truthy tuple would read as success at every existing call site, so
        # the observation hook must not change the return contract.
        with patch.object(app, "do_auth_portal_post", return_value=True), \
             patch.object(app, "check_network", return_value=(True, "OK")):
            ok = app.do_auth(self.config, None, app.TimingRecord())
        self.assertIs(type(ok), bool)
        self.assertTrue(ok)
        with patch.object(app, "do_auth_portal_post", return_value=False):
            failed = app.do_auth(self.config, None, app.TimingRecord())
        self.assertIs(type(failed), bool)
        self.assertFalse(failed)

    def test_verify_attempts_are_counted_per_confirmation_check(self):
        t = app.TimingRecord()
        with patch.object(app, "do_auth_portal_post", return_value=True), \
             patch.object(app, "check_network",
                          side_effect=[(False, "offline"), (False, "offline"), (True, "OK")]), \
             patch.object(app.time, "sleep"):
            self.assertTrue(app.do_auth(self.config, None, t))
        self.assertEqual(t.verify_attempts, 3)
        self.assertIsNotNone(t.verify_ms)

    def test_detection_loop_emits_the_observation_line(self):
        """End-to-end: a real outage must produce a machine-readable A.5 line.

        The loop is driven with synthetic failures and a fast stop so the log
        text itself can be asserted on — that text is the deliverable of
        stage A, and a silent regression there would leave us sampling nothing.
        """
        lines = []
        config = dict(self.config, check_interval_ok=0.01, check_interval_fail=0.01,
                      run_duration_minutes=0)
        stop = threading.Event()
        attempts = []
        real_sleep = app.time.sleep

        def fake_sleep(seconds):
            real_sleep(0.001)

        def auth(cfg, last_auth_time, timings=None):
            attempts.append(timings)
            timings.mark_discovery_done()
            timings.count_request()
            timings.count_request()
            timings.mark_login_done()
            timings.portal_http_status = 200
            timings.portal_result = "fail"
            timings.portal_message = "create session failed(mac collision)!"
            timings.verify_attempts = 1
            timings.mark_verify_done()
            timings.mark_finished()
            if len(attempts) >= 2:
                stop.set()
            return False

        with patch.object(app, "log", side_effect=lambda msg, level="INFO":
                          lines.append(f"[{level}] {msg}")), \
             patch.object(app, "check_network", return_value=(False, "portal injected")), \
             patch.object(app, "do_auth", side_effect=auth), \
             patch.object(app.time, "sleep", side_effect=fake_sleep):
            app.run_detection_loop(config, stop_event=stop)

        self.assertEqual(len(attempts), 2)
        observation_lines = [line for line in lines if "[AUTH] Attempt " in line]
        self.assertEqual(len(observation_lines), 2)
        first = observation_lines[0]
        self.assertIn("attempt #1", first)
        self.assertIn("portal=fail", first)
        self.assertIn("mac collision", first)
        self.assertIn("total ", first)
        self.assertIn("verify ", first)
        self.assertIn("requests=2 socket_probes=0", first)
        # The second attempt must show the interval the loop actually waited.
        self.assertIn("planned_wait=3.0s", observation_lines[1])
        # Clue text must describe, not conclude (see plan A.5).
        self.assertNotIn("残留", first)
        self.assertNotIn("self_inflicted", first)
        # The detection-confirmation cost is now visible instead of implicit.
        self.assertTrue(any("Detection timing:" in line for line in lines))

    def test_single_transient_failure_does_not_poison_the_next_outage(self):
        """A one-off failed probe must not become the start of a later outage.

        Regression: clearing the first-failure stamp only at the end of a
        *confirmed* outage left it behind after a lone failure, so a genuine
        outage later on reported a recovery time inflated by the whole healthy
        stretch in between — and those numbers are what the retry parameters get
        tuned from.

        A virtual clock is used so the healthy stretch is worth hundreds of
        seconds: with the stamp leaking, the reported recovery time lands far
        above the assertion below, so this test actually fails on the old
        behaviour instead of merely documenting the new one.
        """
        lines = []
        config = dict(self.config, check_interval_ok=0.05, check_interval_fail=0.05,
                      run_duration_minutes=0)
        stop = threading.Event()
        # one blip -> a long healthy stretch -> a real outage -> recovery
        outcomes = ([(False, "blip")] + [(True, "OK")] * 12
                    + [(False, "portal injected")] * 2 + [(True, "OK")])
        clock = [1000.0]
        checks = [0]

        def step():
            clock[0] += 0.05
            return clock[0]

        def fake_check(url, timeout, expected=None):
            # Hard cap so a regression can never spin this test forever: if the
            # scenario is consumed the loop is stopped regardless, and the
            # assertions below report the failure instead of hanging.
            checks[0] += 1
            if checks[0] > 60:
                stop.set()
                return True, "OK"
            result = outcomes.pop(0) if outcomes else (True, "OK")
            if not outcomes:
                stop.set()
            return result

        def fake_auth(cfg, last_auth_time, timings=None):
            timings.portal_result = "fail"
            timings.portal_message = "create session failed(mac collision)!"
            timings.mark_discovery_done()
            timings.mark_finished()
            return False

        with patch.object(app, "log", side_effect=lambda msg, level="INFO":
                          lines.append(f"[{level}] {msg}")), \
             patch.object(app, "check_network", side_effect=fake_check), \
             patch.object(app, "do_auth", side_effect=fake_auth), \
             patch.object(app.time, "monotonic", side_effect=step), \
             patch.object(app.time, "sleep", return_value=None):
            app.run_detection_loop(config, stop_event=stop)

        recovery = [line for line in lines if "Recovery observed:" in line]
        self.assertEqual(len(recovery), 1, "expected exactly one confirmed outage")
        match = re.search(r"Recovery observed: (\d+)s", recovery[0])
        self.assertIsNotNone(match, recovery[0])
        # The clock ticks 0.05s per call. The healthy stretch (12 valid probes
        # plus their sleeps) spans >3.5 virtual seconds, while the outage that
        # is actually measured spans about 1s. A leaked stamp therefore pushes
        # this past the bound and FAILS the test, which is what makes it a real
        # guard rather than a description of current behaviour.
        self.assertLess(int(match.group(1)), 2, recovery[0])


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

    def test_writable_program_dir_keeps_config_beside_the_exe(self):
        with tempfile.TemporaryDirectory() as home, \
                tempfile.TemporaryDirectory() as appdata:
            # CONFIG_FILE 也指到空目录：从源码跑时它是"程序目录里没有配置"的旧兜底，
            # 不屏蔽掉就会读到仓库里那份真配置。
            with patch.object(app, "exe_dir", return_value=home), \
                 patch.object(app, "CONFIG_FILE",
                              os.path.join(home, "absent.json")), \
                 patch.dict(os.environ, {"APPDATA": appdata}):
                app._reset_path_cache()
                self.addCleanup(app._reset_path_cache)
                beside = os.path.join(home, app.CONFIG_FILENAME)
                self.assertEqual(app.config_write_path(), beside)
                self.assertEqual(app.find_config_file(), beside)

    def test_protected_program_dir_falls_back_to_appdata(self):
        """受控文件夹访问（勒索软件防护）挡住程序目录时的行为。

        真实症状：放在桌面的 CampusNet.exe 用管理员身份也建不了计划任务，因为
        写不进「桌面」—— 配置和任务的临时文件都撞在这上面。这里断言两件事：配置
        改存 %APPDATA%，而且之后读的也是 %APPDATA% 那份，不会出现"写进去却还在
        读旧文件"的错位。
        """
        with tempfile.TemporaryDirectory() as home, \
                tempfile.TemporaryDirectory() as appdata:
            beside = os.path.join(home, app.CONFIG_FILENAME)
            with open(beside, "w", encoding="utf-8") as f:
                f.write(json.dumps(dict(app.DEFAULT_CONFIG, username="stale")))
            # 只让"程序目录"写不进去，%APPDATA% 照样可写（CFA 就是这么拦的：拦路径）
            unwritable = lambda d: os.path.abspath(d) != os.path.abspath(home)
            with patch.object(app, "exe_dir", return_value=home), \
                 patch.object(app, "CONFIG_FILE",
                              os.path.join(home, "absent.json")), \
                 patch.object(app, "_dir_writable", side_effect=unwritable), \
                 patch.dict(os.environ, {"APPDATA": appdata}):
                app._reset_path_cache()
                self.addCleanup(app._reset_path_cache)
                target = app.config_write_path()
                expected = os.path.join(appdata, "CampusNet", app.CONFIG_FILENAME)
                self.assertEqual(target, expected)
                self.assertTrue(app.save_config(dict(app.DEFAULT_CONFIG, username="fresh"),
                                                path=target))
                self.assertEqual(app.find_config_file(), expected)
                with open(expected, encoding="utf-8") as f:
                    self.assertEqual(json.load(f)["username"], "fresh")
                # 日志也要跟着走：否则静默守护每晚跑完一条记录都不留
                self.assertEqual(app.get_runtime_dir(), os.path.dirname(expected))

    def test_read_only_program_dir_without_appdata_still_reads_beside(self):
        with tempfile.TemporaryDirectory() as home, \
                tempfile.TemporaryDirectory() as appdata:
            beside = os.path.join(home, app.CONFIG_FILENAME)
            with open(beside, "w", encoding="utf-8") as f:
                f.write("{}")
            unwritable = lambda d: os.path.abspath(d) != os.path.abspath(home)
            with patch.object(app, "exe_dir", return_value=home), \
                 patch.object(app, "CONFIG_FILE",
                              os.path.join(home, "absent.json")), \
                 patch.object(app, "_dir_writable", side_effect=unwritable), \
                 patch.dict(os.environ, {"APPDATA": appdata}):
                app._reset_path_cache()
                self.addCleanup(app._reset_path_cache)
                # 只有程序目录里那份配置可读 —— 必须继续读它，不能因为写不进去就丢
                self.assertEqual(app.find_config_file(), beside)
                # 但日志得改存到写得进去的地方
                self.assertEqual(app.get_runtime_dir(),
                                 os.path.join(appdata, "CampusNet"))

    def test_dir_writable_is_false_for_missing_directory(self):
        import uuid
        missing = os.path.join(tempfile.gettempdir(), f"campusnet-{uuid.uuid4().hex}")
        self.assertFalse(app._dir_writable(missing))

    def test_dir_writable_cleans_up_its_probe_file(self):
        with tempfile.TemporaryDirectory() as d:
            self.assertTrue(app._dir_writable(d))
            self.assertEqual(os.listdir(d), [])


class TaskRunTests(unittest.TestCase):
    """`_run_elevated` 的中转文件必须落在 %TEMP%，不能落在程序目录。

    回归守卫：旧实现把 .bat/.out 写在程序目录里，而程序常常就在桌面上 ——
    受控文件夹访问会拦住这次写入，于是明明只是"写不了自己的目录"，却表现成
    "任务创建失败（退出码 -1）"，管理员身份重开也没用。
    """

    def _ui(self):
        import gui_app
        return gui_app.GuiApp.__new__(gui_app.GuiApp)   # 不建窗口

    def test_encoded_command_string_runs_through_the_runner(self):
        """跑一条与 _task_register_command 同形状的命令，验证整条链：

        字符串按空格切分 → subprocess（CREATE_NO_WINDOW + 文件重定向）→ 临时文件
        → 解码 → 子进程退出码。失败时 PowerShell 抛出的报错文本必须能读回来，
        否则弹窗里的「程序回报」又是一句没用的空话。
        """
        import base64
        ui = self._ui()
        prefix = "powershell -NoProfile -ExecutionPolicy Bypass -EncodedCommand "
        encode = lambda s: base64.b64encode(s.encode("utf-16-le")).decode("ascii")

        code, _, out = ui._run_elevated(prefix + encode("exit 0"))
        self.assertEqual(code, 0, out)

        code, _, out = ui._run_elevated(
            prefix + encode("$ErrorActionPreference='Stop'; throw 'boom-1234'"))
        self.assertNotEqual(code, 0)
        self.assertIn("boom-1234", out)

    def test_runs_without_touching_the_program_directory(self):
        missing = os.path.join(tempfile.gettempdir(), "campusnet-does-not-exist")
        runtime_dir = Mock(return_value=missing)
        with patch.object(app, "get_runtime_dir", runtime_dir):
            code, _, out = self._ui()._run_elevated(
                "powershell -NoProfile -Command Write-Output campusnet-ok")
        self.assertEqual(code, 0, out)
        self.assertIn("campusnet-ok", out)
        runtime_dir.assert_not_called()

    def test_exit_code_comes_from_the_child(self):
        code, _, _ = self._ui()._run_elevated(
            "powershell -NoProfile -Command exit 7")
        self.assertEqual(code, 7)

    def test_child_output_survives_the_console_codepage(self):
        """子进程没有控制台，输出按哪个编码落盘由系统区域设置决定，不能读成乱码。"""
        import ctypes
        if ctypes.windll.kernel32.GetOEMCP() != 936:
            self.skipTest("中文系统专用：中文输出在非中文代码页下会退化成 ?")
        _, _, out = self._ui()._run_elevated(
            "powershell -NoProfile -Command Write-Output 中文报错")
        self.assertIn("中文报错", out)

    def test_decode_prefers_utf8_then_falls_back(self):
        import ctypes
        import gui_app
        text = "拒绝访问 (0x80070005)"
        self.assertEqual(gui_app.decode_child_output(text.encode("utf-8")), text)
        # cp936 是中文系统的 OEM 代码页，应当被第二轮接住；别的语言没这个代码页，
        # 只要求它不抛异常。
        if ctypes.windll.kernel32.GetOEMCP() == 936:
            self.assertEqual(gui_app.decode_child_output(text.encode("cp936")), text)
        self.assertEqual(gui_app.decode_child_output(b""), "")


class TrayWorkerTests(unittest.TestCase):
    """托盘的探测线程必须是可选的 —— GUI 传 start_worker=False。

    这是 P1 回归守卫：基类以前无条件启动 worker，GUI 又让 start_monitor() 再起
    一路，于是两个 run_detection_loop 同时探测，而「停止守护」只停得掉界面那一路
    （界面显示"待命中"，后台还在探测与重连）。--silent 不创建托盘，不走这条路。
    """

    def setUp(self):
        self.config = dict(app.DEFAULT_CONFIG, username="student", password="test")

    def _run_tray(self, tray):
        with patch.object(tray, "_create_window"), \
             patch.object(tray, "_create_tray_icon"), \
             patch.object(tray, "_message_loop"), \
             patch.object(tray, "_cleanup"):
            return tray.run()

    def test_start_worker_false_spawns_no_detection_loop(self):
        tray = app.TrayApp(self.config, start_worker=False)
        with patch.object(app, "run_detection_loop") as run:
            self.assertTrue(self._run_tray(tray))
            if tray._worker:
                tray._worker.join(2)
        run.assert_not_called()
        self.assertIsNone(tray._worker)

    def test_default_still_starts_the_worker_for_console_modes(self):
        """--tray / 菜单 [5] 的守护线程本来就是托盘自己的，不能被一起改掉。"""
        tray = app.TrayApp(self.config)
        with patch.object(app, "run_detection_loop") as run:
            self.assertTrue(self._run_tray(tray))
            self.assertIsNotNone(tray._worker)
            tray._worker.join(2)
        run.assert_called_once()

    def test_gui_tray_does_not_start_its_own_worker(self):
        import gui_app
        tray = gui_app.GUITray(self.config, on_open_window=lambda: None,
                               on_exit=lambda reason=None: None)
        self.assertFalse(tray._start_worker)

    def test_window_creation_failure_starts_no_loop_when_start_worker_is_false(self):
        """异常路径同样不能擅自探测：窗口建不出来时 GUI 必须保持"没点就不跑"。"""
        tray = app.TrayApp(self.config, start_worker=False)
        with patch.object(tray, "_create_window",
                          side_effect=RuntimeError("no desktop session")), \
             patch.object(app, "run_detection_loop") as run:
            self.assertFalse(tray.run())
        run.assert_not_called()

    def test_icon_creation_failure_starts_no_loop_when_start_worker_is_false(self):
        tray = app.TrayApp(self.config, start_worker=False)
        with patch.object(tray, "_create_window"), \
             patch.object(tray, "_create_tray_icon",
                          side_effect=RuntimeError("Shell_NotifyIcon denied")), \
             patch.object(app, "run_detection_loop") as run:
            self.assertFalse(tray.run())
        run.assert_not_called()

    def test_console_fallback_still_probes_and_gets_the_stop_event(self):
        """--tray 的守护本来就归托盘管：初始化失败时保留回退，但必须可停止。"""
        tray = app.TrayApp(self.config)
        with patch.object(tray, "_create_window",
                          side_effect=RuntimeError("no desktop session")), \
             patch.object(app, "run_detection_loop") as run:
            self.assertFalse(tray.run())
        run.assert_called_once()
        self.assertIs(run.call_args.kwargs["stop_event"], tray._stop_event)

    def test_console_fallback_also_returns_false_so_callers_know_there_is_no_tray(self):
        tray = app.TrayApp(self.config)
        with patch.object(tray, "_create_window"), \
             patch.object(tray, "_create_tray_icon", side_effect=RuntimeError("denied")), \
             patch.object(app, "run_detection_loop"):
            self.assertFalse(tray.run())


class ElevationOutcomeTests(unittest.TestCase):
    """ShellExecuteW 的判读：>32 成功，≤32 失败且原因在 GetLastError() 里。"""

    def setUp(self):
        import gui_app
        self.gui = gui_app

    def test_return_value_above_32_means_started(self):
        self.assertEqual(self.gui.elevation_outcome(42, 0)[0], "started")
        self.assertEqual(self.gui.elevation_outcome(0x1234, 0)[0], "started")

    def test_cancelled_uac_is_read_from_last_error_not_the_return_value(self):
        # 真实取消：返回值是 ≤32 的失败码，1223 只在 GetLastError() 里。
        # 拿返回值直接跟 1223 比会落到 failed 分支，专门的取消提示就失效了。
        self.assertEqual(self.gui.elevation_outcome(5, self.gui.ERROR_CANCELLED)[0],
                         "cancelled")
        self.assertEqual(self.gui.elevation_outcome(0, self.gui.ERROR_CANCELLED)[0],
                         "cancelled")

    def test_other_failures_stay_generic_and_report_both_codes(self):
        outcome, detail = self.gui.elevation_outcome(2, 5)
        self.assertEqual(outcome, "failed")
        self.assertIn("2", detail)
        self.assertIn("5", detail)

    def test_cancelled_message_is_not_the_generic_failure(self):
        cancelled = self.gui.elevation_outcome(5, self.gui.ERROR_CANCELLED)[1]
        failed = self.gui.elevation_outcome(5, 87)[1]
        self.assertIn("「否」", cancelled)
        self.assertNotEqual(cancelled, failed)


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
        # _is_admin 必须为真：否则会走「先提权重开」的分支，在测试机上真弹 UAC
        self.ui._is_admin = Mock(return_value=True)
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

    def _silent_deploy_stubs(self):
        self.ui.mode_var = Mock()
        self.ui.mode_var.get.return_value = "silent"
        self.ui._config.update(silent_start_time="23:00", silent_run_minutes=30)
        for name in ("_write_config", "_require_credentials", "_explain_elevation"):
            setattr(self.ui, name, Mock(return_value=True))
        self.ui.mode_note = Mock()
        self.ui.root = Mock()
        self.ui._task_register_command = Mock(return_value="command")
        self.ui._run_elevated = Mock(return_value=(0, "command", ""))
        self.ui._task_exists = Mock(return_value=True)

    def test_non_admin_deploy_offers_elevated_restart_before_doing_anything(self):
        """没提权就先别跑注册命令：那条路必定被拒，用户只会看到一句「失败了」。

        回归守卫：旧流程是"就地跑一遍 → 拿到拒绝访问 → 再问要不要提权重开"，
        而界面同时声称"接下来会弹出系统权限窗口"——那句话是假的：`_run_elevated()`
        根本不会触发 UAC，只有 `_self_elevate()` 会。
        """
        self._silent_deploy_stubs()
        self.ui._is_admin = Mock(return_value=False)
        self.ui._self_elevate = Mock(return_value=True)
        with patch.object(self.gui.messagebox, "askokcancel", return_value=True) as ask:
            self.ui._deploy_silent_task()
        ask.assert_called_once()
        self.assertIn("UAC", ask.call_args[0][1])          # 提权窗口这次真的会来
        self.ui._self_elevate.assert_called_once()
        self.ui._run_elevated.assert_not_called()
        self.ui._task_register_command.assert_not_called()

    def test_non_admin_deploy_can_be_cancelled_without_side_effects(self):
        self._silent_deploy_stubs()
        self.ui._is_admin = Mock(return_value=False)
        self.ui._self_elevate = Mock(return_value=False)
        with patch.object(self.gui.messagebox, "askokcancel", return_value=False):
            self.ui._deploy_silent_task()
        self.ui._self_elevate.assert_not_called()
        self.ui._run_elevated.assert_not_called()

    def test_non_admin_remove_cannot_even_try_the_privileged_command(self):
        """取消每天自动守护同样要提权：没提权就别去跑 Unregister-ScheduledTask。"""
        self.ui.mode_note = Mock()
        self.ui.root = Mock()
        self.ui._task_exists = Mock(return_value=True)
        self.ui._is_admin = Mock(return_value=False)
        self.ui._self_elevate = Mock(return_value=True)
        self.ui._run_elevated = Mock()
        with patch.object(self.gui.messagebox, "askokcancel", return_value=True) as ask, \
             patch.object(self.gui.messagebox, "askyesno") as confirm:
            self.ui._remove_silent_task()
        confirm.assert_not_called()
        ask.assert_called_once()
        self.ui._run_elevated.assert_not_called()
        self.ui._self_elevate.assert_called_once()

    def test_tray_that_never_came_up_keeps_the_window_mode_alive(self):
        """托盘建不起来时不能顺手关掉程序，也不能偷偷开始守护。"""
        self.ui._tray = Mock()
        self.ui._do_exit = Mock()
        self.ui.root = Mock()
        self.ui.root.state.return_value = "normal"
        self.ui._handle_message("tray_exited", False)
        self.ui._do_exit.assert_not_called()
        self.assertIsNone(self.ui._tray)

    def test_tray_failure_restores_a_window_that_was_hidden(self):
        """窗口已经缩到托盘、托盘又挂了：必须把窗口找回来。

        回归守卫：只清 _tray 不动窗口，会让程序变成一个"界面上什么都没有、进程
        还在跑"的幽灵 —— 托盘是缩起来的窗口唯一的入口，入口没了就得自己露面。
        """
        self.ui._tray = Mock()
        self.ui._do_exit = Mock()
        self.ui._show_window = Mock()
        self.ui.root = Mock()
        self.ui.root.state.return_value = "withdrawn"      # ✕ 隐藏过
        self.ui._handle_message("tray_exited", False)
        self.ui._show_window.assert_called_once()
        self.ui._do_exit.assert_not_called()
        self.assertIsNone(self.ui._tray)

    def test_minimized_window_also_comes_back(self):
        self.ui._tray = Mock()
        self.ui._do_exit = Mock()
        self.ui._show_window = Mock()
        self.ui.root = Mock()
        self.ui.root.state.return_value = "iconic"
        self.ui._handle_message("tray_exited", False)
        self.ui._show_window.assert_called_once()

    def test_tray_failure_does_not_yank_a_visible_window(self):
        """窗口本来就好好显示着，就别去抢焦点。"""
        self.ui._tray = Mock()
        self.ui._do_exit = Mock()
        self.ui._show_window = Mock()
        self.ui.root = Mock()
        self.ui.root.state.return_value = "normal"
        self.ui._handle_message("tray_exited", False)
        self.ui._show_window.assert_not_called()

    def test_window_state_failure_is_not_fatal(self):
        """state() 抛异常时按"没隐藏"处理，别让恢复逻辑变成新的崩溃点。"""
        self.ui._tray = Mock()
        self.ui._do_exit = Mock()
        self.ui.root = Mock()
        self.ui.root.state.side_effect = RuntimeError("window already destroyed")
        self.ui._handle_message("tray_exited", False)
        self.assertIsNone(self.ui._tray)

    def test_normal_tray_exit_still_closes_the_app(self):
        self.ui._tray = Mock()
        self.ui._do_exit = Mock()
        self.ui._handle_message("tray_exited", True)
        self.ui._do_exit.assert_called_once()

    def test_task_failure_detail_is_written_to_the_log_file(self):
        """失败详情必须落盘到 logs\\：关掉窗口后，这行日志是唯一能要来排障的证据。

        回归守卫：这里曾经用 self._log()，而它只往界面文本框里写、不落盘 ——
        于是在别人电脑上，弹窗一关就什么证据都没了。
        """
        with tempfile.TemporaryDirectory() as run_dir, \
             patch.object(app, "get_runtime_dir", return_value=run_dir), \
             patch.object(self.gui.messagebox, "askyesno", return_value=False):
            self.ui._report_task_failure("任务没有创建成功（脚本退出码 1）。",
                                         "powershell -EncodedCommand xxx",
                                         "拒绝访问 (0x80070005)")
            log_dir = os.path.join(run_dir, "logs")
            files = os.listdir(log_dir)
            self.assertEqual(len(files), 1, files)
            with open(os.path.join(log_dir, files[0]), encoding="utf-8") as f:
                text = f.read()
        self.assertIn("任务没有创建成功（脚本退出码 1）。", text)
        self.assertIn("拒绝访问 (0x80070005)", text)

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
        with patch.object(sys, "argv", ["make_release_zip.py", "1.7.5"]):
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
            with zipfile.ZipFile(os.path.join(staging, "dist", "auto_login_v1.7.5.zip")) as z:
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
