"""Bound room concurrency while isolating every account runner and its state."""

import hashlib
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from pathlib import Path
from typing import Callable
from uuid import uuid4

from loguru import logger

from .errors import SafeOperationError, safe_failure_reasons
from .parallel_state import IsolatedAccountRuns
from .runner import run_saved_accounts
from .request_fingerprint import request_flow


COUNT_KEYS = ('success', 'failed', 'expired', 'temporaryError', 'delivered',
              'acceptedNotObserved', 'listenerUnavailable', 'silenced', 'sent')
_STATUSES = {'queued', 'running', 'completed', 'failed', 'cancelled'}


def _counts(value: dict) -> dict:
    result = {key: max(0, value.get(key, 0)) if type(value.get(key, 0)) is int else 0
              for key in COUNT_KEYS}
    result['processed'] = sum(result[key] for key in
                              ('success', 'failed', 'expired', 'temporaryError'))
    result['cancelled'] = value.get('cancelled') is True
    return result


def _safe_name(value) -> str:
    return re.sub(r'[^A-Za-z0-9._-]', '', str(value or '').lstrip('@'))[:64]


def _safe_id(value) -> str:
    value = str(value or '')
    return (value[:12] if re.fullmatch(r'[a-f0-9]{12,64}', value)
            else hashlib.sha256(value.encode('utf-8')).hexdigest()[:12])


def _error(exc: Exception) -> dict:
    return {'type': _safe_name(type(exc).__name__),
            'classification': (exc.classification if isinstance(exc, SafeOperationError)
                               else 'runtime_error')}


def safe_parallel_result(value: dict | None = None) -> dict:
    """Whitelist both aggregate counters and room metadata at the Qt boundary."""
    value = value or {}
    result = _counts(value)
    if value.get('status') in _STATUSES:
        result['status'] = value['status']
    failed_tasks = value.get('failed_tasks', 0)
    result['failed_tasks'] = max(0, failed_tasks) if type(failed_tasks) is int else 0
    result['rooms'] = []
    for room in value.get('rooms', []) if isinstance(value.get('rooms', []), list) else []:
        if not isinstance(room, dict):
            continue
        clean = {**_counts(room), 'target': _safe_name(room.get('target')),
                 'status': room.get('status') if room.get('status') in _STATUSES else 'queued',
                 'accounts': []}
        accounts = room.get('accounts', [])
        for account in accounts if isinstance(accounts, list) else []:
            if not isinstance(account, dict):
                continue
            item = {**_counts(account), 'id': _safe_id(account.get('id')),
                    'username': _safe_name(account.get('username')),
                    'status': account.get('status') if account.get('status') in _STATUSES else 'queued'}
            item['failure_reasons'] = safe_failure_reasons(account.get('failure_reasons'))
            error = account.get('error')
            if isinstance(error, dict):
                item['error'] = {'type': _safe_name(error.get('type')),
                    'classification': SafeOperationError(error.get('classification')).classification}
            clean['accounts'].append(item)
        result['rooms'].append(clean)
    return result


def _aggregate(items: list[dict]) -> dict:
    result = {key: sum(item[key] for item in items) for key in COUNT_KEYS}
    result['cancelled'] = any(item['cancelled'] for item in items)
    return _counts(result)


def _status(items: list[dict]) -> str:
    statuses = {item['status'] for item in items}
    if 'running' in statuses:
        return 'running'
    if 'queued' in statuses:
        return 'queued'
    if 'failed' in statuses:
        return 'failed'
    if 'cancelled' in statuses:
        return 'cancelled'
    return 'completed'


def run_parallel_targets(targets: tuple[str, ...], message: str, *,
        store_path: Path, temporary_root: Path, report_dir: Path,
        selected_account_ids: tuple[str, ...] | None, message_count: int,
        message_interval_seconds: float, should_cancel: Callable[[], bool],
        on_progress: Callable[[dict], None] | None, runner_kwargs: dict,
        room_execution_mode: str = 'serial') -> dict:
    """Run independent serial account lanes or at most three parallel rooms.

    Message counts are cumulative per task. ``failed_tasks`` counts terminal
    failed account tasks, including failures before any message was attempted.
    A thrown runner's last counts survive, but its account file is not merged.
    """
    lock = threading.RLock()
    abort = threading.Event()
    callback_error = None
    room_stops = [threading.Event() for _ in targets]
    report_root = Path(report_dir) / ('run-' + uuid4().hex)

    def cancelled():
        return abort.is_set() or should_cancel()

    with IsolatedAccountRuns(store_path, temporary_root, selected_account_ids) as runs:
        if not runs.accounts:
            raise ValueError('No saved accounts')
        rooms = [{'target': target, 'accounts': [
            {**_counts({}), 'id': _safe_id(account['id']),
             'username': _safe_name(account.get('username')), 'status': 'queued'}
            for account in runs.accounts]} for target in targets]

        def snapshot():
            room_snapshots = []
            for room in rooms:
                accounts = room['accounts']
                room_snapshots.append({**_aggregate(accounts), 'target': room['target'],
                    'status': _status(accounts), 'accounts': deepcopy(accounts)})
            result = {**_aggregate(room_snapshots), 'rooms': room_snapshots,
                'status': _status(room_snapshots),
                'failed_tasks': sum(account['status'] == 'failed'
                    for room in rooms for account in room['accounts'])}
            result['cancelled'] = result['cancelled'] or cancelled()
            if callback_error is not None:
                result['status'] = 'failed'
            return result

        def publish():
            # Called only while holding lock: building AND emitting are ordered.
            nonlocal callback_error
            result = snapshot()
            if on_progress is not None and callback_error is None:
                try:
                    on_progress(result)
                except Exception as exc:
                    callback_error = _error(exc)
                    abort.set()
                    result = snapshot()
            return result

        def update(room_index, account_index, *, counts=None, status=None, error=None):
            with lock:
                item = rooms[room_index]['accounts'][account_index]
                incoming_reasons = safe_failure_reasons(counts.get('failure_reasons')) if counts else []
                if ('room_comment_restricted' in incoming_reasons or
                        error is not None and error.get('classification') == 'room_comment_restricted'):
                    if not room_stops[room_index].is_set():
                        room_stops[room_index].set()
                        logger.warning('直播间设置禁止评论，停止本房间后续发送并清理连接 | room={} '
                            '| classification=room_comment_restricted', rooms[room_index]['target'])
                room_restricted = room_stops[room_index].is_set() and not cancelled()
                if counts is not None:
                    item.update(_counts(counts))
                    if 'failure_reasons' in counts:
                        item['failure_reasons'] = safe_failure_reasons(counts['failure_reasons'])
                if room_restricted:
                    item['cancelled'] = False
                    item['failure_reasons'] = list(dict.fromkeys(
                        item.get('failure_reasons', []) + ['room_comment_restricted']))
                    if status in ('cancelled', 'completed'):
                        status = 'failed'
                if status is not None:
                    item['status'] = status
                    if status == 'cancelled':
                        item['cancelled'] = True
                if error is not None:
                    item['error'] = error
                publish()

        def fail_pending(room_index, exc):
            with lock:
                for item in rooms[room_index]['accounts']:
                    if item['status'] == 'queued':
                        item.update(status='failed', error=_error(exc))
                publish()

        def account_task(room_index, account_index, *, start_at=None):
            room = rooms[room_index]
            account = runs.accounts[account_index]
            safe = room['accounts'][account_index]
            def task_cancelled():
                return cancelled() or room_stops[room_index].is_set()
            def stop_before_start():
                if cancelled():
                    update(room_index, account_index, status='cancelled')
                else:
                    update(room_index, account_index, status='failed',
                           error=_error(SafeOperationError('room_comment_restricted')))
            with request_flow(), logger.contextualize(target_room=room['target'],
                    sender_account=safe['username'], sender_id=safe['id']):
                try:
                    if start_at is None:
                        start_at = rooms[room_index]['started_at'] + account_index * max(
                            0.0, float(runner_kwargs.get('interval_seconds', 0)))
                    while time.monotonic() < start_at:
                        if task_cancelled():
                            stop_before_start()
                            return
                        time.sleep(min(.05, max(0.0, start_at-time.monotonic())))
                    if task_cancelled():
                        stop_before_start()
                        return
                    update(room_index, account_index, status='running')
                    task_store = runs.create_store(room['target'], account['id'])
                    task_report = report_root / str(room_index) / str(account_index)
                    kwargs = dict(runner_kwargs)
                    kwargs.update(store_path=task_store, add_new_account=False,
                        interval_seconds=0,
                        selected_account_ids=(account['id'],), report_dir=task_report,
                        should_cancel=task_cancelled,
                        on_progress=lambda counts: update(room_index, account_index, counts=counts),
                        message_count=message_count,
                        message_interval_seconds=message_interval_seconds)
                    if task_cancelled():
                        stop_before_start()
                        return
                    counts = run_saved_accounts(room['target'], message, **kwargs)
                    clean = _counts(counts)
                    clean['failure_reasons'] = safe_failure_reasons(counts.get('failure_reasons'))
                    update(room_index, account_index, counts=clean)
                    # Only normal returns (including cancellation) are eligible.
                    runs.collect_outcome(account['id'], task_store, counts)
                    failed = (any(clean[key] for key in ('failed', 'expired', 'temporaryError'))
                              or 'consecutive_not_observed' in clean['failure_reasons'])
                    status = ('failed' if failed else 'cancelled' if clean['cancelled']
                              or cancelled() else 'completed')
                    update(room_index, account_index, counts=clean, status=status)
                except Exception as exc:
                    error = _error(exc)
                    logger.error('账号任务失败 | error_type={} | classification={}',
                                 error['type'], error['classification'])
                    update(room_index, account_index, status='failed', error=error)

        def run_room(room_index):
            futures = []
            rooms[room_index]['started_at'] = time.monotonic()
            try:
                # This scope retains the room slot until every account drains.
                with ThreadPoolExecutor(max_workers=len(runs.accounts)) as executor:
                    for index in range(len(runs.accounts)):
                        if cancelled():
                            update(room_index, index, status='cancelled')
                            continue
                        try:
                            futures.append(executor.submit(account_task, room_index, index))
                        except Exception as exc:
                            abort.set()
                            update(room_index, index, status='failed', error=_error(exc))
                    for future in futures:
                        future.result()
            except Exception as exc:
                abort.set()
                fail_pending(room_index, exc)

        def room_task(room_index):
            if cancelled():
                for account_index in range(len(runs.accounts)):
                    update(room_index, account_index, status='cancelled')
                return
            with logger.contextualize(target_room=rooms[room_index]['target'],
                                      sender_account='', sender_id=''):
                run_room(room_index)

        def serial_account(account_index, first_start):
            # One worker owns this account's whole ordered room list. A runner
            # returns only after its listener and heartbeat have been closed.
            for room_index in range(len(rooms)):
                if cancelled():
                    update(room_index, account_index, status='cancelled')
                    continue
                account_task(room_index, account_index,
                    start_at=first_start if room_index == 0 else time.monotonic())

        with lock:
            publish()
        try:
            if room_execution_mode == 'parallel':
                futures = []
                try:
                    with ThreadPoolExecutor(max_workers=3) as executor:
                        for index in range(len(rooms)):
                            try:
                                futures.append(executor.submit(room_task, index))
                            except Exception as exc:
                                abort.set()
                                fail_pending(index, exc)
                        for future in futures:
                            future.result()
                except Exception as exc:
                    abort.set()
                    for index in range(len(rooms)):
                        fail_pending(index, exc)
            else:
                first_start = time.monotonic()
                futures = []
                try:
                    with ThreadPoolExecutor(max_workers=len(runs.accounts)) as executor:
                        for account_index in range(len(runs.accounts)):
                            try:
                                futures.append(executor.submit(serial_account, account_index,
                                    first_start + account_index * max(0.0,
                                        float(runner_kwargs.get('interval_seconds', 0)))))
                            except Exception as exc:
                                abort.set()
                                for room_index in range(len(rooms)):
                                    update(room_index, account_index, status='failed', error=_error(exc))
                        for future in futures:
                            future.result()
                except Exception as exc:
                    abort.set()
                    for room_index in range(len(rooms)):
                        fail_pending(room_index, exc)
        finally:
            # Executor exits have joined all account threads before this point.
            runs.commit()
        with lock:
            return publish()
