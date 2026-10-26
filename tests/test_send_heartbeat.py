import unittest
from contextlib import contextmanager
from types import SimpleNamespace
from unittest.mock import patch
import importlib.util

from tiktok_live import client


class SendHeartbeatTests(unittest.TestCase):
    def test_prepared_post_and_retry_are_guarded(self):
        self.assertTrue(hasattr(client, 'room_heartbeat'), 'Missing heartbeat send gate')
        events = []
        @contextmanager
        def presence(*args):
            self.assertEqual(args, ('venus', '42', 'sessionid=synthetic', 'UA'))
            events.append('ready')
            try:
                yield
            finally:
                events.append('closed')
        def post(*args, **kwargs):
            self.assertEqual(events[-1], 'ready')
            events.append('post')
            return SimpleNamespace(status_code=200, json=lambda: {'status_code': 0, 'data': {}})
        with patch.object(client, 'room_heartbeat', presence), \
             patch.object(client, 'build_chat_request', return_value=('https://example.invalid', b'x')), \
             patch.object(client, 'post_chat_request', side_effect=post):
            result = client._post_prepared_chat(**self.arguments())
            result.retry_message()
        self.assertEqual(events, ['ready', 'post', 'closed'] * 2)

    def arguments(self):
        return dict(session=None, message='hi', cookies={},
                    room={'roomId': '42', 'username': 'venus', 'url': 'https://www.tiktok.com/@venus/live'},
                    device_id='1', runtime_cookie='sessionid=synthetic', ms_token='x',
                    csrf_token='x', profile=SimpleNamespace(user_agent='UA'),
                    ticket_guard_state=None, ticket_guard_headers=None)

    def test_gate_failure_never_posts(self):
        self.assertTrue(hasattr(client, 'room_heartbeat'), 'Missing heartbeat send gate')
        @contextmanager
        def failed(*args):
            raise RuntimeError('heartbeat unavailable')
            yield
        with patch.object(client, 'room_heartbeat', failed), \
             patch.object(client, 'post_chat_request') as post:
            with self.assertRaisesRegex(RuntimeError, 'heartbeat unavailable'):
                client._post_prepared_chat(**self.arguments())
            post.assert_not_called()

    def test_initial_fetch_and_matching_user_agent_reach_heartbeat_gate(self):
        initial = object()
        observed = {}

        @contextmanager
        def presence(*args, **kwargs):
            observed["args"] = args
            observed["kwargs"] = kwargs
            yield

        with patch.object(client, 'room_heartbeat', presence), \
             patch.object(client, 'build_chat_request', return_value=('https://example.invalid', b'x')), \
             patch.object(client, 'post_chat_request', return_value=SimpleNamespace(
                 status_code=200, json=lambda: {'status_code': 0, 'data': {}})):
            client._post_prepared_chat(**self.arguments(), initial_fetch=initial,
                                       wss_user_agent='FETCH_UA')

        self.assertEqual(observed["args"],
                         ('venus', '42', 'sessionid=synthetic', 'FETCH_UA'))
        self.assertEqual(observed["kwargs"], {"initial_fetch": initial})

    def test_post_exception_releases_gate(self):
        self.assertTrue(hasattr(client, 'room_heartbeat'), 'Missing heartbeat send gate')
        closed = []
        @contextmanager
        def presence(*args):
            try:
                yield
            finally:
                closed.append(True)
        with patch.object(client, 'room_heartbeat', presence), \
             patch.object(client, 'build_chat_request', side_effect=ValueError('bad body')):
            with self.assertRaises(ValueError):
                client._post_prepared_chat(**self.arguments())
        self.assertEqual(closed, [True])


class HeartbeatLifecycleTests(unittest.TestCase):
    def test_account_gate_prepares_exact_account_room_session(self):
        from tiktok_live import heartbeat
        from tiktok_live.heartbeat_socket import FrameEvidence
        from tiktok_live.wss.protocol import encode_enter_room, encode_heartbeat, _encode
        evidence = FrameEvidence()
        evidence.record(encode_enter_room(42), 'sent')
        for stamp in (0, 10, 20):
            evidence.record(encode_heartbeat(42), 'sent', now=stamp)
        evidence.record(_encode('im_enter_room_resp', b''), 'received')
        def listener_factory(username, **kw):
            self.assertEqual(kw['connection_role'], 'account_heartbeat')
            kw['session_factory']()
            return SimpleNamespace(start=lambda **kw: True, error=None,
                connection=SimpleNamespace(room_id='42', connected=True, evidence=evidence), close=lambda: None)
        initial = object()
        with patch.object(heartbeat, 'prepare_account') as prepare, \
                patch.object(heartbeat, 'LiveMessageListener', side_effect=listener_factory), \
                patch.object(heartbeat.time, 'monotonic', side_effect=(0, 30)):
            with heartbeat.room_heartbeat('venus', '42', 'sessionid=ONLY_THIS_ACCOUNT',
                                           'ACCOUNT_UA', initial_fetch=initial):
                pass
        prepare.assert_called_once_with('venus', '42', 'sessionid=ONLY_THIS_ACCOUNT',
                                        'ACCOUNT_UA', initial_fetch=initial)

    def run_gate(self, *, connected=True, started=True, room='42', beats=3,
                 entered=True, response=True, beat_room=42, raises=False,
                 times=(0, 30, 65), expected_waits=0):
        self.assertIsNotNone(importlib.util.find_spec('tiktok_live.heartbeat'), 'Missing heartbeat lifecycle')
        from tiktok_live import heartbeat
        from tiktok_live.wss.protocol import encode_heartbeat, encode_enter_room, _encode
        from tiktok_live.heartbeat_socket import FrameEvidence
        evidence = FrameEvidence()
        if entered:
            evidence.record(encode_enter_room(42), 'sent')
        for index in range(beats):
            evidence.record(encode_heartbeat(beat_room), 'sent', now=index * 15)
        if response:
            evidence.record(_encode('im_enter_room_resp', b''), 'received')
        closed = []
        listener = SimpleNamespace(start=lambda **kw: started, error=None,
            connection=SimpleNamespace(room_id=room, connected=connected, evidence=evidence),
            close=lambda: closed.append(True))
        try:
            with patch.object(heartbeat, 'LiveMessageListener', return_value=listener), \
                 patch.object(heartbeat.time, 'sleep') as sleep, \
                 patch.object(heartbeat.time, 'monotonic', side_effect=times):
                with heartbeat._room_heartbeat_connection('venus', '42', 'sessionid=x', 'UA', connect_retries=0):
                    self.assertEqual(sleep.call_count, expected_waits)
                    if raises:
                        raise ValueError('post failed')
        finally:
            self.assertEqual(closed, [True])

    def test_three_heartbeats_and_thirty_seconds_allow_send(self):
        self.run_gate()

    def test_three_heartbeats_cannot_bypass_thirty_second_wait(self):
        self.run_gate(times=(0, 15, 30), expected_waits=1)

    def test_post_failure_is_released(self):
        with self.assertRaisesRegex(ValueError, 'post failed'):
            self.run_gate(raises=True)

    def test_failed_connection_wrong_room_and_missing_beats_block(self):
        for options in ({'started': False}, {'connected': False}, {'room': '43'},
                        {'beats': 0}, {'beats': 1}, {'beats': 2}, {'entered': False}, {'response': False},
                        {'beat_room': 43}):
            with self.subTest(options=options), self.assertRaises(RuntimeError):
                self.run_gate(**options)


if __name__ == '__main__':
    unittest.main()
