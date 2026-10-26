"""Login lifecycle tests with real stores and a controlled browser boundary."""

import tempfile
import threading
import unittest
from contextlib import ExitStack
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from tiktok_live import account_store
from tiktok_live import browser_auth as subject
from tiktok_live.account_store import load_account_store
from tiktok_live.errors import BrowserError


class LoginBrowser:
    process_id = 24680

    def __init__(self, cookies, *, ticket_state=True):
        self.saved_cookies = cookies
        self.states = SimpleNamespace(is_alive=True)
        self.latest_tab = self
        self.url = 'about:blank'
        self.visited = []
        self.deleted = []
        self.quit_calls = 0
        self.quit_kwargs = []
        self.cookie_reads = 0
        self.on_read = None
        self.on_ticket = None
        self.on_quit = None
        self.on_navigation = None
        self.ticket_state = ticket_state
        self.html = ''

    def get(self, url, **kwargs):
        self.visited.append(url)
        self.url = 'https://www.tiktok.com/foryou'
        return self.on_navigation() if self.on_navigation else True

    def run_cdp(self, command, **kwargs):
        if command == 'Network.getAllCookies':
            return {'cookies': self.cookies(all_info=True)}
        self.deleted.append(kwargs['name'])

    def cookies(self, all_info=False, all_domains=False):
        self.cookie_reads += 1
        if self.on_read:
            self.on_read(self.cookie_reads)
        return list(self.saved_cookies)

    def get_tabs(self):
        return [self]

    def run_js(self, script):
        if self.on_ticket:
            self.on_ticket()
        return {'ts_sign': 'synthetic-sign', 'encrypt_ticket': 'synthetic-ticket'} if self.ticket_state else None

    def quit(self, **kwargs):
        self.quit_calls += 1
        self.quit_kwargs.append(kwargs)
        if self.on_quit:
            self.on_quit()
        self.states.is_alive = False


class DesktopLoginLifecycleTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.store_path = Path(self.temporary.name) / 'accounts.json'
        self.profile_root = Path(self.temporary.name) / 'profiles'
        self.cookies = [
            {'name': 'sessionid', 'value': 'synthetic-session', 'domain': '.tiktok.com', 'path': '/', 'expires': -1},
            {'name': 's_v_web_id', 'value': 'verify_synthetic', 'domain': '.tiktok.com', 'path': '/', 'expires': -1},
            {'name': 'multi_sids', 'value': '1234567890%3Asynthetic', 'domain': '.tiktok.com', 'path': '/', 'expires': -1},
        ]
        self.browser = LoginBrowser(self.cookies)
        self.clock = 0.0
        self.after_sleep = None
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.stack.enter_context(patch.object(subject, '_free_local_port', return_value=45678))
        self.launch = self.stack.enter_context(patch.object(subject, 'launch_available_browser', return_value=self.browser))
        self.port_open = True
        self.stack.enter_context(patch.object(subject, '_debug_port_is_open', side_effect=lambda port: self.port_open))
        self.stack.enter_context(patch.object(subject.time, 'monotonic', side_effect=lambda: self.clock))
        self.stack.enter_context(patch.object(subject.time, 'sleep', side_effect=self.advance))

    def advance(self, seconds):
        self.clock += seconds
        if self.after_sleep:
            self.after_sleep()

    def cancellation_event(self):
        event = threading.Event()
        def wait(seconds):
            self.advance(seconds)
            return event.is_set()
        self.stack.enter_context(patch.object(event, 'wait', side_effect=wait))
        return event

    def login(self, **kwargs):
        return subject.login_new_account(self.store_path, timeout_seconds=3,
                                         profile_root=self.profile_root, **kwargs)

    def assert_cancelled(self, **kwargs):
        with self.assertRaises(BrowserError) as caught:
            self.login(**kwargs)
        self.assertEqual(caught.exception.classification, 'login_cancelled')
        self.assertFalse(self.store_path.exists())

    def test_callback_receives_pid_before_navigation_and_saves_complete_account(self):
        ready = []
        def on_ready(pid):
            ready.append((pid, list(self.browser.visited)))
        account = self.login(on_browser_ready=on_ready)
        self.assertEqual(ready, [(24680, [])])
        saved = load_account_store(self.store_path)['accounts'][0]
        self.assertEqual([(item['name'], item['value']) for item in saved['cookies']], [
            ('sessionid', 'synthetic-session'),
            ('s_v_web_id', 'verify_synthetic'),
            ('multi_sids', '1234567890%3Asynthetic'),
        ])
        self.assertEqual(account['cookies'], self.cookies)
        self.assertEqual(account['ticket_guard']['ts_sign'], 'synthetic-sign')
        self.assertEqual(self.browser.quit_calls, 1)

    def test_login_persists_username_and_nickname_from_authenticated_page(self):
        self.browser.html = (
            '<script id="__UNIVERSAL_DATA_FOR_REHYDRATION__" type="application/json">'
            '{"__DEFAULT_SCOPE__":{"webapp.app-context":{"user":'
            '{"uid":"1234567890","uniqueId":"viewer_name",'
            '"nickName":"Viewer Name"}}}}</script>'
        )

        account = self.login()

        saved = load_account_store(self.store_path)['accounts'][0]
        self.assertEqual(account['username'], 'viewer_name')
        self.assertEqual(account['nickname'], 'Viewer Name')
        self.assertEqual(saved['username'], 'viewer_name')
        self.assertEqual(saved['nickname'], 'Viewer Name')

    def test_default_keywords_preserve_cli_login_and_profile(self):
        account = subject.login_new_account(self.store_path, timeout_seconds=3)
        self.assertEqual(load_account_store(self.store_path)['accounts'][0]['id'], account['id'])
        self.assertEqual(self.browser.visited, ['https://www.tiktok.com/login'])
        self.assertEqual(self.launch.call_args.kwargs['user_data_path'], subject.BROWSER_PROFILE_ROOT / '45678')
        self.assertEqual(self.browser.quit_calls, 1)

    def test_explicit_none_profile_root_preserves_cli_default(self):
        subject.login_new_account(self.store_path, timeout_seconds=3, profile_root=None)
        self.assertEqual(self.launch.call_args.kwargs['user_data_path'], subject.BROWSER_PROFILE_ROOT / '45678')

    def test_already_cancelled_login_does_not_launch_browser(self):
        event = self.cancellation_event()
        event.set()
        self.assert_cancelled(cancel_event=event)
        self.assertEqual(self.browser.quit_calls, 0)
        self.launch.assert_not_called()

    def test_cancellation_in_ready_callback_closes_browser_before_navigation(self):
        event = self.cancellation_event()
        self.assert_cancelled(on_browser_ready=lambda pid: event.set(), cancel_event=event)
        self.assertEqual(self.browser.visited, [])
        self.assertEqual(self.browser.quit_calls, 1)

    def test_cancellation_during_login_polling_never_saves_account(self):
        event = self.cancellation_event()
        self.after_sleep = event.set
        self.assert_cancelled(cancel_event=event)
        self.assertEqual(self.browser.quit_calls, 1)

    def test_cancellation_during_cookie_read_prevents_stable_cookie_success(self):
        event = self.cancellation_event()
        self.browser.on_read = lambda count: event.set() if count == 3 else None
        self.assert_cancelled(cancel_event=event)
        self.assertEqual(self.browser.quit_calls, 1)

    def test_browser_closure_during_login_never_saves_account(self):
        self.after_sleep = lambda: setattr(self.browser.states, 'is_alive', False)
        self.assert_cancelled()
        self.assertEqual(self.browser.quit_calls, 1)

    def test_missing_fingerprint_cookie_never_saves_account(self):
        self.browser.saved_cookies = [self.cookies[0]]
        with self.assertRaises(BrowserError) as caught:
            self.login()
        self.assertEqual(caught.exception.classification, 'login_cookie_incomplete')
        self.assertFalse(self.store_path.exists())
        self.assertEqual(self.browser.quit_calls, 1)

    def test_cancellation_during_ticket_capture_never_saves_account(self):
        event = self.cancellation_event()
        self.browser.on_ticket = event.set
        self.assert_cancelled(cancel_event=event)
        self.assertEqual(self.browser.quit_calls, 1)

    def test_browser_closure_during_ticket_capture_never_saves_account(self):
        self.browser.on_ticket = lambda: setattr(self.browser.states, 'is_alive', False)
        self.assert_cancelled()
        self.assertEqual(self.browser.quit_calls, 1)

    def test_closed_debug_port_during_ticket_capture_prevents_account_save(self):
        self.browser.on_ticket = lambda: setattr(self, 'port_open', False)
        self.assert_cancelled()
        self.assertEqual(self.browser.quit_calls, 1)

    def test_ticket_guard_disconnect_exception_is_classified_as_cancelled(self):
        from DrissionPage.errors import PageDisconnectedError
        def disconnect():
            self.browser.states.is_alive = False
            raise PageDisconnectedError()
        self.browser.on_ticket = disconnect
        self.assert_cancelled()
        self.assertEqual(self.browser.quit_calls, 1)

    def test_ticket_guard_disconnect_with_stale_state_checks_debug_port(self):
        from DrissionPage.errors import PageDisconnectedError
        def disconnect():
            self.port_open = False
            raise PageDisconnectedError()
        self.browser.on_ticket = disconnect
        self.assert_cancelled()
        self.assertEqual(self.browser.quit_calls, 1)

    def test_live_browser_ticket_guard_failure_uses_safe_classification(self):
        def fail():
            raise RuntimeError('synthetic-sensitive-read-error')
        self.browser.on_ticket = fail
        with self.assertRaises(BrowserError) as caught:
            self.login()
        self.assertEqual(caught.exception.classification, 'login_state_unavailable')
        self.assertNotIn('synthetic-sensitive-read-error', str(caught.exception))
        self.assertFalse(self.store_path.exists())
        self.assertEqual(self.browser.quit_calls, 1)

    def test_browser_closing_callback_precedes_cleanup_and_account_commit(self):
        observed = []
        def closing():
            observed.append(('closing', self.browser.states.is_alive, self.store_path.exists()))
        self.browser.on_quit = lambda: observed.append(('quit', self.store_path.exists()))
        self.login(on_browser_closing=closing)
        self.assertEqual(observed, [('closing', True, False), ('quit', False)])
        self.assertEqual(len(load_account_store(self.store_path)['accounts']), 1)

    def test_success_waits_ten_seconds_before_announcing_and_closing_browser(self):
        observed = []

        def login_succeeded(*args, **kwargs):
            observed.append(('success', self.clock))
            return self.cookies

        def closing():
            observed.append(('closing', self.clock))

        self.browser.on_quit = lambda: observed.append(('quit', self.clock))
        with patch.object(subject, 'wait_for_tiktok_login', side_effect=login_succeeded), \
                patch.object(subject, 'build_headers_from_cookie_state', return_value={}), \
                patch.object(subject, 'save_account_store_atomic'):
            self.login(on_browser_closing=closing)

        self.assertEqual(observed, [
            ('success', 0.0),
            ('closing', 10.0),
            ('quit', 10.0),
        ])
        self.assertEqual(self.browser.quit_calls, 1)

    def test_explicit_cancellation_during_closing_callback_still_prevents_commit(self):
        event = self.cancellation_event()
        self.assert_cancelled(on_browser_closing=event.set, cancel_event=event)
        self.assertEqual(self.browser.quit_calls, 1)

    def test_cancellation_during_browser_cleanup_prevents_account_commit(self):
        event = self.cancellation_event()
        self.browser.on_quit = event.set
        self.assert_cancelled(cancel_event=event)
        self.assertEqual(self.browser.quit_calls, 1)

    def test_cancellation_interrupts_ticket_polling_when_state_is_missing(self):
        event = self.cancellation_event()
        self.browser.ticket_state = False
        self.after_sleep = lambda: event.set() if self.clock > 1 else None
        self.assert_cancelled(cancel_event=event)
        self.assertLess(self.clock, 2)
        self.assertEqual(self.browser.quit_calls, 1)

    def test_cancelled_refresh_preserves_existing_account_file(self):
        self.login()
        previous = self.store_path.read_bytes()
        self.browser.states.is_alive = True
        event = self.cancellation_event()
        self.after_sleep = event.set
        with self.assertRaises(BrowserError) as caught:
            self.login(cancel_event=event)
        self.assertEqual(caught.exception.classification, 'login_cancelled')
        self.assertEqual(self.store_path.read_bytes(), previous)

    def test_cancellation_during_failed_navigation_is_classified_as_cancelled(self):
        event = self.cancellation_event()
        def navigate():
            event.set()
            return False
        self.browser.on_navigation = navigate
        self.assert_cancelled(cancel_event=event)
        self.assertEqual(self.browser.quit_calls, 1)

    def test_browser_closure_during_failed_navigation_is_classified_as_cancelled(self):
        def navigate():
            self.browser.states.is_alive = False
            return False
        self.browser.on_navigation = navigate
        self.assert_cancelled()
        self.assertEqual(self.browser.quit_calls, 1)

    def assert_cancelled_during_staging(self, stage, existing):
        if existing:
            self.login()
            previous = self.store_path.read_bytes()
            self.browser.states.is_alive = True
            self.browser.saved_cookies = [
                dict(cookie, value='synthetic-refreshed-session')
                if cookie['name'] == 'sessionid' else dict(cookie)
                for cookie in self.cookies
            ]
        event = self.cancellation_event()
        owner = account_store if stage == 'encoding' else account_store.os
        name = '_encode_persisted_store' if stage == 'encoding' else 'fsync'
        original = getattr(owner, name)
        def cancel_after_stage(*args, **kwargs):
            result = original(*args, **kwargs)
            event.set()
            return result
        with patch.object(owner, name, side_effect=cancel_after_stage):
            with self.assertRaises(BrowserError) as caught:
                self.login(cancel_event=event)
        self.assertEqual(caught.exception.classification, 'login_cancelled')
        self.assertTrue(event.is_set())
        if existing:
            self.assertEqual(self.store_path.read_bytes(), previous)
        else:
            self.assertFalse(self.store_path.exists())
        self.assertEqual(list(self.store_path.parent.glob('.accounts.json.*.tmp')), [])
        self.assertEqual(self.browser.quit_calls, 2 if existing else 1)

    def test_cancellation_during_encoding_never_creates_account_store(self):
        self.assert_cancelled_during_staging('encoding', existing=False)

    def test_cancellation_during_encoding_preserves_duplicate_account_bytes(self):
        self.assert_cancelled_during_staging('encoding', existing=True)

    def test_cancellation_during_fsync_never_creates_account_store(self):
        self.assert_cancelled_during_staging('fsync', existing=False)

    def test_cancellation_during_fsync_preserves_duplicate_account_bytes(self):
        self.assert_cancelled_during_staging('fsync', existing=True)

    def test_ready_callback_failure_is_safe_and_closes_browser(self):
        def on_ready(pid):
            raise RuntimeError('synthetic-sensitive-callback-error')
        self.assert_cancelled(on_browser_ready=on_ready)
        self.assertEqual(self.browser.quit_calls, 1)
        self.assertEqual(self.browser.visited, [])


if __name__ == '__main__':
    unittest.main()
