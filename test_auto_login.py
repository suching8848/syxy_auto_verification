import threading
import unittest
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
        self.assertEqual(len(attempts), 3)
        self.assertGreaterEqual(attempts[1] - attempts[0], 30)
        self.assertGreaterEqual(attempts[2] - attempts[1], 60)


if __name__ == "__main__":
    unittest.main()
