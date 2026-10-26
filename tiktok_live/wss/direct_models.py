"""Immutable direct-transport contracts; no old SDK state assumptions."""
from dataclasses import dataclass, field
from typing import Literal
import math


def validate_heartbeat_interval(value):
    try:
        if type(value) not in (int,float) or not math.isfinite(value) or not 1 <= value <= 60:
            raise ValueError('Heartbeat interval must be between 1 and 60 seconds')
        return float(value)
    except (TypeError,OverflowError):
        raise ValueError('Invalid heartbeat interval') from None


def heartbeat_warmup_seconds(interval):
    return 3 * validate_heartbeat_interval(interval)

@dataclass(frozen=True)
class DirectSession:
    role: Literal['delivery_observer', 'account_heartbeat']
    target_username: str
    room_id: str
    user_agent: str = field(repr=False)
    cookie_header: str = field(repr=False)
    proxy_url: str | None = field(default=None, repr=False)
    heartbeat_interval_seconds: float = 10.0
    initial_fetch: object | None = field(default=None, repr=False)

    def __post_init__(self):
        object.__setattr__(self,'heartbeat_interval_seconds',validate_heartbeat_interval(self.heartbeat_interval_seconds))

@dataclass(frozen=True)
class ChatEvent:
    message_id: str
    user_id: str
    unique_id: str
    nickname: str
    text: str
    room_id: str = ''

@dataclass(frozen=True)
class DecodedFrame:
    payload_type: str
    log_id: int
    need_ack: bool
    internal_ext: str = field(repr=False)
    events: tuple[ChatEvent, ...]
    room_id: str | None = None

@dataclass(frozen=True)
class HandshakeResult:
    status: int
    headers: dict[str, str] = field(repr=False)
    subprotocol: str | None = None
