"""Observer-only routing; all proxy credentials here are synthetic."""
import importlib
import os
import socket
import threading
import time
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')

class ProxyInputTests(unittest.TestCase):
    def normalize(self, value):
        module = importlib.import_module('tiktok_live.wss.proxy')
        return module.normalize_observer_proxy(value)

    def test_shorthand_and_urls_preserve_credentials_with_remote_dns(self):
        cases = [('', ''), ('  ', ''), ('host.test:1080', 'socks5h://host.test:1080'),
            ('host.test:1080:user:p@ss:word', 'socks5h://user:p%40ss%3Aword@host.test:1080'),
            ('socks5://user:p%40ss@host.test:1080', 'socks5h://user:p%40ss@host.test:1080'),
            ('socks5h://[::1]:1080', 'socks5h://[::1]:1080')]
        for value, expected in cases:
            with self.subTest(value=value):
                self.assertEqual(self.normalize(value), expected)

    def test_invalid_explicit_proxy_does_not_become_direct_or_leak_secret(self):
        for value in ('host:0', 'host:65536', 'host:abc', 'host:1080:user',
                'http://user:secret@host:1080', 'socks5://host:1080/path',
                'socks5://user:secret@host:1080?x=1', 'host:1080:user:sec\nret',
                'socks5://user:@host:1080', 'socks5://host', 'direct',
                'socks5://user:%GG@host:1080'):
            with self.subTest(value=value):
                with self.assertRaises(ValueError) as error:
                    self.normalize(value)
                self.assertNotIn('secret', str(error.exception))
                self.assertNotIn(value, str(error.exception))

    def test_password_uri_delimiter_is_data_in_shorthand(self):
        self.assertEqual(self.normalize('host.test:1080:user:p://word'),
                         'socks5h://user:p%3A%2F%2Fword@host.test:1080')

    def test_credentials_reject_pysocks_multibyte_length_boundary(self):
        self.assertTrue(self.normalize('host.test:1080:user:' + 'a'*127))
        for user, password in (('u'*128, 'p'), ('u', 'p'*128), ('u', '密'*43)):
            with self.subTest(user_bytes=len(user.encode()), password_bytes=len(password.encode())):
                with self.assertRaises(ValueError):
                    self.normalize(f'host.test:1080:{user}:{password}')

class ProxySettingsTests(unittest.TestCase):
    def test_settings_roundtrip_default_and_safe_repr(self):
        from desktop.settings_store import DesktopSettings, save_desktop_settings, load_desktop_settings
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'settings.json'
            path.write_text('{}', encoding='utf8')
            self.assertEqual(load_desktop_settings(path).observer_proxy, '')
            value = DesktopSettings(observer_proxy='host.test:1080:user:secret')
            save_desktop_settings(path, value)
            loaded = load_desktop_settings(path)
            self.assertEqual(loaded.observer_proxy, value.observer_proxy)
            self.assertEqual(loaded.runner_kwargs()['observer_proxy'], 'socks5h://user:secret@host.test:1080')
            self.assertNotIn('secret', repr(loaded))

    def test_dialog_masks_validates_and_locks_proxy_while_running(self):
        from PySide6.QtWidgets import QApplication, QLineEdit
        from desktop.settings_store import DesktopSettings
        from desktop.dialogs import SettingsDialog
        app = QApplication.instance() or QApplication([])
        dialog = SettingsDialog(DesktopSettings())
        try:
            field = dialog.findChild(QLineEdit, 'observerProxy')
            self.assertIsNotNone(field)
            self.assertEqual(field.echoMode(), QLineEdit.EchoMode.Password)
            saved = []
            dialog.saved.connect(saved.append)
            field.setText('host:0:user:secret')
            dialog._save()
            self.assertEqual(saved, [])
            field.setText('host.test:1080:user:secret')
            dialog.set_read_only(True)
            self.assertTrue(field.isReadOnly())
            dialog._save()
            self.assertEqual(saved, [])
            dialog.set_read_only(False)
            dialog._save()
            self.assertEqual(saved[0].observer_proxy, 'host.test:1080:user:secret')
        finally:
            dialog.close()

class ProxyRoutingTests(unittest.TestCase):
    def test_listener_bootstrap_and_reconnect_keep_same_explicit_proxy(self):
        from listen_msg import LiveMessageListener
        from tiktok_live.wss.direct_models import DirectSession
        prepared, sessions = [], []
        drop, restored = threading.Event(), threading.Event()
        def bootstrap(username, room, proxy, scope):
            prepared.append(proxy)
            return DirectSession('delivery_observer', username, room, 'UA', 'ttwid=synthetic', proxy)
        class Connection:
            def __init__(inner, session, *, on_comment, on_connected, connect_timeout):
                inner.session, inner.room_id = session, session.room_id
                inner.connected = False
                inner.closed = threading.Event()
                inner.on_connected = on_connected
                sessions.append(session)
            def run(inner, stop):
                inner.connected = True
                inner.on_connected()
                if len(sessions) == 1:
                    drop.wait(2)
                    raise ConnectionError('synthetic')
                restored.set()
                stop.wait(2)
            def close(inner):
                inner.connected = False
                inner.closed.set()
        listener = LiveMessageListener('host', room_id='42', show_comments=False,
            observer_proxy='host.test:1080:user:secret', connection_factory=Connection,
            reconnect_delay=.001)
        try:
            with patch('tiktok_live.wss.visitor_api.bootstrap_visitor', side_effect=bootstrap):
                self.assertTrue(listener.start(2))
                drop.set()
                self.assertTrue(restored.wait(2))
                self.assertEqual(prepared, ['socks5h://user:secret@host.test:1080'])
                self.assertEqual(len(sessions), 2)
                self.assertTrue(all(s.proxy_url == prepared[0] for s in sessions))
        finally:
            drop.set()
            listener.close()
        self.assertFalse(listener._thread.is_alive())

    def test_account_listener_rejects_observer_proxy(self):
        from listen_msg import LiveMessageListener
        with self.assertRaises(ValueError):
            LiveMessageListener('host', connection_role='account_heartbeat',
                observer_proxy='host.test:1080:user:secret')

    def test_observer_proxy_is_used_without_changing_account_default(self):
        from tiktok_live.wss.sessions import prepare_observer, prepare_account
        from tiktok_live.wss.startup import StartupScope
        received = []
        def bootstrap(username, room_id, proxy, scope):
            received.append((username, room_id, proxy))
            return 'anonymous'
        with patch('tiktok_live.wss.visitor_api.bootstrap_visitor', side_effect=bootstrap), \
                patch('tiktok_live.wss.sessions.resolve_proxy', return_value='http://system.test:80') as default:
            with StartupScope(3) as scope:
                self.assertEqual(prepare_observer('host', known_room_id='42',
                    observer_proxy='host.test:1080:user:secret', startup_scope=scope), 'anonymous')
                self.assertEqual(default.call_count, 0)
                prepare_observer('host', known_room_id='42', observer_proxy='', startup_scope=scope)
            account = prepare_account('host', '42', 'sessionid=synthetic', 'UA')
            self.assertEqual(account.proxy_url, 'http://system.test:80')
        self.assertEqual(received, [('host', '42', 'socks5h://user:secret@host.test:1080'),
                                   ('host', '42', 'http://system.test:80')])

    def test_runner_listener_passes_explicit_proxy_and_retries_without_default(self):
        from tiktok_live.runner import _start_live_listener
        options = []
        def factory(username, **kwargs):
            options.append(kwargs)
            return SimpleNamespace(start=lambda **kw: False, close=lambda: None, error=None)
        with patch('tiktok_live.runner.LiveMessageListener', side_effect=factory):
            result = _start_live_listener('host', connect_timeout=1, connect_retries=1,
                retry_delay_seconds=0, show_comments=False, resolve_room_id=lambda: '42',
                observer_proxy='socks5h://user:secret@host.test:1080')
        self.assertIsNone(result)
        self.assertEqual(len(options), 2)
        self.assertTrue(all(o['observer_proxy'] == 'socks5h://user:secret@host.test:1080' for o in options))


class LocalSocksPeer:
    """Test-only SOCKS server: real wire authentication and CONNECT, no external I/O."""
    def __init__(self, *, auth=True, reject=False, stall=False):
        self.server = socket.socket()
        self.server.bind(('127.0.0.1', 0))
        self.server.listen(1)
        self.server.settimeout(3)
        self.port = self.server.getsockname()[1]
        self.client = None
        self.auth, self.destination, self.error = None, None, None
        self.ready, self.stop = threading.Event(), threading.Event()
        def read(sock, count):
            result = b''
            while len(result) < count:
                chunk = sock.recv(count-len(result))
                if not chunk:
                    raise EOFError()
                result += chunk
            return result
        def run():
            try:
                sock, _ = self.server.accept()
                self.client = sock
                sock.settimeout(2)
                version, count = read(sock, 2)
                if version != 5:
                    raise AssertionError('Expected SOCKS5, not HTTP CONNECT')
                read(sock, count)
                self.ready.set()
                if stall:
                    self.stop.wait(2)
                    return
                sock.sendall(b'\x05\x02' if auth else b'\x05\x00')
                if auth:
                    version, size = read(sock, 2)
                    user = read(sock, size)
                    password = read(sock, read(sock, 1)[0])
                    self.auth = (user, password)
                    sock.sendall(b'\x01\x01' if reject else b'\x01\x00')
                    if reject:
                        return
                ver, cmd, _, kind = read(sock, 4)
                if (ver, cmd, kind) != (5, 1, 3):
                    raise AssertionError('Target DNS must be resolved by proxy')
                host = read(sock, read(sock, 1)[0]).decode('ascii')
                port = int.from_bytes(read(sock, 2), 'big')
                self.destination = (host, port)
                sock.sendall(b'\x05\x00\x00\x01\x7f\x00\x00\x01\x01\xbb')
                self.stop.wait(2)
            except Exception as exc:
                self.error = exc
            finally:
                if self.client:
                    self.client.close()
        self.thread = threading.Thread(target=run, daemon=True)
        self.thread.start()

    def close(self):
        self.stop.set()
        if self.client:
            self.client.close()
        self.server.close()
        self.thread.join(4)


class SocksTransportTests(unittest.TestCase):
    def test_real_socks_authentication_remote_dns_and_tls_hostname(self):
        from tiktok_live.wss.isolated_socket import _connect_stream
        from tiktok_live.wss.startup import StartupScope
        for auth in (True, False):
            with self.subTest(auth=auth):
                peer = LocalSocksPeer(auth=auth)
                wrapped = []
                # TLS itself stays covered by live testing; this peer validates SOCKS bytes.
                def wrap(raw, *, server_hostname, do_handshake_on_connect):
                    wrapped.append((server_hostname, do_handshake_on_connect))
                    return SimpleNamespace(settimeout=raw.settimeout,
                        do_handshake=lambda:None, close=raw.close)
                credentials = 'user:p%40ss%3Aword@' if auth else ''
                session = SimpleNamespace(proxy_url=f'socks5h://{credentials}127.0.0.1:{peer.port}')
                try:
                    with StartupScope(2) as scope, patch('tiktok_live.wss.isolated_socket.ssl.create_default_context',
                            return_value=SimpleNamespace(wrap_socket=wrap)):
                        _connect_stream(session, 'wss://webcast-ws.tiktok.com/path', 2, resource_owner=scope.own)
                    self.assertEqual(peer.destination, ('webcast-ws.tiktok.com', 443))
                    self.assertEqual(peer.auth, (b'user', b'p@ss:word') if auth else None)
                    self.assertEqual(wrapped, [('webcast-ws.tiktok.com', False)])
                    self.assertIsNone(peer.error)
                finally:
                    peer.close()

    def test_auth_failure_closes_owned_socket_and_never_tries_direct(self):
        from tiktok_live.wss import isolated_socket
        from tiktok_live.wss.startup import StartupScope
        peer = LocalSocksPeer(reject=True)
        owned = []
        try:
            with patch.object(isolated_socket, '_get_addrinfo_list', side_effect=AssertionError('No direct route')):
                with self.assertRaises(Exception):
                    with StartupScope(2) as scope:
                        def own(sock):
                            owned.append(sock)
                            return scope.own(sock)
                        isolated_socket._connect_stream(SimpleNamespace(proxy_url=f'socks5h://user:secret@127.0.0.1:{peer.port}'),
                            'wss://webcast-ws.tiktok.com/path', 2, resource_owner=own)
            self.assertEqual(peer.auth, (b'user', b'secret'))
            self.assertTrue(owned)
            self.assertTrue(all(s.fileno() == -1 for s in owned))
        finally:
            peer.close()

    def test_cancel_interrupts_pending_socks_authentication(self):
        from tiktok_live.wss.isolated_socket import _connect_stream
        from tiktok_live.wss.startup import StartupScope
        peer = LocalSocksPeer(stall=True)
        scope = StartupScope(5)
        errors = []
        def connect():
            try:
                _connect_stream(SimpleNamespace(proxy_url=f'socks5h://127.0.0.1:{peer.port}'),
                    'wss://webcast-ws.tiktok.com/path', 5, resource_owner=scope.own)
            except Exception as exc:
                errors.append(type(exc).__name__)
        worker = threading.Thread(target=connect, daemon=True)
        try:
            worker.start()
            self.assertTrue(peer.ready.wait(1))
            scope.cancel()
            worker.join(1)
            self.assertFalse(worker.is_alive())
            self.assertTrue(errors)
        finally:
            scope.__exit__(None, None, None)
            peer.close()
            worker.join(3)

if __name__ == '__main__':
    unittest.main()
