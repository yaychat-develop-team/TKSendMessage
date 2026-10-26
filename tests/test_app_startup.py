import importlib
import json
import os
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
from PySide6.QtCore import QCoreApplication, QEvent, QTimer
from PySide6.QtGui import QPixmap
from PySide6.QtWidgets import QApplication
from loguru import logger

from desktop.main_window import MainWindow
from desktop.paths import RuntimePaths
from tiktok_live.errors import BrowserError


class ApplicationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.qt = QApplication.instance() or QApplication([])

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        module = importlib.import_module('app')
        from desktop.single_instance import SingleInstance
        instance_patch = patch.object(module, 'SingleInstance', side_effect=lambda: SingleInstance(self.root / 'instance'))
        instance_patch.start()
        self.addCleanup(instance_patch.stop)
        self.paths = RuntimePaths.from_application(self.root)
        self.window = MainWindow(self.paths)
        self.window.show()
        self.release = threading.Event()
        self.qt.processEvents()

    def tearDown(self):
        self.release.set()
        self.window.request_stop()
        self.pump_until(lambda: not self.window.is_busy)
        self.window.close()
        self.window.deleteLater()
        QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
        self.qt.processEvents()
        self.tmp.cleanup()

    def pump_until(self, predicate, timeout=2):
        deadline = time.monotonic() + timeout
        while not predicate() and time.monotonic() < deadline:
            self.qt.processEvents()
            time.sleep(.002)
        self.qt.processEvents()
        self.assertTrue(predicate(), 'bounded Qt operation did not finish')

    def start_blocked_login(self):
        entered = threading.Event()
        def login(path, **kwargs):
            entered.set()
            self.release.wait(2)
            raise BrowserError('login_cancelled')
        boundary = patch('desktop.workers.login_new_account', side_effect=login)
        boundary.start()
        self.addCleanup(boundary.stop)
        self.window.add_account()
        self.pump_until(entered.is_set)

    def test_shutdown_budget_keeps_active_thread_owned_until_it_finishes(self):
        self.start_blocked_login()
        thread = next(iter(self.window._threads))
        started = time.monotonic()
        completed = self.window.shutdown(timeout_ms=30)
        self.assertFalse(completed)
        self.assertLess(time.monotonic() - started, .3)
        self.assertTrue(self.window._cancel_event.is_set())
        self.assertIsNone(self.window._log_handler)
        self.assertIs(thread.parent(), self.window)
        self.assertTrue(thread.isRunning())
        self.assertTrue(self.window.is_busy)
        self.window.add_account()
        self.assertEqual(len(self.window._threads), 1)
        self.release.set()
        self.pump_until(lambda: not self.window.is_busy)
        self.assertFalse(self.window.isVisible())

    def test_close_request_stops_resources_even_when_worker_has_not_finished(self):
        self.start_blocked_login()
        self.window.shutdown_timeout_ms = 30
        started = time.monotonic()
        closed = self.window.close()
        self.assertFalse(closed)
        self.assertLess(time.monotonic() - started, .3)
        self.assertIsNone(self.window._log_handler)
        self.assertTrue(self.window.isVisible())

    def test_cooperative_shutdown_finishes_without_gui_processing_worker_signals(self):
        entered = threading.Event()
        cancelled = threading.Event()
        def login(path, **kwargs):
            entered.set()
            if kwargs['cancel_event'].wait(1):
                cancelled.set()
            raise BrowserError('login_cancelled')
        with patch('desktop.workers.login_new_account', side_effect=login):
            self.window.add_account()
            self.pump_until(entered.is_set)
            self.assertTrue(self.window.shutdown(timeout_ms=500))
            self.assertTrue(cancelled.is_set())
            self.assertFalse(self.window.is_busy)
            self.window.close()
            self.qt.processEvents()

    def test_late_queued_logs_do_not_write_after_shutdown(self):
        logger.info('queued-before-shutdown')
        self.assertTrue(self.window.shutdown(timeout_ms=30))
        self.qt.processEvents()
        logger.info('after-shutdown')
        self.qt.processEvents()
        self.assertNotIn('queued-before-shutdown', self.window.log_text.toPlainText())
        self.assertNotIn('after-shutdown', self.window.log_text.toPlainText())
        self.assertTrue(self.window.shutdown(timeout_ms=30))

    def test_main_creates_runtime_uses_bundle_and_runs_real_qt_event_loop(self):
        self.window.close()
        module = importlib.import_module('app')
        browser = self.root / 'browser' / 'chromium' / 'chrome.exe'
        browser.parent.mkdir(parents=True)
        browser.touch()
        icon = self.root / 'icon.png'
        pixmap = QPixmap(8, 8)
        pixmap.fill()
        self.assertTrue(pixmap.save(str(icon)))
        observed = []
        original_hook = sys.excepthook
        real_paths = RuntimePaths.from_application
        class StartupWindow(MainWindow):
            def show(inner):
                super().show()
                observed.append((inner.paths, inner.isVisible(), QApplication.applicationName(),
                                 inner.windowIcon().isNull()))
                QTimer.singleShot(0, inner.close)
        with patch.object(module.RuntimePaths, 'from_application', side_effect=lambda: real_paths(self.root)), \
                patch.object(module, 'MainWindow', StartupWindow), patch.dict(os.environ):
            result = module.main(['desktop-test'])
            self.assertEqual(os.environ['TIKTOK_BROWSER_EXECUTABLE'], str(browser))
        self.assertEqual(result, 0)
        paths, visible, name, icon_missing = observed[0]
        self.assertEqual(paths.browser_executable, browser)
        self.assertTrue(all(p.is_dir() for p in (paths.account_store.parent, paths.profile_root,
                                               paths.report_dir, paths.log_dir)))
        self.assertTrue(visible)
        self.assertEqual(name, 'TKChatWave')
        self.assertFalse(icon_missing)
        self.assertIs(sys.excepthook, original_hook)

    def test_smoke_mode_initializes_then_exits_without_business_actions(self):
        self.window.close()
        module = importlib.import_module('app')
        real_paths = RuntimePaths.from_application
        browser = self.root / 'browser/chromium/chrome.exe'
        browser.parent.mkdir(parents=True)
        browser.touch()
        with patch.object(module.RuntimePaths, 'from_application', side_effect=lambda: real_paths(self.root)), \
                patch('desktop.workers.login_new_account', side_effect=AssertionError('unexpected login')), \
                patch('desktop.workers.run_parallel_targets', side_effect=AssertionError('unexpected send')):
            # A deadline ensures a missing smoke branch fails instead of hanging.
            deadline = QTimer()
            deadline.setSingleShot(True)
            deadline.timeout.connect(lambda: self.qt.exit(91))
            deadline.start(1500)
            try:
                self.assertEqual(module.main(['desktop-test', '--smoke-test']), 0)
            finally:
                deadline.stop()
        result = json.loads((self.paths.runtime_dir / 'smoke-result.json').read_text(encoding='utf-8'))
        self.assertTrue(result['window_visible'])
        self.assertTrue(result['bundled_browser'])
        self.assertTrue(result['empty_accounts'])
        self.assertTrue(result['dependencies_loaded'])
        self.assertFalse(self.paths.account_store.exists())

    def test_exception_logging_does_not_expose_exception_credentials(self):
        self.window.close()
        module = importlib.import_module('app')
        real_paths = RuntimePaths.from_application
        original_hook = sys.excepthook
        class StartupWindow(MainWindow):
            def show(inner):
                super().show()
                def fail_then_close():
                    exc = RuntimeError('Cookie: synthetic-secret')
                    sys.excepthook(type(exc), exc, None)
                    inner.close()
                QTimer.singleShot(0, fail_then_close)
        with patch.object(module.RuntimePaths, 'from_application', side_effect=lambda: real_paths(self.root)), \
                patch.object(module, 'MainWindow', StartupWindow):
            self.assertEqual(module.main(['desktop-test']), 0)
        output = ''.join(p.read_text(encoding='utf-8') for p in self.paths.log_dir.glob('*.log'))
        self.assertIn('RuntimeError', output)
        self.assertNotIn('synthetic-secret', output)
        self.assertIs(sys.excepthook, original_hook)

    def test_application_quit_waits_for_worker_without_destroying_it(self):
        # A subprocess exercises the true application quit event with a deadline.
        script = r'''
import tempfile, threading
from pathlib import Path
from unittest.mock import patch
from PySide6.QtCore import QTimer
from PySide6.QtWidgets import QApplication
import app
from desktop.main_window import MainWindow
from desktop.paths import RuntimePaths
from tiktok_live.errors import BrowserError
root = tempfile.TemporaryDirectory()
from desktop.single_instance import SingleInstance
app.SingleInstance = lambda: SingleInstance(Path(root.name) / 'instance')
app.show_startup_error = lambda classification: None
real_paths = RuntimePaths.from_application
release = threading.Event()
entered = threading.Event()
windows = []
def login(path, **kwargs):
    entered.set()
    assert release.wait(20)
    assert kwargs['cancel_event'].is_set()
    raise BrowserError('login_cancelled')
class Window(MainWindow):
    def show(self):
        super().show()
        windows.append(self)
        self.shutdown_timeout_ms = 25
        self.add_account()
        def quit_when_running():
            if not entered.is_set():
                QTimer.singleShot(5, quit_when_running)
                return
            QApplication.instance().quit()
            QTimer.singleShot(75, release.set)
        QTimer.singleShot(5, quit_when_running)
with patch.object(app.RuntimePaths, 'from_application', side_effect=lambda: real_paths(Path(root.name))), patch.object(app, 'MainWindow', Window), patch('desktop.workers.login_new_account', side_effect=login):
    assert app.main(['quit-test']) == 0
assert release.is_set()
assert not windows[0].is_busy
print('safe quit')
'''
        result = subprocess.run([sys.executable, '-X', 'faulthandler', '-c', script],
                                capture_output=True, text=True, timeout=30)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn('safe quit', result.stdout)
        direct_exit = script.replace('QApplication.instance().quit()',
                                     'QApplication.instance().exit(7)').replace(
                                     "app.main(['quit-test']) == 0", "app.main(['quit-test']) == 7")
        result = subprocess.run([sys.executable, '-X', 'faulthandler', '-c', direct_exit],
                                capture_output=True, text=True, timeout=30)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn('safe quit', result.stdout)
        quick_exit = direct_exit.replace('QApplication.instance().exit(7)',
                                         'release.set()\n            QApplication.instance().exit(7)')
        result = subprocess.run([sys.executable, '-X', 'faulthandler', '-c', quick_exit],
                                capture_output=True, text=True, timeout=30)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn('safe quit', result.stdout)
        failed_start = "assert entered.wait(1)\n        QTimer.singleShot(75, release.set)\n        raise RuntimeError('Cookie: synthetic-secret')".join(
            script.rsplit('QTimer.singleShot(5, quit_when_running)', 1)).replace(
            "app.main(['quit-test']) == 0", "app.main(['quit-test']) == 1")
        result = subprocess.run([sys.executable, '-X', 'faulthandler', '-c', failed_start],
                                capture_output=True, text=True, timeout=30)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn('safe quit', result.stdout)
        self.assertNotIn('synthetic-secret', result.stderr)

    def test_startup_failure_before_visible_exits_after_worker_cleanup(self):
        script = r"""
import tempfile, threading
from pathlib import Path
from unittest.mock import patch
from PySide6.QtCore import QTimer
from PySide6.QtWidgets import QApplication
import app
from desktop.main_window import MainWindow
from desktop.paths import RuntimePaths
from tiktok_live.errors import BrowserError
root = tempfile.TemporaryDirectory()
from desktop.single_instance import SingleInstance
app.SingleInstance = lambda: SingleInstance(Path(root.name) / 'instance')
app.show_startup_error = lambda classification: None
real_paths = RuntimePaths.from_application
application = QApplication([])
entered = threading.Event()
release = threading.Event()
watchdog = []
windows = []
def login(path, **kwargs):
    entered.set()
    assert kwargs['cancel_event'].wait(5)
    assert release.wait(5)
    raise BrowserError('login_cancelled')
class HiddenWindow(MainWindow):
    def show(self):
        windows.append(self)
        self.shutdown_timeout_ms = 1
        self.add_account()
        assert entered.wait(5)
        assert not self.isVisible()
        QTimer.singleShot(80, release.set)
        def deadline():
            watchdog.append((self.is_busy, self.isVisible()))
            application.exit(91)
        QTimer.singleShot(1000, deadline)
        raise RuntimeError('synthetic startup failure')
with patch.object(app.RuntimePaths, 'from_application', side_effect=lambda: real_paths(Path(root.name))), patch.object(app, 'MainWindow', HiddenWindow), patch('desktop.workers.login_new_account', side_effect=login):
    assert app.main(['hidden-startup-test']) == 1
assert release.is_set()
assert not windows[0].is_busy
assert not watchdog, 'cleanup needed a watchdog even though owned workers had finished'
print('hidden startup cleaned up')
"""
        result = subprocess.run([sys.executable, '-X', 'faulthandler', '-c', script],
                                capture_output=True, text=True, timeout=30)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn('hidden startup cleaned up', result.stdout)

    def test_reused_application_ignores_unrelated_window_visibility_on_shutdown(self):
        script = r"""
import tempfile, threading
from pathlib import Path
from unittest.mock import patch
from PySide6.QtCore import QTimer
from PySide6.QtWidgets import QApplication, QWidget
import app
from desktop.main_window import MainWindow
from desktop.paths import RuntimePaths
from tiktok_live.errors import BrowserError
root = tempfile.TemporaryDirectory()
from desktop.single_instance import SingleInstance
app.SingleInstance = lambda: SingleInstance(Path(root.name) / 'instance')
app.show_startup_error = lambda classification: None
real_paths = RuntimePaths.from_application
application = QApplication([])
other = QWidget()
other.show()
entered = threading.Event()
release = threading.Event()
watchdog = []
windows = []
def login(path, **kwargs):
    entered.set()
    assert kwargs['cancel_event'].wait(5)
    assert release.wait(5)
    raise BrowserError('login_cancelled')
class Window(MainWindow):
    def show(self):
        super().show()
        windows.append(self)
        self.shutdown_timeout_ms = 1
        self.add_account()
        assert entered.wait(5)
        QTimer.singleShot(0, self.close)
        QTimer.singleShot(80, release.set)
        def deadline():
            watchdog.append((self.is_busy, self.isVisible(), other.isVisible()))
            application.exit(93)
        QTimer.singleShot(1000, deadline)
with patch.object(app.RuntimePaths, 'from_application', side_effect=lambda: real_paths(Path(root.name))), patch.object(app, 'MainWindow', Window), patch('desktop.workers.login_new_account', side_effect=login):
    assert app.main(['reused-application-test']) == 0
assert release.is_set()
assert not windows[0].is_busy
assert other.isVisible()
assert not watchdog, 'unrelated top-level widget prevented application shutdown'
other.close()
print('unrelated window preserved')
"""
        result = subprocess.run([sys.executable, '-X', 'faulthandler', '-c', script],
                                capture_output=True, text=True, timeout=30)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn('unrelated window preserved', result.stdout)

    def test_garbage_collection_with_active_worker_and_repeated_window_disposal(self):
        script = r'''
import gc, tempfile, threading, time
from pathlib import Path
from unittest.mock import patch
from PySide6.QtCore import QCoreApplication, QEvent
from PySide6.QtWidgets import QApplication
from desktop.main_window import MainWindow
from desktop.paths import RuntimePaths
from tiktok_live.errors import BrowserError
application = QApplication([])
application.setQuitOnLastWindowClosed(False)
with tempfile.TemporaryDirectory() as root:
    paths = RuntimePaths.from_application(Path(root))
    for cycle in range(20):
        entered = threading.Event()
        release = threading.Event()
        def login(path, **kwargs):
            entered.set()
            release.wait(2)
            raise BrowserError('login_cancelled')
        with patch('desktop.workers.login_new_account', side_effect=login):
            window = MainWindow(paths)
            window.show()
            window.shutdown_timeout_ms = 5
            window.add_account()
            assert entered.wait(1)
            application.processEvents()
            assert not window.close()
            gc.collect()
            assert window.is_busy
            assert next(iter(window._threads)).isRunning()
            release.set()
            deadline = time.monotonic() + 2
            while window.is_busy and time.monotonic() < deadline:
                application.processEvents()
                time.sleep(.001)
            assert not window.is_busy
            window.deleteLater()
            QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
            del window
            gc.collect()
print('20 safe lifecycle cycles')
'''
        result = subprocess.run([sys.executable, '-X', 'faulthandler', '-c', script],
                                capture_output=True, text=True, timeout=45)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn('20 safe lifecycle cycles', result.stdout)


if __name__ == '__main__':
    unittest.main()
