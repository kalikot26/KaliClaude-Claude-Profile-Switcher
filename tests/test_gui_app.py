from __future__ import annotations

import importlib
import queue
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

import gui.app as app_module
from gui.desktop_backend import LaunchResult, PendingLogin, ProfileState, SwitchResult, SyncReport


class ImmediateThread:
    def __init__(self, *, target, daemon=True):
        self.target = target

    def start(self):
        self.target()


class GuiContractTests(unittest.TestCase):
    def make_app(self):
        app = object.__new__(app_module.App)
        app.root = Mock()
        app._profiles = []
        app._sel = -1
        app._q = queue.Queue()
        app._busy = False
        app._usage_busy = False
        app._startup_error = ""
        app._claude_up = False
        app._claude_detection_error = False
        app._btn_usage = Mock()
        app._btn_switch = Mock()
        app._set_status = Mock()
        app._refresh = Mock()
        return app

    def profile(self, *, active=True, state=ProfileState.ACTIVE):
        return app_module.Profile(
            "alpha", "Alpha", "", 1.0, "hash", active, True, state=state
        )

    def test_headless_import_does_not_construct_tk_root(self) -> None:
        with patch("tkinter.Tk") as root_constructor:
            importlib.reload(app_module)
        root_constructor.assert_not_called()

    def test_startup_audit_failure_blocks_prepare_action(self) -> None:
        app = self.make_app()
        app._startup_error = "fixture audit failure"
        backend = Mock()

        with patch.object(app_module, "_desktop_backend", return_value=backend), patch.object(
            app_module.messagebox, "showerror"
        ) as showerror:
            app._on_prepare_login()

        backend.begin_new_login.assert_not_called()
        showerror.assert_called_once()

    def test_prepare_queues_actual_pending_root_and_managed_launch_result(self) -> None:
        app = self.make_app()
        pending = PendingLogin("pending-fixture", Path("C:/fixture/pending-fixture"))
        launch = LaunchResult(True, pending.name, pending.user_data_dir)
        backend = Mock()
        backend.begin_new_login.return_value = pending
        backend.launch_active.return_value = launch

        with patch.object(app_module, "_desktop_backend", return_value=backend), patch.object(
            app_module.messagebox, "askyesno", return_value=True
        ), patch.object(app_module.threading, "Thread", ImmediateThread):
            app._on_prepare_login()

        self.assertEqual(("prep_ok", (pending, launch)), app._q.get_nowait())
        backend.begin_new_login.assert_called_once_with()
        backend.launch_active.assert_called_once_with()
        self.assertFalse(hasattr(backend, "prepare_new_login") and backend.prepare_new_login.called)

    def test_save_and_verify_use_schema_three_backend_contracts(self) -> None:
        app = self.make_app()
        app._profiles = [self.profile()]
        app._sel = 0
        backend = Mock()
        backend.finalize_current.return_value = SimpleNamespace(name="alpha")
        backend.verify_profile.return_value = SimpleNamespace(name="alpha")
        dialog = SimpleNamespace(result=("alpha", "Alpha", "note"), top=Mock())

        with patch.object(app_module, "_desktop_backend", return_value=backend), patch.object(
            app_module, "SaveDialog", return_value=dialog
        ), patch.object(app_module.messagebox, "askyesno", return_value=True), patch.object(
            app_module.threading, "Thread", ImmediateThread
        ):
            app._on_save_current()
            self.assertEqual(("save_ok", "alpha"), app._q.get_nowait())
            app._busy = False
            app._on_update()
            self.assertEqual(("verify_ok", "alpha"), app._q.get_nowait())

        backend.finalize_current.assert_called_once_with("alpha", "Alpha", "note")
        backend.verify_profile.assert_called_once_with("alpha")

    def test_switch_result_relaunch_flag_is_honored_and_history_warning_is_separate(self) -> None:
        app = self.make_app()
        backend = Mock()
        backend.launch_active.return_value = LaunchResult(True, "alpha", Path("C:/fixture/alpha"))
        result = SwitchResult(
            True,
            "alpha",
            history=SyncReport(False, message="fixture history failure"),
            should_relaunch=True,
        )

        with patch.object(app_module, "_desktop_backend", return_value=backend), patch.object(
            app_module.messagebox, "showwarning"
        ) as warning:
            app._handle_result("switch_ok", result)

        backend.launch_active.assert_called_once_with()
        warning.assert_called_once()

        backend.reset_mock()
        app._handle_result("switch_ok", SwitchResult(True, "beta", should_relaunch=False))
        backend.launch_active.assert_not_called()

    def test_tick_worker_only_queues_and_ui_handler_schedules_next_tick(self) -> None:
        app = self.make_app()
        backend = Mock()
        backend.desktop_running.return_value = True
        app._apply_tick = Mock()

        with patch.object(app_module, "_desktop_backend", return_value=backend), patch.object(
            app_module.threading, "Thread", ImmediateThread
        ):
            app._tick()

        self.assertEqual(("tick", (True, False)), app._q.get_nowait())
        app.root.after.assert_not_called()
        app._handle_result("tick", (True, False))
        app._apply_tick.assert_called_once_with(True, False)
        app.root.after.assert_called_once_with(5000, app._tick)

    def test_usage_refresh_reads_only_backend_active_root(self) -> None:
        app = self.make_app()
        app._profiles = [self.profile()]
        app._sel = 0
        active_root = Path("C:/fixture/active")
        backend = Mock()
        backend.active_user_data_dir.return_value = active_root

        with patch.object(app_module, "_desktop_backend", return_value=backend), patch.object(
            app_module, "_CRYPTO_OK", True
        ), patch.object(
            app_module, "_usage_via_cookies", return_value={"fetched": 1.0}
        ) as usage, patch.object(app_module.threading, "Thread", ImmediateThread):
            app._on_refresh_usage()

        usage.assert_called_once_with(active_root)
        self.assertEqual("usage_ok", app._q.get_nowait()[0])


if __name__ == "__main__":
    unittest.main()
