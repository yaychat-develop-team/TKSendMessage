import threading
from datetime import datetime, timezone
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch


class RequestFingerprintTests(unittest.TestCase):
    def test_account_room_task_picks_once_before_initialization_and_reuses_in_nested_calls(self):
        from tiktok_live.request_fingerprint import request_flow, current_fingerprint
        from tiktok_live import parallel_runner
        from tiktok_live.account_store import load_account_store, upsert_account, save_account_store_atomic
        observed = []
        def run(room, message, **kwargs):
            first = current_fingerprint()
            with request_flow():
                self.assertEqual(current_fingerprint(), first)
            observed.append(first)
            return {'success': 1}
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            path = root/'accounts.json'
            store = load_account_store(path)
            for name in ('one','two'):
                upsert_account(store, [{'name':'uid_tt','value':'synthetic-'+name,'domain':'.tiktok.com'}], datetime.now(timezone.utc))
            save_account_store_atomic(path, store)
            with patch('tiktok_live.request_fingerprint._choose', side_effect=['chrome120','safari153','firefox147','chrome123']), \
                    patch.object(parallel_runner, 'run_saved_accounts', side_effect=run):
                result = parallel_runner.run_parallel_targets(('first','second'), 'hello',
                    store_path=path, temporary_root=root/'tmp', report_dir=root/'reports',
                    selected_account_ids=None, message_count=1, message_interval_seconds=.1,
                    should_cancel=lambda:False, on_progress=None, runner_kwargs={'interval_seconds':0},
                    room_execution_mode='parallel')
        self.assertEqual(result['success'], 4)
        self.assertEqual(set(observed), {'chrome120','safari153','firefox147','chrome123'})

    def test_requested_pool_is_supported_by_installed_native_library_without_network(self):
        from curl_cffi import Curl
        from tiktok_live.request_fingerprint import FINGERPRINTS
        expected = {'chrome120', 'chrome123', 'chrome124', 'chrome131', 'chrome133a', 'chrome136',
                    'chrome142', 'chrome145', 'safari153', 'safari155', 'safari170', 'safari172_ios',
                    'safari180', 'safari180_ios', 'safari184', 'safari184_ios', 'safari260',
                    'safari2601', 'safari260_ios', 'firefox133', 'firefox135', 'firefox144', 'firefox147'}
        self.assertEqual(set(FINGERPRINTS), expected)
        for fingerprint in FINGERPRINTS:
            with self.subTest(fingerprint=fingerprint):
                curl = Curl()
                try:
                    self.assertEqual(curl.impersonate(fingerprint, default_headers=False), 0)
                finally:
                    curl.close()

    def test_nested_flow_and_repeated_requests_keep_one_choice_without_changing_ua(self):
        from tiktok_live.request_fingerprint import request_flow, current_fingerprint
        from tiktok_live.config import browser_profile_from_user_agent
        ua = 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/145.0.0.0 Safari/537.36'
        with patch('tiktok_live.request_fingerprint._choose', side_effect=['safari2601', 'firefox147']):
            with request_flow():
                self.assertEqual(current_fingerprint(), 'safari2601')
                for _ in range(5):
                    with request_flow():
                        self.assertEqual(current_fingerprint(), 'safari2601')
                        profile = browser_profile_from_user_agent(ua)
                        self.assertEqual(profile.user_agent, ua)
                        self.assertEqual(profile.browser_version, ua.removeprefix('Mozilla/'))
            with request_flow():
                self.assertEqual(current_fingerprint(), 'firefox147')

    def test_parallel_flows_are_isolated(self):
        from tiktok_live.request_fingerprint import request_flow, current_fingerprint
        barrier = threading.Barrier(3)
        results = []
        def run():
            with request_flow():
                first = current_fingerprint()
                barrier.wait(2)
                results.append((first, current_fingerprint()))
        with patch('tiktok_live.request_fingerprint._choose', side_effect=['chrome120', 'safari260', 'firefox144']):
            threads = [threading.Thread(target=run) for _ in range(3)]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join(3)
                self.assertFalse(thread.is_alive())
        self.assertEqual({a for a, b in results}, {'chrome120', 'safari260', 'firefox144'})
        self.assertTrue(all(a == b for a, b in results))
