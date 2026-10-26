"""Synthetic OAuth contexts: no real browser, network or account data."""
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from tiktok_live import browser_auth as subject


class Tab:
    def __init__(self, tab_id, cookies, url='https://www.tiktok.com/foryou'):
        self.tab_id = tab_id
        self.url = url
        self.saved_cookies = cookies

    def cookies(self, *, all_domains=False, all_info=False):
        # Characterized against bundled Chromium: DrissionPage's page-level
        # Storage.getCookies still misses the --incognito context.
        return []

    def run_cdp(self, command, **kwargs):
        if command != 'Network.getAllCookies' or kwargs:
            raise AssertionError('Expected context-bound Network cookie read')
        return {'cookies': list(self.saved_cookies)}


class LoginCookieContextTests(unittest.TestCase):
    def setUp(self):
        self.cookies = [
            {'name': 'sessionid', 'value': 'synthetic-private-session',
             'domain': '.tiktok.com', 'expires': -1},
            {'name': 's_v_web_id', 'value': 'synthetic-private-fingerprint',
             'domain': '.tiktok.com', 'expires': -1},
        ]
        self.clock = 0.0
        self.logs = []
        sink = subject.logger.add(self.logs.append, format='{message}')
        self.addCleanup(subject.logger.remove, sink)
        for name, replacement in (
            ('monotonic', lambda: self.clock),
            ('sleep', lambda seconds: setattr(self, 'clock', self.clock + seconds)),
        ):
            patcher = patch.object(subject.time, name, side_effect=replacement)
            patcher.start()
            self.addCleanup(patcher.stop)

    def wait(self, tabs, default_cookies=()):
        browser = SimpleNamespace(
            states=SimpleNamespace(is_alive=True), get_tabs=lambda: tabs,
            cookies=lambda **kwargs: list(default_cookies),
        )
        return subject.wait_for_tiktok_login(browser, 3)

    def test_logged_in_private_tab_succeeds_when_both_storage_reads_are_empty(self):
        tabs = [Tab('tk-private', self.cookies),
                Tab('fb', [], 'https://www.facebook.com/dialog/oauth?code=private-code')]
        self.assertEqual(self.wait(tabs), self.cookies)

    def test_default_context_session_cannot_authenticate_an_empty_private_tab(self):
        with self.assertRaises(TimeoutError):
            self.wait([Tab('tk-private', [])], default_cookies=self.cookies)

    def test_different_contexts_cannot_supply_half_of_a_login_each(self):
        with self.assertRaises(subject._BrowserError):
            self.wait([Tab('one', self.cookies[:1]), Tab('two', self.cookies[1:])])

    def test_wait_logs_missing_session_without_cookie_values_or_oauth_url(self):
        with self.assertRaises(TimeoutError):
            self.wait([Tab('empty', []),
                       Tab('fb', self.cookies, 'https://www.facebook.com/?code=private-code')])
        logs = ''.join(self.logs)
        self.assertIn('classification=login_waiting', logs)
        self.assertIn('reason=session_cookie_missing', logs)
        self.assertNotIn('synthetic-private', logs)
        self.assertNotIn('private-code', logs)
        self.assertNotIn('facebook.com', logs)


if __name__ == '__main__':
    unittest.main()
