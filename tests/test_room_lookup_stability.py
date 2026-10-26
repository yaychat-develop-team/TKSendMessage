"""Transient query failures and offline IDs must not become false live success."""
from copy import deepcopy
import json
from types import SimpleNamespace
import unittest
from unittest.mock import patch
from urllib.parse import urlsplit

from curl_cffi.requests.exceptions import SSLError, Timeout, CertificateVerifyError
from tiktok_live import client, runner
from tiktok_live.errors import SafeOperationError
from tiktok_live.client import get_api_live_room as get_live_room, request_live_room, request_live_room_api
import send_live_message as facade


ROOM = "7698688038969821960"
TARGET = "nanaminamin"


def api_payload(status=2, live_status=2):
    return {"data": {"user": {"uniqueId": TARGET, "roomId": ROOM,
                              "nickname": "Host", "status": status},
                     "liveRoom": {"status": live_status}}}


def live_html(status=2, *, universal=False, recommendation=False):
    room = {"user": {"uniqueId": TARGET, "roomId": ROOM, "status": status},
            "liveRoom": {"status": status}}
    state = ({"__DEFAULT_SCOPE__": {"webapp.live-detail": {"liveRoomUserInfo": room}}}
             if universal else {"LiveRoom": {"liveRoomUserInfo": room}})
    if recommendation:
        state = {"recommendation": {"roomId": "123456789012"}, **state}
    tag = "__UNIVERSAL_DATA_FOR_REHYDRATION__" if universal else "SIGI_STATE"
    return '<script id="' + tag + '">' + json.dumps(state) + '</script>'


class RoomLookupStabilityTests(unittest.TestCase):
    def setUp(self):
        self.clock = 100.0
        self.page = "SlardarWAF"
        self.api_answers = [api_payload()]
        self.calls = []
        self.alive = None
        self.cookie = "sessionid=synthetic-login; msToken=synthetic-stored"
        self.session = SimpleNamespace(get=self.get)

    def get(self, url, **kwargs):
        self.calls.append((urlsplit(url).path, deepcopy(kwargs)))
        self.clock += 1
        if urlsplit(url).path == "/api-live/user/room/":
            answer = self.api_answers.pop(0)
            if isinstance(answer, Exception):
                raise answer
            status = 200
            if isinstance(answer, int):
                status, answer = answer, {}
            return SimpleNamespace(status_code=status, json=lambda: answer)
        if urlsplit(url).path == "/webcast/room/check_alive/":
            return SimpleNamespace(status_code=200, json=lambda: {"data": [{"room_id": ROOM, "alive": self.alive}]})
        if isinstance(self.page, Exception):
            raise self.page
        return SimpleNamespace(status_code=200, text=self.page)

    def lookup(self, timeout=12):
        with patch.object(client.time, "monotonic", side_effect=lambda: self.clock):
            return get_live_room(self.session, TARGET, self.cookie, fallback_cookie="",
                                 timeout_seconds=timeout)

    def outcome(self):
        try:
            return self.lookup()
        except Exception:
            return None

    def test_tls_failure_on_page_does_not_skip_api_fallback(self):
        self.page = SSLError("synthetic private URL", code=35)
        room = self.outcome()
        self.assertIsNotNone(room, "TLS page failure must still permit API lookup")
        self.assertEqual(room["roomId"], ROOM)
        self.assertIs(room.get("isLive"), True)
        self.assertEqual([path for path, _ in self.calls], ["/api-live/user/room/"])

    def test_page_timeout_preserves_time_for_api(self):
        self.page = Timeout("synthetic timeout", code=28)
        room = self.outcome()
        self.assertIsNotNone(room)
        self.assertEqual(room["roomId"], ROOM)
        self.assertLessEqual(sum(call[1]["timeout"] for call in self.calls), 12)

    def test_page_certificate_failure_falls_back_without_disabling_verification(self):
        self.page = CertificateVerifyError("synthetic certificate failure", code=60)
        room = self.outcome()
        self.assertIsNotNone(room)
        self.assertEqual(room["roomId"], ROOM)
        self.assertTrue(all(kwargs.get("verify", True) is True for _, kwargs in self.calls))

    def test_api_certificate_failure_is_safe_and_not_retried(self):
        self.api_answers = [CertificateVerifyError("sessionid=private-cert", code=60), api_payload()]
        with self.assertRaises(ValueError) as raised:
            self.lookup()
        self.assertNotIn("private-cert", str(raised.exception))
        self.assertEqual(len(self.calls), 1)

    def test_api_transient_timeout_retries_once_in_same_session(self):
        self.api_answers = [Timeout("synthetic timeout", code=28), api_payload()]
        room = self.outcome()
        self.assertIsNotNone(room)
        self.assertEqual(room["roomId"], ROOM)
        self.assertEqual(len(self.calls), 2)
        self.assertEqual(self.calls[-1][1]["headers"]["cookie"], self.cookie)
        self.assertLessEqual(self.calls[-1][1]["timeout"], 10)

    def test_api_transient_http_failure_retries_but_403_does_not(self):
        self.api_answers = [503, api_payload()]
        self.assertIsNotNone(self.outcome())
        self.assertEqual(len(self.calls), 2)
        self.calls.clear()
        self.api_answers = [403, api_payload()]
        with self.assertRaises(ValueError):
            self.lookup()
        self.assertEqual(len(self.calls), 1)

    def test_api_network_retries_are_bounded_and_logs_hide_exception_secrets(self):
        self.api_answers = [SSLError("sessionid=private-token", code=35)] * 3
        logs = []
        sink = client.logger.add(lambda event: logs.append(str(event)), format="{message}")
        try:
            with self.assertRaises(ValueError):
                self.lookup()
        finally:
            client.logger.remove(sink)
        self.assertEqual(len(self.calls), 2)
        self.assertNotIn("private-token", "".join(logs))

    def test_page_and_api_return_offline_id_with_separate_live_flag(self):
        self.page = live_html(4)
        page = request_live_room(self.session, TARGET, self.cookie)
        self.assertEqual(page["roomId"], ROOM)
        self.assertIs(page.get("isLive"), False)
        self.assertEqual(page.get("liveStatus"), 4)
        self.assertEqual(page.get("querySource"), "live_page")
        self.page = "SlardarWAF"
        self.api_answers = [api_payload(4, 4), api_payload(4, 4)]
        self.alive = False
        api = self.outcome()
        self.assertIsNotNone(api, "Offline state must not discard a valid queried ID")
        self.assertEqual(api["roomId"], ROOM)
        self.assertIs(api.get("isLive"), False)
        self.assertEqual(api.get("liveStatus"), 4)
        self.assertEqual(api.get("querySource"), "room_api+check_alive")

    def test_universal_hydration_offline_state_is_not_treated_as_live(self):
        self.page = live_html(4, universal=True)
        room = request_live_room(self.session, TARGET, self.cookie)
        self.assertEqual(room["roomId"], ROOM)
        self.assertIs(room.get("isLive"), False)

    def test_recommendation_id_is_not_selected_before_requested_owner(self):
        self.page = live_html(2, recommendation=True)
        room = request_live_room(self.session, TARGET, self.cookie)
        self.assertEqual(room["roomId"], ROOM)
        self.assertEqual(room["username"], TARGET)
        self.assertIs(room.get("isLive"), True)

    def test_api_unknown_or_conflicting_state_never_claims_live(self):
        for user_status, room_status, expected in ((None, None, None), (2, 4, False), (4, 2, False)):
            with self.subTest(status=(user_status, room_status)):
                self.api_answers = [api_payload(user_status, room_status)] * 2
                self.alive = expected
                room = self.outcome()
                self.assertIsNotNone(room)
                self.assertIs(room.get("isLive"), expected)

    def test_unknown_page_state_does_not_claim_live(self):
        self.page = '{"roomId":"7698688038969821960"}'
        room = request_live_room(self.session, TARGET, self.cookie)
        self.assertEqual(room["roomId"], ROOM)
        self.assertIn("isLive", room)
        self.assertIsNone(room["isLive"])

    def test_compat_api_query_does_not_require_token_report_and_preserves_login(self):
        account = {"id": "selected", "cookies": [
            {"name": "sessionid", "value": "synthetic-login", "domain": ".tiktok.com"},
            {"name": "msToken", "value": "synthetic-stored", "domain": ".tiktok.com"}]}
        before = deepcopy(account)
        self.page = live_html(2)
        self.session.close = lambda: None
        with patch.object(runner.requests, "Session", return_value=self.session), \
                patch.object(runner, 'get_live_room', side_effect=lambda *a, should_cancel=None, **k: client.get_api_live_room(*a, **k)), \
                patch.object(runner, "get_fresh_ms_token", side_effect=TimeoutError("must not call")), \
                patch.object(runner.time, "monotonic", side_effect=lambda: self.clock):
            room = runner._resolve_observer_room_id(TARGET, [account], {"selected"}, "", None,
                                                    connect_timeout=12)
        self.assertEqual(room["roomId"], ROOM)
        self.assertEqual(account, before)
        self.assertEqual(self.calls[0][1]["headers"]["cookie"], self.cookie)

    def test_offline_and_unknown_room_do_not_start_observer_or_chat_preparation(self):
        for state in (False, None):
            room = {"exists": True, "roomId": ROOM, "username": TARGET,
                    "isLive": state, "liveStatus": 4 if state is False else None}
            account = {"id": "selected", "cookies": [{"name": "sessionid", "value": "synthetic"}]}
            with self.subTest(state=state), \
                    patch.object(runner.requests, "Session", return_value=SimpleNamespace(close=lambda: None)), \
                    patch.object(runner, "get_live_room", return_value=room), \
                    patch.object(runner, "get_fresh_ms_token", return_value="synthetic"):
                with self.assertRaises(SafeOperationError):
                    runner._resolve_observer_room_id(TARGET, [account], {"selected"}, "", None,
                                                     connect_timeout=12)
            with patch.object(client.requests, "Session", return_value=object()), \
                    patch.object(client, "get_fresh_ms_token", return_value="synthetic"), \
                    patch.object(client, "get_live_room", return_value=room), \
                    patch.object(client, "get_sec_sdk_csrf_token", return_value="synthetic-csrf") as csrf, \
                    patch.object(client, "_post_prepared_chat", return_value={"ok": True}) as post:
                result = facade._ORIGINAL_CLIENT_SEND(TARGET, "sessionid=synthetic", "never sent")
            self.assertFalse(result["ok"])
            self.assertEqual(csrf.call_count, 0)
            self.assertEqual(post.call_count, 0)


if __name__ == "__main__":
    unittest.main()
