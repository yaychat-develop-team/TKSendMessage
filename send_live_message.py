"""Backward-compatible entrypoint for the modular tiktok_live package."""

import os
import time
from types import FunctionType
from pathlib import Path

from curl_cffi import requests
from loguru import logger

from tiktok_live import account_store as _store
from tiktok_live import browser_auth as _browser
from tiktok_live import client as _client
from tiktok_live import runner as _runner
from settings import (
    ACCOUNT_REQUEST_INTERVAL_SECONDS,
    ADD_NEW_ACCOUNT,
    LOGIN_TIMEOUT_SECONDS,
    MANUAL_INPUT,
    MESSAGE_OBSERVATION_ENABLED,
    MESSAGE_OBSERVATION_TIMEOUT_SECONDS,
    MESSAGE_NOT_OBSERVED_RETRIES,
    MESSAGE_NOT_OBSERVED_RETRY_DELAY_SECONDS,
    MESSAGE_OBSERVER_CONNECT_RETRIES,
    MESSAGE_OBSERVER_CONNECT_TIMEOUT_SECONDS,
    MESSAGE_OBSERVER_RETRY_DELAY_SECONDS,
    SHOW_LIVE_COMMENTS_IN_CONSOLE,
    ROOM_ID_COOKIE,
)
from tiktok_live.config import (
    ACCOUNT_STORE_PATH,
    BROWSER_ENV_PATH,
    CHAT_URL,
    IMPERSONATE,
    MS_TOKEN_URL,
    PROJECT_ROOT,
    RISK_KEYS,
    SEC_CH_UA,
    USER_AGENT,
)
from tiktok_live.errors import (
    AccountFileError,
    AccountStoreError,
    BrowserError,
    LoginTimeoutError,
    SafeOperationError,
    log_safe_error,
)
from tiktok_live.models import AccountValidation

_ORIGINAL_GET_FRESH_MS_TOKEN = _client.get_fresh_ms_token
_ORIGINAL_GET_SEC_SDK_CSRF_TOKEN = _client.get_sec_sdk_csrf_token
_ORIGINAL_GET_LIVE_ROOM = _client.get_live_room
_ORIGINAL_BUILD_CHAT_REQUEST = _client.build_chat_request
_ORIGINAL_POST_CHAT_REQUEST = _client.post_chat_request
_ORIGINAL_CLIENT_SEND = _client._send_live_message

def _invoke_isolated(module, function, overrides, *args, **kwargs):
    """Bind legacy overrides to call-local globals, never the shared modules.

    Rebind module-owned helpers too so nested calls and deferred retry closures
    retain this same environment. Imported dependencies and classes stay shared.
    This uses in-memory code objects and works in frozen builds without sources.
    """
    namespace = vars(module).copy()

    def bind(value):
        if not isinstance(value, FunctionType) or value.__globals__ is not vars(module):
            return value
        bound = FunctionType(value.__code__, namespace, value.__name__,
                             value.__defaults__, value.__closure__)
        bound.__kwdefaults__ = value.__kwdefaults__
        bound.__annotations__ = value.__annotations__
        return bound

    namespace.update({name: bind(value) for name, value in namespace.copy().items()})
    namespace.update(overrides)
    return bind(function)(*args, **kwargs)

# Store API
load_account_store = _store.load_account_store
save_account_store_atomic = _store.save_account_store_atomic
account_id_from_cookies = _store.account_id_from_cookies
upsert_account = _store.upsert_account
remove_account = _store.remove_account
cookie_header_from_playwright = _store.cookie_header_from_playwright
_account_identity = _store._account_identity
_identities_match = _store._identities_match
_deduplicate_accounts = _store._deduplicate_accounts

# Browser API
has_critical_login_cookie = _browser.has_critical_login_cookie
critical_cookies_locally_expired = _browser.critical_cookies_locally_expired
classify_login_page = _browser.classify_login_page
validate_saved_account = _browser.validate_saved_account
wait_for_tiktok_login = _browser.wait_for_tiktok_login
login_new_account = _browser.login_new_account

# Client helpers retained for existing callers and tests.
generate_ms_seed_token = _client.generate_ms_seed_token
build_ms_token_report_json = _client.build_ms_token_report_json
build_ms_token_environment = _client.build_ms_token_environment
sign_url = _client.sign_url
log_send_result = _client.log_send_result
interpret_chat_response = _client.interpret_chat_response


def get_fresh_ms_token(session, *args, **kwargs):
    return _invoke_isolated(_client, _ORIGINAL_GET_FRESH_MS_TOKEN, {
        'generate_ms_seed_token': generate_ms_seed_token,
        'build_ms_token_report_json': build_ms_token_report_json,
        'build_ms_token_environment': build_ms_token_environment,
        'sign_url': sign_url}, session, *args, **kwargs)


def get_sec_sdk_csrf_token(session, cookie, *args, **kwargs):
    return _invoke_isolated(_client, _ORIGINAL_GET_SEC_SDK_CSRF_TOKEN,
                            {'sign_url': sign_url}, session, cookie, *args, **kwargs)


def get_live_room(session, username, cookie, *args, **kwargs):
    return _invoke_isolated(_client, _ORIGINAL_GET_LIVE_ROOM,
                            {'ROOM_ID_COOKIE': ROOM_ID_COOKIE}, session, username, cookie, *args, **kwargs)


def build_chat_request(*args, **kwargs):
    return _invoke_isolated(_client, _ORIGINAL_BUILD_CHAT_REQUEST,
                            {'sign_url': sign_url}, *args, **kwargs)


def post_chat_request(*args, **kwargs):
    return _invoke_isolated(_client, _ORIGINAL_POST_CHAT_REQUEST,
                            {'sign_url': sign_url}, *args, **kwargs)


def _send_live_message(username: str, cookie: str, message: str, *, ticket_guard_state=None, ticket_guard_headers=None, account_display_name=None, user_agent=None, known_room_id=None, known_fetch_endpoint=None, known_region=None, known_device_id=None, room_id_cookie=None):
    return _invoke_isolated(_client, _ORIGINAL_CLIENT_SEND, {
        'get_fresh_ms_token': get_fresh_ms_token,
        'get_sec_sdk_csrf_token': get_sec_sdk_csrf_token,
        'get_live_room': get_live_room, 'build_chat_request': build_chat_request,
        'post_chat_request': post_chat_request, 'ROOM_ID_COOKIE': ROOM_ID_COOKIE},
        username, cookie, message, ticket_guard_state=ticket_guard_state,
        ticket_guard_headers=ticket_guard_headers,
        account_display_name=account_display_name, user_agent=user_agent,
        known_room_id=known_room_id, room_id_cookie=room_id_cookie,
        known_fetch_endpoint=known_fetch_endpoint, known_region=known_region,
        known_device_id=known_device_id,
    )


def send_live_message(username: str, cookie: str, message: str, *, ticket_guard_state=None, ticket_guard_headers=None, account_display_name=None, user_agent=None, known_room_id=None, known_fetch_endpoint=None, known_region=None, known_device_id=None, room_id_cookie=None):
    return _invoke_isolated(_client, _client.send_live_message,
        {'_send_live_message': _send_live_message},
        username, cookie, message, ticket_guard_state=ticket_guard_state,
        ticket_guard_headers=ticket_guard_headers,
        account_display_name=account_display_name, user_agent=user_agent,
        known_room_id=known_room_id, room_id_cookie=room_id_cookie,
        known_fetch_endpoint=known_fetch_endpoint, known_region=known_region,
        known_device_id=known_device_id,
    )


def run_saved_accounts(username, message, store_path=ACCOUNT_STORE_PATH,
                       add_new_account=ADD_NEW_ACCOUNT,
                       interval_seconds=ACCOUNT_REQUEST_INTERVAL_SECONDS,
                       observation_enabled=None,
                       observation_timeout_seconds=MESSAGE_OBSERVATION_TIMEOUT_SECONDS,
                       observation_retries=MESSAGE_NOT_OBSERVED_RETRIES,
                       observation_retry_delay_seconds=MESSAGE_NOT_OBSERVED_RETRY_DELAY_SECONDS,
                       observer_connect_timeout_seconds=MESSAGE_OBSERVER_CONNECT_TIMEOUT_SECONDS,
                       observer_connect_retries=MESSAGE_OBSERVER_CONNECT_RETRIES,
                       observer_retry_delay_seconds=MESSAGE_OBSERVER_RETRY_DELAY_SECONDS,
                       show_live_comments_in_console=SHOW_LIVE_COMMENTS_IN_CONSOLE,
                       login_timeout_seconds=LOGIN_TIMEOUT_SECONDS,
                       room_id_cookie=ROOM_ID_COOKIE):
    if observation_enabled is None:
        # The normal script entrypoint uses the configured account store and enables
        # observation. Compatibility callers with their own store keep legacy behavior.
        observation_enabled = (
            MESSAGE_OBSERVATION_ENABLED
            and Path(store_path).resolve() == Path(ACCOUNT_STORE_PATH).resolve()
        )
    return _invoke_isolated(_runner, _runner.run_saved_accounts,
        {'login_new_account': login_new_account, 'send_live_message': send_live_message},
        username, message, store_path, add_new_account, interval_seconds,
        observation_enabled, observation_timeout_seconds,
        observation_retries, observation_retry_delay_seconds,
        observer_connect_timeout_seconds,
        observer_connect_retries,
        observer_retry_delay_seconds,
        show_live_comments_in_console,
        login_timeout_seconds,
        room_id_cookie,
    )


def _run_cli() -> int:
    try:
        run_saved_accounts(MANUAL_INPUT["username"], MANUAL_INPUT["message"])
    except SafeOperationError as exc:
        log_safe_error(exc.classification)
        return 1
    except KeyboardInterrupt:
        log_safe_error("run_cancelled")
        return 1
    except Exception:
        log_safe_error("runtime_error")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(_run_cli()) from None
