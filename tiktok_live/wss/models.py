"""Shared session contract. Sensitive session material is never represented."""
from dataclasses import dataclass, field
from typing import Literal

Role = Literal['delivery_observer', 'account_heartbeat']


@dataclass(frozen=True)
class SessionContext:
    role: Role
    target_username: str
    room_id: str
    live_id: str
    user_agent: str = field(repr=False)
    referer: str = field(repr=False)
    cookie_header: str = field(repr=False)
    document_cookie: str = field(repr=False)
    ms_token: str = field(repr=False)
    device_id: str = field(repr=False)
    metrics: dict[str, object] = field(repr=False)
    local_storage: dict[str, str] = field(repr=False)
    session_storage: dict[str, str] = field(repr=False)
