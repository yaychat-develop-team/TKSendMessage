"""Legacy calls must not poison later modular client calls."""
import unittest
from concurrent.futures import ThreadPoolExecutor
from threading import Event
from unittest.mock import patch

import send_live_message as legacy
from tiktok_live import client, runner


class LegacyEntrypointTests(unittest.TestCase):
    def test_concurrent_legacy_calls_never_replace_public_dependencies(self):
        original = client.sign_url
        self.addCleanup(setattr, client, 'sign_url', original)
        entered = [Event(), Event()]
        release = [Event(), Event()]
        def build(index):
            entered[index].set()
            if not release[index].wait(5):
                raise TimeoutError('synthetic worker stalled')
            return index
        with patch.object(legacy, 'sign_url', return_value='synthetic'), \
                patch.object(legacy, '_ORIGINAL_BUILD_CHAT_REQUEST', side_effect=build), \
                ThreadPoolExecutor(max_workers=2) as pool:
            first = pool.submit(legacy.build_chat_request, 0)
            second = None
            try:
                self.assertTrue(entered[0].wait(5))
                second = pool.submit(legacy.build_chat_request, 1)
                self.assertTrue(entered[1].wait(5))
                self.assertIs(client.sign_url, original)
                release[0].set()
                self.assertEqual(first.result(5), 0)
                release[1].set()
                self.assertEqual(second.result(5), 1)
            finally:
                for event in release:
                    event.set()
        self.assertIs(client.sign_url, original)

    def test_send_restores_modular_entrypoint_after_exception(self):
        original = client._send_live_message
        self.addCleanup(setattr, client, '_send_live_message', original)
        with patch.object(legacy, '_send_live_message', side_effect=RuntimeError('synthetic')):
            with self.assertRaises(RuntimeError):
                legacy.send_live_message('host', 'synthetic', 'hello')
        self.assertIs(client._send_live_message, original)

    def test_send_forwards_current_room_context(self):
        captured = {}
        def send(*args, **kwargs):
            captured.update(kwargs)
            return {'ok': True}
        with patch.object(legacy, '_ORIGINAL_CLIENT_SEND', side_effect=send):
            result = legacy._send_live_message('host', 'synthetic', 'hello',
                known_fetch_endpoint='https://webcast.us.tiktok.com/webcast/im/fetch/',
                known_region='US', known_device_id='7000000000000000001')
        self.assertEqual(result, {'ok': True})
        self.assertEqual(captured['known_region'], 'US')
        self.assertEqual(captured['known_device_id'], '7000000000000000001')
        self.assertEqual(captured['known_fetch_endpoint'],
                         'https://webcast.us.tiktok.com/webcast/im/fetch/')

    def test_helpers_restore_dependencies_after_exception(self):
        original = client.sign_url
        self.addCleanup(setattr, client, 'sign_url', original)
        with patch.object(legacy, 'sign_url', return_value='synthetic'), \
                patch.object(legacy, '_ORIGINAL_BUILD_CHAT_REQUEST', side_effect=ValueError('synthetic')):
            with self.assertRaises(ValueError):
                legacy.build_chat_request()
        self.assertIs(client.sign_url, original)

    def test_runner_restores_dependencies_after_exception(self):
        originals = (runner.send_live_message, runner.login_new_account)
        self.addCleanup(setattr, runner, 'send_live_message', originals[0])
        self.addCleanup(setattr, runner, 'login_new_account', originals[1])
        with patch.object(legacy, 'send_live_message'), \
                patch.object(runner, 'run_saved_accounts', side_effect=ValueError('synthetic')):
            with self.assertRaises(ValueError):
                legacy.run_saved_accounts('host', 'hello', observation_enabled=False)
        self.assertIs(runner.send_live_message, originals[0])
        self.assertIs(runner.login_new_account, originals[1])
