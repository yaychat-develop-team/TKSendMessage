import unittest
from types import SimpleNamespace
from unittest.mock import patch
from urllib.parse import parse_qs, parse_qsl, urlsplit

from tiktok_live import client


class RoomEnterBeforeChatTests(unittest.TestCase):
    def run_send(self, *, enter_status=0, enter_http_status=200,
                 fetch_error=None,
                 known_fetch_endpoint=None, known_region='JP', user_agent=None):
        events = []
        request = {}
        sessions = []
        initial = SimpleNamespace(cursor="server-cursor", push_server="wss://example.invalid")

        class Session:
            def __init__(self, **kwargs):
                self.options = kwargs
                self.closed = False
                sessions.append(self)
            def post(self, url, **kwargs):
                events.append("enter")
                request["enter_http"] = self
                request.update(url=url, **kwargs)
                return SimpleNamespace(
                    status_code=enter_http_status,
                    json=lambda: {"status_code": enter_status},
                )
            def close(self):
                self.closed = True

        def chat(**kwargs):
            events.append("chat")
            request["chat_device_id"] = kwargs["device_id"]
            request["initial_fetch"] = kwargs["initial_fetch"]
            request["wss_user_agent"] = kwargs["wss_user_agent"]
            return {"ok": True}

        def fetch(http_session, direct_session, **kwargs):
            events.append("fetch")
            request["fetch_http"] = http_session
            request["fetch_session"] = direct_session
            request["fetch_options"] = kwargs
            if fetch_error:
                raise fetch_error
            return initial

        def csrf(*args, **kwargs):
            events.append("csrf")
            return "csrf"

        with patch.object(client.requests, "Session", side_effect=Session), \
                patch.object(client, "get_fresh_ms_token", return_value="fresh-ms"), \
                patch.object(client, "get_sec_sdk_csrf_token", side_effect=csrf), \
                patch.object(client, "create_device_id", return_value="7000000000000000001"), \
                patch.object(client, "sign_url", side_effect=lambda url, *args, **kwargs: url), \
                patch.object(client, "enter_live_room",
                             side_effect=AssertionError("room enter must not be called")), \
                patch.object(client.time, "sleep", return_value=None), \
                patch.object(client, "fetch_initial_response", side_effect=fetch, create=True), \
                patch.object(client, "_post_prepared_chat", side_effect=chat):
            try:
                kwargs = {"known_room_id": "7696000000000000001"}
                if known_fetch_endpoint is not None:
                    kwargs["known_fetch_endpoint"] = known_fetch_endpoint
                if known_region is not None:
                    kwargs["known_region"] = known_region
                if user_agent is not None:
                    kwargs["user_agent"] = user_agent
                result = client._send_live_message(
                    "host", ("sessionid=synthetic-session; s_v_web_id=verify_test; "
                             "sid_guard=synthetic%7C1700000000%7C15552000"),
                    "hello", **kwargs,
                )
            except Exception as exc:
                result = exc
        return events, request, result, sessions

    def test_initial_fetch_precedes_chat_with_shared_room_and_device(self):
        events, request, result, sessions = self.run_send()

        self.assertEqual(events, ["fetch", "csrf", "chat"])
        self.assertEqual(result, {"ok": True})
        self.assertIs(request["initial_fetch"], request["initial_fetch"])
        self.assertEqual(request["initial_fetch"].cursor, "server-cursor")
        self.assertEqual(request["wss_user_agent"], request["fetch_session"].user_agent)
        self.assertEqual(request["fetch_session"].cookie_header,
                         "sessionid=synthetic-session; s_v_web_id=verify_test; "
                         "sid_guard=synthetic%7C1700000000%7C15552000; msToken=fresh-ms")
        self.assertEqual(request["fetch_options"], {
            "device_id": "7000000000000000001", "ms_token": "fresh-ms",
        })

    def test_send_path_never_calls_separate_room_enter(self):
        events, _, result, _ = self.run_send(enter_status=30001)

        self.assertEqual(events, ["fetch", "csrf", "chat"])
        self.assertEqual(result, {"ok": True})

    def test_initial_fetch_and_wss_use_the_saved_account_user_agent(self):
        account_user_agent = (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/145.0.0.0 Safari/537.36"
        )
        _, request, result, _ = self.run_send(user_agent=account_user_agent)

        self.assertEqual({"ok": True}, result)
        self.assertEqual(account_user_agent, request["fetch_session"].user_agent)
        self.assertEqual(account_user_agent, request["wss_user_agent"])

    def test_rejected_initial_fetch_stops_before_chat(self):
        events, _, result, _ = self.run_send(fetch_error=ValueError("synthetic details"))

        self.assertEqual(events, ["fetch"] * 4)
        self.assertIsInstance(result, client.SafeOperationError)
        self.assertEqual(result.classification, "initial_fetch_unavailable")

    def test_initial_fetch_uses_isolated_http_session_and_page_endpoint(self):
        endpoint = 'https://webcast.eu.tiktok.com/webcast/im/fetch/'
        events, request, result, sessions = self.run_send(known_fetch_endpoint=endpoint)

        self.assertEqual({"ok": True}, result)
        self.assertEqual(["fetch", "csrf", "chat"], events)
        self.assertEqual(2, len(sessions))
        self.assertIs(request["fetch_http"], sessions[1])
        self.assertIsNot(sessions[0], request["fetch_http"])
        self.assertEqual(endpoint, request["fetch_options"]["endpoint"])
        self.assertTrue(sessions[1].closed)

    def test_runtime_ms_token_replaces_last_saved_position_without_duplicates(self):
        from tiktok_live.signing import cookie_with_ms_token
        self.assertEqual(
            cookie_with_ms_token(
                "first=1; msToken=old-one; middle=2; msToken=old-two; last=3",
                "fresh",
            ),
            "first=1; middle=2; msToken=fresh; last=3",
        )


if __name__ == "__main__":
    unittest.main()
