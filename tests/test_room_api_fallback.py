"""Original-session API lookup contracts; legacy page parsing is tested separately."""
from copy import deepcopy
from types import SimpleNamespace
import unittest
from unittest.mock import patch
from urllib.parse import urlsplit

from tiktok_live import client
from tiktok_live.client import get_api_live_room as get_live_room

ROOM = "7698688038969821960"


def payload(owner="amanyandayo", room=ROOM):
    return {"data": {"user": {"uniqueId": owner, "roomId": room,
                             "nickname": "Host", "status": 2},
                     "liveRoom": {"status": 2}}}


class RoomApiFallbackTests(unittest.TestCase):
    def setUp(self):
        self.clock = 100.0
        self.calls = []
        self.elapsed = 1
        self.api_payload = payload()
        self.api_status = 200
        self.cookie = "sessionid=synthetic-login; msToken=synthetic-current"
        self.session = SimpleNamespace(get=self.get)

    def get(self, url, **kwargs):
        self.calls.append((url, deepcopy(kwargs)))
        self.clock += self.elapsed
        if urlsplit(url).path != "/api-live/user/room/":
            raise AssertionError("HTML fallback is forbidden")
        def response_json():
            if isinstance(self.api_payload, Exception):
                raise self.api_payload
            return deepcopy(self.api_payload)
        return SimpleNamespace(status_code=self.api_status, json=response_json)

    def lookup(self, username="amanyandayo", **kwargs):
        with patch.object(client.time, "monotonic", side_effect=lambda: self.clock):
            return get_live_room(self.session, username, self.cookie,
                fallback_cookie=kwargs.pop("fallback_cookie", ""),
                timeout_seconds=kwargs.pop("timeout_seconds", 12), **kwargs)

    def test_api_uses_current_account_cookie_and_json_headers(self):
        room = self.lookup()
        self.assertEqual(room["roomId"], ROOM)
        self.assertEqual(room["nickname"], "Host")
        self.assertEqual(len(self.calls), 1)
        url, request = self.calls[0]
        self.assertEqual(urlsplit(url).path, "/api-live/user/room/")
        self.assertEqual(request["params"]["uniqueId"], "amanyandayo")
        self.assertEqual(request["params"]["aid"], 1988)
        self.assertEqual(request["headers"]["cookie"], self.cookie)
        self.assertEqual(request["headers"]["sec-fetch-mode"], "cors")
        self.assertEqual(request["headers"]["sec-fetch-dest"], "empty")
        self.assertLessEqual(request["timeout"], 12)

    def test_api_does_not_invent_page_device_or_account_identity(self):
        room = self.lookup()
        self.assertIsNone(room["deviceId"])
        self.assertIsNone(room["accountUsername"])

    def test_configured_fallback_does_not_switch_login_identity(self):
        room = self.lookup(fallback_cookie="sessionid=synthetic-other-account")
        self.assertEqual(room["roomId"], ROOM)
        self.assertEqual([k["headers"]["cookie"] for _, k in self.calls], [self.cookie])

    def test_target_is_dynamic_and_at_prefix_is_normalized(self):
        self.api_payload = payload("another.host", "123456789012")
        self.assertEqual(self.lookup(username=" @another.host ")["username"], "another.host")
        self.assertEqual(self.calls[-1][1]["params"]["uniqueId"], "another.host")

    def test_wrong_or_missing_api_owner_is_rejected(self):
        for owner in ("other_host", "", None):
            with self.subTest(owner=owner):
                self.api_payload = payload(owner)
                with self.assertRaises(ValueError):
                    self.lookup()

    def test_invalid_api_room_ids_are_rejected(self):
        for room in ("", "0", "not-a-room", "18446744073709551616", True, None):
            with self.subTest(room=room):
                self.api_payload = payload(room=room)
                with self.assertRaises(ValueError):
                    self.lookup()

    def test_raw_offline_metadata_is_preserved_without_claiming_verified_state(self):
        self.api_payload = payload()
        self.api_payload["data"]["user"]["status"] = 4
        self.api_payload["data"]["liveRoom"]["status"] = 4
        room = client.request_live_room_api(self.session, "amanyandayo", self.cookie)
        self.assertEqual(room["roomId"], ROOM)
        self.assertIs(room["isLive"], False)
        self.assertEqual(room["liveStatus"], 4)

    def test_bad_api_response_never_leaks_raw_body(self):
        for bad in ([], None, {"data": []}, {"status_code": 123, **payload()},
                    ValueError("sessionid=private; msToken=private")):
            with self.subTest(bad_type=type(bad).__name__):
                self.api_payload = bad
                with self.assertRaises(ValueError) as raised:
                    self.lookup()
                self.assertNotIn("private", str(raised.exception))
                self.assertNotIn("sessionid", str(raised.exception))

    def test_non_success_http_response_is_rejected(self):
        self.api_status = 403
        with self.assertRaises(ValueError):
            self.lookup()
        self.assertEqual(len(self.calls), 1)

    def test_shared_deadline_discards_late_api_success(self):
        self.elapsed = 13
        with self.assertRaises(TimeoutError):
            self.lookup()
        self.assertEqual(len(self.calls), 1)


if __name__ == "__main__":
    unittest.main()
