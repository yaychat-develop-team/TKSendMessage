"""Single-send delivery diagnostic; account data and production code stay unchanged."""
import json
import argparse
import secrets
import string
import sys
import time
from contextlib import contextmanager
from pathlib import Path
from threading import Lock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from loguru import logger
from listen_msg import LiveMessageListener, _normalize_comment
from tiktok_live import client
from tiktok_live.account_store import load_account_store, cookie_header_from_playwright
from tiktok_live.algorithms.ticket_guard import build_headers_from_cookie_state, decode_client_data
from tiktok_live.runner import _numeric_tiktok_uids
from tiktok_live.wss.sessions import prepare_account


def main():
    logger.remove()
    parser = argparse.ArgumentParser()
    parser.add_argument('host', nargs='?', default='gonghuiwurao')
    parser.add_argument('--long-heartbeat', action='store_true')
    parser.add_argument('--min-beats', type=int, choices=(2, 3, 4), default=3)
    parser.add_argument('--prewarm-seconds', type=float, default=30)
    parser.add_argument('--account')
    parser.add_argument('--skip-enter', action='store_true')
    parser.add_argument('--message')
    options = parser.parse_args()
    host = options.host
    long_heartbeat = options.long_heartbeat
    print(json.dumps({'stage': 'configuration', 'host': host,
                      'min_beats': options.min_beats,
                      'prewarm_seconds': options.prewarm_seconds,
                      'hold_seconds': 30}), flush=True)
    accounts = load_account_store(Path('dist/runtime/accounts/tiktok_accounts.json'))['accounts']
    account = next(
        (item for item in accounts if item.get('username') == options.account),
        accounts[0] if accounts and options.account is None else None,
    )
    if account is None:
        print(json.dumps({'stage': 'error', 'error_type': 'account_not_found'}), flush=True)
        return 2
    cookie = cookie_header_from_playwright(account['cookies'])
    state = account.get('ticket_guard') or account.get('ticket_guard_state')
    headers = build_headers_from_cookie_state(cookie, state, client.urlsplit(client.CHAT_URL).path)
    timestamp = int(decode_client_data(headers['tt-ticket-guard-client-data'])['timestamp'])
    marker = ''.join(secrets.choice(string.ascii_lowercase) for _ in range(6))
    message = options.message or ('great stream ' + marker)
    lock = Lock()
    evidence = {'comments': 0, 'matching_content': 0, 'matching_sender': 0,
                'matching_message_id': 0}
    expected_ids = _numeric_tiktok_uids(account['cookies'])
    expected_name = str(account.get('username') or '').lstrip('@').lower()
    sent_id = ''
    content_match_ids = []

    class Listener(LiveMessageListener):
        def _on_comment(self, event):
            super()._on_comment(event)
            user = getattr(event, 'user', None)
            content = _normalize_comment(event.text)
            name = event.unique_id.lstrip('@').lower()
            uid = event.user_id
            common = getattr(event, 'common', None)
            msg_id = event.message_id
            with lock:
                evidence['comments'] += 1
                evidence['matching_content'] += content == _normalize_comment(message)
                evidence['matching_sender'] += bool((name and name == expected_name) or uid in expected_ids)
                evidence['matching_message_id'] += bool(sent_id and msg_id == sent_id)
                if content == _normalize_comment(message):
                    content_match_ids.append(msg_id)

    listener = Listener(host, show_comments=False)
    original_heartbeat = client.room_heartbeat
    original_post = client.post_chat_request
    original_enter = client.enter_live_room
    presence = None
    frames = None
    presence_started = None
    post_finished = None

    def presence_connected():
        return bool(presence and presence.available
                    and presence.connection.connected
                    and str(presence.room_id) == str(listener.room_id))

    @contextmanager
    def trace_heartbeat(*args, **kwargs):
        nonlocal presence, presence_started, frames
        print(json.dumps({'stage': 'heartbeat_start'}), flush=True)
        if not long_heartbeat:
            with original_heartbeat(*args, **kwargs):
                print(json.dumps({'stage': 'heartbeat_ready'}), flush=True)
                yield
            return
        username, room_id, runtime_cookie, user_agent = args
        presence = LiveMessageListener(username, show_comments=False,
            session_factory=lambda: prepare_account(username, room_id, runtime_cookie, user_agent),
            connection_role='account_heartbeat')
        if not presence.start(connect_timeout=30):
            raise RuntimeError('心跳连接失败')
        frames = presence.connection.evidence
        presence_started = time.monotonic()
        while True:
            elapsed = time.monotonic() - presence_started
            connected = presence_connected()
            snapshot = frames.snapshot()
            if not connected:
                raise RuntimeError('心跳断开')
            beats = snapshot['sent'].get('hb', 0)
            if (elapsed >= options.prewarm_seconds and beats >= options.min_beats
                    and str(room_id) in snapshot['entered_rooms']
                    and snapshot['received'].get('im_enter_room_resp', 0) > 0):
                break
            if elapsed >= 60:
                raise RuntimeError('心跳预热超时')
            time.sleep(0.25)
        print(json.dumps({'stage': 'heartbeat_ready', 'elapsed_seconds': round(elapsed, 2),
                          'connected': connected, **snapshot}), flush=True)
        # The outer finally owns cleanup so this socket survives the POST.
        yield

    def trace_post(*args, **kwargs):
        print(json.dumps({'stage': 'chat_post_started'}), flush=True)
        return original_post(*args, **kwargs)

    client.room_heartbeat = trace_heartbeat
    client.post_chat_request = trace_post
    if options.skip_enter:
        client.enter_live_room = lambda *args, **kwargs: None
    try:
        ready = listener.start(connect_timeout=25)
        print(json.dumps({'stage': 'listener', 'connected': ready,
                          'room_id': listener.room_id,
                          'header_age_seconds': int(time.time()) - timestamp}), flush=True)
        if not ready:
            return 2
        since = time.monotonic()
        print(json.dumps({'stage': 'sending', 'message': message,
                          'account': account.get('username'), 'mode': 'fresh_headers'}), flush=True)
        result = client.send_live_message(
            host, cookie, message, known_room_id=listener.room_id,
            user_agent=account.get('user_agent'), ticket_guard_state=state,
            ticket_guard_headers=headers,
        )
        post_finished = time.monotonic()
        payload = result.get('response') or {}
        data = payload.get('data') or {}
        sent_id = str(data.get('msg_id_str') or data.get('msg_id') or '')
        print(json.dumps({'stage': 'response', 'ok': result.get('ok'),
                          'http': result.get('httpStatus'), 'status': result.get('statusCode'),
                          'data_fields': sorted(data), 'has_message_id': bool(sent_id),
                          'has_server_prompt': bool(data.get('prompts')),
                          'server_content_matches': data.get('content') == message}), flush=True)
        observed = listener.wait_for_comment(
            message, since=since, timeout=30, expected_usernames={expected_name},
            expected_user_ids=expected_ids,
        )
        if long_heartbeat:
            while time.monotonic() - post_finished < 30:
                if not presence_connected():
                    break
                time.sleep(0.25)
            with presence._comments.mutex:
                account_comments = list(presence._comments.queue)
            print(json.dumps({'stage': 'heartbeat_after_send',
                              'held_seconds': round(time.monotonic() - post_finished, 2),
                              'connected': presence_connected(),
                              'account_stream_comments': len(account_comments),
                              'account_stream_content_matches': sum(
                                  _normalize_comment(c.comment) == _normalize_comment(message)
                                  for c in account_comments),
                              **frames.snapshot()}), flush=True)
        print(json.dumps({'stage': 'delivery', 'observed': observed is not None,
                          'listener_available': listener.available,
                          'error_type': type(listener.error).__name__ if listener.error else None,
                          **evidence,
                          'matching_message_id': sum(bool(sent_id and value == sent_id)
                                                     for value in content_match_ids),
                          'observed_message_id_matches': bool(
                              observed and sent_id and observed.message_id == sent_id)}), flush=True)
        return 0
    except Exception as exc:
        print(json.dumps({'stage': 'error', 'error_type': type(exc).__name__,
                          'classification': getattr(exc, 'classification', None),
                          'heartbeat_timeout': '心跳预热超时' in str(exc),
                          'heartbeat_disconnected': '心跳断开' in str(exc),
                          'heartbeat_connect_failed': '心跳连接失败' in str(exc),
                          'response_not_json': '不是 JSON' in str(exc)}), flush=True)
        return 3
    finally:
        if presence is not None:
            presence.close()
        listener.close()
        client.room_heartbeat = original_heartbeat
        client.post_chat_request = original_post
        client.enter_live_room = original_enter


if __name__ == '__main__':
    raise SystemExit(main())
