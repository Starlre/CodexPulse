"""Windows integration tests for native tray, Tk visibility and callback threading."""
import os
import tempfile
import threading
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pulse


@unittest.skipUnless(os.name == 'nt', 'Native Windows tray required')
class TrayTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.data_patch = patch.object(pulse, 'DATA', Path(self.temp.name))
        self.data_patch.start()
        self.monitor_patch = patch.object(pulse, 'monitor',
            lambda home, messages, stop, commands: stop.wait())
        self.monitor_patch.start()
        self.app = pulse.Pulse(SimpleNamespace(home=Path(self.temp.name), session='', smoke_test=False))
        self.wait_until(lambda: self.app.tray is not None and self.app.tray.visible)

    def tearDown(self):
        if not self.app.closing:
            # Prevent callbacks leaking between Tk interpreters in this test process.
            for callback in self.app.root.tk.call('after', 'info'):
                self.app.root.after_cancel(callback)
            self.app.close()
        self.monitor_patch.stop()
        self.data_patch.stop()
        self.temp.cleanup()

    def wait_until(self, predicate, timeout=4):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            self.app.root.update()
            if predicate():
                return
            time.sleep(.01)
        self.fail('Timed out waiting for native tray / Tk state')

    def invoke_menu(self, text):
        item = next(item for item in self.app.tray.menu.items if item.text == text)
        thread = threading.Thread(target=lambda: item(self.app.tray))
        thread.start()
        thread.join(timeout=1)
        self.assertFalse(thread.is_alive(), 'Tray callback must not wait on Tk')

    def test_native_menu_restores_window_and_history_without_changing_zoom(self):
        app = self.app
        app.set_zoom(150)
        app.pinned.set(False)
        app.show_history()
        app.root.update()
        app.minimize_to_tray()
        self.assertEqual(app.root.state(), 'withdrawn')
        self.assertEqual(app.history.state(), 'withdrawn')
        self.assertTrue(app.tray.visible)
        self.assertFalse(app.stop.is_set())
        self.invoke_menu('显示悬浮窗')
        self.wait_until(lambda: not app.hidden_to_tray)
        self.assertEqual(app.root.state(), 'normal')
        self.assertEqual(app.history.state(), 'normal')
        self.assertEqual(app.zoom_percent.get(), 150)
        self.assertFalse(bool(app.root.attributes('-topmost')))

    def test_unavailable_icon_keeps_window_accessible(self):
        app = self.app
        tray = app.tray
        app.tray = None
        try:
            with patch.object(pulse.messagebox, 'showwarning') as warning:
                app.minimize_to_tray()
                warning.assert_called_once()
            self.assertFalse(app.hidden_to_tray)
            self.assertEqual(app.root.state(), 'normal')
        finally:
            app.tray = tray

    def test_metrics_messages_processed_while_minimized(self):
        app = self.app
        app.minimize_to_tray()
        app.messages.put(([], 'tray-background-update'))
        self.wait_until(lambda: app.status_note == 'tray-background-update')
        self.assertEqual(app.root.state(), 'withdrawn')

    def test_history_menu_restores_window_before_opening_history(self):
        app = self.app
        app.minimize_to_tray()
        self.invoke_menu('会话与历史记录')
        self.wait_until(lambda: app.history is not None)
        self.assertEqual(app.root.state(), 'normal')
        self.assertEqual(app.history.state(), 'normal')

    def test_quit_from_tray_stops_monitor_and_removes_icon(self):
        app = self.app
        app.minimize_to_tray()
        self.invoke_menu('退出')
        # Drain on Tk's owning thread, exactly as its polling callback does.
        for callback in app.root.tk.call('after', 'info'):
            app.root.after_cancel(callback)
        app.poll_tray_actions()
        self.assertTrue(app.closing)
        self.assertTrue(app.stop.is_set())
        self.assertFalse(app.tray._running)
        app.close()  # Closing is idempotent, including after root destruction.


if __name__ == '__main__':
    unittest.main()
