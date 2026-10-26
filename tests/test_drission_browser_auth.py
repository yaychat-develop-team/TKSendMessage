import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import tiktok_live.browser_auth as subject


class FakeOptions:
    def __init__(self, read_file=False):
        self.calls = [('init', read_file)]

    def set_browser_path(self, value):
        self.calls.append(('browser_path', value)); return self

    def set_user_data_path(self, value):
        self.calls.append(('user_data_path', value)); return self

    def set_local_port(self, value):
        self.calls.append(('local_port', value)); return self

    def auto_port(self):
        self.calls.append(('auto_port',)); return self

    def headless(self, value=True):
        self.calls.append(('headless', value)); return self

    def set_user_agent(self, value):
        self.calls.append(('user_agent', value)); return self

    def set_argument(self, name, value=None):
        self.calls.append(('argument', name, value)); return self

    def set_timeouts(self, **kwargs):
        self.calls.append(('timeouts', kwargs)); return self


class FakeTab:
    def __init__(self, url='https://www.tiktok.com/login', html='', login_ui=False,
                 body_text=None):
        self.url = url
        self.html = html
        self.body_text = html if body_text is None else body_text
        self.login_ui = login_ui
        self.visited = []
        self.deleted = []

    def get(self, url, **kwargs):
        self.visited.append((url, kwargs))
        return True

    def ele(self, locator, timeout=0):
        if locator == 'tag:body':
            return SimpleNamespace(text=self.body_text)
        return object() if self.login_ui else None

    def run_cdp(self, command, **kwargs):
        if command == 'Network.getAllCookies':
            return {'cookies': list(self.cookie_provider())}
        self.deleted.append((command, kwargs))


class FakeBrowser:
    def __init__(self, cookies=None, tabs=None, alive=True):
        self._cookies = list(cookies or [])
        self._tabs = list(tabs or [FakeTab()])
        for tab in self._tabs:
            tab.cookie_provider = lambda: list(self._cookies)
        self.latest_tab = self._tabs[-1]
        self.states = SimpleNamespace(is_alive=alive)
        self.set = SimpleNamespace(cookies=lambda values: setattr(self, '_cookies', list(values)))
        self.quit_calls = 0
        self.quit_kwargs = []

    def cookies(self, all_info=False):
        return list(self._cookies)

    def get_tabs(self):
        return list(self._tabs)

    def quit(self, **kwargs):
        self.quit_calls += 1
        self.quit_kwargs.append(kwargs)
        self.states.is_alive = False


class DrissionBrowserAuthTests(unittest.TestCase):
    cookies = [
        {'name': 'sessionid', 'value': 'synthetic-session',
         'domain': '.tiktok.com', 'path': '/', 'expires': -1},
        {'name': 's_v_web_id', 'value': 'verify_synthetic',
         'domain': '.tiktok.com', 'path': '/', 'expires': -1},
    ]

    def setUp(self):
        # FakeBrowser owns no OS processes. Keep these login unit tests off the
        # real process table; ownership/termination is covered separately by
        # test_browser_cleanup with a controlled psutil boundary.
        processes_patch = patch.object(subject, '_owned_browser_processes', return_value=[])
        processes_patch.start()
        self.addCleanup(processes_patch.stop)
        # FakeBrowser has no real Chromium debug endpoint. Individual port-closure
        # tests override this transport boundary to exercise failed liveness.
        port_patch = patch.object(subject, '_debug_port_is_open', return_value=True)
        port_patch.start()
        self.addCleanup(port_patch.stop)
        delay_patch = patch.object(subject, '_LOGIN_SUCCESS_CLOSE_DELAY_SECONDS', 0)
        delay_patch.start()
        self.addCleanup(delay_patch.stop)

    def test_options_use_persistent_profile_and_custom_chrome(self):
        with tempfile.TemporaryDirectory() as directory, \
                patch.dict('os.environ', {'TIKTOK_BROWSER_EXECUTABLE': 'D:/Browser/chrome.exe'}, clear=True):
            profile = Path(directory) / 'profile'
            options = subject.build_chromium_options(
                FakeOptions, headless=False, user_data_path=profile, timeout_seconds=20,
                local_port=45678)
            self.assertIn(('user_data_path', str(profile.resolve())), options.calls)
            self.assertIn(('local_port', 45678), options.calls)
        self.assertIn(('browser_path', 'D:/Browser/chrome.exe'), options.calls)
        self.assertIn(('headless', False), options.calls)
        self.assertIn(('argument', '--start-maximized', None), options.calls)
        self.assertIn(('argument', '--incognito', None), options.calls)
        self.assertFalse(any(call[0] == 'user_agent' for call in options.calls))

    def test_long_login_deadline_does_not_create_long_browser_operation_timeout(self):
        with tempfile.TemporaryDirectory() as directory:
            options = subject.build_chromium_options(
                FakeOptions, headless=False,
                user_data_path=Path(directory) / 'profile',
                timeout_seconds=300, local_port=45678,
            )
        timeout_call = next(call for call in options.calls if call[0] == 'timeouts')
        self.assertLessEqual(timeout_call[1]['base'], 10)
        self.assertLessEqual(timeout_call[1]['page_load'], 10)

    def test_wait_does_not_accept_cookie_before_tiktok_redirect_finishes(self):
        browser = FakeBrowser(self.cookies, [
            FakeTab('https://www.tiktok.com/login'),
            FakeTab('https://accounts.google.com/signin'),
        ])
        clock = [0.0]
        def advance(seconds):
            clock[0] += seconds
        with patch.object(subject.time, 'monotonic', side_effect=lambda: clock[0]), \
                patch.object(subject.time, 'sleep', side_effect=advance):
            with self.assertRaises(TimeoutError) as caught:
                subject.wait_for_tiktok_login(browser, timeout_seconds=3, poll_seconds=1)
        self.assertEqual(caught.exception.classification, 'login_timeout')

    def test_wait_accepts_stable_cookie_after_tiktok_redirect_finishes(self):
        browser = FakeBrowser(self.cookies, [
            FakeTab('https://www.tiktok.com/foryou'),
            FakeTab('https://accounts.google.com/signin'),
        ])
        clock = [0.0]
        def advance(seconds):
            clock[0] += seconds
        with patch.object(subject.time, 'monotonic', side_effect=lambda: clock[0]), \
                patch.object(subject.time, 'sleep', side_effect=advance):
            result = subject.wait_for_tiktok_login(browser, timeout_seconds=10, poll_seconds=1)
        self.assertEqual(result, self.cookies)

    def test_wait_rejects_authenticated_cookie_without_s_v_web_id(self):
        incomplete = [self.cookies[0]]
        browser = FakeBrowser(incomplete, [FakeTab('https://www.tiktok.com/foryou')])
        clock = [0.0]
        def advance(seconds):
            clock[0] += seconds
        with patch.object(subject.time, 'monotonic', side_effect=lambda: clock[0]), \
                patch.object(subject.time, 'sleep', side_effect=advance):
            with self.assertRaises(RuntimeError) as caught:
                subject.wait_for_tiktok_login(browser, timeout_seconds=3, poll_seconds=1)
        self.assertEqual(caught.exception.classification, 'login_cookie_incomplete')

    def test_closed_browser_is_reported_as_cancelled(self):
        browser = FakeBrowser(self.cookies, alive=False)
        with self.assertRaises(RuntimeError) as caught:
            subject.wait_for_tiktok_login(browser, timeout_seconds=10)
        self.assertEqual(caught.exception.classification, 'login_cancelled')

    def test_closed_debug_port_cancels_even_when_driver_state_is_stale(self):
        browser = FakeBrowser(self.cookies, alive=True)
        with patch.object(subject, '_debug_port_is_open', return_value=False), \
                patch.object(subject, '_browser_cookies') as read_cookies:
            with self.assertRaises(RuntimeError) as caught:
                subject.wait_for_tiktok_login(
                    browser, timeout_seconds=10, local_port=45678)
        self.assertEqual(caught.exception.classification, 'login_cancelled')
        read_cookies.assert_not_called()
    def test_clear_tiktok_cookies_preserves_google_and_facebook(self):
        tab = FakeTab()
        browser = FakeBrowser([
            {'name': 'sessionid', 'value': 'secret', 'domain': '.tiktok.com', 'path': '/'},
            {'name': 'SID', 'value': 'google', 'domain': '.google.com', 'path': '/'},
            {'name': 'c_user', 'value': 'facebook', 'domain': '.facebook.com', 'path': '/'},
        ], [tab])
        subject.clear_tiktok_cookies(browser)
        self.assertEqual(tab.deleted, [('Network.deleteCookies', {
            'name': 'sessionid', 'domain': '.tiktok.com', 'path': '/'
        })])

    def test_login_saves_drission_cookies_and_closes_browser(self):
        tab = FakeTab()
        browser = FakeBrowser(self.cookies, [tab])
        logs = []
        sink = subject.logger.add(logs.append, format='{message}')
        self.addCleanup(subject.logger.remove, sink)
        with tempfile.TemporaryDirectory() as directory:
            store_path = Path(directory) / 'accounts.json'
            with patch.object(subject, '_free_local_port', return_value=45678), \
                    patch.object(subject, 'launch_available_browser', return_value=browser) as launch, \
                    patch.object(subject, 'clear_tiktok_cookies'), \
                    patch.object(subject, 'wait_for_tiktok_login', return_value=self.cookies):
                account = subject.login_new_account(store_path)
            self.assertEqual(account['cookies'], self.cookies)
            self.assertTrue(store_path.exists())
            self.assertIn('新增用户成功', ''.join(logs))
            self.assertIn('classification=account_added', ''.join(logs))
        self.assertEqual(tab.visited[0][0], 'https://www.tiktok.com/login')
        self.assertEqual(browser.quit_calls, 1)
        self.assertEqual(browser.quit_kwargs, [{'timeout': 2, 'force': True}])
        launch.assert_called_once_with(
            headless=False,
            user_data_path=subject.BROWSER_PROFILE_ROOT / '45678',
            timeout_seconds=300,
            local_port=45678,
        )

    def test_duplicate_login_logs_that_no_new_account_was_written(self):
        first_cookies = self.cookies + [{
            'name': 'multi_sids', 'value': '7686807371494851604%3Afirst-login',
            'domain': '.tiktok.com', 'path': '/', 'expires': -1,
        }]
        refreshed_cookies = [
            dict(cookie, value='refreshed-session')
            if cookie['name'] == 'sessionid' else dict(cookie)
            for cookie in first_cookies
        ]
        first_browser = FakeBrowser(first_cookies, [FakeTab()])
        second_browser = FakeBrowser(refreshed_cookies, [FakeTab()])
        logs = []
        sink = subject.logger.add(logs.append, format='{message}')
        self.addCleanup(subject.logger.remove, sink)
        with tempfile.TemporaryDirectory() as directory:
            store_path = Path(directory) / 'accounts.json'
            with patch.object(subject, '_free_local_port', return_value=45678), \
                    patch.object(subject, 'launch_available_browser',
                                 side_effect=[first_browser, second_browser]), \
                    patch.object(subject, 'clear_tiktok_cookies'), \
                    patch.object(subject, 'wait_for_tiktok_login',
                                 side_effect=[first_cookies, refreshed_cookies]):
                subject.login_new_account(store_path)
                subject.login_new_account(store_path)
            store = subject.load_account_store(store_path)
        self.assertEqual(len(store['accounts']), 1)
        self.assertEqual(
            {cookie['name']: cookie['value'] for cookie in store['accounts'][0]['cookies']},
            {cookie['name']: cookie['value'] for cookie in refreshed_cookies},
        )
        text = ''.join(logs)
        self.assertEqual(text.count('新增用户成功'), 1)
        self.assertIn('用户重复，未写入新账户', text)
        self.assertIn('已覆盖原 Cookie', text)
        self.assertIn('duplicate_account_id=' + store['accounts'][0]['id'][:12], text)
        self.assertIn('classification=account_duplicate', text)

    def test_login_reports_unrecognisable_transitional_cookies(self):
        browser = FakeBrowser(self.cookies, [FakeTab()])
        with tempfile.TemporaryDirectory() as directory:
            store_path = Path(directory) / 'accounts.json'
            with patch.object(subject, '_free_local_port', return_value=45678), \
                    patch.object(subject, 'launch_available_browser', return_value=browser), \
                    patch.object(subject, 'clear_tiktok_cookies'), \
                    patch.object(subject, 'wait_for_tiktok_login', return_value=self.cookies), \
                    patch.object(subject, 'upsert_account', side_effect=ValueError('transitional')):
                with self.assertRaises(RuntimeError) as caught:
                    subject.login_new_account(store_path)
        self.assertEqual(caught.exception.classification, 'login_identity_unavailable')

    def test_validation_injects_saved_cookies_and_preserves_input(self):
        tab = FakeTab('https://www.tiktok.com/', html='Welcome')
        browser = FakeBrowser(self.cookies, [tab])
        account = {'id': 'synthetic', 'cookies': [dict(item) for item in self.cookies]}
        original = [dict(item) for item in account['cookies']]
        with patch.object(subject, 'launch_available_browser', return_value=browser):
            result = subject.validate_saved_account(account, timeout_seconds=7)
        self.assertEqual(result.status, 'valid')
        self.assertEqual(account['cookies'], original)
        self.assertEqual(browser.quit_calls, 1)

    def test_validation_rejects_cookie_without_s_v_web_id(self):
        incomplete = [self.cookies[0]]
        tab = FakeTab('https://www.tiktok.com/foryou', html='Welcome')
        browser = FakeBrowser(incomplete, [tab])
        account = {'id': 'synthetic', 'cookies': incomplete}
        with patch.object(subject, 'launch_available_browser', return_value=browser):
            result = subject.validate_saved_account(account, timeout_seconds=7)
        self.assertEqual(result.status, 'expired')

    def test_validation_ignores_captcha_word_inside_script_on_authenticated_page(self):
        tab = FakeTab(
            'https://www.tiktok.com/foryou',
            html='<script>const captchaModule = "lazy";</script>',
            body_text='',
        )
        browser = FakeBrowser(self.cookies, [tab])
        account = {'id': 'synthetic', 'cookies': [dict(item) for item in self.cookies]}
        with patch.object(subject, 'launch_available_browser', return_value=browser):
            result = subject.validate_saved_account(account, timeout_seconds=7)
        self.assertEqual(result.status, 'valid')
        self.assertEqual(browser.quit_calls, 1)

    def test_validation_reports_visible_security_challenge_as_temporary(self):
        tab = FakeTab(
            'https://www.tiktok.com/foryou',
            html='<main>Please wait</main>',
            body_text='Please wait',
        )
        browser = FakeBrowser(self.cookies, [tab])
        account = {'id': 'synthetic', 'cookies': [dict(item) for item in self.cookies]}
        with patch.object(subject, 'launch_available_browser', return_value=browser):
            result = subject.validate_saved_account(account, timeout_seconds=7)
        self.assertEqual(result.status, 'temporary_error')

    def test_missing_drissionpage_has_actionable_install_message(self):
        with patch.object(subject, '_load_drissionpage', side_effect=ImportError):
            with self.assertRaises(RuntimeError) as caught:
                subject.login_new_account(Path('unused.json'))
        self.assertEqual(caught.exception.classification, 'drissionpage_missing')
        self.assertIn('pip install DrissionPage', str(caught.exception))
        self.assertNotIn('playwright install', str(caught.exception))


if __name__ == '__main__':
    unittest.main()
