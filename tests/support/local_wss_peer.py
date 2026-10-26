"""Bounded localhost stream peer, never a platform connection."""
import base64
import hashlib
import socket
import threading


class LocalPeer:
    def __init__(self, *, status=101, bad_accept=False, tail=b'\x82\x02OK', oversized=False,
                 extra_headers=''):
        self.client, self.server = socket.socketpair()
        self.client.settimeout(3)
        self.server.settimeout(3)
        self.request = b''
        self.error = None
        def serve():
            try:
                while b'\r\n\r\n' not in self.request and len(self.request) < 200000:
                    chunk = self.server.recv(4096)
                    if not chunk:
                        return
                    self.request += chunk
                lines = self.request.decode('ascii').split('\r\n')
                key = next(line.split(':', 1)[1].strip() for line in lines if line.lower().startswith('sec-websocket-key:'))
                accept = base64.b64encode(hashlib.sha1((key + '258EAFA5-E914-47DA-95CA-C5AB0DC85B11').encode()).digest()).decode()
                if bad_accept:
                    accept = 'wrong'
                header = (f'HTTP/1.1 {status} result\r\nUpgrade: websocket\r\nConnection: Upgrade\r\n'
                          f'Sec-WebSocket-Accept: {accept}\r\nSet-Cookie: sessionid=SERVER; Domain=.tiktok.com\r\n')
                header += extra_headers
                if oversized:
                    header += 'X-Large: ' + 'x'*32768 + '\r\n'
                self.server.sendall(header.encode() + b'\r\n' + tail)
            except (OSError, StopIteration) as exc:
                self.error = exc
        self.thread = threading.Thread(target=serve, daemon=True)
        self.thread.start()

    def close(self):
        self.client.close()
        self.server.close()
        self.thread.join(4)
        if self.thread.is_alive():
            raise AssertionError('Local peer did not exit')
