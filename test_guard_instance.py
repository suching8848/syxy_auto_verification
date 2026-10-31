"""Offline regression tests for the shared CampusNet guard lease.

All names are unique test names: these tests never acquire the production
mutex, load credentials, change adapters, or submit a network request.
"""

import ctypes
import json
import os
from pathlib import Path
import queue
import shutil
import subprocess
import sys
import tempfile
import threading
import unittest
import uuid
from unittest.mock import Mock, patch

import auto_login as app


_CHILD = r"""
import importlib.util
import json
import sys

spec = importlib.util.spec_from_file_location("guard_test_copy", sys.argv[1])
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)
module.GUARD_MUTEX_NAME = sys.argv[2]
lock = module.GuardInstanceLock()
acquired = lock.acquire()
print(json.dumps({"acquired": acquired}), flush=True)
if acquired and sys.argv[3] == "hold":
    sys.stdin.readline()
lock.release()
"""


@unittest.skipUnless(os.name == "nt", "CampusNet guard uses a Win32 mutex")
class GuardInstanceTests(unittest.TestCase):
    def setUp(self):
        self.name = "Global\\CampusNet.Guard.Test." + uuid.uuid4().hex
        self.config = dict(app.DEFAULT_CONFIG, username="test", password="test")
        self.log = self.enterContext(patch.object(app, "log"))
        self.enterContext(patch.object(app, "GUARD_MUTEX_NAME", self.name))
        for method in ("check_network", "do_auth"):
            self.enterContext(patch.object(
                app, method, side_effect=AssertionError("Unexpected network/auth call")))
        self.enterContext(patch.object(
            app.urllib.request, "urlopen",
            side_effect=AssertionError("Unexpected network request")))

    def lock(self):
        lock = app.GuardInstanceLock(self.name)
        self.addCleanup(lock.release)
        return lock

    def start_child(self, *, hold=False, module_path=None):
        child = subprocess.Popen(
            [sys.executable, "-c", _CHILD,
             str(module_path or Path(app.__file__).resolve()), self.name,
             "hold" if hold else "try"],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.PIPE, text=True,
            creationflags=subprocess.CREATE_NO_WINDOW,
        )
        self.addCleanup(self.finish_child, child)
        return child

    @staticmethod
    def finish_child(child):
        """Reap every child even when a failed assertion leaves it holding a lock."""
        if child.poll() is None:
            child.kill()
        try:
            child.communicate(timeout=5)
        except subprocess.TimeoutExpired:
            child.kill()
            child.communicate(timeout=5)

    def child_result(self, child):
        output, error = child.communicate(timeout=10)
        self.assertEqual(child.returncode, 0, error)
        return json.loads(output)

    def wait_holder(self, child):
        # readline itself has no timeout. A daemon reader lets the test impose
        # a deadline and the registered cleanup always kills/reaps the child.
        received = queue.Queue()
        reader = threading.Thread(
            target=lambda: received.put(child.stdout.readline()), daemon=True)
        reader.start()
        try:
            line = received.get(timeout=10)
        except queue.Empty:
            self.fail("Child did not report its mutex acquisition within 10 seconds")
        reader.join(timeout=1)
        if not line:
            _output, error = child.communicate(timeout=5)
            self.fail("Child exited before acquiring its mutex: " + error)
        self.assertTrue(json.loads(line)["acquired"])

    def test_same_thread_reentry_is_rejected_without_releasing_owner(self):
        owner, contender = self.lock(), self.lock()
        self.assertTrue(owner.acquire())
        self.assertFalse(owner.acquire())
        self.assertFalse(contender.acquire())
        contender.release()  # An unsuccessful contender must not release owner.
        self.assertFalse(contender.acquire())
        owner.release()
        owner.release()
        self.assertTrue(contender.acquire())

    def test_other_thread_in_same_process_is_rejected(self):
        owner = self.lock()
        self.assertTrue(owner.acquire())
        outcome = []
        errors = []

        def compete():
            contender = app.GuardInstanceLock(self.name)
            try:
                outcome.append(contender.acquire())
            except BaseException as error:
                errors.append(error)
            finally:
                contender.release()

        thread = threading.Thread(target=compete, daemon=True)
        thread.start()
        thread.join(timeout=5)
        self.assertFalse(thread.is_alive(), "Duplicate guard acquire blocked")
        self.assertEqual(errors, [])
        self.assertEqual(outcome, [False])

    def test_different_test_names_are_independent(self):
        owner = self.lock()
        independent = app.GuardInstanceLock(self.name + ".other")
        self.addCleanup(independent.release)
        self.assertTrue(owner.acquire())
        self.assertTrue(independent.acquire())

    def test_loop_forwards_options_returns_true_and_releases(self):
        stop, callback = threading.Event(), Mock()
        calls = []

        def run(config, stop_event=None, status_callback=None, duration_override=None):
            calls.append((config, stop_event, status_callback, duration_override))
            contender = self.lock()
            self.assertFalse(contender.acquire())

        with patch.object(app, "_run_detection_loop", side_effect=run):
            self.assertIs(app.run_detection_loop(
                self.config, stop_event=stop, status_callback=callback,
                duration_override=3), True)
        self.assertEqual(calls, [(self.config, stop, callback, 3)])
        self.assertTrue(self.lock().acquire())

    def test_busy_loop_returns_false_without_probing_or_authenticating(self):
        owner = self.lock()
        self.assertTrue(owner.acquire())
        stop = threading.Event()
        with patch.object(app, "_run_detection_loop") as run:
            self.assertIs(app.run_detection_loop(self.config, stop_event=stop), False)
        run.assert_not_called()
        self.assertFalse(stop.is_set())
        self.assertTrue(self.log.called, "Busy guard must explain why it skipped")
        self.assertFalse(self.lock().acquire())

    def test_rejected_loop_leaves_running_guard_untouched(self):
        entered, finish = threading.Event(), threading.Event()
        owner_stop = threading.Event()
        calls, results, errors = [], [], []

        def run(config, stop_event=None, status_callback=None, duration_override=None):
            calls.append(stop_event)
            entered.set()
            if not finish.wait(timeout=10):
                raise AssertionError("Owner guard was not released by test")

        def own():
            try:
                results.append(app.run_detection_loop(
                    self.config, stop_event=owner_stop))
            except BaseException as error:
                errors.append(error)

        with patch.object(app, "_run_detection_loop", side_effect=run):
            thread = threading.Thread(target=own, daemon=True)
            thread.start()
            try:
                self.assertTrue(entered.wait(timeout=5))
                self.assertIs(app.run_detection_loop(self.config), False)
                self.assertTrue(thread.is_alive())
                self.assertFalse(owner_stop.is_set())
                self.assertEqual(calls, [owner_stop])
            finally:
                finish.set()
                thread.join(timeout=5)
        self.assertFalse(thread.is_alive())
        self.assertEqual(errors, [])
        self.assertEqual(results, [True])
        self.assertTrue(self.lock().acquire())

    def test_loop_releases_on_exception_and_keyboard_interrupt(self):
        for error in (RuntimeError("offline test failure"), KeyboardInterrupt()):
            with self.subTest(error=type(error).__name__):
                with patch.object(app, "_run_detection_loop", side_effect=error):
                    with self.assertRaises(type(error)):
                        app.run_detection_loop(self.config)
                replacement = self.lock()
                self.assertTrue(replacement.acquire())
                replacement.release()

    def test_lock_error_is_logged_and_never_runs_unlocked(self):
        failing_lock = Mock()
        failing_lock.acquire.side_effect = OSError(5, "Offline access-denied fixture")
        with patch.object(app, "GuardInstanceLock", return_value=failing_lock), \
             patch.object(app, "_run_detection_loop") as run:
            with self.assertRaises(OSError):
                app.run_detection_loop(self.config)
        run.assert_not_called()
        self.assertTrue(self.log.called, "A mutex failure must be visible in logs")

    def test_mutex_creation_failure_does_not_leave_local_gate_locked(self):
        failed = self.lock()
        with patch.object(failed, "_create_mutex", side_effect=OSError(
                5, "Offline creation-failure fixture")):
            with self.assertRaises(OSError):
                failed.acquire()
        failed.release()
        replacement = self.lock()
        self.assertTrue(replacement.acquire(), "Creation failure leaked the local gate")
        replacement.release()
        self.assertTrue(failed.acquire(), "The failed instance could not be retried")

    def test_failed_wait_closes_handle_and_allows_same_name_retry(self):
        failed = self.lock()
        handle = 0x12345678
        kernel32 = Mock()
        kernel32.WaitForSingleObject.return_value = 0xFFFFFFFF  # WAIT_FAILED
        kernel32.CloseHandle.return_value = True

        def created_handle():
            failed._kernel32 = kernel32
            return handle

        with patch.object(failed, "_create_mutex", side_effect=created_handle), \
             patch.object(ctypes, "get_last_error", return_value=5):
            with self.assertRaises(OSError):
                failed.acquire()
        kernel32.WaitForSingleObject.assert_called_once_with(handle, 0)
        kernel32.CloseHandle.assert_called_once_with(handle)
        kernel32.ReleaseMutex.assert_not_called()
        failed.release()
        kernel32.CloseHandle.assert_called_once_with(handle)
        replacement = self.lock()
        self.assertTrue(replacement.acquire(), "Failed wait leaked the local gate")
        replacement.release()
        self.assertTrue(failed.acquire(), "The failed instance could not be retried")

    def test_separate_process_cannot_acquire_until_owner_releases(self):
        owner = self.lock()
        self.assertTrue(owner.acquire())
        self.assertEqual(self.child_result(self.start_child()), {"acquired": False})
        owner.release()
        self.assertEqual(self.child_result(self.start_child()), {"acquired": True})

    def test_separate_process_owner_blocks_parent_and_releases_cleanly(self):
        holder = self.start_child(hold=True)
        self.wait_holder(holder)
        contender = self.lock()
        self.assertFalse(contender.acquire())
        _output, error = holder.communicate(input="release\n", timeout=10)
        self.assertEqual(holder.returncode, 0, error)
        self.assertTrue(contender.acquire())

    def test_killed_owner_can_be_replaced_without_stale_lock(self):
        holder = self.start_child(hold=True)
        self.wait_holder(holder)
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.OpenMutexW.argtypes = [ctypes.c_uint32, ctypes.c_int, ctypes.c_wchar_p]
        kernel32.OpenMutexW.restype = ctypes.c_void_p
        kernel32.CloseHandle.argtypes = [ctypes.c_void_p]
        kernel32.CloseHandle.restype = ctypes.c_int
        # Preserve the kernel object after the owner dies, exercising the
        # abandoned-mutex case rather than just creating a fresh mutex.
        handle = kernel32.OpenMutexW(0x00100000, False, self.name)  # SYNCHRONIZE
        self.assertTrue(handle, "Could not preserve the live test mutex")
        try:
            holder.kill()
            holder.communicate(timeout=10)
            self.assertTrue(self.lock().acquire())
        finally:
            kernel32.CloseHandle(handle)

    def test_copied_module_in_another_directory_uses_the_same_mutex(self):
        owner = self.lock()
        self.assertTrue(owner.acquire())
        with tempfile.TemporaryDirectory(prefix="campusnet_guard_test_") as directory:
            copied_module = Path(directory) / "another_auto_login.py"
            shutil.copyfile(app.__file__, copied_module)
            child = self.start_child(module_path=copied_module)
            self.assertEqual(self.child_result(child), {"acquired": False})
            owner.release()
            child = self.start_child(module_path=copied_module)
            self.assertEqual(self.child_result(child), {"acquired": True})

    def assert_silent_lock_error_returns_failure(self, start_time):
        config = dict(self.config, auth_method="portal_post",
                      silent_start_time=start_time)
        self.assertFalse(app._need_setup(config))
        with patch.object(app, "load_config", return_value=config), \
             patch.object(app, "clean_old_logs"), \
             patch.object(app, "run_detection_loop", side_effect=OSError(
                 5, "Offline access-denied fixture")) as run, \
             patch.object(app, "notify_setup_required") as notify, \
             patch.object(app, "TrayApp") as tray, \
             patch("tkinter.Tk") as window, \
             patch("tkinter.messagebox.showerror") as dialog, \
             patch("builtins.input", side_effect=AssertionError(
                 "Silent guard must not request terminal input")), \
             patch.object(app.time, "sleep", side_effect=AssertionError(
                 "An interactive --now launch must not wait")):
            self.assertEqual(app.run_silent_mode(
                run_minutes=1, interactive_launch=True), 1)
        run.assert_called_once_with(config, duration_override=1)
        notify.assert_not_called()
        tray.assert_not_called()
        window.assert_not_called()
        dialog.assert_not_called()

    def test_silent_fallback_lock_error_returns_failure_without_ui(self):
        self.assert_silent_lock_error_returns_failure("")

    def test_silent_window_lock_error_returns_failure_without_ui(self):
        self.assert_silent_lock_error_returns_failure("12:00")


if __name__ == "__main__":
    unittest.main()
