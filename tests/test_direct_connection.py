import threading
import unittest
from types import SimpleNamespace
from unittest.mock import patch
from tiktok_live.wss.direct_models import DirectSession
from tiktok_live.wss.initial_fetch import InitialFetch
from tiktok_live.wss.protocol import _encode, _wire, encode_heartbeat, encode_enter_room


class DirectConnectionTests(unittest.TestCase):
    def connection(self, frames, *, fail=False):
        from tiktok_live.wss.connection import DirectConnection
        stop = threading.Event()
        now = [0.0]
        class Socket:
            connected = True
            sent = []
            def send(inner, payload, opcode=2):
                if fail:
                    raise OSError('synthetic secret')
                inner.sent.append((opcode, payload))
            def recv_frame(inner):
                now[0] += 10
                try:
                    return next(frames)
                except StopIteration:
                    stop.set()
                    raise TimeoutError()
            def shutdown(inner):
                inner.connected = False
        sock = Socket()
        events = []
        initial = InitialFetch('server-cursor',
            'wss://webcast-ws.tiktok.com/webcast/im/ws_proxy/ws_reuse_supplement/',
            (('wrss', 'server-route'),), '', 0)
        connection = DirectConnection(DirectSession(
            'account_heartbeat', 'host', '42', 'Mozilla/5.0 Chrome/150.0.0.0',
            'sessionid=A', initial_fetch=initial),
            on_comment=events.append, on_connected=lambda: None,
            socket_factory=lambda *a, **kw: sock, clock=lambda: now[0])
        return connection, sock, stop, events

    def test_busy_idle_and_timed_out_receives_keep_heartbeats(self):
        frames = iter([SimpleNamespace(opcode=2, data=_encode('im_enter_room_resp', b''), fin=1)] * 4)
        connection, sock, stop, _ = self.connection(frames)
        connection.run(stop)
        self.assertGreaterEqual(connection.evidence.snapshot()['sent']['hb'], 4)
        self.assertTrue(connection.evidence.ready('42', elapsed=30, connected=True))
        self.assertFalse(connection.connected)

    def test_exceeding_cli_limits_does_not_stop_owned_connection(self):
        frames = iter([SimpleNamespace(opcode=2, data=_encode('msg', b''), fin=1)] * 10001)
        connection, sock, stop, _ = self.connection(frames)
        connection.run(stop)
        self.assertEqual(connection.evidence.snapshot()['received']['msg'], 10001)
        self.assertTrue(stop.is_set())

    def test_only_successful_target_room_sends_and_server_entry_count(self):
        from tiktok_live.heartbeat_socket import FrameEvidence
        e = FrameEvidence()
        e.record(encode_enter_room(42), 'sent')
        for stamp in [0, 10, 20]:
            e.record(encode_heartbeat(42), 'sent', now=stamp)
        self.assertFalse(e.ready('42', elapsed=30, connected=True))
        e.record(_encode('im_enter_room_resp', b''), 'received')
        self.assertTrue(e.ready('42', elapsed=30, connected=True))
        for room, elapsed, connected in [('43', 30, True), ('42', 29, True), ('42', 30, False)]:
            self.assertFalse(e.ready(room, elapsed=elapsed, connected=connected))
        connection, sock, stop, _ = self.connection(iter([]), fail=True)
        with self.assertRaises(RuntimeError) as caught:
            connection.run(stop)
        self.assertNotIn('secret', str(caught.exception))
        self.assertEqual(connection.evidence.snapshot()['sent'], {})

    def test_fragment_ping_ack_cancel_and_malformed_frame(self):
        raw = _encode('msg', _wire(5, 'ext') + _wire(9, 1), 123)
        frames = iter([SimpleNamespace(opcode=2, data=raw[:3], fin=0),
                       SimpleNamespace(opcode=9, data=b'ping', fin=1),
                       SimpleNamespace(opcode=0, data=raw[3:], fin=1),
                       SimpleNamespace(opcode=2, data=b'\xff', fin=1)])
        connection, sock, stop, _ = self.connection(frames)
        with self.assertRaises(RuntimeError):
            connection.run(stop)
        self.assertIn((10, b'ping'), sock.sent)
        self.assertEqual(connection.evidence.snapshot()['sent']['ack'], 1)
        connection.close()
        connection.close()
        self.assertFalse(sock.connected)

    def test_initial_cancel_does_not_create_socket(self):
        connection, sock, stop, _ = self.connection(iter([]))
        stop.set()
        with patch.object(connection, '_socket_factory') as create:
            connection.run(stop)
            create.assert_not_called()
