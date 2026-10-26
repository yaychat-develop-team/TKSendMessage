"""Exact unsigned first-fetch query contract, with synthetic room identity."""

import unittest
from urllib.parse import parse_qsl, urlsplit

from tiktok_live.wss import initial_fetch


class FetchQueryShapeTests(unittest.TestCase):
    def test_initial_fetch_has_one_270000_version_without_legacy_duplicate(self):
        ua = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
              "AppleWebKit/537.36 (KHTML, like Gecko) "
              "Chrome/150.0.0.0 Safari/537.36")
        self.assertTrue(hasattr(initial_fetch, "build_initial_fetch_url"))
        url = initial_fetch.build_initial_fetch_url("7699307763838618420", ua)
        parts = urlsplit(url)
        pairs = parse_qsl(parts.query, keep_blank_values=True)
        self.assertEqual("https", parts.scheme)
        self.assertEqual("webcast.tiktok.com", parts.hostname)
        self.assertEqual("/webcast/im/fetch/", parts.path)
        self.assertEqual([("version_code", "270000")],
                         [(key, value) for key, value in pairs if key == "version_code"])
        self.assertEqual(("version_code", "270000"), pairs[0])
        self.assertEqual([
            "version_code", "device_platform", "cookie_enabled",
            "screen_width", "screen_height", "browser_language",
            "browser_platform", "browser_name", "browser_version",
            "browser_online", "tz_name", "ws_direct", "aid", "app_name",
            "live_id", "app_language", "client_enter", "room_id", "identity",
            "history_comment_count", "fetch_rule", "last_rtt", "cursor",
            "internal_ext", "sup_ws_ds_opt", "resp_content_type", "did_rule",
            "webcast_language",
        ], [key for key, _ in pairs])
        self.assertEqual(ua.removeprefix("Mozilla/"), dict(pairs)["browser_version"])
        self.assertEqual("7699307763838618420", dict(pairs)["room_id"])

    def test_room_is_supplied_for_each_request_not_fixed_to_example(self):
        self.assertTrue(hasattr(initial_fetch, "build_initial_fetch_url"))
        pairs = dict(parse_qsl(urlsplit(initial_fetch.build_initial_fetch_url(
            "7699313974173240072", "Mozilla/5.0 Chrome/150.0.0.0"
        )).query))
        self.assertEqual("7699313974173240072", pairs["room_id"])
        self.assertEqual("5.0 Chrome/150.0.0.0", pairs["browser_version"])


if __name__ == "__main__":
    unittest.main()
