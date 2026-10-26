import importlib
import ast
import unittest
from pathlib import Path
from unittest.mock import patch


class ProjectStructureTests(unittest.TestCase):
    def test_package_is_split_by_responsibility(self):
        root = Path(__file__).resolve().parents[1]
        expected = {
            "tiktok_live/config.py",
            "tiktok_live/errors.py",
            "tiktok_live/models.py",
            "tiktok_live/account_store.py",
            "tiktok_live/browser_auth.py",
            "tiktok_live/signing.py",
            "tiktok_live/client.py",
            "tiktok_live/runner.py",
            "tiktok_live/cli.py",
            "tiktok_live/algorithms/ms_token.py",
            "tiktok_live/algorithms/x_dynosaur.py",
            "tiktok_live/algorithms/x_gnarly.py",
            "tiktok_live/algorithms/browser_env.json",
        }
        missing = sorted(path for path in expected if not root.joinpath(path).is_file())
        self.assertEqual(missing, [])

    def test_modules_import_without_running_network_or_browser(self):
        for name in (
            "tiktok_live.account_store",
            "tiktok_live.browser_auth",
            "tiktok_live.signing",
            "tiktok_live.client",
            "tiktok_live.runner",
            "tiktok_live.cli",
        ):
            with self.subTest(name=name):
                importlib.import_module(name)

    def test_compatibility_entrypoint_keeps_public_api(self):
        subject = importlib.import_module("send_live_message")
        for name in (
            "send_live_message",
            "run_saved_accounts",
            "login_new_account",
            "validate_saved_account",
            "load_account_store",
            "save_account_store_atomic",
            "AccountValidation",
        ):
            self.assertTrue(hasattr(subject, name), name)

    def test_compatibility_entrypoint_uses_explicit_imports(self):
        root = Path(__file__).resolve().parents[1]
        tree = ast.parse(root.joinpath("send_live_message.py").read_text(encoding="utf-8"))
        wildcard_imports = [
            node.module
            for node in ast.walk(tree)
            if isinstance(node, ast.ImportFrom)
            and any(alias.name == "*" for alias in node.names)
        ]
        self.assertEqual(wildcard_imports, [])

    def test_user_settings_are_imported_from_their_defining_module(self):
        root = Path(__file__).resolve().parents[1]
        tree = ast.parse(root.joinpath("send_live_message.py").read_text(encoding="utf-8"))
        imported_from_settings = {
            alias.name
            for node in ast.walk(tree)
            if isinstance(node, ast.ImportFrom) and node.module == "settings"
            for alias in node.names
        }
        self.assertEqual(imported_from_settings, {
            "ACCOUNT_REQUEST_INTERVAL_SECONDS",
            "ADD_NEW_ACCOUNT",
            "LOGIN_TIMEOUT_SECONDS",
            "MANUAL_INPUT",
            "MESSAGE_OBSERVATION_ENABLED",
            "MESSAGE_OBSERVATION_TIMEOUT_SECONDS",
            "MESSAGE_NOT_OBSERVED_RETRIES",
            "MESSAGE_NOT_OBSERVED_RETRY_DELAY_SECONDS",
            "MESSAGE_OBSERVER_CONNECT_TIMEOUT_SECONDS",
            "MESSAGE_OBSERVER_CONNECT_RETRIES",
            "MESSAGE_OBSERVER_RETRY_DELAY_SECONDS",
            "SHOW_LIVE_COMMENTS_IN_CONSOLE",
            "ROOM_ID_COOKIE",
        })


if __name__ == "__main__":
    unittest.main()

