"""Validated desktop preferences persisted beside other runtime data."""

import json
import math
import os
import tempfile
from dataclasses import asdict, dataclass, field
from pathlib import Path

import settings as script_settings
from tiktok_live.wss.proxy import normalize_observer_proxy


@dataclass(frozen=True)
class DesktopSettings:
    room_execution_mode: str = 'serial'
    login_timeout_seconds: int = script_settings.LOGIN_TIMEOUT_SECONDS
    account_interval_seconds: float = 0.0
    observation_enabled: bool = script_settings.MESSAGE_OBSERVATION_ENABLED
    show_live_comments: bool = script_settings.SHOW_LIVE_COMMENTS_IN_CONSOLE
    observation_timeout_seconds: float = script_settings.MESSAGE_OBSERVATION_TIMEOUT_SECONDS
    message_attempts: int = script_settings.MESSAGE_NOT_OBSERVED_RETRIES
    message_retry_delay_seconds: float = script_settings.MESSAGE_NOT_OBSERVED_RETRY_DELAY_SECONDS
    listener_connect_timeout_seconds: float = script_settings.MESSAGE_OBSERVER_CONNECT_TIMEOUT_SECONDS
    listener_connect_retries: int = script_settings.MESSAGE_OBSERVER_CONNECT_RETRIES
    listener_retry_delay_seconds: float = script_settings.MESSAGE_OBSERVER_RETRY_DELAY_SECONDS
    heartbeat_interval_seconds: float = 10.0
    room_id_cookie: str = script_settings.ROOM_ID_COOKIE
    observer_proxy: str = field(default='', repr=False)

    def to_dict(self) -> dict:
        return asdict(self)

    def runner_kwargs(self) -> dict:
        values = _coerce(self.to_dict())
        return {
            'interval_seconds': values.account_interval_seconds,
            'observation_enabled': values.observation_enabled,
            'observation_timeout_seconds': values.observation_timeout_seconds,
            'observation_retries': values.message_attempts,
            'observation_retry_delay_seconds': values.message_retry_delay_seconds,
            'observer_connect_timeout_seconds': values.listener_connect_timeout_seconds,
            'observer_connect_retries': values.listener_connect_retries,
            'observer_retry_delay_seconds': values.listener_retry_delay_seconds,
            'heartbeat_interval_seconds': values.heartbeat_interval_seconds,
            'show_live_comments_in_console': values.show_live_comments,
            'login_timeout_seconds': values.login_timeout_seconds,
            'room_id_cookie': values.room_id_cookie,
            'observer_proxy': normalize_observer_proxy(values.observer_proxy),
        }


def _coerce(data: object) -> DesktopSettings:
    if not isinstance(data, dict):
        return DesktopSettings()
    defaults = DesktopSettings()

    def finite(value) -> bool:
        try:
            return type(value) in (int, float) and math.isfinite(value)
        except (ValueError, OverflowError):
            return False

    def integer(name: str, low: int, high: int) -> int:
        value = data.get(name, getattr(defaults, name))
        if not finite(value):
            return getattr(defaults, name)
        return min(high, max(low, int(value)))

    def number(name: str, low: float, high: float) -> float:
        value = data.get(name, getattr(defaults, name))
        if not finite(value):
            return getattr(defaults, name)
        return min(high, max(low, float(value)))

    def boolean(name: str) -> bool:
        value = data.get(name, getattr(defaults, name))
        return value if type(value) is bool else getattr(defaults, name)

    cookie = data.get('room_id_cookie', defaults.room_id_cookie)
    return DesktopSettings(
        room_execution_mode=('parallel' if data.get('room_execution_mode') == 'parallel' else 'serial'),
        login_timeout_seconds=integer('login_timeout_seconds', 10, 3600),
        account_interval_seconds=number('account_interval_seconds', 0, 3600),
        observation_enabled=boolean('observation_enabled'),
        show_live_comments=boolean('show_live_comments'),
        observation_timeout_seconds=number('observation_timeout_seconds', 1, 3600),
        message_attempts=integer('message_attempts', 1, 20),
        message_retry_delay_seconds=number('message_retry_delay_seconds', 0, 3600),
        listener_connect_timeout_seconds=number('listener_connect_timeout_seconds', 1, 3600),
        listener_connect_retries=integer('listener_connect_retries', 0, 20),
        listener_retry_delay_seconds=number('listener_retry_delay_seconds', 0, 3600),
        heartbeat_interval_seconds=number('heartbeat_interval_seconds',1,60),
        room_id_cookie=str(cookie if isinstance(cookie, str) else '')[:20000],
        observer_proxy=(data.get('observer_proxy', '')
                        if isinstance(data.get('observer_proxy', ''), str) else 'INVALID'),
    )


def load_desktop_settings(path: Path) -> DesktopSettings:
    path = Path(path)
    if not path.exists():
        return DesktopSettings()
    try:
        return _coerce(json.loads(path.read_text(encoding='utf-8-sig')))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return DesktopSettings()


def save_desktop_settings(path: Path, values: DesktopSettings) -> None:
    values = _coerce(values.to_dict())
    normalize_observer_proxy(values.observer_proxy)
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(
            mode='w', encoding='utf-8', dir=path.parent,
            prefix='desktop-settings-', suffix='.tmp', delete=False,
        ) as handle:
            temporary = Path(handle.name)
            json.dump(values.to_dict(), handle, ensure_ascii=False, indent=2)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)
