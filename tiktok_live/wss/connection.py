"""Task-owned WSS receive loop, with no CLI lifetime limits or reconnect."""
from collections import OrderedDict
from dataclasses import replace
import threading
import time
from urllib.parse import urlsplit,urlunsplit
from loguru import logger
from websocket import WebSocketTimeoutException
from .initial_fetch import build_initialized_ws_url
from .isolated_socket import open_socket
from .protocol import decode_push_frame, encode_ack, encode_enter_room, encode_heartbeat
from ..heartbeat_socket import FrameEvidence
from .startup import close_resource
from .direct_signer import build_direct_url,WS_PATH

OBSERVER_WSS_ENDPOINTS = ('wss://webcast-ws.tiktok.com' + WS_PATH,
                          'wss://webcast-ws.eu.tiktok.com' + WS_PATH)


class DirectConnection:
    def __init__(self, session, *, on_comment, on_connected, connect_timeout=15.0,
                 socket_factory=open_socket, clock=time.monotonic):
        self.session = session
        self.room_id = session.room_id
        self.connected = False
        self.endpoint = None
        self.evidence = FrameEvidence(heartbeat_interval_seconds=session.heartbeat_interval_seconds)
        self._socket_factory = socket_factory
        self._clock = clock
        self._on_comment, self._on_connected = on_comment, on_connected
        self._timeout = connect_timeout
        self._socket = None
        self._closed = threading.Event()
        self._lock = threading.Lock()
        self._pending = []

    def _own_pending(self, resource):
        with self._lock:
            if not self._closed.is_set():
                self._pending.append(resource)
                return resource
        close_resource(resource)
        raise TimeoutError('WSS startup cancelled')

    def close(self):
        self._closed.set()
        self.connected = False
        with self._lock:
            sock, self._socket = self._socket, None
            pending, self._pending = self._pending, []
        for resource in pending:
            close_resource(resource)
        if sock is not None:
            try:
                sock.shutdown()
            except Exception:
                pass

    def _send(self, payload, stop_event, opcode=2):
        if self._closed.is_set() or stop_event.is_set():
            return False
        self._socket.send(payload, opcode=opcode)
        if opcode == 2:
            self.evidence.record(payload, 'sent', now=self._clock())
        return True

    def _send_heartbeat(self, stop_event):
        try:
            if not self._send(encode_heartbeat(int(self.room_id)), stop_event):
                return False
        except Exception as exc:
            logger.warning('WSS 心跳发送失败 | username={} | room_id={} | role={} '
                           '| error_type={} | classification=wss_heartbeat_send_failed',
                           self.session.target_username, self.room_id, self.session.role,
                           type(exc).__name__)
            raise
        logger.info('WSS 心跳已发送 | username={} | room_id={} | role={} | count={} '
                    '| interval_seconds={} | classification=wss_heartbeat_sent',
                    self.session.target_username, self.room_id, self.session.role,
                    self.evidence.snapshot()['sent'].get('hb', 0),
                    self.session.heartbeat_interval_seconds)
        return True

    def run(self, stop_event):
        try:
            if stop_event.is_set() or self._closed.is_set():
                return
            interval=self.session.heartbeat_interval_seconds
            guest_direct = self.session.role == 'delivery_observer' and self.session.initial_fetch is None
            if guest_direct:
                cursor = ''
            elif self.session.role in ('account_heartbeat', 'delivery_observer'):
                if self.session.initial_fetch is None:
                    raise ValueError('Initial fetch required')
                url = build_initialized_ws_url(
                    self.session.initial_fetch, room_id=self.room_id,
                    user_agent=self.session.user_agent,
                    additional_params=(
                        ('aid', '1988'), ('app_language', 'zh-Hans'),
                        ('app_name', 'tiktok_web'), ('browser_platform', 'Win32'),
                        ('browser_language', 'zh-CN'), ('browser_name', 'Mozilla'),
                        ('browser_online', 'true'), ('cookie_enabled', 'true'),
                        ('tz_name', 'Asia/Shanghai'), ('device_platform', 'web'),
                        ('identity', 'audience'), ('live_id', '12'),
                        ('sup_ws_ds_opt', '1'), ('update_version_code', '2.0.0'),
                        ('version_code', '180800'), ('client_enter', '1'),
                        ('ws_direct', '1'), ('did_rule', '3'),
                        ('webcast_language', 'zh-Hans'), ('screen_height', '1067'),
                        ('screen_width', '1707'),
                        ('heartbeat_duration', str(round(interval * 1000))),
                        ('resp_content_type', 'protobuf'),
                        ('history_comment_count', '6'), ('last_rtt', '-1'),
                        ('compress', 'gzip'), ('version_code', '270000'),
                    ),
                )
                cursor = self.session.initial_fetch.cursor
            else:
                raise ValueError('Unsupported connection role')
            if guest_direct:
                deadline=time.monotonic()+self._timeout
                for index,endpoint in enumerate(OBSERVER_WSS_ENDPOINTS):
                    if stop_event.is_set() or self._closed.is_set():
                        return
                    remaining=deadline-time.monotonic()
                    if remaining<=0:
                        raise TimeoutError('WSS startup timed out')
                    url=build_direct_url(self.room_id,user_agent=self.session.user_agent,
                        heartbeat_interval_seconds=interval,endpoint=endpoint)
                    try:
                        sock=self._socket_factory(self.session,url,timeout=remaining,
                            resource_owner=self._own_pending,cancel_event=stop_event)
                        break
                    except Exception:
                        if index+1==len(OBSERVER_WSS_ENDPOINTS):
                            raise
                        logger.warning('访客 WSS 地址连接失败，尝试下一地址 | endpoint={} '
                            '| role=delivery_observer | classification=wss_endpoint_retrying',endpoint)
            else:
                sock = self._socket_factory(self.session, url, timeout=self._timeout,
                    resource_owner=self._own_pending, cancel_event=stop_event)
            target=urlsplit(url)
            self.endpoint=urlunsplit((target.scheme,target.netloc,target.path,'',''))
            with self._lock:
                self._socket = sock
            if stop_event.is_set() or self._closed.is_set():
                return
            self.connected = True
            self._send(encode_enter_room(int(self.room_id), cursor=cursor), stop_event)
            self._send_heartbeat(stop_event)
            next_beat = self._clock() + interval
            self._on_connected()
            fragments = None
            seen = OrderedDict()
            while not stop_event.is_set() and not self._closed.is_set():
                if self._clock() >= next_beat:
                    self._send_heartbeat(stop_event)
                    next_beat = self._clock() + interval
                try:
                    # Idle reads must yield by the heartbeat deadline, including
                    # fractional intervals; keep the one-second cancel bound.
                    sock.read_window_seconds = max(.001,min(1.0,next_beat-self._clock()))
                    frame = sock.recv_frame()
                except (TimeoutError, WebSocketTimeoutException):
                    continue
                if stop_event.is_set() or self._closed.is_set():
                    break
                if frame.opcode == 8:
                    raise RuntimeError('WSS peer closed')
                if frame.opcode == 9:
                    self._send(frame.data, stop_event, opcode=10)
                    continue
                if frame.opcode == 10:
                    continue
                if frame.opcode == 2:
                    if fragments is not None:
                        raise ValueError('Unexpected fragmented message')
                    fragments = bytearray()
                elif frame.opcode != 0 or fragments is None:
                    raise ValueError('Unexpected WSS opcode')
                if len(fragments) + len(frame.data) > 2 * 1024 * 1024:
                    raise ValueError('WSS message exceeds limit')
                fragments.extend(frame.data)
                if not frame.fin:
                    continue
                raw, fragments = bytes(fragments), None
                decoded = decode_push_frame(raw)
                self.evidence.record(raw, 'received', now=self._clock())
                if decoded.need_ack:
                    self._send(encode_ack(decoded.log_id, decoded.internal_ext), stop_event)
                for event in decoded.events:
                    if event.message_id:
                        if event.message_id in seen:
                            continue
                        seen[event.message_id] = None
                        if len(seen) > 4096:
                            seen.popitem(last=False)
                    if stop_event.is_set() or self._closed.is_set():
                        break
                    self._on_comment(replace(event, room_id=self.room_id))
        except Exception:
            if not stop_event.is_set() and not self._closed.is_set():
                raise RuntimeError('Direct WSS connection failed') from None
        finally:
            self.close()
