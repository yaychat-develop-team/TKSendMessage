"""Explicit, account-isolated state for browser-free webcast request signing.

This module does not derive a live SDK environment. Callers must supply a
validated state; absent or unsupported material is an error, not a fallback.
"""

import json
import secrets
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Any, Mapping
from urllib.parse import parse_qsl, urlsplit

from .algorithms import x_dynosaur, x_gnarly
from .algorithms.x_dynosaur import EX_VERSION, _DYNOSAUR_SESSION_CONTEXT
from .algorithms.x_gnarly import (
    SCM_VERSION, SDK_VERSION, _GNARLY_SESSION_CONTEXT,
)
from .config import BrowserProfile
from .reference_signing import sign_url


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
                or dyn["field47"] != gnarly["field12"]
                or int(gnarly["field13"]) not in {
                    int(dyn["field37"]),
                    (int(dyn["field37"]) + int(dyn["field47"]))
                    & 0xFFFFFFFF,
                }
                or gnarly.get("sdk_version") != SDK_VERSION
                or gnarly.get("scm_version") != SCM_VERSION
                or sorted(gnarly["order"]) != list(range(17))):
            raise ValueError("signing_state_unsupported")
        object.__setattr__(self, "dynosaur_context", MappingProxyType(dyn))
        object.__setattr__(self, "gnarly_context", MappingProxyType(gnarly))


def create_sdk_state(page_url: str, environment_plaintext: str) -> SdkState:
    """Create one fresh page lifecycle using the same canvas marker as /web/report."""
    page = urlsplit(page_url)
    if (page.scheme != "https" or page.hostname != "www.tiktok.com"
            or not page.path.startswith("/@") or not page.path.endswith("/live")
            or page.query or page.fragment):
        raise ValueError("signing_identity_mismatch")
    try:
        environment = json.loads(environment_plaintext)
        canvas = int(environment["wID"]["canvas"])
    except (KeyError, TypeError, ValueError, json.JSONDecodeError):
        raise ValueError("signing_state_unsupported") from None
    if not 0 <= canvas <= 0xFFFFFFFF:
        raise ValueError("signing_state_unsupported")

    # Start from the accepted reference request, then advance only the four
    # lifecycle counters across protected requests.
    field37 = 12
    shared_step = 72
    field8 = x_gnarly._gnarly_field8()
    field52 = x_gnarly._gnarly_field8()
    current_gnarly = x_gnarly._GNARLY_WEBCAST_ROOM_ENTER_CONTEXT
    field_order = list(current_gnarly["order"])
    secrets.SystemRandom().shuffle(field_order)
    dyn = {
        **_DYNOSAUR_SESSION_CONTEXT,
        "field32_seed": secrets.randbelow(256),
        "field37": field37,
        "field40": secrets.randbits(32),
        "field44": canvas,
        "field47": shared_step,
        "field50": EX_VERSION,
        "field51": "3142b7a7f3ac9b8150f994835ba3173a",
        "field52": field52,
        "field53": f"www.tiktok.com{page.path}",
        "field54": 14,
        "field56": bytes.fromhex("e05ecf09"),
    }
    gnarly = {
        **_GNARLY_SESSION_CONTEXT,
        "field2": 14,
        "field8": field8,
        "perf_data": canvas,
        "sdk_version": SDK_VERSION,
        "scm_version": SCM_VERSION,
        "field12": shared_step,
        "field13": field37,
        "order": field_order,
    }
    return SdkState(dyn, gnarly)


class SigningSession:
    def __init__(
        self, account_id: str, room_id: str | None, page_url: str,
        profile: BrowserProfile, sdk_state: SdkState | None,
    ) -> None:
        page = urlsplit(page_url)
        if (not account_id or (room_id is not None and not str(room_id).isdigit())
                or page.scheme != "https"
                or page.hostname != "www.tiktok.com"
                or not page.path.startswith("/@") or not page.path.endswith("/live")
                or page.query or page.fragment):
            raise ValueError("signing_identity_mismatch")
        if sdk_state is None:
            raise ValueError("signing_state_unsupported")
        self.account_id = account_id
        self.room_id = str(room_id) if room_id is not None else None
        self.page_url = page_url
        self.profile = profile
        self._sdk_state = sdk_state
        self._sequence: list[str] = []

    def bind_room(self, room_id: str) -> None:
        room_id = str(room_id or "")
        if not room_id.isdigit():
            raise ValueError("signing_identity_mismatch")
        if self.room_id is not None and self.room_id != room_id:
            raise ValueError("signing_identity_mismatch")
        self.room_id = room_id

    def checkpoint(self) -> tuple[SdkState, tuple[str, ...]]:
        """Capture immutable signing state before a retryable request."""
        return self._sdk_state, tuple(self._sequence)

    def restore(self, checkpoint: tuple[SdkState, tuple[str, ...]]) -> None:
        """Roll back a request that did not receive an accepted response."""
        if (not isinstance(checkpoint, tuple) or len(checkpoint) != 2
                or not isinstance(checkpoint[0], SdkState)
                or not isinstance(checkpoint[1], tuple)
                or not all(isinstance(item, str) for item in checkpoint[1])):
            raise ValueError("invalid_signing_checkpoint")
        self._sdk_state, sequence = checkpoint
        self._sequence = list(sequence)

    def sign(
        self, url: str, *, account_id: str, room_id: str | None,
        query_token: str, body: bytes | None = None, operation: str,
    ) -> str:
        if account_id != self.account_id:
            raise ValueError("signing_identity_mismatch")
        if operation == "report":
            if room_id is not None:
                raise ValueError("signing_identity_mismatch")
        elif self.room_id is None or room_id != self.room_id:
            raise ValueError("signing_identity_mismatch")
        parts = urlsplit(url)
        path_for_operation = {
            "report": "/web/report",
            "enter": "/webcast/room/enter/",
            "initial_fetch": "/webcast/im/fetch/",
            "epiphron": "/webcast/epiphron/feature/upload/",
            "check_alive": "/webcast/room/check_alive/",
            "event_feed": "/tiktok/event/feed/v1",
            "chat": "/webcast/room/chat/",
        }
        if operation == "report":
            supported_hosts = {"mssdk-sg.tiktok.com"}
        elif operation == "event_feed":
            supported_hosts = {"www.tiktok.com"}
        else:
            supported_hosts = {"webcast.tiktok.com", "webcast.us.tiktok.com"}
        if (operation not in path_for_operation or parts.scheme != "https"
                or parts.hostname not in supported_hosts
                or parts.path != path_for_operation[operation]
                or parts.username or parts.password or parts.port not in (None, 443)):
            raise ValueError("signing_request_unsupported")
        if operation == "report":
            valid_order = not self._sequence and self.room_id is None
        elif operation == "enter":
            valid_order = self._sequence in ([], ["report"])
        elif operation == "initial_fetch":
            sequence = list(self._sequence)
            if sequence[:1] == ["report"]:
                sequence.pop(0)
            valid_order = ("chat" not in sequence
                           and sequence.count("enter") <= 1
                           and ("enter" not in sequence or sequence[0] == "enter")
                           and all(item in {"enter", "initial_fetch"}
                                   for item in sequence))
        elif operation in {"epiphron", "check_alive", "event_feed"}:
            sequence = list(self._sequence)
            if sequence[:1] == ["report"]:
                sequence.pop(0)
            if sequence[:1] == ["enter"]:
                sequence.pop(0)
            while sequence[:1] == ["initial_fetch"]:
                sequence.pop(0)
            if operation == "epiphron":
                valid_order = (
                    all(item == "epiphron" for item in sequence)
                    or sequence == ["epiphron", "check_alive", "event_feed"]
                )
            elif operation == "check_alive":
                valid_order = sequence == ["epiphron"]
            else:
                valid_order = sequence == ["epiphron", "check_alive"]
        else:
            sequence = list(self._sequence)
            if sequence[:1] == ["report"]:
                sequence.pop(0)
            if sequence[:1] == ["enter"]:
                sequence.pop(0)
            while sequence[:1] == ["initial_fetch"]:
                sequence.pop(0)
            browser_prerequisites = [
                "epiphron", "check_alive", "event_feed", "epiphron",
            ]
            if sequence[:4] == browser_prerequisites:
                del sequence[:4]
            else:
                while sequence[:1] == ["epiphron"]:
                    sequence.pop(0)
            valid_order = all(item == "chat" for item in sequence)
        if not valid_order:
            raise ValueError("signing_request_order")
        if body is not None and not isinstance(body, bytes):
            raise ValueError("signing_body_type")
        query_room = dict(parse_qsl(parts.query, keep_blank_values=True)).get("room_id")
        if query_room and query_room != self.room_id:
            raise ValueError("signing_identity_mismatch")
        next_dyn = dict(self._sdk_state.dynosaur_context)
        next_gnarly = dict(self._sdk_state.gnarly_context)
        dyn = dict(next_dyn)
        gnarly = dict(next_gnarly)
        if operation == "report":
            dyn = {
                **x_dynosaur._DYNOSAUR_REPORT_CONTEXT,
                "field32_seed": dyn["field32_seed"],
                "field37": dyn["field37"],
                "field40": dyn["field40"],
                "field44": dyn["field44"],
                "field47": dyn["field47"],
                "field50": dyn["field50"],
                "field51": dyn["field51"],
                "field52": dyn["field52"],
                "field53": dyn["field53"],
                "field54": dyn["field54"],
                "field56": dyn["field56"],
            }
            gnarly = {
                **x_gnarly._GNARLY_REPORT_CONTEXT,
                "field2": gnarly["field2"],
                "field8": gnarly["field8"],
                "perf_data": gnarly["perf_data"],
                "sdk_version": gnarly["sdk_version"],
                "scm_version": gnarly["scm_version"],
                "field12": gnarly["field12"],
                "field13": gnarly["field13"],
                "order": list(gnarly["order"]),
            }
        elif operation == "enter":
            dyn = {
                **x_dynosaur._DYNOSAUR_WEBCAST_ROOM_ENTER_CONTEXT,
                "field32_seed": dyn["field32_seed"],
                "field44": dyn["field44"],
                "field52": dyn["field52"],
                "field53": dyn["field53"],
            }
            gnarly = {
                **x_gnarly._GNARLY_WEBCAST_ROOM_ENTER_CONTEXT,
                "field8": gnarly["field8"],
                "perf_data": gnarly["perf_data"],
                "order": list(gnarly["order"]),
            }
        signed = sign_url(
            url, query_token, body, user_agent=self.profile.user_agent,
            dynosaur_context=dyn, gnarly_context=gnarly,
        )
        if operation != "enter":
            aggregate_counter_mode = int(next_gnarly["field13"]) == (
                int(next_dyn["field37"]) + int(next_dyn["field47"])
            ) & 0xFFFFFFFF
            next_dyn["field37"] = (int(next_dyn["field37"]) + 1) & 0xFFFFFFFF
            next_dyn["field47"] = (int(next_dyn["field47"]) + 2) & 0xFFFFFFFF
            next_gnarly["field13"] = (
                int(next_gnarly["field13"])
                + (3 if aggregate_counter_mode else 1)
            ) & 0xFFFFFFFF
            next_gnarly["field12"] = (
                int(next_gnarly["field12"]) + 2
            ) & 0xFFFFFFFF
            self._sdk_state = SdkState(next_dyn, next_gnarly)
        self._sequence.append(operation)
        return signed
