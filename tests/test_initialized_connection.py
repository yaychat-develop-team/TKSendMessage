import threading
import time
import unittest
from types import SimpleNamespace
from urllib.parse import parse_qs, urlsplit

from tiktok_live.wss.connection import DirectConnection
from tiktok_live.wss.direct_models import DirectSession
from tiktok_live.wss.initial_fetch import InitialFetch
from tiktok_live.wss.protocol import _fields, _text


class InitializedConnectionTests(unittest.TestCase):
    def test_success_log_matches_actual_socket_endpoint_without_query(self):
        from listen_msg import LiveMessageListener
        from loguru import logger
        for role in ('account_heartbeat', 'delivery_observer'):
            for host in ('webcast-ws.tiktok.com', 'webcast-ws.us.tiktok.com', 'webcast-ws.eu.tiktok.com'):
                with self.subTest(role=role, host=host):
                    stop = threading.Event()
                    opened, logs = [], []
                    class Socket:
                        def send(self, payload, opcode=2):
                            pass
                        def recv_frame(self):
                            stop.set()
                            raise TimeoutError()
                        def shutdown(self):
                            pass
                    endpoint = 'wss://' + host + '/webcast/im/ws_proxy/ws_reuse_supplement/'
                    session = DirectSession(role, 'host', '42', 'Mozilla/5.0 Chrome/150.0.0.0',
                        'ttwid=synthetic', initial_fetch=InitialFetch('private-cursor', endpoint,
                            (('wrss', 'private-route'),), '', 0))
                    listener = LiveMessageListener('host', connection_role=role)
                    listener._deadline = time.monotonic() + 10
                    listener._startup_scope = SimpleNamespace(finish=lambda: None)
                    def open_socket(active, url, **kwargs):
                        opened.append(url)
                        return Socket()
                    connection = DirectConnection(session, on_comment=lambda e: None,
                        on_connected=listener._on_connect, socket_factory=open_socket)
                    listener._connection = connection
                    handle = logger.add(lambda message: logs.append(message.record['message']))
                    try:
                        connection.run(stop)
                    finally:
                        logger.remove(handle)
                    message = next(line for line in logs if 'classification=wss_connected' in line)
                    actual = urlsplit(opened[0])
                    self.assertIn('endpoint=' + actual.scheme + '://' + actual.netloc + actual.path + ' |', message)
                    self.assertNotIn('?', message)
                    self.assertNotIn('private-', message)
                    self.assertNotIn('wrss', message)

    def test_observer_connection_uses_server_route_and_cursor(self):
        self.test_account_connection_uses_server_route_and_cursor(role='delivery_observer')

    def test_account_connection_uses_server_route_and_cursor(self, role='account_heartbeat'):
        stop = threading.Event()
        opened = {}

        class Socket:
            connected = True
            sent = []
            def send(self, payload, opcode=2):
                self.sent.append((opcode, payload))
            def recv_frame(self):
                stop.set()
                raise TimeoutError()
            def shutdown(self):
                self.connected = False

        sock = Socket()
        initial = InitialFetch('server-cursor',
            'wss://webcast-ws.us.tiktok.com/webcast/im/ws_proxy/ws_reuse_supplement/',
            (('wrss', 'server-route'),), '', 0)
        session = DirectSession(role, 'host', '42',
            'Mozilla/5.0 Chrome/150.0.0.0', 'ttwid=synthetic' if role == 'delivery_observer' else 'sessionid=synthetic',
            initial_fetch=initial)

        def open_socket(active_session, url, **kwargs):
            opened['url'] = url
            return sock

        connection = DirectConnection(session, on_comment=lambda event: None,
            on_connected=lambda: None, socket_factory=open_socket, clock=lambda: 0)
        connection.run(stop)
        query = parse_qs(urlsplit(opened['url']).query)
        self.assertEqual(urlsplit(opened['url']).hostname, 'webcast-ws.us.tiktok.com')
        self.assertEqual(['server-route'], query['wrss'])
        self.assertEqual(['42'], query['room_id'])
        self.assertEqual(['180800', '270000'], query['version_code'])
        self.assertEqual(['1988'], query['aid'])
        self.assertEqual(['zh-CN'], query['browser_language'])
        self.assertEqual(['1707'], query['screen_width'])
        self.assertEqual(['1067'], query['screen_height'])
        self.assertEqual(['1'], query['sup_ws_ds_opt'])
        self.assertEqual(['2.0.0'], query['update_version_code'])
        self.assertEqual(['1'], query['client_enter'])
        self.assertEqual(['3'], query['did_rule'])
        enter = _fields(sock.sent[0][1])
        payload = _fields(enter[8][0])
        self.assertEqual('server-cursor', _text(payload, 6))

    def test_observer_without_fetch_receives_chat_and_falls_back_to_another_wss_host(self):
        from tiktok_live.wss.protocol import _encode,_wire
        stop=threading.Event()
        opened,events=[],[]
        body=_wire(2,_wire(1,123)+_wire(38,'viewer'))+_wire(3,'hello')
        envelope=_wire(1,'WebcastChatMessage')+_wire(2,body)+_wire(3,456)
        packet=_encode('msg',_wire(1,envelope)+_wire(5,'ack-ext')+_wire(9,1),789)
        class Socket:
            def __init__(self):
                self.sent=[]
                self.read=False
            def send(self,payload,opcode=2):
                self.sent.append(payload)
            def recv_frame(self):
                if not self.read:
                    self.read=True
                    return SimpleNamespace(opcode=2,data=packet,fin=1)
                stop.set()
                raise TimeoutError()
            def shutdown(self):
                pass
        sock=Socket()
        session=DirectSession('delivery_observer','host','42','Mozilla/5.0 Chrome/153.0.0.0','ttwid=synthetic')
        def open_socket(active,url,**kwargs):
            self.assertIs(active,session)
            opened.append(url)
            if len(opened)==1:
                raise OSError('first endpoint unavailable')
            return sock
        connection=DirectConnection(session,on_comment=events.append,on_connected=lambda:None,socket_factory=open_socket)
        connection.run(stop)
        self.assertEqual(['webcast-ws.tiktok.com','webcast-ws.eu.tiktok.com'],[urlsplit(url).hostname for url in opened])
        for url in opened:
            query=parse_qs(urlsplit(url).query)
            self.assertEqual(['42'],query['room_id'])
            self.assertEqual(['12'],query['live_id'])
            self.assertIn('X-Bogus',query)
            self.assertNotIn('msToken',query)
        self.assertEqual('wss://webcast-ws.eu.tiktok.com/webcast/im/ws_proxy/ws_reuse_supplement/',connection.endpoint)
        self.assertEqual(['hello'],[event.text for event in events])
        self.assertEqual(['42'],[event.room_id for event in events])
        self.assertEqual('',_text(_fields(_fields(sock.sent[0])[8][0]),6))
        self.assertEqual(1,connection.evidence.snapshot()['sent']['ack'])

    def test_guest_cancel_during_handshake_does_not_try_next_endpoint(self):
        stop = threading.Event()
        opened = []
        def open_socket(*args, **kwargs):
            opened.append(True)
            stop.set()
            raise TimeoutError('cancelled')
        session = DirectSession('delivery_observer', 'host', '42',
                                'Mozilla/5.0 Chrome/153.0.0.0', 'ttwid=synthetic')
        connection = DirectConnection(session, on_comment=lambda event: None,
            on_connected=lambda: self.fail('Cancelled observer connected'),
            socket_factory=open_socket)
        connection.run(stop)
        self.assertEqual([True], opened)
        self.assertFalse(connection.connected)

    def test_account_connection_without_fetch_never_opens_socket(self, role='account_heartbeat'):
        session = DirectSession(role, 'host', '42', 'UA',
                                'ttwid=synthetic' if role == 'delivery_observer' else 'sessionid=synthetic')
        opened = []
        connection = DirectConnection(session, on_comment=lambda event: None,
            on_connected=lambda: None,
            socket_factory=lambda *args, **kwargs: opened.append(True))
        with self.assertRaises(RuntimeError):
            connection.run(threading.Event())
        self.assertEqual([], opened)


if __name__ == '__main__':
    unittest.main()
