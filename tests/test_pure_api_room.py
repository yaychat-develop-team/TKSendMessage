"""API-only discovery, stale room refresh and terminal state propagation."""
from copy import deepcopy
from types import SimpleNamespace
import unittest
from unittest.mock import patch
from urllib.parse import urlsplit

from curl_cffi.requests.exceptions import Timeout
from tiktok_live import client, runner
from tiktok_live.client import get_api_live_room as get_live_room
from tiktok_live.errors import SafeOperationError

TARGET = 'elkichigai'
OLD, CURRENT = '7698913803928357653', '7698956300555324180'


def metadata(room=CURRENT, status=2):
    return {'data': {'user': {'uniqueId': TARGET, 'roomId': room, 'status': status},
                     'liveRoom': {'status': status}}}


class PureApiRoomTests(unittest.TestCase):
    def setUp(self):
        self.calls = []
        self.answers = [metadata()]
        self.clock = 100.
        self.cookie = 'sessionid=synthetic-login; msToken=synthetic-token'
        self.session = SimpleNamespace(get=self.get)

    def get(self, url, **kwargs):
        self.calls.append((urlsplit(url), deepcopy(kwargs)))
        if urlsplit(url).path not in ('/api-live/user/room/', '/webcast/room/check_alive/'):
            raise AssertionError('Webpage requests forbidden')
        self.clock += .5
        answer = self.answers.pop(0)
        if isinstance(answer, Exception):
            raise answer
        return SimpleNamespace(status_code=200, json=lambda: answer)

    def lookup(self):
        with patch.object(client.time, 'monotonic', side_effect=lambda: self.clock):
            return get_live_room(self.session, '@'+TARGET, self.cookie,
                                 fallback_cookie='sessionid=never-use-other-account', timeout_seconds=12)

    def test_online_lookup_never_requests_html_or_replaces_login(self):
        room = self.lookup()
        self.assertEqual(room['roomId'], CURRENT)
        self.assertIs(room['isLive'], True)
        self.assertEqual([u.path for u, _ in self.calls], ['/api-live/user/room/'])
        self.assertEqual(self.calls[0][1]['headers']['cookie'], self.cookie)

    def test_offline_metadata_refreshes_current_room_before_independent_check(self):
        self.answers = [metadata(OLD, 4), metadata(CURRENT, 4),
                        {'data': [{'room_id': CURRENT, 'alive': True}]}]
        room = self.lookup()
        self.assertEqual(room['roomId'], CURRENT)
        self.assertEqual(room['liveStatus'], 4)
        self.assertIs(room['isLive'], True)
        self.assertEqual(self.calls[-1][1]['params']['room_ids'], CURRENT)
        self.assertTrue(all(k['headers']['cookie'] == self.cookie for _, k in self.calls))
        self.assertIn('_', self.calls[1][1]['params'])
        self.assertTrue(all(0 < k['timeout'] <= 12 for _, k in self.calls))

    def test_refreshed_online_metadata_replaces_old_room_without_forcing_it_live(self):
        self.answers = [metadata(OLD, 4), metadata(CURRENT, 2)]
        room = self.lookup()
        self.assertEqual(room['roomId'], CURRENT)
        self.assertIs(room['isLive'], True)
        self.assertEqual(len(self.calls), 2)

    def test_confirmed_offline_remains_offline(self):
        self.answers = [metadata(status=4), metadata(status=4),
                        {'data': [{'room_id': CURRENT, 'alive': False}]}]
        self.assertIs(self.lookup()['isLive'], False)

    def test_missing_mismatched_duplicate_or_non_boolean_alive_stays_unknown(self):
        for entries in ([], [{'room_id': OLD, 'alive': True}],
                        [{'room_id': CURRENT, 'alive': 1}],
                        [{'room_id': CURRENT, 'alive': True}]*2):
            with self.subTest(entries=entries):
                self.answers = [metadata(status=4), metadata(status=4), {'data': entries}]
                self.assertIsNone(self.lookup()['isLive'])

    def test_alive_timeout_never_turns_old_offline_metadata_into_confirmed_offline(self):
        self.answers = [metadata(status=4), metadata(status=4), Timeout('sessionid=private', code=28)]
        self.assertIsNone(self.lookup()['isLive'])

    def test_transient_metadata_failure_retries_only_once_without_page_fallback(self):
        self.answers = [Timeout('synthetic', code=28), metadata()]
        self.assertIs(self.lookup()['isLive'], True)
        self.assertEqual(len(self.calls), 2)

    def test_terminal_room_state_does_not_retry_or_construct_any_listener(self):
        account = {'id': 'selected', 'cookies': [{'name': 'sessionid', 'value': 'synthetic'}]}
        for state, classification in ((False, 'room_offline'), (None, 'room_state_unconfirmed')):
            logs = []
            sink = runner.logger.add(lambda e: logs.append(str(e)), format='{message}')
            try:
                with self.subTest(state=state), \
                     patch.object(runner.requests, 'Session', return_value=SimpleNamespace(close=lambda: None)), \
                     patch.object(runner, 'get_live_room', return_value={'exists': True, 'roomId': CURRENT,
                         'username': TARGET, 'isLive': state}), \
                     patch.object(runner, 'LiveMessageListener', side_effect=AssertionError('No WSS allowed')):
                    with self.assertRaises(SafeOperationError) as raised:
                        runner._start_live_listener(TARGET, connect_timeout=12, connect_retries=3,
                            retry_delay_seconds=0, show_comments=False,
                            resolve_room_id=lambda: runner._resolve_observer_room_id(
                                TARGET, [account], {'selected'}, '', None, connect_timeout=12))
                    self.assertEqual(raised.exception.classification, classification)
                    self.assertNotIn('classification=listener_connect_failed', ''.join(logs))
                    self.assertNotIn('classification=listener_retrying', ''.join(logs))
            finally:
                runner.logger.remove(sink)

    def test_exhausted_metadata_lookup_is_terminal_before_wss_retries(self):
        account = {'id': 'selected', 'cookies': [{'name': 'sessionid', 'value': 'synthetic'}]}
        logs = []
        sink = runner.logger.add(lambda event: logs.append(str(event)), format='{message}')
        try:
            with patch.object(runner.requests, 'Session', return_value=SimpleNamespace(close=lambda: None)), \
                 patch.object(runner, 'get_live_room', side_effect=ValueError('private-state')):
                with self.assertRaises(SafeOperationError) as raised:
                    runner._start_live_listener(TARGET, connect_timeout=12, connect_retries=3,
                        retry_delay_seconds=0, show_comments=False,
                        resolve_room_id=lambda: runner._resolve_observer_room_id(
                            TARGET, [account], {'selected'}, '', None, connect_timeout=12))
                self.assertEqual(raised.exception.classification, 'room_state_unconfirmed')
                self.assertNotIn('classification=listener_connect_failed', ''.join(logs))
                self.assertNotIn('private-state', ''.join(logs))
        finally:
            runner.logger.remove(sink)

    def test_failed_refresh_does_not_return_stale_offline_state(self):
        self.answers = [metadata(status=4), Timeout('synthetic', code=28)]
        with self.assertRaises(ValueError):
            self.lookup()
        self.assertEqual(len(self.calls), 2)

    def test_late_alive_success_does_not_authorize_sending(self):
        self.answers = [metadata(status=4), metadata(status=4),
                        {'data': [{'room_id': CURRENT, 'alive': True}]}]
        original = self.session.get
        def late_get(url, **kwargs):
            answer = original(url, **kwargs)
            if urlsplit(url).path == '/webcast/room/check_alive/':
                self.clock += 20
            return answer
        self.session.get = late_get
        self.assertIsNone(self.lookup()['isLive'])
