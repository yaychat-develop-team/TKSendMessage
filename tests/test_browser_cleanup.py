"""Login-owned browser cleanup, with the OS process boundary controlled."""
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import psutil
from loguru import logger
from tiktok_live import browser_auth as subject
from tiktok_live.errors import BrowserError


class Process:
    def __init__(self, pid, args):
        self.pid = pid
        self.info = {'pid': pid, 'name': 'chrome.exe', 'cmdline': args}
        self.alive = True
    def is_running(self):
        return self.alive
    def children(self, recursive=False):
        return []
    def kill(self):
        self.alive = False


class BrowserCleanupTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.profile = Path(self.temp.name) / 'owned'
        self.owned = Process(12345, ['chrome.exe', '--remote-debugging-port=45678',
                                   '--user-data-dir=' + str(self.profile)])
        self.other = Process(12346, ['chrome.exe', '--remote-debugging-port=45678',
                                   '--user-data-dir=' + str(self.profile) + '-other'])
        self.browser = SimpleNamespace(address='127.0.0.1:45678',
                                       user_data_path=str(self.profile),
                                       states=SimpleNamespace(is_alive=True))
        self.browser.quit = lambda **kw: None  # Driver can return without closing Chrome.
        self.logs = []
        sink = logger.add(lambda message: self.logs.append(str(message)))
        self.addCleanup(logger.remove, sink)
        processes = patch.object(psutil, 'process_iter', return_value=[self.owned, self.other])
        processes.start()
        self.addCleanup(processes.stop)
        waiter = patch.object(psutil, 'wait_procs', side_effect=lambda items, timeout: (
            [p for p in items if not p.alive], [p for p in items if p.alive]))
        waiter.start()
        self.addCleanup(waiter.stop)

    def test_silent_quit_failure_closes_only_exact_owned_profile_and_port(self):
        subject._close_browser(self.browser)
        self.assertFalse(self.owned.alive)
        self.assertTrue(self.other.alive)
        self.assertIn('classification=browser_closed', ''.join(self.logs))

    def test_known_browser_pid_does_not_enumerate_all_system_command_lines(self):
        self.browser.process_id = 12345
        with patch.object(psutil, 'Process', return_value=self.owned), \
                patch.object(psutil, 'process_iter', return_value=[]):
            subject._close_browser(self.browser)
        self.assertFalse(self.owned.alive)
        self.assertTrue(self.other.alive)

    def test_driver_exception_is_logged_without_leaking_exception_message(self):
        def fail(**kw):
            raise RuntimeError('synthetic-private-driver-data')
        self.browser.quit = fail
        subject._close_browser(self.browser)
        self.assertFalse(self.owned.alive)
        text = ''.join(self.logs)
        self.assertIn('classification=browser_quit_failed', text)
        self.assertNotIn('synthetic-private-driver-data', text)

    def test_constructor_failure_cleans_partial_launch_before_reporting_failure(self):
        def fail(options):
            raise RuntimeError('synthetic-private-launch-data')
        from tests.test_drission_browser_auth import FakeOptions
        with patch.object(subject, '_load_drissionpage', return_value=(fail, FakeOptions)):
            with self.assertRaises(BrowserError):
                subject.launch_available_browser(headless=False, user_data_path=self.profile,
                                                  local_port=45678)
        self.assertFalse(self.owned.alive)
        self.assertTrue(self.other.alive)
        self.assertNotIn('synthetic-private-launch-data', ''.join(self.logs))

    def test_no_matching_owner_never_kills_other_chrome_and_reports_failure(self):
        self.browser.user_data_path = str(self.profile / 'missing')
        subject._close_browser(self.browser)
        self.assertTrue(self.owned.alive)
        self.assertTrue(self.other.alive)
        self.assertIn('classification=browser_close_failed', ''.join(self.logs))

    def test_unclosable_owned_process_is_reported_not_silently_ignored(self):
        self.owned.kill = lambda: None
        subject._close_browser(self.browser)
        self.assertTrue(self.owned.alive)
        self.assertIn('classification=browser_close_failed', ''.join(self.logs))


if __name__ == '__main__':
    unittest.main()
