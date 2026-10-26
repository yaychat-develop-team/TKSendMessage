"""Explicit, account-isolated state for browser-free webcast request signing.

This module does not derive a live SDK environment. Callers must supply a
validated state; absent or unsupported material is an error, not a fallback.
"""

from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Any, Mapping
from urllib.parse import parse_qsl, urlsplit

from .algorithms.x_dynosaur import EX_VERSION
from .config import BrowserProfile
from .signing import sign_url


@dataclass(frozen=True)
class SdkState:
    dynosaur_context: Mapping[str, Any] = field(repr=False)
    gnarly_context: Mapping[str, Any] = field(repr=False)

    def __post_init__(self) -> None:
        dyn = dict(self.dynosaur_context)
        gnarly = dict(self.gnarly_context)
        required_dyn = {
            "field32_seed", "field37", "field40", "field44", "field47",
            "field50", "field51", "field52", "field53", "field54", "field56",
        }
        required_gnarly = {
            "field2", "field8", "perf_data", "field12", "field13", "order",
        }
        if (not required_dyn.issubset(dyn) or not required_gnarly.issubset(gnarly)
                or dyn["field50"] != EX_VERSION
                or not isinstance(dyn["field51"], str)
                or len(dyn["field51"]) != 32
                or not isinstance(dyn["field53"], str)
                or not dyn["field53"].startswith("www.tiktok.com/@")
                or dyn["field44"] != gnarly["perf_data"]
                or dyn["field37"] != gnarly["field13"]
                or dyn["field47"] != gnarly["field12"]):
            raise ValueError("signing_state_unsupported")
        object.__setattr__(self, "dynosaur_context", MappingProxyType(dyn))
        object.__setattr__(self, "gnarly_context", MappingProxyType(gnarly))


class SigningSession:
    def __init__(
        self, account_id: str, room_id: str, page_url: str,
        profile: BrowserProfile, sdk_state: SdkState | None,
    ) -> None:
        page = urlsplit(page_url)
        if (not account_id or not room_id or page.scheme != "https"
                or page.hostname != "www.tiktok.com"
                or not page.path.startswith("/@") or not page.path.endswith("/live")
                or page.query or page.fragment):
            raise ValueError("signing_identity_mismatch")
        if sdk_state is None:
            raise ValueError("signing_state_unsupported")
        self.account_id = account_id
        self.room_id = room_id
        self.page_url = page_url
        self.profile = profile
        self._sdk_state = sdk_state
        self._sequence: list[str] = []

    def sign(
        self, url: str, *, account_id: str, room_id: str,
        query_token: str, body: bytes | None = None, operation: str,
    ) -> str:
        if account_id != self.account_id or room_id != self.room_id:
            raise ValueError("signing_identity_mismatch")
        parts = urlsplit(url)
        path_for_operation = {
            "enter": "/webcast/room/enter/",
            "initial_fetch": "/webcast/im/fetch/",
        }
        if (operation not in path_for_operation or parts.scheme != "https"
                or parts.hostname not in {"webcast.tiktok.com", "webcast.us.tiktok.com"}
                or parts.path != path_for_operation[operation]
                or parts.username or parts.password or parts.port not in (None, 443)):
            raise ValueError("signing_request_unsupported")
        if (operation == "enter" and self._sequence
                or operation == "initial_fetch" and self._sequence != ["enter"]):
            raise ValueError("signing_request_order")
        if operation == "initial_fetch":
            raise ValueError("signing_state_unsupported")
        if body is not None and not isinstance(body, bytes):
            raise ValueError("signing_body_type")
        query_room = dict(parse_qsl(parts.query, keep_blank_values=True)).get("room_id")
        if query_room and query_room != self.room_id:
            raise ValueError("signing_identity_mismatch")
        dyn = dict(self._sdk_state.dynosaur_context)
        gnarly = dict(self._sdk_state.gnarly_context)
        signed = sign_url(
            url, query_token, body, user_agent=self.profile.user_agent,
            dynosaur_context=dyn, gnarly_context=gnarly,
        )
        self._sequence.append(operation)
        return signed
