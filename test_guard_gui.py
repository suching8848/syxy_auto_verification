"""Offline GUI lifecycle checks for a rejected duplicate guard launch."""

import queue
import threading
import unittest
from unittest.mock import Mock, patch

import auto_login as core
import gui_app


class DuplicateGuardGuiTests(unittest.TestCase):
    def setUp(self):
        self.ui = gui_app.GuiApp.__new__(gui_app.GuiApp)
        self.ui._config = dict(core.DEFAULT_CONFIG, username="student", password="test")
        self.ui._q = queue.Queue()
        self.ui._monitor = Mock()
        self.ui._monitor_stop = threading.Event()
        self.ui._monitor_blocked = None
        self.ui._session_start = 123
        self.ui._state = gui_app.STATE_PENDING
        self.ui._set_state = Mock(
            side_effect=lambda state: setattr(self.ui, "_state", state))
        self.ui._log = Mock()
        self.ui._on_status_line = Mock()

    def drain(self):
        while not self.ui._q.empty():
            self.ui._handle_message(*self.ui._q.get_nowait())

    def test_rejected_launch_returns_to_idle_and_can_be_started_again(self):
        event = self.ui._monitor_stop
        with patch.object(core, "run_detection_loop", return_value=False):
            self.ui._monitor_loop(dict(self.ui._config), event)

        # The worker reports through the queue and never touches the UI.
        self.ui._set_state.assert_not_called()
        self.ui._log.assert_not_called()
        self.drain()
        self.assertIsNone(self.ui._monitor)
        self.assertIsNone(self.ui._monitor_stop)
        self.assertIsNone(self.ui._session_start)
        self.assertEqual(self.ui._state, gui_app.STATE_IDLE)
        self.assertIs(self.ui._monitor_blocked, event)
        messages = [call.args[0] for call in self.ui._log.call_args_list]
        self.assertTrue(any("已有校园网守护" in message and "本次未启动" in message
                            for message in messages))
        self.assertNotIn("守护已停止。", messages)

        # A later click is allowed; the refusal applies only to that launch.
        with patch.object(core, "run_detection_loop", return_value=True) as run:
            self.ui.start_monitor()
            self.ui._monitor.join(2)
            self.assertFalse(self.ui._monitor.is_alive())
            self.assertIsNone(self.ui._monitor_blocked)
            self.drain()
        run.assert_called_once()
        self.assertIsNone(self.ui._monitor)
        self.ui._log.assert_any_call("守护已停止。", "dim")

    def test_stale_rejection_and_completion_do_not_change_new_guard(self):
        old = threading.Event()
        current = self.ui._monitor_stop
        worker = self.ui._monitor
        self.ui._handle_message("monitor_blocked", old)
        self.ui._handle_message("monitor_done", old)
        self.assertIs(self.ui._monitor_stop, current)
        self.assertIs(self.ui._monitor, worker)
        self.assertIsNone(self.ui._monitor_blocked)
        self.ui._set_state.assert_not_called()
        self.ui._log.assert_not_called()

    def test_none_result_keeps_existing_mock_and_legacy_completion_behavior(self):
        with patch.object(core, "run_detection_loop", return_value=None):
            self.ui._monitor_loop(dict(self.ui._config), self.ui._monitor_stop)
        self.drain()
        self.assertIsNone(self.ui._monitor_blocked)
        self.assertIsNone(self.ui._monitor)
        self.ui._log.assert_called_once_with("守护已停止。", "dim")

    def test_lock_error_is_logged_in_visible_language_and_still_finishes(self):
        event = self.ui._monitor_stop
        with patch.object(core, "run_detection_loop", side_effect=OSError("mutex denied")), \
                patch.object(core, "log") as log:
            self.ui._monitor_loop(dict(self.ui._config), event)
        log.assert_called_once_with("无法启动守护：mutex denied", "ERROR")
        self.assertEqual(self.ui._q.get_nowait(), ("monitor_done", event))

    def test_guard_refusal_log_is_readable_and_marked_as_warning(self):
        self.assertEqual(gui_app.humanize(
            "[2026-10-18 12:00:00] [GUARD] "
            "Another guard is already running; this launch was skipped"),
            "已有校园网守护正在运行，本次未启动。")
        self.assertEqual(gui_app.GuiApp._tag_for("GUARD"), "warn")


if __name__ == "__main__":
    unittest.main()
