"""Configured observer retry budget includes anonymous room discovery failures."""
from copy import deepcopy
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from curl_cffi.requests.exceptions import Timeout, ConnectionError as CurlConnectionError, CertificateVerifyError, ProxyError
from tiktok_live import runner
from tiktok_live.errors import SafeOperationError

TARGET = '_sxfxyxah_'
ROOM = '7698689442881522440'
LIVE = dict(exists=True, username=TARGET, roomId=ROOM, isLive=True)


class RoomQueryRetryTests(unittest.TestCase):
    def setUp(self):
        self.accounts = [dict(id='synthetic', cookies=[]), dict(id='second', cookies=[])]
        self.before = deepcopy(self.accounts)
        self.cancelled = False
        self.answers = []
        self.queries, self.waits, self.constructed = [], [], []
        self.transport_answers = [True]
        self.logs = []
        sink = runner.logger.add(lambda event: self.logs.append(str(event)), format='{message}')
        self.addCleanup(runner.logger.remove, sink)

    def lookup(self, *args, **kwargs):
        self.queries.append(kwargs['timeout_seconds'])
        answer = self.answers.pop(0) if self.answers else Timeout('private-secret', code=28)
        if isinstance(answer, Exception):
            raise answer
        return answer

    def factory(self, username, **kwargs):
        self.constructed.append((username, kwargs['room_id']))
        answer = self.transport_answers.pop(0)
        return SimpleNamespace(start=lambda **kw: answer, close=lambda: None,
                               error=None if answer else ConnectionError('private-secret'))

    def start(self, retries=3, delay=2.5, wait=None):
        with patch.object(runner.requests, 'Session', return_value=SimpleNamespace(close=lambda: None)), \
             patch.object(runner, 'get_live_room', side_effect=self.lookup), \
             patch.object(runner, 'LiveMessageListener', side_effect=self.factory), \
             patch.object(runner, '_pause', side_effect=wait or (lambda seconds, cancel: self.waits.append(seconds))):
            return runner._start_live_listener(TARGET, connect_timeout=12,
                connect_retries=retries, retry_delay_seconds=delay, show_comments=False,
                should_cancel=lambda: self.cancelled,
                resolve_room_id=lambda: runner._resolve_observer_room_id(
                    TARGET, self.accounts, None, '', lambda: self.cancelled, connect_timeout=12))

    def test_network_timeout_retries_then_starts_wss_after_valid_room(self):
        self.answers = [Timeout('private-secret', code=28), LIVE]
        self.assertIsNotNone(self.start())
        self.assertEqual(len(self.queries), 2)
        self.assertEqual(self.waits, [2.5])
        self.assertEqual(self.constructed, [(TARGET, ROOM)])
        self.assertEqual(self.accounts, self.before)
        self.assertNotIn('private-secret', ''.join(self.logs))

    def test_exhausted_three_retries_means_four_queries_not_four_per_account(self):
        try:
            result = self.start()
        except SafeOperationError:
            result = None
        self.assertIsNone(result)
        self.assertEqual(len(self.queries), 4)
        self.assertEqual(self.waits, [2.5, 2.5, 2.5])
        self.assertEqual(self.constructed, [])

    def test_zero_retries_performs_one_query(self):
        try:
            self.start(retries=0)
        except SafeOperationError:
            pass
        self.assertEqual(len(self.queries), 1)
        self.assertEqual(self.waits, [])

    def test_temporary_connection_failure_uses_custom_budget_and_delay(self):
        self.answers = [CurlConnectionError('private-secret', code=7), ConnectionError('private-secret'), LIVE]
        self.assertIsNotNone(self.start(retries=2, delay=.7))
        self.assertEqual(len(self.queries), 3)
        self.assertEqual(self.waits, [.7, .7])

    def test_builtin_timeout_is_retryable(self):
        self.answers = [TimeoutError('private-secret'), LIVE]
        self.assertIsNotNone(self.start())
        self.assertEqual(self.waits, [2.5])

    def test_temporary_proxy_receive_failure_retries(self):
        self.answers = [ProxyError('private-secret', code=56), LIVE]
        self.assertIsNotNone(self.start(retries=2, delay=.7))
        self.assertEqual(self.waits, [.7])
        self.assertEqual(len(self.queries), 2)

    def test_proxy_configuration_or_authentication_error_is_terminal(self):
        self.answers = [ProxyError('private-secret', code=97), LIVE]
        with self.assertRaises(SafeOperationError):
            self.start()
        self.assertEqual(len(self.queries), 1)
        self.assertEqual(self.waits, [])

    def test_confirmed_offline_stops_without_retry_or_wss(self):
        self.answers = [dict(LIVE, isLive=False), LIVE]
        with self.assertRaises(SafeOperationError) as raised:
            self.start()
        self.assertEqual(raised.exception.classification, 'room_offline')
        self.assertEqual(len(self.queries), 1)
        self.assertEqual((self.waits, self.constructed), ([], []))

    def test_owner_mismatch_stops_even_with_multiple_selected_accounts(self):
        self.answers = [dict(LIVE, username='other_host'), LIVE]
        with self.assertRaises(SafeOperationError):
            self.start()
        self.assertEqual(len(self.queries), 1)
        self.assertEqual((self.waits, self.constructed), ([], []))

    def test_malformed_room_and_certificate_errors_are_terminal(self):
        for error in (ValueError('private-secret'), CertificateVerifyError('private-secret', code=60)):
            with self.subTest(kind=type(error).__name__):
                self.answers = [error, LIVE]
                self.queries.clear()
                with self.assertRaises(SafeOperationError):
                    self.start()
                self.assertEqual(len(self.queries), 1)
                self.assertEqual((self.waits, self.constructed), ([], []))

    def test_cancel_after_failed_query_does_not_wait_or_connect(self):
        def cancelled_lookup(*args, **kwargs):
            self.cancelled = True
            raise Timeout('private-secret', code=28)
        with patch.object(self, 'lookup', side_effect=cancelled_lookup):
            self.assertIsNone(self.start())
        self.assertEqual((self.waits, self.constructed), ([], []))

    def test_cancel_during_retry_delay_stops_before_second_query(self):
        def wait(seconds, cancel):
            self.waits.append(seconds)
            self.cancelled = True
        self.assertIsNone(self.start(wait=wait))
        self.assertEqual(len(self.queries), 1)
        self.assertEqual(self.waits, [2.5])
        self.assertEqual(self.constructed, [])

    def test_transport_retry_reuses_room_and_shares_remaining_budget(self):
        self.answers = [Timeout('private-secret', code=28), LIVE]
        self.transport_answers = [False, True]
        self.assertIsNotNone(self.start(retries=2))
        self.assertEqual(len(self.queries), 2)
        self.assertEqual(self.constructed, [(TARGET, ROOM), (TARGET, ROOM)])
        self.assertEqual(self.waits, [2.5, 2.5])


if __name__ == '__main__':
    unittest.main()
