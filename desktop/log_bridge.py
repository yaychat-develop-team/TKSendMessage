"""Loguru sink that sanitizes text before crossing the Qt signal boundary."""

import re
from datetime import datetime
from pathlib import Path
from threading import Lock

from PySide6.QtCore import QObject, Signal

# Reject the whole record: a Cookie header, mapping or PEM may span lines, so
# redacting only a token value could accidentally expose its continuation.
_SECRET_ASSIGNMENT = re.compile(
    r'''(?ix)
    (?:["']?(?:cookies?|ms[_-]?token|(?:[\w-]*csrf[\w-]*)|
    (?:ticket[_-]?guard[\w-]*)|(?:[\w-]*ticket[_-]guard[\w-]*)|
    private[_-]?key(?:[_-]?pem)?|ec[_-]?privatekey|encrypt[_-]?ticket|
    ts[_-]?sign|sessionid(?:_ss)?|sid_tt|uid_tt)["']?\s*[:=])
    |ticket[\s_-]*guard[\s\w-]*[:=]
    |(?:ec[\s_-]*)?private[\s_-]*key(?:[\s_-]*pem)?\s*[:=]
    |-----BEGIN\s+(?:[A-Z]+\s+)*PRIVATE\s+KEY-----
    '''
)


# Browser APIs represent credentials as name/value pairs rather than assigning
# the sensitive label directly. Search anywhere so log prefixes, nested lists,
# JSON, Python repr and Cookie(name=..., value=...) receive the same treatment.
_RECORD_NAME = re.compile(r'''(?ix)
    (?<![\w-])["']?name["']?\s*[:=]\s*["']([^"']+)["']
    ''')
_COOKIE_FIELD = re.compile(r'''(?ix)
    (?<![\w-])["']?(name|value|domain)["']?\s*[:=]
    ''')


def sanitize_log_text(value: object) -> str:
    """Produce UI-safe text; never send raw exception/credential records to Qt."""
    text = str(value)
    # Normalize escaped quote delimiters for detection only; keep readable text intact.
    check_text = re.sub(r"\\+([\"'])", r"\1", text)
    sensitive_name = any(_SECRET_ASSIGNMENT.search(match.group(1) + '=')
                         for match in _RECORD_NAME.finditer(check_text))
    cookie_fields = {match.group(1).lower() for match in _COOKIE_FIELD.finditer(check_text)}
    if (_SECRET_ASSIGNMENT.search(check_text) or sensitive_name
            or {'name', 'value', 'domain'} <= cookie_fields):
        return '[redacted sensitive log]'
    return text.rstrip('\r\n')


def daily_log_path(directory, when=None):
    return Path(directory).resolve() / f'{(when or datetime.now()):%Y-%m-%d}.log'


def _context_prefix(record):
    """Use only task identity metadata; never serialize arbitrary log extras."""
    extra = record.get('extra') or {}
    if not any(key in extra for key in ('target_room', 'sender_account', 'sender_id')):
        return ''
    def clean(value):
        return re.sub(r'[^A-Za-z0-9._-]', '', str(value or '').lstrip('@'))[:64]
    room = clean(extra.get('target_room')) or '-'
    account_id = clean(extra.get('sender_id')) or '-'
    account = clean(extra.get('sender_account')) or account_id
    return f'room={room} | account_username={account} | account_id={account_id} | '


class DailyLogSink:
    """Append each record to its local calendar day, including across restarts."""

    def __init__(self, directory):
        self.directory = Path(directory)
        self._lock = Lock()

    def __call__(self, message):
        record = message.record
        path = daily_log_path(self.directory, record['time'])
        text = sanitize_log_text(_context_prefix(record) + record['message'])
        line = f"{record['time']:%Y-%m-%d %H:%M:%S} | {record['level'].name} | {text}\n"
        with self._lock:
            path.parent.mkdir(parents=True, exist_ok=True)
            with path.open('a', encoding='utf-8') as stream:
                stream.write(line)


class QtLogSink(QObject):
    message = Signal(str)
    record_message = Signal(str, str)

    def __call__(self, record: object) -> None:
        prefix = ''
        level = "INFO"
        try:
            level = str(record.record["level"].name).upper()
            prefix = _context_prefix(record.record)
        except (AttributeError, KeyError, TypeError):
            pass
        text = sanitize_log_text(prefix + str(record))
        self.message.emit(text)
        self.record_message.emit(text, level)
