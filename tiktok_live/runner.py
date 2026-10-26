"""One-pass multi-account orchestration."""

import hashlib
import json
import os
import re
import tempfile
import threading
import time
from contextlib import nullcontext
from copy import deepcopy
from pathlib import Path
from typing import Callable
from urllib.parse import urlsplit

from loguru import logger
from curl_cffi import requests

from .errors import SafeOperationError, RetryableRoomLookupError

from .account_store import (
    _account_identity,
    _deduplicate_accounts,
    _ensure_environment_seeds,
    _identities_match,
    _numeric_tiktok_uids,
    cookie_header_from_playwright,
    load_account_store,
    remove_account,
    save_account_store_atomic,
)
from .browser_auth import login_new_account
from .algorithms.ticket_guard import build_headers_from_cookie_state
from .client import send_live_message, get_live_room, get_fresh_ms_token, _transient_room_error
from .errors import SafeOperationError
from .config import (
    ACCOUNT_REQUEST_INTERVAL_SECONDS,
    ACCOUNT_STORE_PATH,
    ADD_NEW_ACCOUNT,
    CHAT_URL,
    LOGIN_TIMEOUT_SECONDS,
    MESSAGE_OBSERVATION_ENABLED,
    MESSAGE_OBSERVATION_TIMEOUT_SECONDS,
    MESSAGE_NOT_OBSERVED_RETRIES,
    MESSAGE_NOT_OBSERVED_RETRY_DELAY_SECONDS,
    MESSAGE_OBSERVER_CONNECT_RETRIES,
    MESSAGE_OBSERVER_CONNECT_TIMEOUT_SECONDS,
    MESSAGE_OBSERVER_RETRY_DELAY_SECONDS,
    ROOM_ID_COOKIE,
    SHOW_LIVE_COMMENTS_IN_CONSOLE,
    browser_profile_from_user_agent,
)
from listen_msg import LiveMessageListener
from .heartbeat import room_heartbeat_session
from .retry_policy import transient_failure, terminal_failure
from .wss.sessions import validate_room_id
from .signing import cookie_with_ms_token
from .realtime_delivery import RealtimeDelivery
from .delivery_guard import ConsecutiveUnobserved
from .request_fingerprint import request_flow, current_fingerprint


def _collect_message_burst(send_first, count, interval, should_cancel, *, attempts=1, retry_delay=0,
                           on_posted=None):
    """Dispatch the requested POSTs before waiting for delivery receipts."""
    posted = []
    retry = None
    for index in range(count):
        if _cancel_requested(should_cancel):
            break
        if index:
            _pause(interval, should_cancel)
            if _cancel_requested(should_cancel):
                break
        logger.info('连续发送 | message_index={}/{} | classification=message_sending',
                    index + 1, count)
        attempt = 0
        while True:
            attempt += 1
            started = time.monotonic()
            try:
                result = retry() if callable(retry) else send_first()
            except Exception as exc:
                result = exc
            candidate = getattr(result, 'retry_message', None)
            if callable(candidate):
                retry = candidate
            if not transient_failure(result) or attempt >= attempts or _cancel_requested(should_cancel):
                break
            logger.warning('消息临时失败，准备重试 | message_index={}/{} | attempt={}/{} '
                           '| delay_seconds={} | classification=message_request_retrying',
                           index+1,count,attempt+1,attempts,retry_delay)
            if retry_delay > 0:
                _pause(retry_delay, should_cancel)
            if _cancel_requested(should_cancel):
                break
        posted.append((result, started, attempt))
        if on_posted is not None:
            on_posted(index + 1, result, started)
        if isinstance(result, Exception):
            break
        retry = getattr(result, 'retry_message', None)
        if (not result.get('ok') and not result.get('requiresObservation')
                or result.get('loginExpired') or result.get('accountSilenced')
                or result.get('accountBanned')
                or result.get('stopProcessing')):
            break
    return posted


def _failed_account_identity(account: dict, short_id: str) -> tuple[str, str]:
    """Return log-safe sender username and numeric TikTok UID when available."""
    raw_username = str(account.get("username") or "").strip().lstrip("@")
    username = re.sub(r"[^A-Za-z0-9._-]", "", raw_username)[:64] or "-"
    numeric_uids = sorted(_numeric_tiktok_uids(account.get("cookies", [])))
    user_id = numeric_uids[0] if len(numeric_uids) == 1 else short_id
    return username, user_id


def _clean_account_username(value: object) -> str:
    return re.sub(r"[^A-Za-z0-9._-]", "", str(value or "").strip().lstrip("@"))[:64]


def log_message_display(*, room: str, account: dict, index: int, total: int,
                        message_id: str, status: str, classification: str) -> None:
    """Log receipt evidence, without asserting what every viewer can see."""
    if status not in {'observed', 'not_observed', 'listener_unavailable', 'send_failed'}:
        raise ValueError('Invalid display status')
    account_id = str(account.get('id') or '')
    short_id = (account_id[:12] if re.fullmatch(r'[a-f0-9]{64}', account_id)
                else hashlib.sha256(account_id.encode('utf-8')).hexdigest()[:12])
    safe_message_id = str(message_id or '')
    if not re.fullmatch(r'[0-9]+', safe_message_id):
        safe_message_id = '-'
    log = logger.success if status == 'observed' else logger.info
    log(
        '{} | room={} | account_username={} | account_id={} | '
        'message_index={}/{} | message_id={} | display_status={} | classification={}',
        '消息成功展示' if status == 'observed' else '消息展示观察结果',
        _clean_account_username(room) or '-',
        _clean_account_username(account.get('username')) or short_id, short_id,
        index, total, safe_message_id, status,
        re.sub(r'[^A-Za-z0-9_]', '', str(classification or ''))[:64] or 'unknown',
    )


def _listener_unavailable(listener, since=None) -> bool:
    coverage = getattr(listener, 'observation_available_since', None)
    if since is not None and callable(coverage) and not coverage(since):
        return True
    return (listener is None or not getattr(listener, 'available', True)
            or getattr(listener, 'error', None) is not None)


def _save_failed_accounts_report(path: Path, records: list[dict[str, str]]) -> None:
    """Atomically replace the current-run failure report."""
    temporary = None
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        with tempfile.NamedTemporaryFile(
            mode="w", encoding="utf-8", dir=path.parent,
            prefix=f".{path.name}.", suffix=".tmp", delete=False,
        ) as handle:
            temporary = Path(handle.name)
            json.dump({"failed_accounts": records}, handle, ensure_ascii=False, indent=2)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        if temporary is not None:
            try:
                temporary.unlink(missing_ok=True)
            except OSError:
                pass


def _save_silenced_accounts_report(path: Path, records: list[dict]) -> None:
    """Atomically replace the current-run silenced account report."""
    temporary = None
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        with tempfile.NamedTemporaryFile(
            mode="w", encoding="utf-8", dir=path.parent,
            prefix=f".{path.name}.", suffix=".tmp", delete=False,
        ) as handle:
            temporary = Path(handle.name)
            json.dump({"silenced_accounts": records}, handle, ensure_ascii=False, indent=2)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        if temporary is not None:
            try:
                temporary.unlink(missing_ok=True)
            except OSError:
                pass


def _cancel_requested(should_cancel: Callable[[], bool] | None) -> bool:
    return should_cancel is not None and bool(should_cancel())


def _pause(seconds: float, should_cancel: Callable[[], bool] | None) -> None:
    if should_cancel is None:
        time.sleep(seconds)
        return
    deadline = time.monotonic() + max(0.0, seconds)
    while not _cancel_requested(should_cancel):
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            break
        time.sleep(min(0.1, remaining))


def _start_live_listener(
    username: str,
    *,
    connect_timeout: float,
    connect_retries: int,
    retry_delay_seconds: float,
    show_comments: bool,
    heartbeat_interval_seconds: float = 10.0,
    should_cancel: Callable[[], bool] | None = None,
    resolve_room_id: Callable[[], str | None] | None = None,
    observer_proxy: str = '',
):
    retries = max(0, int(connect_retries))
    total_attempts = retries + 1
    delay = max(0.0, float(retry_delay_seconds))
    clean_username = str(username).strip().lstrip("@")
    known_room_id = None
    known_fetch_endpoint = None
    known_region = None
    known_device_id = None
    for attempt in range(1, total_attempts + 1):
        if _cancel_requested(should_cancel):
            return None
        logger.info(
            "开始连接直播弹幕监听 | username={} | attempt={}/{} | classification=listener_connecting",
            clean_username,
            attempt,
            total_attempts,
        )
        candidate = None
        failure_kind = 'ConnectTimeout'
        failure_stage = 'room_lookup' if known_room_id is None and resolve_room_id is not None else 'wss_transport'
        try:
            if known_room_id is None and resolve_room_id is not None:
                resolved_room = resolve_room_id()
                if isinstance(resolved_room, dict):
                    known_room_id = validate_room_id(resolved_room.get('roomId'))
                    endpoint = resolved_room.get('fetchEndpoint')
                    if isinstance(endpoint, str) and endpoint.strip():
                        known_fetch_endpoint = endpoint.strip()
                    region = resolved_room.get('region')
                    if isinstance(region, str) and region.strip():
                        known_region = region.strip().upper()
                    device_id = resolved_room.get('deviceId')
                    if isinstance(device_id, str) and device_id.strip():
                        known_device_id = device_id.strip()
                else:
                    known_room_id = resolved_room
                if _cancel_requested(should_cancel):
                    return None
                if known_room_id is None:
                    raise SafeOperationError('room_state_unconfirmed')
            failure_stage = 'wss_transport'
            listener_options = dict(show_comments=show_comments,
                heartbeat_interval_seconds=heartbeat_interval_seconds)
            if observer_proxy:
                listener_options['observer_proxy'] = observer_proxy
            if known_room_id is not None:
                listener_options['room_id'] = known_room_id
            candidate = LiveMessageListener(username, **listener_options)
            if known_fetch_endpoint:
                candidate.fetch_endpoint = known_fetch_endpoint
            if known_region:
                candidate.region = known_region
            if known_device_id:
                candidate.device_id = known_device_id
            start_options = {'connect_timeout':connect_timeout}
            if should_cancel is not None:
                start_options['should_cancel'] = should_cancel
            if candidate.start(**start_options):
                if _cancel_requested(should_cancel):
                    candidate.close()
                    return None
                logger.success(
                    "直播弹幕监听已连接 | username={} | attempt={}/{} | classification=listener_connected",
                    clean_username,
                    attempt,
                    total_attempts,
                )
                return candidate
            error = getattr(candidate, 'error', None)
            if error is not None:
                failure_kind = type(error).__name__
        except RetryableRoomLookupError:
            failure_kind = 'RoomLookupTemporaryFailure'
        except SafeOperationError:
            raise
        except Exception as exc:
            failure_kind = type(exc).__name__
        logger.warning('直播监听连接未就绪 | stage={} | error_type={} | classification=listener_connect_failed',
                       failure_stage, failure_kind)
        if candidate is not None:
            try:
                candidate.close()
            except Exception:
                pass
        if _cancel_requested(should_cancel):
            return None
        if attempt <= retries:
            logger.warning(
                "直播弹幕监听连接失败，{} 秒后重试 | retry={}/{} | stage={} | classification=listener_retrying",
                delay,
                attempt,
                retries,
                failure_stage,
            )
            _pause(delay, should_cancel)
    logger.warning(
        "直播弹幕监听不可用，停止本次发送流程 | classification=listener_unavailable"
    )
    return None


def _resolve_observer_room_id(username, accounts, selected_ids, room_id_cookie, should_cancel,
                              *, connect_timeout):
    with request_flow():
        return _resolve_observer_room_id_in_flow(username, accounts, selected_ids,
            room_id_cookie, should_cancel, connect_timeout=connect_timeout)


def _resolve_observer_room_id_in_flow(username, accounts, selected_ids, room_id_cookie, should_cancel,
                              *, connect_timeout):
    """One anonymous lookup per outer attempt; never multiply retries by accounts."""
    deadline = time.monotonic() + max(0.1, float(connect_timeout))
    for account in accounts:
        if selected_ids is not None and account.get('id') not in selected_ids:
            continue
        if _cancel_requested(should_cancel):
            return None
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise RetryableRoomLookupError()
        stage = 'live_room'
        try:
            cookie = cookie_header_from_playwright(account['cookies'])
            profile = browser_profile_from_user_agent(account.get('user_agent'))
            session = requests.Session(impersonate=current_fingerprint())
            try:
                logger.info('使用账号会话查询直播页 room_id，匿名会话兜底 | transport=curl_cffi '
                            '| classification=observer_room_lookup_start')
                room = get_live_room(session, username, cookie, profile=profile,
                                     fallback_cookie=room_id_cookie,
                                     timeout_seconds=remaining, should_cancel=should_cancel)
            finally:
                session.close()
            if _cancel_requested(should_cancel):
                return None
            if time.monotonic() >= deadline:
                logger.warning('room_id 查询等待超时 | classification=observer_room_lookup_timeout')
                raise RetryableRoomLookupError()
            if room.get('exists'):
                resolved_username = str(room.get('username') or '').lstrip('@')
                if resolved_username and resolved_username.casefold() != str(username).lstrip('@').casefold():
                    logger.warning('访客监听 room_id 查询的主播不匹配 | '
                                   'classification=observer_room_owner_mismatch')
                    raise SafeOperationError('room_state_unconfirmed')
                room_id = validate_room_id(room['roomId'])
                if 'isLive' in room and room['isLive'] is not True:
                    state = 'offline' if room['isLive'] is False else 'unknown'
                    logger.warning('已查询到房间 ID，但开播状态不允许连接或发送 | room_id={} '
                                   '| live_state={} | classification=observer_room_not_live', room_id, state)
                    raise SafeOperationError('room_offline' if room['isLive'] is False
                                             else 'room_state_unconfirmed')
                logger.info('已取得访客监听所需 room_id | room_id={} '
                            '| classification=observer_room_ready', room_id)
                resolved = dict(room)
                resolved['roomId'] = room_id
                return resolved
        except SafeOperationError:
            raise
        except Exception as exc:
            reason = 'waf_challenge' if isinstance(exc, (RuntimeError, ValueError)) and 'WAF' in str(exc) else 'request_failed'
            logger.warning('访客监听 room_id 查询失败 | stage={} | reason={} | error_type={} '
                           '| classification=observer_room_lookup_failed', stage, reason, type(exc).__name__)
            if _cancel_requested(should_cancel):
                return None
            if _transient_room_error(exc):
                raise RetryableRoomLookupError() from None
            raise SafeOperationError('room_state_unconfirmed') from None
        # Unknown metadata is terminal, not a reason to try another account.
        raise SafeOperationError('room_state_unconfirmed')
    if _cancel_requested(should_cancel):
        return None
    raise SafeOperationError('room_state_unconfirmed')

def run_saved_accounts(
    username: str,
    message: str,
    store_path: Path = ACCOUNT_STORE_PATH,
    add_new_account: bool = ADD_NEW_ACCOUNT,
    interval_seconds: float = ACCOUNT_REQUEST_INTERVAL_SECONDS,
    observation_enabled: bool = MESSAGE_OBSERVATION_ENABLED,
    observation_timeout_seconds: float = MESSAGE_OBSERVATION_TIMEOUT_SECONDS,
    observation_retries: int = MESSAGE_NOT_OBSERVED_RETRIES,
    observation_retry_delay_seconds: float = MESSAGE_NOT_OBSERVED_RETRY_DELAY_SECONDS,
    observer_connect_timeout_seconds: float = MESSAGE_OBSERVER_CONNECT_TIMEOUT_SECONDS,
    observer_connect_retries: int = MESSAGE_OBSERVER_CONNECT_RETRIES,
    observer_retry_delay_seconds: float = MESSAGE_OBSERVER_RETRY_DELAY_SECONDS,
    show_live_comments_in_console: bool = SHOW_LIVE_COMMENTS_IN_CONSOLE,
    login_timeout_seconds: int = LOGIN_TIMEOUT_SECONDS,
    room_id_cookie: str = ROOM_ID_COOKIE,
    *,
    should_cancel: Callable[[], bool] | None = None,
    on_progress: Callable[[dict], None] | None = None,
    report_dir: Path | None = None,
    selected_account_ids: tuple[str, ...] | None = None,
    message_count: int | None = None,
    message_interval_seconds: float = 1.0,
    heartbeat_interval_seconds: float = 10.0,
    observer_proxy: str = '',
) -> dict[str, int]:
    """Send per distinct saved account; cancellation is cooperative.

    Optional desktop callbacks run on the calling worker thread and receive only
    counters. Explicit message_count counts individual messages and keeps a room
    connection open for each batch. CLI callers omitting it retain legacy retries.
    """
    from .wss.proxy import normalize_observer_proxy
    observer_proxy = normalize_observer_proxy(observer_proxy)
    counters = {"success": 0, "failed": 0, "expired": 0, "temporaryError": 0}
    if observation_enabled:
        counters.update({"delivered": 0, "acceptedNotObserved": 0, "listenerUnavailable": 0})
    silenced_count = 0
    failure_reasons = []
    progress_lock = threading.RLock()
    live_trackers = []

    def snapshot() -> dict:
        counts = dict(counters)
        if message_count is not None:
            with progress_lock:
                counts['sent'] = sum(len(tracker.sent) for tracker in live_trackers)
                if observation_enabled:
                    counts['delivered'] = max(counts['delivered'],
                        sum(len(tracker.observed) for tracker in live_trackers))
        if on_progress is not None:
            from .errors import safe_failure_reasons
            counts['failure_reasons'] = safe_failure_reasons(failure_reasons +
                [tracker.restriction for tracker in live_trackers if tracker.restriction])
        if should_cancel is not None or on_progress is not None:
            counts.update(processed=sum(counters[key] for key in
                ("success", "failed", "expired", "temporaryError")),
                silenced=silenced_count, cancelled=_cancel_requested(should_cancel))
        return counts

    def publish_progress():
        with progress_lock:
            if on_progress is not None:
                on_progress(snapshot())

    if _cancel_requested(should_cancel):
        return snapshot()
    store = load_account_store(store_path)
    if selected_account_ids is not None:
        selected_ids = set(selected_account_ids)
        if not selected_ids or not any(
                account['id'] in selected_ids for account in store['accounts']):
            raise ValueError('No selected saved accounts')
    newly_logged_account = None
    if not add_new_account and not store["accounts"]:
        logger.info(
            "账号库中没有账号，正在打开登录界面新增账号 | "
            "classification=account_store_empty"
        )
    if add_new_account or not store["accounts"]:
        logger.info("开始新增账户登录 | classification=scan_start")
        try:
            newly_logged_account = login_new_account(
                store_path, timeout_seconds=max(0, int(login_timeout_seconds))
            )
        except Exception:
            logger.error("登录未完成，未新增账户 | classification=scan_failed")
            raise
        logger.success("登录流程完成 | classification=scan_saved")
        store = load_account_store(store_path)

    if _cancel_requested(should_cancel):
        return snapshot()
    listener = None
    sent_any = False
    sent_identities = []
    failed_accounts: dict[str, dict[str, str]] = {}
    silenced_accounts: dict[str, dict] = {}
    metadata_save_failed = False

    def save_completed_account_state() -> None:
        # POSTs already completed: a local write failure must not discard their
        # buffered receipts, logs or counters. Fail the task after publishing them.
        nonlocal metadata_save_failed
        if metadata_save_failed:
            return
        try:
            save_account_store_atomic(store_path, store)
        except Exception:
            metadata_save_failed = True

    def record_failed_account(account: dict, short_id: str, classification: str) -> None:
        if classification not in failure_reasons and len(failure_reasons) < 32:
            failure_reasons.append(classification)
        account_name, _ = _failed_account_identity(account, short_id)
        failed_accounts[str(account.get("id") or short_id)] = {
            "account_id": short_id,
            "username": account_name,
            "classification": classification,
        }

    try:
        if observation_enabled:
            listener_kwargs = {}
            if should_cancel is not None:
                listener_kwargs["should_cancel"] = should_cancel
            if observer_proxy:
                listener_kwargs['observer_proxy'] = observer_proxy
            listener = _start_live_listener(
                username,
                connect_timeout=observer_connect_timeout_seconds,
                connect_retries=observer_connect_retries,
                retry_delay_seconds=observer_retry_delay_seconds,
                show_comments=show_live_comments_in_console,
                heartbeat_interval_seconds=heartbeat_interval_seconds,
                resolve_room_id=lambda: _resolve_observer_room_id(
                    username, store['accounts'], selected_ids if selected_account_ids is not None else None,
                    room_id_cookie, should_cancel,
                    connect_timeout=observer_connect_timeout_seconds),
                **listener_kwargs,
            )
            if listener is None:
                if _cancel_requested(should_cancel):
                    return snapshot()
                logger.error('直播间任务启动失败 | room={} | classification=listener_unavailable',
                             _clean_account_username(username) or '-')
                raise SafeOperationError("listener_unavailable")
        store_changed = _deduplicate_accounts(store)
        store_changed = _ensure_environment_seeds(store) or store_changed
        for saved_account in store["accounts"]:
            if (isinstance(saved_account.get("ticket_guard_headers"), dict)
                    or not isinstance(saved_account.get("ticket_guard"), dict)):
                continue
            try:
                saved_account["ticket_guard_headers"] = build_headers_from_cookie_state(
                    cookie_header_from_playwright(saved_account["cookies"]),
                    saved_account["ticket_guard"],
                    urlsplit(CHAT_URL).path,
                )
                store_changed = True
            except Exception:
                logger.warning(
                    "旧账号 Ticket Guard 请求头迁移失败，将保留原兼容流程 | "
                    "classification=ticket_guard_headers_migration_failed"
                )
        if store_changed:
            save_account_store_atomic(store_path, store)
        accounts_to_send = store["accounts"]
        if selected_account_ids is not None:
            accounts_to_send = [account for account in accounts_to_send
                                if account['id'] in selected_ids]
        if add_new_account and newly_logged_account is not None:
            new_identity = _account_identity(newly_logged_account["cookies"])
            accounts_to_send = [
                account for account in store["accounts"]
                if _identities_match(_account_identity(account["cookies"]), new_identity)
            ]
        for index, account in enumerate(deepcopy(accounts_to_send), start=1):
            if _cancel_requested(should_cancel):
                break
            account_id = account["id"]
            # A previous login-required response may have removed this snapshot entry.
            if not any(item["id"] == account_id for item in store["accounts"]):
                continue
            short_id = (account_id[:12] if re.fullmatch(r"[a-f0-9]{64}", account_id)
                        else hashlib.sha256(account_id.encode("utf-8")).hexdigest()[:12])
            identity = _account_identity(account["cookies"])
            if any(_identities_match(previous, identity) for previous in sent_identities):
                continue
            if sent_any and interval_seconds > 0:
                _pause(interval_seconds, should_cancel)
            if _cancel_requested(should_cancel):
                break
            sent_any = True
            sent_identities.append(identity)
            logger.info("账户开始发送 | account_index={} | account_id={}", index, short_id)
            total_messages = max(1, int(message_count)) if message_count is not None else 1
            message_interval_seconds = max(0.1, float(message_interval_seconds))
            pending_messages = None
            batch_terminal = False
            def stop_for_misses():
                record_failed_account(account, short_id, 'consecutive_not_observed')
                logger.warning('连续 3 条消息未观察到展示，停止该账号在本直播间的后续发送 | '
                    'account_id={} | classification=consecutive_not_observed', short_id)
                publish_progress()
            missed_guard = ConsecutiveUnobserved(on_stop=stop_for_misses, lock=progress_lock)
            def send_cancelled():
                with progress_lock:
                    return _cancel_requested(should_cancel) or missed_guard.stopped
            def receipt_outcome(number, status):
                if status != 'not_observed' or not _cancel_requested(should_cancel):
                    missed_guard.record(number, status)
            session_scope = room_heartbeat_session(should_cancel,
                connect_timeout=observer_connect_timeout_seconds,
                connect_retries=observer_connect_retries,
                retry_delay_seconds=observer_retry_delay_seconds,
                heartbeat_interval_seconds=heartbeat_interval_seconds)
            def log_unobserved(number, mid):
                log_message_display(room=username, account=account, index=number,
                    total=total_messages, message_id=mid, status='not_observed',
                    classification='accepted_not_observed')
                logger.warning('监听期内未发现实际弹幕 | account_id={} | message_index={}/{} '
                    '| classification=accepted_not_observed', short_id, number, total_messages)
            live_scope = (RealtimeDelivery(listener, account=account, message=message,
                lock=progress_lock, notify=publish_progress,
                timeout=observation_timeout_seconds if observation_enabled else None,
                log_timeout=log_unobserved, should_cancel=should_cancel, on_outcome=receipt_outcome,
                log_observed=lambda number, mid: log_message_display(room=username, account=account,
                    index=number, total=total_messages, message_id=mid,
                    status='observed', classification='delivered'))
                if message_count is not None else nullcontext())
            with request_flow(), session_scope, live_scope as live:
                if live is not None:
                    live_trackers.append(live)
                receipt_check_failed = False
                for message_number in range(total_messages):
                    if pending_messages is not None and message_number >= len(pending_messages):
                        break
                    attempt_limit = max(1, int(observation_retries))
                    retry_delay = max(0.0, float(observation_retry_delay_seconds))
                    send_attempt = 1
                    retry_message_request = None
                    stop_processing = False
                    while True:
                        if (send_cancelled() and
                                (message_count is None or pending_messages is None or send_attempt > 1)):
                            break
                        send_started_at = time.monotonic()
                        message_id = ''

                        def display(status, classification):
                            if live is not None and not live.finalize_display(message_number + 1, status):
                                return
                            log_message_display(room=username, account=account,
                                index=message_number + 1, total=total_messages,
                                message_id=message_id, status=status,
                                classification=classification)

                        try:
                            if send_attempt > 1 and callable(retry_message_request):
                                result = retry_message_request()
                            else:
                                send_kwargs = {}
                                if isinstance(account.get("ticket_guard"), dict):
                                    send_kwargs["ticket_guard_state"] = account["ticket_guard"]
                                if isinstance(account.get("ticket_guard_headers"), dict):
                                    send_kwargs["ticket_guard_headers"] = account["ticket_guard_headers"]
                                if isinstance(account.get("user_agent"), str) and account["user_agent"].strip():
                                    send_kwargs["user_agent"] = account["user_agent"].strip()
                                listener_room_id = str(getattr(listener, "room_id", "") or "").strip()
                                if listener_room_id:
                                    send_kwargs["known_room_id"] = listener_room_id
                                listener_fetch_endpoint = str(
                                    getattr(listener, "fetch_endpoint", "") or ""
                                ).strip()
                                if listener_fetch_endpoint:
                                    send_kwargs["known_fetch_endpoint"] = listener_fetch_endpoint
                                listener_region = str(
                                    getattr(listener, "region", "") or ""
                                ).strip()
                                if listener_region:
                                    send_kwargs["known_region"] = listener_region
                                listener_device_id = str(
                                    getattr(listener, "device_id", "") or ""
                                ).strip()
                                if listener_device_id:
                                    send_kwargs["known_device_id"] = listener_device_id
                                account_display_name = str(
                                    account.get("nickname") or account.get("username") or ""
                                ).strip()
                                if account_display_name and account_display_name != "-":
                                    send_kwargs["account_display_name"] = account_display_name
                                send_kwargs["room_id_cookie"] = room_id_cookie
                                if message_count is not None and send_attempt == 1:
                                    if pending_messages is None:
                                        pending_messages = _collect_message_burst(
                                            lambda: send_live_message(
                                                username, cookie_header_from_playwright(account["cookies"]), message,
                                                **send_kwargs,
                                            ),
                                            total_messages, message_interval_seconds, send_cancelled,
                                            attempts=attempt_limit, retry_delay=retry_delay,
                                            on_posted=lambda number, result, started: live.register(
                                                number, result, started, _numeric_tiktok_uids(account['cookies'])))
                                        batch_terminal = any(terminal_failure(item[0]) or
                                            isinstance(item[0],Exception) and not transient_failure(item[0])
                                            for item in pending_messages)
                                    if message_number >= len(pending_messages):
                                        break
                                    result, send_started_at, send_attempt = pending_messages[message_number]
                                    if isinstance(result, Exception):
                                        raise result
                                else:
                                    result = send_live_message(
                                        username, cookie_header_from_playwright(account["cookies"]), message,
                                        **send_kwargs,
                                    )
                            if live is not None and send_attempt > 1:
                                live.register(message_number + 1, result, send_started_at,
                                              _numeric_tiktok_uids(account['cookies']))
                            candidate_retry = getattr(result, "retry_message", None)
                            if callable(candidate_retry):
                                retry_message_request = candidate_retry
                            response = result.get('response')
                            response_data = response.get('data') if isinstance(response, dict) else None
                            if isinstance(response_data, dict):
                                message_id = str(response_data.get('msg_id_str')
                                                 or response_data.get('msg_id') or '')
                        except Exception as exc:
                            candidate_retry = getattr(exc,'retry_message',None)
                            if callable(candidate_retry):
                                retry_message_request = candidate_retry
                            if (transient_failure(exc) and send_attempt < attempt_limit
                                    and not send_cancelled()):
                                send_attempt += 1
                                logger.warning('消息临时异常，准备重试 | attempt={}/{} '
                                    '| classification=message_request_retrying', send_attempt, attempt_limit)
                                if retry_delay > 0:
                                    _pause(retry_delay, should_cancel)
                                if not _cancel_requested(should_cancel):
                                    continue
                            # Count this account once and continue without exposing exception content.
                            classification = exc.classification if isinstance(exc, SafeOperationError) else "send_exception"
                            counters["failed"] += 1
                            record_failed_account(account, short_id, classification)
                            display('send_failed', classification)
                            logger.error(
                                "账户发送异常 | account_index={} | account_id={} | "
                                "classification={}",
                                index, short_id, classification,
                            )
                            break

                        resolved_account_username = _clean_account_username(result.get("accountUsername"))
                        resolved_account_nickname = str(result.get("accountNickname") or "").strip()
                        if resolved_account_username:
                            account["username"] = resolved_account_username
                        if resolved_account_nickname:
                            account["nickname"] = resolved_account_nickname
                        if resolved_account_username or resolved_account_nickname:
                            for saved_account in store["accounts"]:
                                if saved_account["id"] != account_id:
                                    continue
                                changed = False
                                if (resolved_account_username
                                        and saved_account.get("username") != resolved_account_username):
                                    saved_account["username"] = resolved_account_username
                                    changed = True
                                if (resolved_account_nickname
                                        and saved_account.get("nickname") != resolved_account_nickname):
                                    saved_account["nickname"] = resolved_account_nickname
                                    changed = True
                                if changed:
                                    save_completed_account_state()
                                break

                        if result.get("loginExpired") is True:
                            remove_account(store, account["id"])
                            save_completed_account_state()
                            counters["expired"] += 1
                            record_failed_account(account, short_id, "expired")
                            display('send_failed', 'expired')
                            logger.info(
                                "账户发送完成 | account_index={} | account_id={} | classification=expired",
                                index, short_id,
                            )
                            break

                        succeeded = bool(result.get("ok"))
                        requires_observation = result.get("requiresObservation") is True
                        classification = str(result.get("classification") or (
                            "success" if succeeded else "send_failed"
                        ))
                        if result.get('accountBanned'):
                            classification = 'account_banned'
                        elif result.get('accountSilenced'):
                            classification = 'account_silenced'
                        if result.get("accountSilenced") is True:
                            account_name, _ = _failed_account_identity(account, short_id)
                            silenced_accounts[str(account.get("id") or short_id)] = {
                                "account_id": short_id,
                                "username": account_name,
                                "classification": "account_silenced",
                                "status_code": (
                                    result.get("statusCode")
                                    if type(result.get("statusCode")) is int else None
                                ),
                            }
                            logger.warning(
                                "账号已被直播间禁言 | account_id={} | username={} | "
                                "classification=account_silenced",
                                short_id, account_name,
                            )

                        if not requires_observation and not succeeded:
                            if (transient_failure(result) and send_attempt < attempt_limit
                                    and not send_cancelled()):
                                send_attempt += 1
                                logger.warning('消息接口临时失败，准备重试 | attempt={}/{} '
                                    '| classification=message_request_retrying', send_attempt, attempt_limit)
                                if retry_delay > 0:
                                    _pause(retry_delay, should_cancel)
                                if not _cancel_requested(should_cancel):
                                    continue
                            counters["failed"] += 1
                            record_failed_account(account, short_id, classification)
                            display('send_failed', classification)
                            logger.info(
                                "账户发送完成 | account_index={} | account_id={} | classification={}",
                                index, short_id, classification,
                            )
                            if result.get("stopProcessing") is True:
                                logger.error(
                                    "发送响应要求终止后续账号 | status_code={} | classification={}",
                                    result.get("statusCode"), classification,
                                )
                                stop_processing = True
                            break

                        if not observation_enabled:
                            display('listener_unavailable', 'observation_disabled')
                            if requires_observation:
                                counters["failed"] += 1
                                record_failed_account(account, short_id, "observation_required")
                                logger.warning(
                                    "未启用直播弹幕确认，按失败处理 | account_id={} | "
                                    "classification=observation_required",
                                    short_id,
                                )
                            else:
                                counters["success"] += 1
                                logger.info(
                                    "账户发送完成 | account_index={} | account_id={} | "
                                    "classification={}",
                                    index, short_id, classification,
                                )
                            break

                        if requires_observation:
                            logger.warning(
                                "接口提示无法发布评论，正在等待直播间实际弹幕确认 | "
                                "account_id={} | classification=comment_delivery_uncertain",
                                short_id,
                            )

                        live_observed = live.get(message_number + 1) if live is not None else None
                        window_confirmed = live is not None and live.timeout_reported(message_number + 1)
                        recover = getattr(listener, 'wait_until_available', None)
                        if (live_observed is None and not window_confirmed and callable(recover)
                                and getattr(listener, 'recovering', False)):
                            recover(observation_timeout_seconds, should_cancel=should_cancel)
                            live_observed = live.get(message_number + 1) if live is not None else None
                        listener_failed = receipt_check_failed or (
                            not window_confirmed and _listener_unavailable(listener, send_started_at))
                        if listener_failed:
                            if live is not None:
                                live_observed = live.resolve(message_number + 1, None)
                        if live_observed is None and listener_failed:
                            counters["listenerUnavailable"] += 1
                            counters["failed" if requires_observation else "success"] += 1
                            record_failed_account(account, short_id, "listener_unavailable")
                            display('listener_unavailable', 'listener_unavailable')
                            logger.warning(
                                "消息展示无法确认 | account_id={} | classification=listener_unavailable",
                                short_id,
                            )
                            break

                        expected_usernames = {
                            str(account.get("username") or "").strip().lstrip("@")
                        } - {""}
                        expected_user_ids = _numeric_tiktok_uids(account["cookies"])
                        receipt_kwargs = {}
                        if should_cancel is not None:
                            receipt_kwargs['should_cancel'] = should_cancel
                        if message_count is not None:
                            if message_id:
                                receipt_kwargs['expected_message_ids'] = {message_id}
                        try:
                            observed = live_observed or listener.wait_for_comment(
                                message,
                                since=send_started_at,
                                expected_usernames=expected_usernames,
                                expected_user_ids=expected_user_ids,
                                timeout=(0 if _cancel_requested(should_cancel)
                                         else live.remaining(message_number + 1, observation_timeout_seconds)
                                         if live is not None else observation_timeout_seconds),
                                **receipt_kwargs,
                            )
                        except Exception:
                            # Every completed POST still needs its own terminal result.
                            receipt_check_failed = True
                            observed = None
                        if live is not None:
                            observed = live.resolve(message_number + 1, observed)
                        if observed is None and (receipt_check_failed or
                                not window_confirmed and _listener_unavailable(listener, send_started_at)):
                            counters['listenerUnavailable'] += 1
                            counters['failed' if requires_observation else 'success'] += 1
                            record_failed_account(account, short_id, 'listener_unavailable')
                            display('listener_unavailable', 'listener_unavailable')
                            break
                        if (observed is None and send_attempt < attempt_limit and not batch_terminal
                                and not metadata_save_failed
                                and not send_cancelled()):
                            display('not_observed', 'accepted_not_observed')
                            send_attempt += 1
                            logger.warning(
                                "监听期内未发现实际弹幕，准备仅重试消息接口 | "
                                "account_id={} | retry={}/{} | delay_seconds={} | "
                                "classification=message_observation_retrying",
                                short_id, send_attempt, attempt_limit, retry_delay,
                            )
                            if retry_delay > 0:
                                _pause(retry_delay, should_cancel)
                            if _cancel_requested(should_cancel):
                                # Finalize the completed request without another POST.
                                counters["acceptedNotObserved"] += 1
                                counters["failed" if requires_observation else "success"] += 1
                                record_failed_account(account, short_id, "accepted_not_observed")
                                break
                            continue

                        if observed is None:
                            counters["acceptedNotObserved"] += 1
                            counters["failed" if requires_observation else "success"] += 1
                            record_failed_account(account, short_id, "accepted_not_observed")
                            display('not_observed', 'accepted_not_observed')
                            if live is None or not live.timeout_reported(message_number + 1):
                                if _cancel_requested(should_cancel):
                                    logger.info('当前房间任务已停止，提前结束展示观察 | account_id={} '
                                        '| classification=observation_stopped', short_id)
                                else:
                                    logger.warning(
                                        "监听期内未发现实际弹幕 | account_id={} | "
                                        "classification=accepted_not_observed",
                                        short_id,
                                    )
                            break

                        counters["success"] += 1
                        counters["delivered"] += 1
                        display('observed', 'delivered')
                        observed_username = str(
                            getattr(observed, "unique_id", "") or ""
                        ).strip().lstrip("@")
                        if observed_username and observed_username != account.get("username"):
                            for saved_account in store["accounts"]:
                                if saved_account["id"] == account_id:
                                    saved_account["username"] = observed_username
                                    save_completed_account_state()
                                    break
                        break
            if missed_guard.stopped:
                # Keep the protection reason even after draining already-sent results.
                record_failed_account(account, short_id, 'consecutive_not_observed')
            silenced_count = len(silenced_accounts)
            publish_progress()
            if metadata_save_failed:
                logger.error(
                    '账号信息保存失败，已保留完成消息的结果 | account_id={} | classification=runtime_error',
                    short_id,
                )
                # A thrown runner is never collected as a mergeable private store.
                raise SafeOperationError('runtime_error') from None
            if stop_processing:
                break
    finally:
        if listener is not None:
            listener.close()
    failure_records = list(failed_accounts.values())
    report_root = store_path.parent if report_dir is None else Path(report_dir)
    _save_failed_accounts_report(report_root / "failed_accounts.json", failure_records)
    silenced_records = list(silenced_accounts.values())
    _save_silenced_accounts_report(
        report_root / "silenced_accounts.json", silenced_records
    )
    for record in failure_records:
        logger.warning(
            "失败账号 | account_id={} | username={} | classification={}",
            record["account_id"],
            record["username"],
            record["classification"],
        )
    processed = counters["success"] + counters["failed"] + counters["expired"] + counters["temporaryError"]
    if observation_enabled:
        logger.info(
            "本次处理完成 | processed={} | success={} | failed={} | expired={} | temporaryError={} "
            "| delivered={} | acceptedNotObserved={} | listenerUnavailable={}",
            processed,
            counters["success"],
            counters["failed"],
            counters["expired"],
            counters["temporaryError"],
            counters["delivered"],
            counters["acceptedNotObserved"],
            counters["listenerUnavailable"],
        )
    else:
        logger.info(
            "本次处理完成 | processed={} | success={} | failed={} | expired={} | temporaryError={}",
            processed,
            counters["success"],
            counters["failed"],
            counters["expired"],
            counters["temporaryError"],
        )
    return snapshot()

