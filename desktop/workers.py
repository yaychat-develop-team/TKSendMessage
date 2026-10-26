"""Qt workers around the existing business layer, with safe signal payloads."""

import hashlib
import re
import threading
from pathlib import Path

from PySide6.QtCore import QObject, QThread, Qt, Signal, Slot
from loguru import logger

from tiktok_live.account_store import load_account_store
from tiktok_live.browser_auth import login_new_account
from tiktok_live.errors import BrowserError, SafeOperationError
from tiktok_live.parallel_runner import run_parallel_targets, safe_parallel_result
from tiktok_live.targets import parse_targets
from .settings_store import DesktopSettings


def _safe_error(exc: Exception) -> str:
    return exc.classification if isinstance(exc, SafeOperationError) else 'runtime_error'


def _account_summary(account: dict, duplicate: bool) -> dict:
    raw_id = str(account.get('id') or '')
    short_id = (raw_id[:12] if re.fullmatch(r'[a-f0-9]{64}', raw_id)
                else hashlib.sha256(raw_id.encode('utf-8')).hexdigest()[:12])
    return {'id': short_id,
            'username': re.sub(r'[^A-Za-z0-9._-]', '',
                              str(account.get('username') or '').lstrip('@'))[:64],
            'status': 'saved', 'duplicate': duplicate}


class LoginWorker(QObject):
    browser_ready = Signal(int)
    browser_closing = Signal()
    account_saved = Signal(dict)
    failed = Signal(str)
    finished = Signal()

    def __init__(self, store_path: Path, timeout_seconds: int = 300,
                 cancel_event: threading.Event | None = None, *,
                 profile_root: Path | None = None):
        super().__init__()
        self.store_path = Path(store_path)
        self.timeout_seconds = timeout_seconds
        self.cancel_event = cancel_event if cancel_event is not None else threading.Event()
        self.profile_root = profile_root
        self.browser_cleanup_started = threading.Event()

    def _browser_ready(self, process_id: int) -> None:
        self.browser_ready.emit(process_id)
        if self.cancel_event.is_set():
            raise BrowserError('login_cancelled')

    def _browser_closing(self) -> None:
        # Set before quit can remove the HWND. A queued Qt signal alone can lag
        # behind a UI timer tick, so consumers also read this thread-safe flag.
        self.browser_cleanup_started.set()
        self.browser_closing.emit()

    @Slot()
    def run(self) -> None:
        try:
            if self.cancel_event.is_set():
                self.failed.emit('login_cancelled')
                return
            existing_ids = {account['id'] for account in
                            load_account_store(self.store_path)['accounts']}
            kwargs = {'on_browser_ready': self._browser_ready,
                      'on_browser_closing': self._browser_closing,
                      'cancel_event': self.cancel_event}
            if self.profile_root is not None:
                kwargs['profile_root'] = self.profile_root
            account = login_new_account(self.store_path,
                                        timeout_seconds=self.timeout_seconds, **kwargs)
            self.account_saved.emit(_account_summary(account, account['id'] in existing_ids))
        except Exception as exc:
            classification = _safe_error(exc)
            logger.error('账号登录失败，未保存本次登录 | error_type={} | classification={}',
                         type(exc).__name__, classification)
            self.failed.emit(classification)
        finally:
            self.finished.emit()


class SendWorker(QObject):
    progress = Signal(dict)
    failed = Signal(str)
    finished = Signal(dict)

    def __init__(self, username: str, message: str, paths,
                 cancel_event: threading.Event | None = None, *,
                 settings: DesktopSettings | None = None,
                 selected_account_ids: tuple[str, ...] | None = None,
                 message_count: int = 1,
                 message_interval_seconds: float = 1.0):
        super().__init__()
        self.username = username.strip()
        self.message = message.strip()
        self.paths = paths
        self.cancel_event = cancel_event if cancel_event is not None else threading.Event()
        self.settings = settings or DesktopSettings()
        self.selected_account_ids = selected_account_ids
        self.message_count = max(1, int(message_count))
        self.message_interval_seconds = max(0.1, min(3600.0, float(message_interval_seconds)))

    def _progress(self, result: dict) -> None:
        self._last_counts = safe_parallel_result(result)
        self.progress.emit({'phase': 'sending', **self._last_counts})

    @Slot()
    def run(self) -> None:
        self._last_counts = safe_parallel_result()
        result = {**self._last_counts, 'status': 'failed'}
        try:
            if self.cancel_event.is_set():
                result.update(status='cancelled', cancelled=True)
                return
            if not self.username or not self.message:
                self.failed.emit('invalid_input')
                return
            targets = parse_targets(self.username)
            if not load_account_store(self.paths.account_store)['accounts']:
                self.progress.emit({'phase': 'login_required'})
                result['status'] = 'login_required'
                return
            if self.selected_account_ids is not None and not self.selected_account_ids:
                self.failed.emit('no_accounts_selected')
                return
            self._progress({})
            counts = run_parallel_targets(targets, self.message,
                room_execution_mode=self.settings.room_execution_mode,
                store_path=self.paths.account_store,
                temporary_root=self.paths.runtime_dir / 'temporary',
                selected_account_ids=self.selected_account_ids,
                report_dir=self.paths.report_dir,
                should_cancel=self.cancel_event.is_set, on_progress=self._progress,
                message_count=self.message_count,
                message_interval_seconds=self.message_interval_seconds,
                runner_kwargs=self.settings.runner_kwargs())
            result = safe_parallel_result(counts)
            result['cancelled'] = result['cancelled'] or self.cancel_event.is_set()
            if result.get('status') == 'failed' or result['failed_tasks']:
                result['status'] = 'failed'
                self.failed.emit('runtime_error')
            else:
                result['status'] = 'cancelled' if result['cancelled'] else 'completed'
        except Exception as exc:
            result = {**self._last_counts, 'status': 'failed'}
            result['cancelled'] = result['cancelled'] or self.cancel_event.is_set()
            self.failed.emit(_safe_error(exc))
        finally:
            self.finished.emit(result)


def create_worker_thread(worker: QObject, parent: QObject | None = None) -> QThread:
    """Wire a worker to an owned thread; the caller retains and starts the thread.

    Call from the worker's original thread before starting. Direct quit permits
    a bounded shutdown wait while the GUI is no longer processing queued slots.
    """
    thread = QThread(parent)
    worker.moveToThread(thread)
    thread.started.connect(worker.run)
    worker.finished.connect(thread.quit, Qt.ConnectionType.DirectConnection)
    thread.finished.connect(worker.deleteLater)
    return thread
