import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import send_live_message as facade
from tiktok_live import account_store, browser_auth, client, runner


class SavedRequestIdentityTests(unittest.TestCase):
    cookies = [
        {"name": "sessionid", "value": "synthetic-session", "domain": ".tiktok.com", "path": "/", "expires": -1},
        {"name": "s_v_web_id", "value": "verify_synthetic", "domain": ".tiktok.com", "path": "/", "expires": -1},
    ]

    def test_profile_is_derived_from_saved_login_user_agent(self):
        ua = "Mozilla/5.0 Chrome/145.0.0.0 Safari/537.36"
        profile = client.browser_profile_from_user_agent(ua)
        self.assertEqual(profile.user_agent, ua)
        self.assertIn('v="145"', profile.sec_ch_ua)
        self.assertEqual(profile.impersonate, "chrome145")

    def test_post_rejects_saved_ticket_headers_without_signing_state(self):
        captured = {}
        response = SimpleNamespace(status_code=200)
        session = SimpleNamespace(post=lambda url, **kwargs: (
            captured.update({"url": url, **kwargs}) or response
        ))
        saved = {
            "tt-ticket-guard-client-data": "saved-client-data",
            "tt-ticket-guard-public-key": "saved-public-key",
            "tt-ticket-guard-version": "2",
        }
        with patch.object(client, "build_headers_from_cookie_state",
                          side_effect=AssertionError("must not regenerate")), \
                patch.object(client, "sign_url", return_value="https://synthetic.test/chat"):
            with self.assertRaises(client.SafeOperationError) as caught:
                facade._ORIGINAL_POST_CHAT_REQUEST(
                    session, client.CHAT_URL, b"{}", "sessionid=synthetic",
                    "fresh-ms", None, "https://www.tiktok.com/@host/live",
                    ticket_guard_headers=saved,
                )
        self.assertEqual(caught.exception.classification, "ticket_guard_unavailable")
        self.assertEqual(captured, {})

    def test_login_saves_user_agent_and_generated_ticket_headers_once(self):
        class FakeTab:
            url = "https://www.tiktok.com/foryou"
            def get(self, *args, **kwargs):
                return True

        class FakeBrowser:
            process_id = 123
            latest_tab = FakeTab()
            states = SimpleNamespace(is_alive=True)
            def get_tabs(self):
                return [self.latest_tab]
            def quit(self):
                self.states.is_alive = False

        ua = "Mozilla/5.0 Chrome/145.0.0.0 Safari/537.36"
        state = {"ts_sign": "ts.1.synthetic", "encrypt_ticket": "cipher", "private_key_pem": "private"}
        headers = {
            "tt-ticket-guard-client-data": "saved-client-data",
            "tt-ticket-guard-public-key": "saved-public-key",
        }
        with tempfile.TemporaryDirectory() as directory:
            store_path = Path(directory) / "accounts.json"
            with patch.object(browser_auth, "_free_local_port", return_value=45678), \
                    patch.object(browser_auth, "launch_available_browser", return_value=FakeBrowser()), \
                    patch.object(browser_auth, "clear_tiktok_cookies"), \
                    patch.object(browser_auth, "_check_login_active"), \
                    patch.object(browser_auth, "wait_for_tiktok_login", return_value=self.cookies), \
                    patch.object(browser_auth, "read_browser_user_agent", return_value=ua, create=True), \
                    patch.object(browser_auth, "wait_for_ticket_guard_state", return_value=state), \
                    patch.object(browser_auth, "build_headers_from_cookie_state", return_value=headers, create=True) as build:
                account = browser_auth.login_new_account(store_path)
            saved = account_store.load_account_store(store_path)["accounts"][0]
        self.assertEqual(account["user_agent"], ua)
        self.assertEqual(saved["user_agent"], ua)
        self.assertEqual(account["ticket_guard_headers"], headers)
        self.assertEqual(saved["ticket_guard_headers"], headers)
        build.assert_called_once()

    def test_runner_forwards_saved_ua_and_ticket_headers(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "accounts.json"
            store = {"version": 1, "accounts": []}
            account = account_store.upsert_account(store, self.cookies, datetime.now(timezone.utc))
            account["user_agent"] = "Mozilla/5.0 Chrome/145.0.0.0 Safari/537.36"
            account["ticket_guard_headers"] = {
                "tt-ticket-guard-client-data": "saved-client-data",
                "tt-ticket-guard-public-key": "saved-public-key",
            }
            account_store.save_account_store_atomic(path, store)
            captured = {}

            def send(username, cookie, message, **kwargs):
                captured.update(kwargs)
                return {"ok": True}

            with patch.object(runner, "send_live_message", side_effect=send):
                result = runner.run_saved_accounts(
                    "host", "hello", path, add_new_account=False,
                    interval_seconds=0, observation_enabled=False,
                )
        self.assertEqual(result["success"], 1)
        self.assertEqual(captured["user_agent"], account["user_agent"])
        self.assertEqual(captured["ticket_guard_headers"], account["ticket_guard_headers"])


    def test_known_listener_room_id_skips_public_live_page(self):
        profile = client.browser_profile_from_user_agent(
            "Mozilla/5.0 Chrome/145.0.0.0 Safari/537.36"
        )
        expected = {"ok": True, "roomId": "7696000000000000001"}
        with patch.object(client.requests, "Session",
                          return_value=SimpleNamespace(close=lambda: None)), \
                patch.object(client, "get_fresh_ms_token", return_value="fresh"), \
                patch.object(client, "get_sec_sdk_csrf_token", return_value="csrf"), \
                patch.object(client, "get_live_room",
                             side_effect=AssertionError("public live page must be skipped")) as live, \
                patch.object(client, "enter_live_room",
                             side_effect=AssertionError("room enter must not be called")), \
                patch.object(client, "fetch_initial_response", return_value=object()), \
                patch.object(client, "_post_prepared_chat", return_value=expected) as post:
            actual = facade._ORIGINAL_CLIENT_SEND(
                "host", "s_v_web_id=verify_synthetic; sessionid=synthetic",
                "hello", user_agent=profile.user_agent,
                known_room_id="7696000000000000001",
            )
        self.assertIs(actual, expected)
        live.assert_not_called()
        self.assertEqual(post.call_args.kwargs["room"]["roomId"], "7696000000000000001")



if __name__ == "__main__":
    unittest.main()

