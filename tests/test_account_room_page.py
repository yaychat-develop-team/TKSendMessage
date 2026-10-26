"""Account-first live-page discovery, isolated anonymous fallback and stop rules."""
from http.cookiejar import Cookie
import unittest
from unittest.mock import patch
from curl_cffi.requests.exceptions import Timeout, CertificateVerifyError
from tiktok_live import client
from tests import test_public_room_page as fixtures
page, waf, HOST, ROOM = fixtures.page, fixtures.waf, fixtures.HOST, fixtures.ROOM


class AccountRoomPageTests(unittest.TestCase):
    def setUp(self):
        fixtures.PublicRoomPageTests.setUp(self)
        self.account = self.factory()
        self.logs = []
        sink = client.logger.add(lambda event: self.logs.append(str(event)), format='{message}')
        self.addCleanup(client.logger.remove, sink)

    def lookup(self, **options):
        with patch.object(client.requests, 'Session', self.factory):
            return client.get_live_room(self.account, HOST, 'sessionid=synthetic-login; tt_chain_token=synthetic-chain',
                fallback_cookie='sessionid=must-not-use', timeout_seconds=12, **options)

    def test_account_success_uses_original_session_without_anonymous_request(self):
        self.pages = [page()]
        result = self.lookup()
        self.assertEqual(result['roomId'], ROOM)
        self.assertEqual(result['querySource'], 'account_live_page')
        self.assertEqual(len(self.sessions), 1)
        self.assertIn('sessionid=synthetic-login', self.calls[0][2])
        self.assertFalse(self.account.closed)
        self.assertNotIn('synthetic-login', ''.join(self.logs))

    def test_waf_reload_keeps_account_cookie_and_session_and_preserves_jar(self):
        self.assertEqual(self.lookup()['querySource'], 'account_live_page_waf')
        self.assertEqual(len(self.sessions), 1)
        self.assertIn('sessionid=synthetic-login', self.calls[1][2])
        self.assertIn('_wafchallengeid=', self.calls[1][2])
        self.assertTrue(any(c.name == 'sessionid' for c in self.account.cookies.jar))
        self.assertFalse(self.account.closed)

    def test_timeout_fallback_is_an_independent_anonymous_jar(self):
        self.pages = [Timeout('private-secret', code=28), waf(), page()]
        result = self.lookup()
        self.assertEqual(result['querySource'], 'live_page_waf')
        self.assertEqual(len(self.sessions), 2)
        self.assertNotIn('sessionid', self.calls[1][2] + self.calls[2][2])
        self.assertEqual(list(self.sessions[1].cookies.jar), [])
        self.assertTrue(self.sessions[1].closed)
        self.assertFalse(self.account.closed)
        self.assertNotIn('private-secret', ''.join(self.logs))

    def test_offline_is_terminal_without_anonymous_fallback(self):
        self.pages = [page(status=4), page()]
        self.assertFalse(self.lookup()['isLive'])
        self.assertEqual(len(self.calls), 1)
        self.assertEqual(len(self.sessions), 1)

    def test_wrong_owner_is_terminal_without_anonymous_fallback(self):
        self.pages = [page(owner='other_host'), page()]
        with self.assertRaises(ValueError):
            self.lookup()
        self.assertEqual(len(self.calls), 1)
        self.assertEqual(len(self.sessions), 1)

    def test_wrong_owner_without_room_id_is_still_terminal(self):
        self.pages = [page(owner='other_host').replace('"roomId": "'+ROOM+'", ', ''), page()]
        with self.assertRaises(ValueError):
            self.lookup()
        self.assertEqual(len(self.calls), 1)

    def test_confirmed_offline_without_valid_room_id_cannot_be_overridden(self):
        from tiktok_live.errors import SafeOperationError
        for html in (page(status=4).replace('"roomId": "'+ROOM+'", ', ''),
                     page(status=4, room='0'),
                     page(status=4).replace('"liveRoom": {"status": 4}',
                         '"liveRoom": {"status": 4, "id": "123456789012"}')):
            with self.subTest(html_kind='missing' if 'roomId' not in html else 'zero'):
                self.calls.clear()
                self.pages = [html, page()]
                with self.assertRaises(SafeOperationError) as raised:
                    self.lookup()
                self.assertEqual(raised.exception.classification, 'room_offline')
                self.assertEqual(len(self.calls), 1)

    def test_certificate_failure_does_not_downgrade_to_anonymous(self):
        self.pages = [CertificateVerifyError('private-secret', code=60), page()]
        with self.assertRaises(CertificateVerifyError):
            self.lookup()
        self.assertEqual(len(self.calls), 1)

    def test_cancelled_request_does_not_start_anonymous_fallback(self):
        with self.assertRaises(TimeoutError):
            self.lookup(should_cancel=lambda: True)
        self.assertEqual(self.calls, [])

    def test_temporary_primary_and_fallback_failures_remain_retryable(self):
        self.pages = [Timeout('private-secret', code=28), Timeout('private-secret', code=28)]
        with self.assertRaises(Timeout):
            self.lookup()
        self.assertEqual(len(self.calls), 2)
        self.assertTrue(self.sessions[1].closed)

    def test_account_budget_reserves_time_for_fallback(self):
        from tiktok_live import public_room
        clock = [100.]
        get = self.factory.get
        def timed(session, *args, **kwargs):
            if session is self.account:
                clock[0] += kwargs['timeout']
            return get(session, *args, **kwargs)
        self.pages = [Timeout('private-secret', code=28), page()]
        with patch.object(client.time, 'monotonic', side_effect=lambda: clock[0]), \
             patch.object(self.factory, 'get', timed):
            self.assertEqual(self.lookup()['roomId'], ROOM)
        self.assertEqual(self.calls[0][1]['timeout'], 6)
        self.assertEqual(self.calls[1][1]['timeout'], 6)


if __name__ == '__main__':
    unittest.main()
