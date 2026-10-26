import threading
import unittest
from types import SimpleNamespace

from loguru import logger
from tiktok_live.wss.connection import DirectConnection
from tiktok_live.wss.direct_models import DirectSession
from tiktok_live.wss.initial_fetch import InitialFetch
from tiktok_live.wss.protocol import decode_push_frame


class HeartbeatLoggingTests(unittest.TestCase):
    def exercise(self, role, *, fail_heartbeat=False, cancelled=False):
        now = [0.0]
        sent = []
        records = []
        stop = threading.Event()
        class Socket:
            def send(self, payload, opcode=2):
                kind = decode_push_frame(payload).payload_type
                if kind == 'hb' and fail_heartbeat:
                    raise OSError('synthetic-private-token')
                sent.append(kind)
            def recv_frame(self):
                now[0] += 5
                if now[0] >= 16:
                    stop.set()
                raise TimeoutError()
            def shutdown(self):
                pass
        # Both roles now require a validated server-issued initial route.
        initial = InitialFetch('server-cursor',
            'wss://webcast-ws.tiktok.com/webcast/im/ws_proxy/ws_reuse_supplement/',
            (('wrss', 'server-route'),), '', 0)
        session = DirectSession(role, 'host', '42', 'UA', 'synthetic-private-cookie',
                                heartbeat_interval_seconds=5, initial_fetch=initial)
        connection = DirectConnection(session, on_comment=lambda event: None,
            on_connected=lambda: None, socket_factory=lambda *a, **kw: Socket(),
            clock=lambda: now[0])
        if cancelled:
            stop.set()
        handle = logger.add(lambda msg: records.append(msg.record), format='{message}')
        try:
            if fail_heartbeat:
                with self.assertRaises(RuntimeError):
                    connection.run(stop)
            else:
                connection.run(stop)
        finally:
            logger.remove(handle)
        return sent, records

    def test_each_successful_heartbeat_is_logged_for_both_connection_roles(self):
        for role in ('account_heartbeat', 'delivery_observer'):
            with self.subTest(role=role):
                sent, records = self.exercise(role)
                logs = [item['message'] for item in records
                        if 'classification=wss_heartbeat_sent' in item['message']]
                self.assertEqual(len(logs), sent.count('hb'))
                self.assertEqual(len(logs), 4)
                for count, text in enumerate(logs, 1):
                    self.assertIn('room_id=42', text)
                    self.assertIn('username=host', text)
                    self.assertIn('role=' + role, text)
                    self.assertIn('count=' + str(count), text)
                    self.assertIn('interval_seconds=5', text)
                    self.assertNotIn('synthetic-private', text)

    def test_failed_heartbeat_has_failure_log_but_no_success_log(self):
        sent, records = self.exercise('account_heartbeat', fail_heartbeat=True)
        self.assertNotIn('hb', sent)
        self.assertTrue(any('classification=wss_heartbeat_send_failed' in item['message']
                            for item in records))
        self.assertFalse(any('classification=wss_heartbeat_sent' in item['message']
                             for item in records))
        self.assertNotIn('synthetic-private', str([item['message'] for item in records]))

    def test_cancelled_connection_never_claims_a_heartbeat_was_sent(self):
        sent, records = self.exercise('delivery_observer', cancelled=True)
        self.assertEqual(sent, [])
        self.assertFalse(any('wss_heartbeat_sent' in item['message'] for item in records))
