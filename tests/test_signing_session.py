import unittest
from urllib.parse import parse_qs, urlsplit

from tiktok_live.algorithms import x_dynosaur, x_gnarly
from tiktok_live.signing import sign_url
from tiktok_live.config import strict_browser_profile_from_user_agent
from tiktok_live.signing_session import SdkState, SigningSession


class SigningContextTests(unittest.TestCase):
    def test_explicit_page_context_reaches_both_signature_payloads(self):
        page = "www.tiktok.com/@synthetic/live"
        dyn = {
            **x_dynosaur._DYNOSAUR_WEBCAST_ROOM_ENTER_CONTEXT,
            "field37": 3,
            "field47": 9,
            "field40": 123456,
            "field44": 24680,
            "field50": "1.0.0.synthetic",
            "field51": "a" * 32,
            "field52": 13579,
            "field53": page,
        }
        gnarly = {
            **x_gnarly._GNARLY_WEBCAST_ROOM_ENTER_CONTEXT,
            "field8": 97531,
            "perf_data": 24680,
            "field12": 9,
            "field13": 3,
        }
        signed = sign_url(
            "https://webcast.tiktok.com/webcast/room/enter/?room_id=42",
            "synthetic-query-token",
            b"room_id=42",
            user_agent="Mozilla/5.0 Chrome/136.0.0.0",
            dynosaur_context=dyn,
            gnarly_context=gnarly,
        )
        query = parse_qs(urlsplit(signed).query)
        observed_dyn = x_dynosaur._decode_x_dynosaur_context(query["X-Dynosaur"][0])
        observed_gnarly = x_gnarly._decode_x_gnarly_context(query["X-Gnarly"][0])
        self.assertEqual((observed_dyn["field37"], observed_dyn["field47"]), (3, 9))
        self.assertEqual(observed_dyn["field40"], 123456)
        self.assertEqual(observed_dyn["field44"], 24680)
        self.assertEqual(observed_dyn["field52"], 13579)
        self.assertEqual(observed_dyn["field53"], page)
        self.assertEqual(observed_gnarly["field8"], 97531)
        self.assertEqual(observed_gnarly["perf_data"], 24680)
        self.assertEqual((observed_gnarly["field12"], observed_gnarly["field13"]), (9, 3))

    def test_session_keeps_page_state_and_rejects_other_account(self):
        profile = strict_browser_profile_from_user_agent(
            "Mozilla/5.0 Chrome/136.0.0.0 Safari/537.36"
        )
        dyn = {
            **x_dynosaur._DYNOSAUR_WEBCAST_ROOM_ENTER_CONTEXT,
            "field37": 1, "field47": 2, "field40": 123456,
            "field44": 24680, "field50": "1.0.0.2717", "field51": "a" * 32,
            "field52": 13579, "field53": "www.tiktok.com/@host/live",
        }
        gnarly = {
            **x_gnarly._GNARLY_WEBCAST_ROOM_ENTER_CONTEXT,
            "field8": 97531, "perf_data": 24680,
            "field12": 2, "field13": 1,
        }
        signer = SigningSession(
            account_id="account-A", room_id="42",
            page_url="https://www.tiktok.com/@host/live", profile=profile,
            sdk_state=SdkState(dyn, gnarly),
        )
        enter = signer.sign(
            "https://webcast.tiktok.com/webcast/room/enter/",
            account_id="account-A", room_id="42", query_token="synthetic-a",
            body=b"room_id=42", operation="enter",
        )
        with self.assertRaisesRegex(ValueError, "signing_identity_mismatch"):
            signer.sign(
                "https://webcast.tiktok.com/webcast/im/fetch/?room_id=42",
                account_id="account-B", room_id="42", query_token="synthetic-a",
                operation="initial_fetch",
            )
        query = parse_qs(urlsplit(enter).query)
        first_d = x_dynosaur._decode_x_dynosaur_context(query["X-Dynosaur"][0])
        first_g = x_gnarly._decode_x_gnarly_context(query["X-Gnarly"][0])
        self.assertEqual((first_d["field37"], first_d["field47"]), (1, 2))
        self.assertEqual((first_g["field13"], first_g["field12"]), (1, 2))
        self.assertEqual(first_d["field44"], first_g["perf_data"])
        self.assertEqual(first_d["field44"], 24680)
        self.assertEqual(first_g["field8"], 97531)
        self.assertNotIn("synthetic-a", repr(signer))

    def test_uninitialized_or_unsupported_sdk_state_is_rejected(self):
        profile = strict_browser_profile_from_user_agent(
            "Mozilla/5.0 Chrome/136.0.0.0 Safari/537.36"
        )
        with self.assertRaisesRegex(ValueError, "signing_state_unsupported"):
            SigningSession("account-A", "42", "https://www.tiktok.com/@host/live",
                           profile, None)
        with self.assertRaisesRegex(ValueError, "signing_state_unsupported"):
            SdkState({"field50": "9.9.9"}, {})

    def test_sdk_state_is_isolated_from_caller_mutation(self):
        dyn = {
            **x_dynosaur._DYNOSAUR_WEBCAST_ROOM_ENTER_CONTEXT,
            "field37": 1, "field47": 2, "field40": 123456,
            "field44": 24680, "field50": "1.0.0.2717", "field51": "a" * 32,
            "field52": 13579, "field53": "www.tiktok.com/@host/live",
        }
        gnarly = {
            **x_gnarly._GNARLY_WEBCAST_ROOM_ENTER_CONTEXT,
            "field8": 97531, "perf_data": 24680,
            "field12": 2, "field13": 1,
        }
        state = SdkState(dyn, gnarly)
        dyn["field40"] = -1
        gnarly["field8"] = -1
        self.assertEqual(state.dynosaur_context["field40"], 123456)
        self.assertEqual(state.gnarly_context["field8"], 97531)
        with self.assertRaises(TypeError):
            state.dynosaur_context["field40"] = -1
        with self.assertRaises(TypeError):
            state.gnarly_context["field8"] = -1

    def test_second_operation_rejects_unproven_counter_progression(self):
        profile = strict_browser_profile_from_user_agent(
            "Mozilla/5.0 Chrome/136.0.0.0 Safari/537.36"
        )
        dyn = {
            **x_dynosaur._DYNOSAUR_WEBCAST_ROOM_ENTER_CONTEXT,
            "field37": 1, "field47": 2, "field40": 123456,
            "field44": 24680, "field50": "1.0.0.2717", "field51": "a" * 32,
            "field52": 13579, "field53": "www.tiktok.com/@host/live",
        }
        gnarly = {
            **x_gnarly._GNARLY_WEBCAST_ROOM_ENTER_CONTEXT,
            "field8": 97531, "perf_data": 24680,
            "field12": 2, "field13": 1,
        }
        signer = SigningSession("account-A", "42", "https://www.tiktok.com/@host/live",
                                profile, SdkState(dyn, gnarly))
        signer.sign("https://webcast.tiktok.com/webcast/room/enter/",
                    account_id="account-A", room_id="42", query_token="synthetic-a",
                    body=b"room_id=42", operation="enter")
        with self.assertRaisesRegex(ValueError, "signing_state_unsupported"):
            signer.sign("https://webcast.tiktok.com/webcast/im/fetch/?room_id=42",
                        account_id="account-A", room_id="42", query_token="synthetic-b",
                        operation="initial_fetch")


if __name__ == "__main__":
    unittest.main()
