import unittest
from contextlib import nullcontext
from types import SimpleNamespace
from unittest.mock import patch

import send_live_message as facade
from tiktok_live import client


class ChatCsrfFlowTests(unittest.TestCase):
    def setUp(self):
        gate = patch.object(client, 'room_heartbeat',
                            side_effect=lambda *a, **kw: nullcontext())
        gate.start()
        self.addCleanup(gate.stop)

    def run_flow(self, raw_token):
        calls = []

        def head(url, **kwargs):
            calls.append(('HEAD', url, kwargs))
            return SimpleNamespace(headers={'x-ware-csrf-token': raw_token})

        def post(url, **kwargs):
            calls.append(('POST', url, kwargs))
            return SimpleNamespace(status_code=200, json=lambda: {'status_code': 0})

        session = SimpleNamespace(head=head, post=post, close=lambda: None)
        self.calls = calls
        with patch.object(client.requests, 'Session', return_value=session), \
                patch.object(client, 'get_fresh_ms_token', return_value='fresh-ms'), \
                patch.object(client, 'get_sec_sdk_csrf_token', facade._ORIGINAL_GET_SEC_SDK_CSRF_TOKEN), \
                patch.object(client, 'post_chat_request', facade._ORIGINAL_POST_CHAT_REQUEST), \
                patch.object(client, 'fetch_initial_response', return_value=object()), \
                patch.object(client, 'build_headers_from_cookie_state', return_value={
                    'tt-ticket-guard-client-data': 'fresh-synthetic-data',
                    'tt-ticket-guard-public-key': 'synthetic-key'}), \
                patch.object(client, 'sign_url', side_effect=lambda url, *a, **kw: url), \
                patch.object(client.time, 'sleep'):
            return facade._ORIGINAL_CLIENT_SEND(
                'host', 's_v_web_id=verify_test; sessionid=synthetic', 'hello',
                known_room_id='7696000000000000001',
                ticket_guard_state={'ts_sign': 'synthetic', 'encrypt_ticket': 'synthetic'},
                ticket_guard_headers={'tt-ticket-guard-client-data': 'synthetic-data',
                                      'tt-ticket-guard-public-key': 'synthetic-key'},
            )

    def test_head_token_is_attached_to_post_in_same_session(self):
        result = self.run_flow('1,synthetic-token,unused,unused,synthetic-sign')
        self.assertTrue(result['ok'])
        self.assertEqual([c[0] for c in self.calls], ['HEAD', 'POST'])
        head, post = self.calls
        self.assertIn('/room/chat/', post[1])
        self.assertEqual(head[1], client.CHAT_URL)
        self.assertEqual(head[2]['headers']['x-secsdk-csrf-request'], '1')
        self.assertEqual(post[2]['headers']['x-secsdk-csrf-token'],
                         'synthetic-token,synthetic-sign')
        self.assertEqual(head[2]['headers']['cookie'], post[2]['headers']['cookie'])
        self.assertEqual(head[2]['headers']['user-agent'], post[2]['headers']['user-agent'])

    def test_missing_token_stops_before_chat_post(self):
        with self.assertRaises(client.SafeOperationError) as caught:
            self.run_flow('')
        self.assertEqual(caught.exception.classification, 'csrf_token_unavailable')
        self.assertTrue(self.calls)
        self.assertFalse(any('/room/chat/' in c[1] and c[0] == 'POST' for c in self.calls))

