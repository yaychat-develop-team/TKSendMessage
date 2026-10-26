import tempfile
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
from urllib.parse import parse_qs,urlsplit
from desktop.settings_store import DesktopSettings,load_desktop_settings,save_desktop_settings
from tiktok_live.wss.direct_models import DirectSession
from tiktok_live.wss.initial_fetch import InitialFetch
from tiktok_live.wss.protocol import _encode,encode_enter_room,encode_heartbeat,decode_push_frame
from tiktok_live.heartbeat_socket import FrameEvidence


def account_session(interval):
    initial = InitialFetch('server-cursor',
        'wss://webcast-ws.tiktok.com/webcast/im/ws_proxy/ws_reuse_supplement/',
        (('wrss', 'server-route'),), '', 0)
    return DirectSession('account_heartbeat', 'host', '42', 'UA', 'sessionid=A',
                         heartbeat_interval_seconds=interval, initial_fetch=initial)


class HeartbeatIntervalTests(unittest.TestCase):
    def test_warmup_duration_is_three_configured_periods_and_requires_real_beats(self):
        from tiktok_live import heartbeat
        for interval in (1, 5, 10, 20, 60):
            with self.subTest(interval=interval):
                evidence = FrameEvidence(heartbeat_interval_seconds=interval)
                evidence.record(encode_enter_room(42), 'sent')
                evidence.record(_encode('im_enter_room_resp', b''), 'received')
                for stamp in (0, interval, 2 * interval):
                    evidence.record(encode_heartbeat(42), 'sent', now=stamp)
                required = 3 * interval
                self.assertFalse(evidence.ready('42', elapsed=required - .01, connected=True))
                self.assertTrue(evidence.ready('42', elapsed=required, connected=True))
                listener = SimpleNamespace(error=None, connection=SimpleNamespace(
                    connected=True, room_id='42', evidence=evidence))
                with patch.object(heartbeat.time, 'monotonic', side_effect=(0, required - .01, required)), \
                        patch.object(heartbeat.time, 'sleep') as sleep:
                    heartbeat._warm_listener(listener, '42', heartbeat_interval_seconds=interval)
                self.assertEqual(sleep.call_count, 1)

    def test_burst_beats_cannot_replace_the_configured_heartbeat_periods(self):
        evidence = FrameEvidence(heartbeat_interval_seconds=5)
        evidence.record(encode_enter_room(42), 'sent')
        evidence.record(_encode('im_enter_room_resp', b''), 'received')
        for stamp in (0, .01, .02):
            evidence.record(encode_heartbeat(42), 'sent', now=stamp)
        self.assertFalse(evidence.ready('42', elapsed=15, connected=True))

    def test_fractional_interval_bounds_actual_idle_socket_read_deadline(self):
        from tiktok_live.wss.connection import DirectConnection
        from tiktok_live.wss.isolated_socket import IsolatedWebSocket
        from websocket import WebSocket,WebSocketTimeoutException
        now=[0.0]
        beats=[]
        stop=threading.Event()
        class Socket(IsolatedWebSocket):
            def send(self,payload,opcode=2):
                if decode_push_frame(payload).payload_type=='hb':
                    beats.append(now[0])
                    if len(beats)==4:
                        stop.set()
            def shutdown(self):
                pass
        sock=Socket()
        def idle(_):
            now[0]=sock._read_deadline
            raise WebSocketTimeoutException()
        session=account_session(1.1)
        connection=DirectConnection(session,on_comment=lambda e:None,on_connected=lambda:None,
            socket_factory=lambda *a,**kw:sock,clock=lambda:now[0])
        with patch('tiktok_live.wss.isolated_socket.time.monotonic',side_effect=lambda:now[0]), \
                patch.object(WebSocket,'recv_frame',idle):
            connection.run(stop)
        self.assertEqual([round(value,6) for value in beats],[0,1.1,2.2,3.3])

    def test_settings_default_and_custom_interval_survive_reload_and_runner_boundary(self):
        self.assertEqual(DesktopSettings().heartbeat_interval_seconds,10)
        with tempfile.TemporaryDirectory() as directory:
            path=Path(directory)/'settings.json'
            values=DesktopSettings(heartbeat_interval_seconds=5.5)
            save_desktop_settings(path,values)
            loaded=load_desktop_settings(path)
            self.assertEqual(loaded.heartbeat_interval_seconds,5.5)
            self.assertEqual(loaded.runner_kwargs()['heartbeat_interval_seconds'],5.5)

    def test_wire_scheduler_uses_configured_interval_not_hardcoded_ten(self):
        from tiktok_live.wss.connection import DirectConnection
        for interval,end,want in ((1,4,[0,1,2,3]),(5,16,[0,5,10,15]),(60,121,[0,60,120])):
            with self.subTest(interval=interval):
                now=[0]
                beats=[]
                stop=threading.Event()
                class Socket:
                    def send(self,payload,opcode=2):
                        if decode_push_frame(payload).payload_type=='hb':
                            beats.append(now[0])
                    def recv_frame(self):
                        now[0]+=1
                        if now[0]>=end:
                            stop.set()
                            raise TimeoutError()
                        return SimpleNamespace(opcode=2,fin=1,data=_encode('msg',b''))
                    def shutdown(self):
                        pass
                session=account_session(interval)
                connection=DirectConnection(session,on_comment=lambda e:None,on_connected=lambda:None,
                    socket_factory=lambda *a,**kw:Socket(),clock=lambda:now[0])
                connection.run(stop)
                self.assertEqual(beats,want)

    def test_one_second_beats_do_not_lose_required_span_when_samples_roll_over(self):
        evidence=FrameEvidence(heartbeat_interval_seconds=1)
        evidence.record(encode_enter_room(42),'sent')
        evidence.record(_encode('im_enter_room_resp',b''),'received')
        for stamp in range(31):
            evidence.record(encode_heartbeat(42),'sent',now=stamp)
        self.assertTrue(evidence.ready('42',elapsed=3,connected=True))
        self.assertFalse(evidence.ready('42',elapsed=2.99,connected=True))
        self.assertLessEqual(len(evidence.snapshot()['heartbeat_gaps_s']['42']),9)

    def test_sixty_second_interval_allows_third_beat_without_fixed_sixty_second_timeout(self):
        from tiktok_live import heartbeat
        evidence=FrameEvidence(heartbeat_interval_seconds=60)
        evidence.record(encode_enter_room(42),'sent')
        evidence.record(_encode('im_enter_room_resp',b''),'received')
        for stamp in (0,60):
            evidence.record(encode_heartbeat(42),'sent',now=stamp)
        listener=SimpleNamespace(error=None,connection=SimpleNamespace(connected=True,room_id='42',evidence=evidence))
        def add_third_beat(_):
            if evidence.snapshot()['sent']['hb'] < 3:
                evidence.record(encode_heartbeat(42),'sent',now=120)
        with patch.object(heartbeat.time,'monotonic',side_effect=(0,60,120,180)), \
                patch.object(heartbeat.time,'sleep',side_effect=add_third_beat):
            heartbeat._warm_listener(listener,'42',heartbeat_interval_seconds=60)
        self.assertEqual(evidence.snapshot()['sent']['hb'],3)

    def test_direct_url_advertises_interval_and_rejects_invalid_values(self):
        from tiktok_live.wss.direct_signer import build_direct_url
        url=build_direct_url('42',user_agent='UA',heartbeat_interval_seconds=5.5)
        self.assertEqual(parse_qs(urlsplit(url).query)['heartbeat_duration'],['5500'])
        for value in (0,61,float('nan'),float('inf'),True):
            with self.subTest(value=str(value)),self.assertRaises(ValueError):
                build_direct_url('42',user_agent='UA',heartbeat_interval_seconds=value)

    def test_observer_and_account_factories_receive_configured_interval(self):
        from listen_msg import LiveMessageListener
        from tests.support.direct_fakes import WaitingConnection,visitor
        from tiktok_live import heartbeat
        seen=[]
        def factory(session,**kw):
            seen.append((session.role,session.heartbeat_interval_seconds))
            return WaitingConnection(session,**kw)
        observer=LiveMessageListener('host',session_factory=visitor,connection_factory=factory,
            heartbeat_interval_seconds=5)
        self.assertTrue(observer.start(1))
        observer.close()
        real_listener=LiveMessageListener
        def listener(*args,**kwargs):
            return real_listener(*args,**kwargs,connection_factory=factory)
        with patch.object(heartbeat,'LiveMessageListener',side_effect=listener), \
                patch.object(heartbeat,'prepare_account',return_value=DirectSession('account_heartbeat','host','42','UA','sessionid=A')), \
                patch.object(heartbeat,'_warm_listener'):
            with heartbeat.room_heartbeat_session(heartbeat_interval_seconds=5):
                with heartbeat.room_heartbeat('host','42','sessionid=A','UA'):
                    pass
        self.assertEqual(seen,[('delivery_observer',5),('account_heartbeat',5)])


class HeartbeatIntervalUiTests(unittest.TestCase):
    def test_settings_dialog_saves_heartbeat_control(self):
        from PySide6.QtWidgets import QApplication,QDoubleSpinBox
        from desktop.dialogs import SettingsDialog
        self.qt=QApplication.instance() or QApplication([])
        dialog=SettingsDialog(DesktopSettings())
        try:
            control=dialog.findChild(QDoubleSpinBox,'heartbeatIntervalSeconds')
            self.assertIsNotNone(control)
            self.assertEqual(control.value(),10)
            self.assertEqual((control.minimum(),control.maximum()),(1,60))
            control.setValue(7.5)
            saved=[]
            dialog.saved.connect(saved.append)
            dialog._save()
            self.assertEqual(saved[0].heartbeat_interval_seconds,7.5)
        finally:
            dialog.close()
            dialog.deleteLater()
