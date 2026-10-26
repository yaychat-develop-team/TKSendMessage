# -*- coding: utf-8 -*-
"""Task-owned direct TikTok WSS listener."""
import queue
import threading
import time
import unicodedata
from dataclasses import dataclass,replace
from contextvars import copy_context
from typing import Any
from urllib.parse import urlsplit, urlunsplit
from loguru import logger
from tiktok_live.wss.connection import DirectConnection
from tiktok_live.wss.sessions import prepare_observer
from tiktok_live.wss.startup import StartupScope
from tiktok_live.wss.direct_models import validate_heartbeat_interval

def _clean_username(value):
    return str(value or '').strip().lstrip('@').lower()

def _event_user_label(event: Any) -> str:
    """Return a printable user identity even when an upstream event omits user."""
    user = getattr(event, "user", None)
    unique_id = str(getattr(user, "unique_id", "") or "").strip().lstrip("@")
    nickname = str(getattr(user, "nickname", "") or "").strip()
    user_id = str(getattr(user, "id", "") or "").strip()
    return unique_id or nickname or user_id or "-"


def _normalize_comment(value: Any) -> str:
    normalized = unicodedata.normalize("NFKC", str(value or "")).strip()
    # 监听与发送接口可能对同一表情保留或省略文本/彩色展示选择符。
    # 只忽略展示选择符，保留 ZWJ、肤色修饰符及组合表情的语义差异。
    return normalized.replace("\ufe0e", "").replace("\ufe0f", "")



@dataclass(frozen=True)
class ObservedComment:
    observed_at: float
    unique_id: str
    nickname: str
    user_id: str
    comment: str
    message_id: str



class LiveMessageListener:
    def __init__(self, unique_id, *, show_comments=True, session_factory=None,
                 connection_factory=DirectConnection, connection_role='delivery_observer',
                 heartbeat_interval_seconds=10.0, room_id=None,
                 reconnect_attempts=3, reconnect_delay=1.0, observer_proxy=''):
        self.unique_id = _clean_username(unique_id)
        self.show_comments = bool(show_comments)
        self.connection_role = connection_role
        self._session_factory = session_factory
        self._connection_factory = connection_factory
        self._heartbeat_interval_seconds=validate_heartbeat_interval(heartbeat_interval_seconds)
        self._known_room_id=room_id
        from tiktok_live.wss.proxy import normalize_observer_proxy
        self._observer_proxy = normalize_observer_proxy(observer_proxy)
        if self._observer_proxy and (connection_role != 'delivery_observer' or session_factory is not None):
            raise ValueError('Observer proxy requires an anonymous observer session')
        self.failure_stage=None
        self._connection = None
        self._comments = queue.Queue(maxsize=4096)
        self._connected = threading.Event()
        self._stopped = threading.Event()
        self._stop_requested = threading.Event()
        self._thread = None
        self._error = None
        self._deadline = 0.0
        self._lock = threading.Lock()
        self._startup_scope = None
        self._comment_callbacks = set()
        self._comment_callbacks_lock = threading.RLock()
        self._recovering = threading.Event()
        self._ever_connected = False
        self._last_interruption = 0.0
        self._connect_timeout = 15.0
        self._reconnect_attempts = max(0, int(reconnect_attempts))
        self._reconnect_delay = max(0.0, float(reconnect_delay))

    @property
    def recovering(self):
        return self._recovering.is_set() and not self._stop_requested.is_set()

    def observation_available_since(self, since):
        """A reconnect cannot recover evidence lost during the disconnected gap."""
        return self.available and since >= self._last_interruption

    def wait_until_available(self, timeout, *, should_cancel=None):
        deadline = time.monotonic() + max(0.0, timeout)
        while self.recovering and time.monotonic() < deadline:
            if should_cancel is not None and should_cancel():
                return False
            self._stop_requested.wait(min(.05, max(0.0, deadline-time.monotonic())))
        return self.available

    @property
    def connection(self):
        return self._connection

    @property
    def room_id(self):
        return self._connection.room_id if self._connection is not None else None

    @property
    def error(self):
        return self._error

    @property
    def available(self):
        return bool(self._connected.is_set() and self._error is None
                    and not self._stop_requested.is_set() and not self._stopped.is_set()
                    and self._connection is not None and self._connection.connected)

    def _on_connect(self):
        with self._lock:
            if self._stop_requested.is_set() or time.monotonic() >= self._deadline:
                return
            self._startup_scope.finish()
            if self._recovering.is_set():
                self._last_interruption = time.monotonic()
            self._recovering.clear()
            self._ever_connected = True
            self._connected.set()
        session = getattr(self._connection, 'session', None)
        initial = getattr(session, 'initial_fetch', None)
        endpoint = getattr(self._connection, 'endpoint', None) or '-'
        if endpoint == '-' and initial is not None:
            target = urlsplit(initial.push_server)
            endpoint = urlunsplit((target.scheme, target.netloc, target.path, '', ''))
        logger.success('WSS 连接成功 | username={} | room_id={} | endpoint={} | role={} '
                       '| classification=wss_connected', self.unique_id, self.room_id or '-',
                       endpoint, self.connection_role)

    def _on_comment(self, event):
        self.record_comment(event.text, unique_id=event.unique_id, nickname=event.nickname,
                            user_id=event.user_id, message_id=event.message_id)
        if self.show_comments:
            logger.info('[弹幕] {} -> {}', event.unique_id or event.nickname or event.user_id or '-', event.text)

    def _enqueue(self, item):
        while True:
            try:
                self._comments.put_nowait(item)
                return
            except queue.Full:
                try:
                    self._comments.get_nowait()
                except queue.Empty:
                    pass

    def record_comment(self, comment, *, unique_id='', nickname='', user_id='',
                       message_id='', observed_at=None):
        observed = ObservedComment(
            time.monotonic() if observed_at is None else observed_at,
            str(unique_id or '').strip().lstrip('@'), str(nickname or '').strip(),
            str(user_id or '').strip(), str(comment or ''), str(message_id or '').strip())
        self._enqueue(observed)
        # Subscription is non-consuming: the legacy receipt queue remains intact.
        with self._comment_callbacks_lock:
            for callback in tuple(self._comment_callbacks):
                try:
                    callback(observed)
                except Exception:
                    logger.warning('实时展示进度回调失败 | classification=delivery_progress_callback_failed')

    def subscribe_comments(self, callback):
        with self._comment_callbacks_lock:
            self._comment_callbacks.add(callback)
        def unsubscribe():
            with self._comment_callbacks_lock:
                self._comment_callbacks.discard(callback)
        return unsubscribe

    def snapshot_comments(self):
        """Non-consuming evidence captured before a subscriber acquires its lock."""
        with self._comments.mutex:
            return tuple(self._comments.queue)

    def wait_for_comment(
        self,
        comment: str,
        *,
        since: float,
        expected_usernames: set[str] | None = None,
        expected_user_ids: set[str] | None = None,
        expected_message_ids: set[str] | None = None,
        timeout: float = 15,
        should_cancel=None,
    ) -> ObservedComment | None:
        usernames = {_clean_username(value) for value in (expected_usernames or set()) if value}
        user_ids = {str(value) for value in (expected_user_ids or set()) if value}
        deadline = time.monotonic() + max(0.0, timeout)
        deferred = []
        try:
            while True:
                if should_cancel is not None and should_cancel():
                    return None
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return None
                try:
                    observed = self._comments.get(timeout=min(.1,remaining) if should_cancel is not None else remaining)
                except queue.Empty:
                    if should_cancel is None:
                        return None
                    continue
                if (expected_message_ids and observed.message_id not in expected_message_ids):
                    if observed.observed_at >= since:
                        deferred.append(observed)
                        del deferred[:-4096]
                    continue
                if observed.observed_at < since:
                    continue
                if _normalize_comment(observed.comment) != _normalize_comment(comment):
                    continue
                observed_names = {
                    _clean_username(observed.unique_id),
                    _clean_username(observed.nickname),
                } - {""}
                if usernames or user_ids:
                    username_matches = bool(usernames.intersection(observed_names))
                    user_id_matches = bool(user_ids and observed.user_id in user_ids)
                    if not (username_matches or user_id_matches):
                        continue
                return observed
        finally:
            for observed in deferred:
                self._enqueue(observed)


    def _run_connections(self, session):
        retries = 0
        while not self._stop_requested.is_set():
            remaining = self._deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError('WSS startup timed out')
            connection = self._connection_factory(session, on_comment=self._on_comment,
                on_connected=self._on_connect, connect_timeout=remaining)
            with self._lock:
                self._connection = connection
            try:
                self._startup_scope.own(connection)
                connection.run(self._stop_requested)
                if not self._stop_requested.is_set():
                    raise ConnectionError('WSS receive loop ended')
                return
            except Exception as exc:
                if self._stop_requested.is_set():
                    return
                self._last_interruption = time.monotonic()
                if (self.connection_role != 'delivery_observer' or not self._ever_connected
                        or retries >= self._reconnect_attempts):
                    raise
                self._recovering.set()
                retries += 1
                logger.warning('旁观监听断开，正在重连 | username={} | attempt={}/{} '
                    '| error_type={} | classification=observer_reconnecting',
                    self.unique_id, retries, self._reconnect_attempts, type(exc).__name__)
            finally:
                connection.close()
                self._startup_scope.__exit__(None, None, None)
            if self._stop_requested.wait(self._reconnect_delay):
                return
            with self._lock:
                if self._stop_requested.is_set():
                    return
                self._deadline = time.monotonic() + self._connect_timeout
                self._startup_scope = StartupScope(self._connect_timeout, self._stop_requested)

    def _run(self):
        logger.info('WSS 开始连接 | username={} | transport=wss | role={} '
                    '| classification=wss_connecting', self.unique_id, self.connection_role)
        stage = 'observer_prepare' if self._session_factory is None else 'session_prepare'
        try:
            session = (self._session_factory() if self._session_factory is not None else
                       prepare_observer(self.unique_id, startup_scope=self._startup_scope,
                                        **({'observer_proxy': self._observer_proxy}
                                           if self._observer_proxy else {}),
                                        **({'known_room_id':self._known_room_id}
                                           if self._known_room_id is not None else {})))
            session=replace(session,heartbeat_interval_seconds=self._heartbeat_interval_seconds)
            remaining = self._deadline - time.monotonic()
            if self._stop_requested.is_set() or remaining <= 0:
                return
            if not self._stop_requested.is_set():
                stage = 'wss_transport'
                self._run_connections(session)
        except Exception as exc:
            self.failure_stage=stage
            # Retain only type and a safe message, never raw URL/cookie-bearing exceptions.
            try:
                self._error = type(exc)('WSS connection failed')
            except Exception:
                self._error = RuntimeError('WSS connection failed')
            self._connected.set()
            logger.warning('WSS 连接失败 | username={} | error_type={} | stage={} | role={} '
                '| classification=wss_connect_failed', self.unique_id, type(exc).__name__,
                stage, self.connection_role)
        finally:
            if self._connection is not None:
                self._connection.close()
            self._startup_scope.__exit__(None, None, None)
            self._stopped.set()
            self._recovering.clear()
            self._connected.set()
            logger.info('WSS 连接已关闭 | username={} | role={} | classification=wss_closed',
                        self.unique_id, self.connection_role)

    def start(self, connect_timeout=15, *, should_cancel=None):
        if self._thread is not None:
            return self.available
        self._deadline = time.monotonic() + max(0.0, connect_timeout)
        startup_deadline = self._deadline
        self._connect_timeout = max(0.0, connect_timeout)
        self._startup_scope = StartupScope(connect_timeout, self._stop_requested)
        self._thread = threading.Thread(target=copy_context().run, args=(self._run,),
            name=f'tiktok-live-listener-{self.unique_id}', daemon=True)
        self._thread.start()
        while time.monotonic() < startup_deadline:
            if should_cancel is not None and should_cancel():
                break
            if self._connected.wait(min(.1,max(0.0,startup_deadline-time.monotonic()))):
                if self.available or self._stopped.is_set() or self._error is not None:
                    break
                self._stop_requested.wait(.01)
        if should_cancel is not None and should_cancel():
            self._stop_requested.set()
        if not self.available:
            self._stop_requested.set()
            self._startup_scope.cancel()
            if self._connection is not None:
                self._connection.close()
        return self.available

    def close(self):
        self._stop_requested.set()
        if self._startup_scope is not None:
            self._startup_scope.cancel()
        with self._lock:
            connection = self._connection
        if connection is not None:
            connection.close()
        if self._thread is not None and self._thread is not threading.current_thread():
            # Retain ownership until termination: never retry while an old
            # preparation/DNS syscall still belongs to this listener.
            self._thread.join()


def run_console_listener(unique_id, show_comments=True):
    listener = LiveMessageListener(unique_id, show_comments=show_comments)
    try:
        if not listener.start():
            raise RuntimeError('Listener unavailable')
        while listener.available:
            time.sleep(.25)
    except KeyboardInterrupt:
        pass
    finally:
        listener.close()


if __name__ == '__main__':
    run_console_listener('stef_gamingg')
