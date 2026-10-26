import unittest

from tiktok_live.wss.sessions import prepare_account


class InitialFetchSessionTests(unittest.TestCase):
    def test_account_session_keeps_initial_fetch_for_connection(self):
        initial = object()

        session = prepare_account(
            "venus", "42", "sessionid=synthetic", "FETCH_UA",
            initial_fetch=initial,
        )

        self.assertIs(session.initial_fetch, initial)


if __name__ == "__main__":
    unittest.main()
