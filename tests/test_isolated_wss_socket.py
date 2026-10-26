import importlib.util
import unittest
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import patch
from tests.support.local_wss_peer import LocalPeer


class IsolatedSocketTests(unittest.TestCase):
    def test_transport_and_upgrade_share_one_startup_budget(self):
        import time
        from types import SimpleNamespace
        from tiktok_live.wss.direct_models import HandshakeResult
        now = [0.0]
        sock = SimpleNamespace(settimeout=lambda n: None, close=lambda: None)
        def connect(*args, **kw):
            now[0] = 2.9
            return sock
        session = self.Session('account_heartbeat', 'host', '42', 'UA', 'sessionid=SYNTHETIC')
        with patch.object(time, 'monotonic', side_effect=lambda: now[0]), \
                patch.object(self.s, '_connect_stream', side_effect=connect), \
                patch.object(self.s, 'perform_handshake', return_value=HandshakeResult(101, {}, None)) as upgrade:
            ws = self.s.open_socket(session, self.url, timeout=3)
            self.assertLess(upgrade.call_args.kwargs['timeout'], .11)
            ws.shutdown()

    def test_trickling_partial_frame_yields_without_losing_bytes(self):
        from types import SimpleNamespace
        from websocket import WebSocketTimeoutException
        now = [0.0]
        raw = bytearray(b'\x82\x0a0123456789')
        class SlowSocket:
            timeout = 1.0
            def settimeout(inner, value):
                inner.timeout = value
            def gettimeout(inner):
                return inner.timeout
            def recv(inner, size):
                now[0] += .2
                return bytes([raw.pop(0)])
            def close(inner):
                pass
        ws = self.s.IsolatedWebSocket()
        ws.sock = SlowSocket()
        ws.connected = True
        with patch.object(self.s, 'time', SimpleNamespace(monotonic=lambda: now[0])):
            with self.assertRaises(WebSocketTimeoutException):
                ws.recv_frame()
            self.assertLessEqual(now[0], 1.21)
            frame = None
            for _ in range(4):
                try:
                    frame = ws.recv_frame()
                    break
                except WebSocketTimeoutException:
                    pass
            self.assertIsNotNone(frame)
            self.assertEqual(frame.data, b'0123456789')

    def setUp(self):
        self.assertIsNotNone(importlib.util.find_spec('tiktok_live.wss.isolated_socket'))
        from tiktok_live.wss import isolated_socket, direct_signer
        from tiktok_live.wss.direct_models import DirectSession
        self.s = isolated_socket
        self.Session = DirectSession
        self.url = direct_signer.build_direct_url('42', user_agent='UA')

    def connect(self, peer, role='account_heartbeat', cookie='sessionid=SYNTHETIC'):
        session = self.Session(role, 'host', '42', 'UA', cookie)
        with patch.object(self.s, '_connect_stream', return_value=peer.client):
            return self.s.open_socket(session, self.url, timeout=3)

    def test_concurrent_roles_and_set_cookie_never_cross_sessions(self):
        from websocket._handshake import CookieJar
        with patch.object(CookieJar, 'get', return_value='sessionid=GLOBAL') as read, \
                patch.object(CookieJar, 'add', side_effect=AssertionError('Global cache write')):
            def handshake(value):
                peer = LocalPeer()
                try:
                    role = 'delivery_observer' if value.startswith('ttwid') else 'account_heartbeat'
                    # No patch of shared functions across worker threads.
                    self.s.perform_handshake(peer.client, self.url, cookie_header=value, user_agent='UA', timeout=3)
                    cookies = [line for line in peer.request.decode().split('\r\n') if line.startswith('Cookie:')]
                    self.assertEqual(cookies, ['Cookie: ' + value])
                    return peer.request
                finally:
                    peer.close()
            with ThreadPoolExecutor(3) as pool:
                requests = list(pool.map(handshake, ['sessionid=A', 'sessionid=B', 'ttwid=VISITOR']))
            self.assertEqual(len(requests), 3)
            read.assert_not_called()

    def test_header_and_first_frame_in_one_write_preserve_frame(self):
        peer = LocalPeer()
        try:
            ws = self.connect(peer)
            frame = ws.recv_frame()
            self.assertEqual((frame.opcode, frame.data, frame.fin), (2, b'OK', 1))
            self.assertTrue(ws.connected)
            # shutdown avoids waiting for a peer close response in this test.
            ws.shutdown()
        finally:
            peer.close()

    def test_duplicate_server_timing_does_not_reject_valid_upgrade(self):
        peer = LocalPeer(extra_headers='Server-Timing: edge;dur=1\r\n'
                                       'Server-Timing: origin;dur=2\r\n')
        try:
            ws = self.connect(peer, role='delivery_observer', cookie='ttwid=VISITOR')
            self.assertTrue(ws.connected)
            self.assertEqual(ws.recv_frame().data, b'OK')
            ws.shutdown()
        finally:
            peer.close()

    def test_duplicate_security_critical_accept_remains_rejected(self):
        peer = LocalPeer(extra_headers='Sec-WebSocket-Accept: wrong\r\n')
        try:
            with self.assertRaises(ValueError):
                self.connect(peer)
        finally:
            peer.close()

    def test_bad_accept_redirect_and_oversize_headers_never_connect(self):
        for options in ({'bad_accept': True}, {'status': 302}, {'status': 417}, {'oversized': True}):
            peer = LocalPeer(**options)
            try:
                with self.subTest(options=options), self.assertRaises(ValueError) as caught:
                    self.connect(peer)
                self.assertNotIn('SERVER', str(caught.exception))
                self.assertNotIn('SYNTHETIC', str(caught.exception))
            finally:
                peer.close()

    def test_destination_and_role_cookie_are_checked_before_stream_creation(self):
        from tiktok_live.wss.direct_models import DirectSession
        session = DirectSession('delivery_observer', 'host', '42', 'UA', 'sessionid=LOGIN')
        with patch.object(self.s, '_connect_stream') as connect:
            with self.assertRaises(ValueError):
                self.s.open_socket(session, self.url, timeout=3)
            connect.assert_not_called()

    def test_actual_wire_fragment_ping_ack_and_close(self):
        import threading
        from websocket import ABNF, WebSocket
        from tiktok_live.wss.connection import DirectConnection
        from tiktok_live.wss.initial_fetch import InitialFetch
        from tiktok_live.wss.protocol import _encode, _wire, decode_push_frame
        def frame(data, opcode, fin=1):
            value = ABNF.create_frame(data, opcode, fin)
            value.mask_value = 0
            return value.format()
        raw = _encode('msg', _wire(5, 'wire-ext') + _wire(9, 1), 18446744073709551614)
        tail = frame(raw[:5], 2, 0) + frame(b'ping', 9) + frame(raw[5:], 0)
        peer = LocalPeer(tail=tail)
        stop = threading.Event()
        errors = []
        initial = InitialFetch(
            'server-cursor',
            'wss://webcast-ws.tiktok.com/webcast/im/ws_proxy/ws_reuse_supplement/',
            (('wrss', 'server-route'),), '', 0,
        )
        session = self.Session(
            'account_heartbeat', 'host', '42', 'UA', 'sessionid=WIRE',
            initial_fetch=initial,
        )
        def factory(session, url, **kw):
            with patch.object(self.s, '_connect_stream', return_value=peer.client):
                return self.s.open_socket(session, url, **kw)
        connection = DirectConnection(session, on_comment=lambda e: None,
            on_connected=lambda: None, socket_factory=factory, connect_timeout=3)
        def run():
            try:
                connection.run(stop)
            except RuntimeError:
                errors.append('peer closed')
        thread = threading.Thread(target=run)
        thread.start()
        try:
            peer.thread.join(4)  # upgrade reader must finish before frame reader owns server stream
            self.assertFalse(peer.thread.is_alive())
            reader = WebSocket()
            reader.sock = peer.server
            frames = [reader.recv_frame() for _ in range(4)]
            self.assertTrue(all(item.mask_value == 1 for item in frames))
            binary = [decode_push_frame(item.data) for item in frames if item.opcode == 2]
            self.assertEqual([item.payload_type for item in binary], ['im_enter_room', 'hb', 'ack'])
            self.assertEqual(binary[-1].log_id, 18446744073709551614)
            self.assertTrue(any(item.opcode == 10 and item.data == b'ping' for item in frames))
            peer.server.sendall(frame(b'', 8))
            thread.join(4)
            self.assertFalse(thread.is_alive())
            self.assertFalse(connection.connected)
        finally:
            stop.set()
            connection.close()
            thread.join(4)
            peer.close()
        session = self.Session('account_heartbeat', 'host', '42', 'UA', 'sessionid=LOGIN')
        with patch.object(self.s, '_connect_stream') as connect:
            with self.assertRaises(ValueError):
                self.s.open_socket(session, self.url.replace('.tiktok.com', '.evil.test'), timeout=3)
            connect.assert_not_called()
