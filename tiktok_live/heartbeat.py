"""Per-account room presence, configured startup retries and persistent WSS."""
from contextlib import contextmanager, ExitStack
from contextvars import ContextVar
import time
from loguru import logger
from listen_msg import LiveMessageListener
from .wss.sessions import prepare_account
from .wss.direct_models import heartbeat_warmup_seconds, validate_heartbeat_interval
from .config import (MESSAGE_OBSERVER_CONNECT_TIMEOUT_SECONDS,
                     MESSAGE_OBSERVER_CONNECT_RETRIES, MESSAGE_OBSERVER_RETRY_DELAY_SECONDS)

_active_session = ContextVar('room_heartbeat_session', default=None)


def _cancelled():
    scope = _active_session.get()
    return bool(scope and scope['should_cancel'] and scope['should_cancel']())


def _pause(seconds, should_cancel):
    deadline = time.monotonic() + max(0, seconds)
    while time.monotonic() < deadline:
        if should_cancel():
            return
        time.sleep(min(.1, max(0, deadline-time.monotonic())))


@contextmanager
def room_heartbeat_session(should_cancel=None, *,
        connect_timeout=MESSAGE_OBSERVER_CONNECT_TIMEOUT_SECONDS,
        connect_retries=MESSAGE_OBSERVER_CONNECT_RETRIES,
        retry_delay_seconds=MESSAGE_OBSERVER_RETRY_DELAY_SECONDS,heartbeat_interval_seconds=10.0):
    with ExitStack() as stack:
        scope = {'stack':stack,'connections':{},'should_cancel':should_cancel,
            'options':dict(connect_timeout=connect_timeout,connect_retries=connect_retries,
                           retry_delay_seconds=retry_delay_seconds,heartbeat_interval_seconds=heartbeat_interval_seconds)}
        token = _active_session.set(scope)
        try:
            yield
        finally:
            _active_session.reset(token)


@contextmanager
def room_heartbeat(username, room_id, cookie, user_agent, *, initial_fetch=None):
    scope = _active_session.get()
    if scope is None:
        with _room_heartbeat_connection(username,room_id,cookie,user_agent,
                                        initial_fetch=initial_fetch):
            yield
        return
    key = (username,str(room_id),cookie,user_agent)
    connection = scope['connections'].get(key)
    if connection is None:
        connection = scope['stack'].enter_context(_room_heartbeat_connection(
            username,room_id,cookie,user_agent,initial_fetch=initial_fetch,
            **scope['options']))
        scope['connections'][key] = connection
    if not connection.available or connection.error or not connection.connection.connected:
        raise RuntimeError('直播间心跳已断开，停止继续发送')
    yield


def _warm_listener(listener, room_id,*,heartbeat_interval_seconds=10.0):
    evidence = listener.connection.evidence
    interval = validate_heartbeat_interval(heartbeat_interval_seconds)
    required_seconds = heartbeat_warmup_seconds(interval)
    timeout_seconds = required_seconds + max(10, interval)
    started = time.monotonic()
    logger.info('账号 WSS 预热中 | required_seconds={} | heartbeat_interval_seconds={} '
                '| required_heartbeats=3 | timeout_seconds={} | classification=heartbeat_warming',
                required_seconds, interval, timeout_seconds)
    while True:
        if _cancelled():
            raise RuntimeError('发送已停止')
        elapsed = time.monotonic()-started
        connection = listener.connection
        if listener.error or not connection.connected or str(connection.room_id)!=str(room_id):
            raise RuntimeError('直播间心跳断开或房间不匹配，未发送消息')
        if (evidence.ready(room_id,elapsed=elapsed,connected=True)
                and evidence.ready_to_send(room_id,connected=True)):
            logger.info('账号 WSS 预热完成 | elapsed_seconds={} | heartbeats={} '
                        '| classification=heartbeat_ready',round(elapsed,2),evidence.snapshot()['sent'].get('hb',0))
            return
        if elapsed>=timeout_seconds:
            raise RuntimeError('直播间心跳预热超时，未发送消息')
        time.sleep(.25)


@contextmanager
def _room_heartbeat_connection(username,room_id,cookie,user_agent,*,
        initial_fetch=None,
        connect_timeout=MESSAGE_OBSERVER_CONNECT_TIMEOUT_SECONDS,
        connect_retries=MESSAGE_OBSERVER_CONNECT_RETRIES,
        retry_delay_seconds=MESSAGE_OBSERVER_RETRY_DELAY_SECONDS,heartbeat_interval_seconds=10.0):
    listener = None
    total = max(0,int(connect_retries))+1
    for attempt in range(1,total+1):
        if _cancelled():
            raise RuntimeError('发送已停止')
        listener = LiveMessageListener(username,show_comments=False,
            session_factory=lambda:prepare_account(
                username,room_id,cookie,user_agent,initial_fetch=initial_fetch),
            connection_role='account_heartbeat',heartbeat_interval_seconds=heartbeat_interval_seconds)
        try:
            options = {'connect_timeout':max(0,float(connect_timeout))}
            scope = _active_session.get()
            if scope and scope['should_cancel'] is not None:
                options['should_cancel'] = scope['should_cancel']
            if not listener.start(**options):
                raise RuntimeError('直播间心跳连接失败，未发送消息')
            _warm_listener(listener,room_id,heartbeat_interval_seconds=heartbeat_interval_seconds)
            break
        except Exception as exc:
            listener.close()
            if _cancelled():
                raise RuntimeError('发送已停止') from None
            logger.warning('账号 WSS 连接/预热失败 | role=account_heartbeat | attempt={}/{} '
                '| error_type={} | classification=heartbeat_connect_failed',attempt,total,type(exc).__name__)
            if attempt>=total:
                raise RuntimeError('直播间心跳连接重试已耗尽，未发送消息') from None
            logger.warning('账号 WSS 等待重试 | role=account_heartbeat | delay_seconds={} '
                '| classification=heartbeat_retrying',retry_delay_seconds)
            _pause(retry_delay_seconds,_cancelled)
    try:
        yield listener
    finally:
        listener.close()
