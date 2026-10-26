"""Single-account, single-send diagnostic; never updates the account store."""
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from loguru import logger
from listen_msg import LiveMessageListener
from tiktok_live import client
from tiktok_live.account_store import load_account_store, cookie_header_from_playwright
from tiktok_live.runner import _numeric_tiktok_uids


def main():
    logger.remove()  # Do not persist account credentials or raw server replies.
    accounts = load_account_store(Path('dist/runtime/accounts/tiktok_accounts.json'))['accounts']
    if not accounts and '--send' in sys.argv:
        print(json.dumps({'error': 'no_saved_accounts'}), flush=True)
        return 2
    listener = LiveMessageListener('kongziystj3', show_comments=False)
    try:
        if not listener.start(connect_timeout=30):
            print(json.dumps({'error': 'listener_unavailable',
                              'type': type(listener.error).__name__}), flush=True)
            return 3
        if '--send' not in sys.argv:
            print(json.dumps({'listener_ready': True, 'account_count': len(accounts)}), flush=True)
            return 0
        account = accounts[0]
        message = 'hello ' + time.strftime('%H%M%S')
        since = time.monotonic()
        print(json.dumps({'stage': 'sending_one_message', 'message': message}), flush=True)
        result = client.send_live_message(
            'kongziystj3', cookie_header_from_playwright(account['cookies']), message,
            known_room_id=listener.room_id,
            user_agent=account.get('user_agent'),
            ticket_guard_state=account.get('ticket_guard_state'),
            ticket_guard_headers=account.get('ticket_guard_headers'),
        )
        data = (result.get('response') or {}).get('data') or {}
        sent_id = str(data.get('msg_id_str') or data.get('msg_id') or '')
        print(json.dumps({'stage': 'response', 'http': result.get('httpStatus'),
                          'status': result.get('statusCode'), 'msg_id': sent_id}), flush=True)
        observed = listener.wait_for_comment(
            message, since=since, timeout=30,
            expected_usernames={account.get('username') or ''},
            expected_user_ids=_numeric_tiktok_uids(account['cookies']),
        )
        print(json.dumps({'observed': observed is not None,
                          'message_id_matches': bool(observed and sent_id and
                                                     observed.message_id == sent_id)}), flush=True)
        return 0 if observed else 4
    except Exception as exc:
        print(json.dumps({'error': type(exc).__name__,
                          'classification': getattr(exc, 'classification', None)}), flush=True)
        return 5
    finally:
        listener.close()


if __name__ == '__main__':
    raise SystemExit(main())
