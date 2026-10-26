"""Anonymous page discovery preserves accounts and obeys startup deadlines."""
from copy import deepcopy
from http.cookiejar import CookieJar
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from tiktok_live import runner
from tiktok_live.errors import SafeOperationError


ROOM_ID = "7698689442881522440"
LIVE_HTML = ('<script id="SIGI_STATE">{"LiveRoom":{"liveRoomUserInfo":{"user":'
             '{"uniqueId":"tt.yam1","roomId":"7698689442881522440","status":2}}}}</script>')


class OriginalSessionRoomLookupTests(unittest.TestCase):
    def setUp(self):
        self.clock = 100.0
        self.cancelled = False
        self.account = {"id": "selected", "cookies": [
            {"name": "sessionid", "value": "synthetic-login", "domain": ".tiktok.com"},
            {"name": "msToken", "value": "synthetic-stale", "domain": ".tiktok.com"},
        ]}
        self.calls = []
        self.closed = False
        self.page_elapsed = 2
        self.cancel_after_page = False
        self.html = LIVE_HTML
        owner = self
        class Visitor:
            def __init__(self):
                jar = CookieJar()
                self.cookies = SimpleNamespace(jar=jar)
            def __enter__(self):
                return self
            def __exit__(self, *args):
                owner.close()
            def get(self, *args, **kwargs):
                return owner.get(*args, **kwargs)
            def close(self):
                owner.close()
        self.session = Visitor()

    def post(self, url, **kwargs):
        self.calls.append(("post", kwargs))
        raise AssertionError("Room lookup must not depend on token-report POST")

    def get(self, url, **kwargs):
        self.calls.append(("get", kwargs))
        self.clock += self.page_elapsed
        self.cancelled = self.cancel_after_page
        kwargs['content_callback'](self.html.encode())
        return SimpleNamespace(status_code=200, headers={'Content-Type': 'text/html'})

    def close(self):
        self.closed = True

    def resolve(self, *, timeout=12):
        with patch.object(runner.requests, "Session", return_value=self.session), \
                patch.object(runner.time, "monotonic", side_effect=lambda: self.clock):
            return runner._resolve_observer_room_id(
                "tt.yam1", [self.account], {"selected"}, "", lambda: self.cancelled,
                connect_timeout=timeout)

    def test_room_query_preserves_login_without_mandatory_token_report(self):
        before = deepcopy(self.account)
        self.assertEqual(self.resolve()["roomId"], ROOM_ID)
        self.assertEqual([kind for kind, _ in self.calls], ["get"])
        self.assertFalse(any(name.lower() == 'cookie' for name in self.calls[0][1]['headers']))
        self.assertEqual(self.account, before)
        self.assertTrue(self.closed)

    def test_page_uses_configured_total_budget_without_api_fallback(self):
        self.assertEqual(self.resolve(timeout=12)["roomId"], ROOM_ID)
        # One configured deadline: primary reserves half for anonymous fallback.
        self.assertEqual(self.calls[0][1]["timeout"], 6)

    def test_cancel_before_query_prevents_any_http_request(self):
        self.cancelled = True
        self.assertIsNone(self.resolve())
        self.assertEqual(self.calls, [])

    def test_expired_lookup_budget_prevents_the_live_page_request(self):
        self.page_elapsed = 13
        with self.assertRaises(SafeOperationError):
            self.resolve(timeout=12)
        self.assertEqual([kind for kind, _ in self.calls], ["get"])
        self.assertTrue(self.closed)

    def test_cancel_after_page_discards_result_and_closes_session(self):
        self.cancel_after_page = True
        self.assertIsNone(self.resolve())
        self.assertEqual([kind for kind, _ in self.calls], ["get"])
        self.assertTrue(self.closed)

    def test_owner_mismatch_is_rejected_without_an_observer_connection(self):
        self.html = LIVE_HTML.replace("tt.yam1", "other_host")
        with self.assertRaises(SafeOperationError):
            self.resolve()
        self.assertTrue(self.closed)


class ListenerRoomLookupTests(unittest.TestCase):
    def start(self, lookup):
        return runner._start_live_listener("tt.yam1", connect_timeout=12,
            connect_retries=2, retry_delay_seconds=0, show_comments=False,
            resolve_room_id=lookup)

    def test_original_session_resolves_room_before_guest_listener_is_constructed(self):
        order = []
        def lookup():
            order.append("account_lookup")
            return ROOM_ID
        def factory(username, **kwargs):
            order.append(("guest_listener", kwargs.get("room_id")))
            return SimpleNamespace(start=lambda **kw: True, close=lambda: None)
        with patch.object(runner, "LiveMessageListener", side_effect=factory):
            self.assertIsNotNone(self.start(lookup))
        self.assertEqual(order, ["account_lookup", ("guest_listener", ROOM_ID)])

    def test_unconfirmed_room_lookup_is_terminal_before_guest_connection(self):
        with patch.object(runner, "LiveMessageListener", side_effect=AssertionError('No WSS allowed')):
            with self.assertRaises(SafeOperationError) as raised:
                self.start(lambda: None)
        self.assertEqual(raised.exception.classification, 'room_state_unconfirmed')

    def test_guest_transport_retries_reuse_the_resolved_room_id(self):
        lookup_calls = []
        def lookup():
            lookup_calls.append(1)
            return ROOM_ID
        listener = SimpleNamespace(start=lambda **kw: False, close=lambda: None,
                                   error=RuntimeError("transport"), failure_stage="wss_transport")
        with patch.object(runner, "LiveMessageListener", return_value=listener) as factory, \
                patch.object(runner, "_pause"):
            self.assertIsNone(self.start(lookup))
        self.assertEqual(len(lookup_calls), 1)
        self.assertEqual(factory.call_count, 3)


if __name__ == "__main__":
    unittest.main()
