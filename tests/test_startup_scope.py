import threading
import time
import unittest
from unittest.mock import patch


class StartupScopeTests(unittest.TestCase):
    def test_curl_visitor_body_read_is_interrupted_by_shared_startup_deadline(self):
        from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
        from tiktok_live.wss import sessions, visitor_api
        from tiktok_live.wss.startup import StartupScope
        body_entered, closed = threading.Event(), threading.Event()
        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass
            def do_POST(self):
                self.rfile.read(int(self.headers.get('content-length', '0')))
                self.send_response(200)
                self.send_header('content-type', 'application/json')
                self.send_header('content-length', '999')
                self.end_headers()
                self.wfile.write(b'{')
                self.wfile.flush()
                body_entered.set()
                self.connection.settimeout(1)
                try:
                    while self.connection.recv(1):
                        pass
                    closed.set()
                except OSError:
                    pass
        server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        thread = threading.Thread(target=lambda: server.serve_forever(poll_interval=.01), daemon=True)
        thread.start()
        started = time.monotonic()
        try:
            with patch.object(visitor_api, 'ORIGIN', 'http://127.0.0.1:' + str(server.server_port)), \
                    StartupScope(.2) as scope:
                with self.assertRaises(Exception):
                    sessions.prepare_observer('host', known_room_id='42',
                        proxy_setting='direct', startup_scope=scope)
            self.assertTrue(body_entered.is_set())
            self.assertTrue(closed.wait(.5), 'Cancelled curl request must release its actual TCP stream')
            self.assertLess(time.monotonic() - started, .8)
        finally:
            server.shutdown()
            server.server_close()
            thread.join(1)

    def test_default_transports_cancel_pending_tcp_and_tls(self):
        import socket
        from types import SimpleNamespace
        from tiktok_live.wss import isolated_socket as transport, sessions
        from tiktok_live.wss.direct_models import DirectSession
        from tiktok_live.wss.direct_signer import build_direct_url
        from tiktok_live.wss.startup import StartupScope
        address = [(socket.AF_INET, socket.SOCK_STREAM, 6, '', ('127.0.0.1', 443))]
        for kind in ('tcp', 'tls'):
            closed = threading.Event()
            class Stream:
                def settimeout(self, value):
                    pass
                def setsockopt(self, *args):
                    pass
                def connect(self, endpoint):
                    if kind == 'tcp':
                        if not closed.wait(.5):
                            raise AssertionError('TCP was not cancellation-owned')
                        raise OSError('cancelled')
                def do_handshake(self):
                    if not closed.wait(.5):
                        raise AssertionError('TLS was not cancellation-owned')
                    raise OSError('cancelled')
                def shutdown(self, *args):
                    closed.set()
                def close(self):
                    pass
            raw, tls = Stream(), Stream()
            context = SimpleNamespace(wrap_socket=lambda *a, **kw: tls,
                check_hostname=True, verify_mode=2, post_handshake_auth=False,
                set_alpn_protocols=lambda *a: None)
            started = time.monotonic()
            with self.subTest(kind=kind), patch.object(socket, 'socket', return_value=raw):
                session = DirectSession('account_heartbeat', 'host', '42', 'UA', 'sessionid=SYNTHETIC')
                with patch.object(transport, '_get_addrinfo_list', return_value=(address, False, None)), \
                        patch.object(transport.ssl, 'create_default_context', return_value=context):
                    with self.assertRaises(ValueError):
                        transport.open_socket(session, build_direct_url('42', user_agent='UA'), timeout=.04)
                self.assertTrue(closed.is_set())
                self.assertLess(time.monotonic() - started, .4)

    def test_deadline_closes_pending_resource_without_waiting_for_handshake(self):
        from tiktok_live.wss.startup import StartupScope
        closed = threading.Event()
        class Resource:
            def shutdown(self, *args):
                closed.set()
            def close(self):
                closed.set()
        # Allow resource registration even when Windows schedules this test
        # alongside the release build; still verify timer-driven closure.
        with StartupScope(.2) as scope:
            scope.own(Resource())
            self.assertTrue(closed.wait(1))
            with self.assertRaises(TimeoutError):
                scope.remaining()

    def test_late_resource_is_closed_and_success_disarms_deadline(self):
        from tiktok_live.wss.startup import StartupScope
        class Resource:
            closed = False
            def close(self):
                self.closed = True
        with StartupScope(.03) as scope:
            scope.cancel()
            resource = Resource()
            with self.assertRaises(TimeoutError):
                scope.own(resource)
            self.assertTrue(resource.closed)
        resource = Resource()
        with StartupScope(.03) as scope:
            scope.own(resource)
            scope.finish()
        time.sleep(.05)
        self.assertFalse(resource.closed)
