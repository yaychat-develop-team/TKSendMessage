import unittest
from unittest.mock import patch
from urllib.parse import parse_qsl, urlsplit

from tiktok_live.config import strict_browser_profile_from_user_agent
from tiktok_live import client


class SigningProfileTests(unittest.TestCase):
    def test_supported_major_keeps_all_browser_versions_aligned(self):
        ua = (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/136.0.0.0 Safari/537.36"
        )
        profile = strict_browser_profile_from_user_agent(ua)
        self.assertEqual(profile.user_agent, ua)
        self.assertEqual(profile.browser_version, ua.removeprefix("Mozilla/"))
        self.assertIn('"Chromium";v="136"', profile.sec_ch_ua)
        self.assertEqual(profile.impersonate, "chrome136")

    def test_unsupported_or_approximate_major_fails_closed(self):
        for ua in (
            "Mozilla/5.0 Chrome/150.0.0.0 Safari/537.36",
            "Mozilla/5.0 Chrome/146.0.0.0 Safari/537.36",
            "Synthetic agent without Chrome major",
        ):
            with self.subTest(ua=ua), self.assertRaisesRegex(
                ValueError, "unsupported_browser_profile"
            ):
                strict_browser_profile_from_user_agent(ua)

    def test_chat_query_expands_sequence_values_without_python_list_literal(self):
        profile = strict_browser_profile_from_user_agent(
            "Mozilla/5.0 Chrome/136.0.0.0 Safari/537.36"
        )
        with patch.object(client, "web_id_last_time", return_value=["111", "222"]):
            url, _ = client.build_chat_request(
                "42", "hello", {}, "777", "https://www.tiktok.com/@host/live",
                profile=profile,
            )
        values = [value for key, value in parse_qsl(urlsplit(url).query)
                  if key == "WebIdLastTime"]
        self.assertEqual(values, ["111", "222"])


if __name__ == "__main__":
    unittest.main()
