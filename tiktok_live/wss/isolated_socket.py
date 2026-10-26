"""Instance-owned HTTP upgrade; websocket-client is used only for TLS/frames.

websocket-client 1.7.0 is Apache-2.0 licensed. Its default handshake (and
module-global CookieJar) is deliberately never called here.
"""
import base64
import hashlib
import hmac
import secrets
import socket
import ssl
import time
from urllib.parse import unquote, urlsplit,parse_qs

import websocket
import socks
from websocket._abnf import frame_buffer
from websocket._http import _get_addrinfo_list, _tunnel, proxy_info

from .direct_models import HandshakeResult
from .sessions import validate_destination, validate_header
from .startup import StartupScope


class BoundedFrameBuffer(frame_buffer):
    def recv_length(self):
        super().recv_length()
        if self.length > 2 * 1024 * 1024:
            raise ValueError('WSS frame exceeds limit')


def perform_handshake(sock, url, *, cookie_header, user_agent, timeout):
    parts = urlsplit(url)
    validate_header(cookie_header)
    validate_header(user_agent, limit=2048)
    key = base64.b64encode(secrets.token_bytes(16)).decode('ascii')
    resource = parts.path + ('?' + parts.query if parts.query else '')
    request = (f'GET {resource} HTTP/1.1\r\nHost: {parts.hostname}\r\n'
               f'Upgrade: websocket\r\nConnection: Upgrade\r\n'
               f'Sec-WebSocket-Key: {key}\r\nSec-WebSocket-Version: 13\r\n'
               f'Origin: https://www.tiktok.com\r\nUser-Agent: {user_agent}\r\n'
               f'Cookie: {cookie_header}\r\n\r\n')
    deadline = time.monotonic() + timeout
    try:
        sock.settimeout(max(.001, timeout))
        sock.sendall(request.encode('ascii'))
        raw = bytearray()
        while not raw.endswith(b'\r\n\r\n'):
            remaining = deadline - time.monotonic()
            if remaining <= 0 or len(raw) >= 32768:
                raise ValueError('WSS upgrade header limit')
            sock.settimeout(remaining)
            piece = sock.recv(1)  # never consumes the first frame
            if not piece:
                raise ValueError('WSS upgrade closed')
            raw.extend(piece)
        lines = raw.decode('ascii').split('\r\n')
        if lines[0].split(' ', 2)[:2] != ['HTTP/1.1', '101']:
            raise ValueError('WSS upgrade rejected')
        headers = {}
        for line in lines[1:-2]:
            name, separator, value = line.partition(':')
            name = name.lower()
            if not separator or not name:
                raise ValueError('WSS upgrade invalid headers')
            if name == 'set-cookie':
                continue  # Never retain server cookies in the handshake result.
            if name in headers:
                if name == 'server-timing':
                    continue  # TikTok can send multiple timing entries on a valid 101.
                raise ValueError('WSS upgrade invalid headers')
            headers[name] = value.strip()
        expected = base64.b64encode(hashlib.sha1(
            (key + '258EAFA5-E914-47DA-95CA-C5AB0DC85B11').encode('ascii')).digest()).decode()
        if (headers.get('upgrade', '').lower() != 'websocket'
                or 'upgrade' not in [x.strip().lower() for x in headers.get('connection', '').split(',')]
                or not hmac.compare_digest(headers.get('sec-websocket-accept', ''), expected)
                or 'sec-websocket-extensions' in headers
                or 'sec-websocket-protocol' in headers):
            raise ValueError('WSS upgrade validation failed')
        headers.pop('set-cookie', None)
        return HandshakeResult(101, headers, None)
    except Exception:
        raise ValueError('WSS upgrade failed') from None


class DeadlineSocksSocket(socks.socksocket):
    """Bound PySocks negotiation reads and make Windows cancellation prompt.

    PySocks' unbuffered makefile reader retains the descriptor after close(),
    which can leave a Windows authentication read blocked. Read directly from
    the socket with short polling windows instead; no bytes are prefetched.
    """
    def __init__(self, family, *, deadline):
        super().__init__(family)
        self._negotiation_deadline = deadline

    def _readall(self, file, count):
        data = bytearray()
        while len(data) < count:
            if self._closed:
                raise OSError('SOCKS connection cancelled')
            remaining = self._negotiation_deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError('SOCKS negotiation deadline')
            socket.socket.settimeout(self, min(.1, remaining))
            try:
                chunk = socket.socket.recv(self, count - len(data))
            except socket.timeout:
                continue
            finally:
                if not self._closed:
                    socket.socket.settimeout(self, self._timeout)
            if not chunk:
                raise OSError('SOCKS connection closed')
            data.extend(chunk)
        return bytes(data)


def _connect_stream(session, url, timeout, *, resource_owner):
    proxy_options = {'http_no_proxy': ['*']}  # resolve_proxy already applied
    proxy = None
    if session.proxy_url:
        proxy = urlsplit(session.proxy_url)
        proxy_options = dict(http_proxy_host=proxy.hostname,
                             http_proxy_port=proxy.port,
                             proxy_type=proxy.scheme,
                             http_proxy_timeout=timeout,
                             http_no_proxy=[])
        if proxy.username is not None:
            proxy_options['http_proxy_auth'] = (unquote(proxy.username), unquote(proxy.password or ''))
    target = urlsplit(url)
    host, port = target.hostname, target.port or 443
    deadline = time.monotonic() + timeout
    def remaining():
        value = deadline - time.monotonic()
        if value <= 0:
            raise TimeoutError('WSS transport deadline')
        return value
    if proxy is not None and proxy.scheme in ('socks5', 'socks5h'):
        # Own the socket BEFORE authentication can block, so cancellation and
        # the common startup timer interrupt it. Never patch global sockets.
        family = socket.AF_INET6 if ':' in proxy.hostname else socket.AF_INET
        stream = resource_owner(DeadlineSocksSocket(family, deadline=deadline))
        stream.set_proxy(socks.SOCKS5, proxy.hostname, proxy.port, rdns=True,
            username=unquote(proxy.username) if proxy.username is not None else None,
            password=unquote(proxy.password) if proxy.password is not None else None)
        stream.settimeout(remaining())
        stream.connect((host, port))
    else:
        addresses, tunnel, auth = _get_addrinfo_list(host, port, True, proxy_info(**proxy_options))
        # Preserve the existing direct/HTTP CONNECT path for account sessions.
        stream = None
        for family, kind, proto, _, address in addresses:
            raw = resource_owner(socket.socket(family, kind, proto))
            raw.settimeout(remaining())
            try:
                raw.connect(address)
                stream = raw
                break
            except OSError:
                raw.close()
        if stream is None:
            raise ValueError('WSS transport unavailable')
        if tunnel:
            stream.settimeout(remaining())
            _tunnel(stream, host, port, auth)
    context = ssl.create_default_context()
    encrypted = context.wrap_socket(stream, server_hostname=host, do_handshake_on_connect=False)
    resource_owner(encrypted)
    encrypted.settimeout(remaining())
    encrypted.do_handshake()
    return encrypted


class IsolatedWebSocket(websocket.WebSocket):
    def __init__(self):
        super().__init__(enable_multithread=True)
        self.frame_buffer = BoundedFrameBuffer(self._recv, False)
        self._read_deadline = None
        self.read_window_seconds = 1.0

    def recv_frame(self):
        self._read_deadline = time.monotonic() + self.read_window_seconds
        try:
            return super().recv_frame()
        finally:
            self._read_deadline = None
            if self.sock is not None:
                self.sock.settimeout(1.0)

    def _recv(self, size):
        if self._read_deadline is not None:
            remaining = self._read_deadline - time.monotonic()
            if remaining <= 0:
                raise websocket.WebSocketTimeoutException('WSS read window elapsed')
            self.sock.settimeout(remaining)
        return super()._recv(size)

    def connect(self, *args, **kwargs):
        raise RuntimeError('Use open_socket with an isolated session')


def open_socket(session, url, *, timeout, resource_owner=None, cancel_event=None):
    validate_destination(url, session.room_id)
    if parse_qs(urlsplit(url).query).get('heartbeat_duration') != [str(round(session.heartbeat_interval_seconds*1000))]:
        raise ValueError('WSS heartbeat interval mismatch')
    validate_header(session.cookie_header)
    validate_header(session.user_agent, limit=2048)
    names = {part.split('=', 1)[0].strip().lower() for part in session.cookie_header.split(';')}
    if session.role == 'delivery_observer':
        if not names or not names <= {'ttwid', 'tt_chain_token', 'tt_csrf_token'}:
            raise ValueError('Invalid visitor session')
    elif session.role == 'account_heartbeat':
        if not names & {'sessionid', 'sessionid_ss', 'sid_tt'}:
            raise ValueError('Invalid account session')
    else:
        raise ValueError('Invalid connection role')
    with StartupScope(timeout, cancel_event) as scope:
        def own(resource):
            scope.own(resource)
            if resource_owner is not None:
                resource_owner(resource)
            return resource
        ws = own(IsolatedWebSocket())
        try:
            ws.sock = _connect_stream(session, url, scope.remaining(), resource_owner=own)
            own(ws.sock)
            ws.handshake_response = perform_handshake(ws.sock, url,
                cookie_header=session.cookie_header, user_agent=session.user_agent, timeout=scope.remaining())
            ws.sock.settimeout(1.0)
            scope.finish()
            ws.connected = True
            return ws
        except Exception:
            ws.shutdown()
            raise ValueError('WSS connection failed') from None
