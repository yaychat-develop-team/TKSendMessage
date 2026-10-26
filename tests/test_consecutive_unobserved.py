"""Only three consecutive distinct misses stop one account in one room."""
from contextlib import nullcontext
from datetime import datetime, timezone
import json
from pathlib import Path
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

from loguru import logger
from tests.test_realtime_delivery import PassiveListener
from tiktok_live import runner, parallel_runner
from tiktok_live.account_store import load_account_store, upsert_account, save_account_store_atomic


class ConsecutiveUnobservedTests(unittest.TestCase):
    def guard(self):
        from tiktok_live.delivery_guard import ConsecutiveUnobserved
        self.stops = []
        return ConsecutiveUnobserved(on_stop=lambda: self.stops.append('stopped'))

    def test_three_distinct_consecutive_misses_stop_once(self):
        guard = self.guard()
        for index in (1, 2):
            guard.record(index, 'not_observed')
            self.assertFalse(guard.stopped)
        guard.record(3, 'not_observed')
        self.assertTrue(guard.stopped)
        guard.record(4, 'not_observed')
        self.assertEqual(self.stops, ['stopped'])

    def test_observed_or_other_result_resets_instead_of_accumulating_misses(self):
        for separator in ('observed', 'listener_unavailable', 'send_failed'):
            with self.subTest(separator=separator):
                guard = self.guard()
                for index, status in enumerate(('not_observed', 'not_observed', separator,
                                                'not_observed', 'not_observed'), 1):
                    guard.record(index, status)
                self.assertFalse(guard.stopped)
                self.assertEqual(guard.streak, 2)
                guard.record(6, 'not_observed')
                self.assertTrue(guard.stopped)

    def test_duplicate_logs_and_retries_do_not_count_the_same_message_again(self):
        guard = self.guard()
        for _ in range(5):
            guard.record(1, 'not_observed')
        self.assertEqual(guard.streak, 1)
        self.assertFalse(guard.stopped)
        guard.record(2, 'observed')
        guard.record(1, 'not_observed')
        self.assertEqual(guard.streak, 0)
        guard.record(3, 'not_observed')
        guard.record(2, 'observed')  # A repeated success must not reset a later streak.
        self.assertEqual(guard.streak, 1)

    def test_late_success_resets_before_threshold_but_does_not_resume_after_stop(self):
        guard = self.guard()
        guard.record(1, 'not_observed')
        guard.record(2, 'not_observed')
        guard.record(1, 'observed')
        guard.record(3, 'not_observed')
        guard.record(4, 'not_observed')
        self.assertFalse(guard.stopped)
        guard.record(5, 'not_observed')
        self.assertTrue(guard.stopped)
        guard.record(5, 'observed')
        self.assertTrue(guard.stopped)

    def test_new_task_starts_clean_and_concurrent_duplicate_notices_stop_once(self):
        from tiktok_live.delivery_guard import ConsecutiveUnobserved
        guard = self.guard()
        threads = [threading.Thread(target=guard.record, args=(index, 'not_observed'))
                   for index in (1, 1, 2, 2, 3, 3)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(1)
            self.assertFalse(thread.is_alive())
        self.assertTrue(guard.stopped)
        self.assertEqual(len(self.stops), 1)
        other = ConsecutiveUnobserved(on_stop=lambda: None)
        other.record(1, 'not_observed')
        self.assertFalse(other.stopped)


class ConsecutiveRunnerTests(unittest.TestCase):
    def test_real_observed_receipt_breaks_the_streak_without_counting_total_misses(self):
        listener = PassiveListener('host', show_comments=False)
        posts = []
        missed = {i: threading.Event() for i in range(1, 13)}
        def log(msg):
            text = msg.record['message']
            if '监听期内未发现实际弹幕' in text and 'message_index=' in text:
                missed[int(text.split('message_index=')[1].split('/')[0])].set()
        handle = logger.add(log)
        self.addCleanup(logger.remove, handle)
        def pause(*args):
            if len(posts) not in (3, 6):
                self.assertTrue(missed[len(posts)].wait(3), 'expected real timeout notice')
        def send(*args, **kwargs):
            posts.append(len(posts)+1)
            number = len(posts)
            if number in (3, 6):
                listener.record_comment('hello', unique_id='one', message_id=str(number))
            return {'ok': True, 'response': {'data': {'msg_id': str(number)}}}
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)/'accounts.json'
            store = load_account_store(path)
            account = upsert_account(store, [{'name':'uid_tt','value':'synthetic-one','domain':'.tiktok.com'}], datetime.now(timezone.utc))
            account['username'] = 'one'
            save_account_store_atomic(path, store)
            with patch.object(runner, '_start_live_listener', return_value=listener), \
                    patch.object(runner, 'room_heartbeat_session', return_value=nullcontext()), \
                    patch.object(runner, 'send_live_message', side_effect=send), \
                    patch.object(runner, '_pause', side_effect=pause):
                result = runner.run_saved_accounts('host', 'hello', path, message_count=12,
                    observation_timeout_seconds=.02, observation_retries=1,
                    interval_seconds=0, on_progress=lambda *a: None)
        self.assertEqual(len(posts), 9)
        self.assertEqual(result['sent'], 9)
        self.assertEqual(result['processed'], 9)
        self.assertEqual(result['delivered'], 2)
        self.assertEqual(result['acceptedNotObserved'], 7)

    def test_real_time_third_miss_stops_only_affected_account_room_and_records_reason(self):
        for mode, attempts in (('serial', 1), ('parallel', 1), ('serial', 3), ('parallel', 3)):
            with self.subTest(mode=mode, attempts=attempts), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                path = root / 'accounts.json'
                store = load_account_store(path)
                for name in ('one', 'two'):
                    account = upsert_account(store, [{'name': 'uid_tt', 'value': 'synthetic-' + name,
                        'domain': '.tiktok.com'}], datetime.now(timezone.utc))
                    account['username'] = name
                save_account_store_atomic(path, store)
                before = path.read_bytes()
                local = threading.local()
                posts, snapshots, logs = {}, [], []
                mutex = threading.Lock()
                missed = {i: threading.Event() for i in range(1, 6)}
                def log(msg):
                    text = msg.record['message']
                    logs.append(text)
                    extra = msg.record['extra']
                    if (extra.get('target_room') == 'first' and extra.get('sender_account') == 'one'
                            and '监听期内未发现实际弹幕' in text and 'message_index=' in text):
                        missed[int(text.split('message_index=')[1].split('/')[0])].set()
                handle = logger.add(log)
                def listen(room, **kwargs):
                    local.listener = PassiveListener(room, show_comments=False)
                    return local.listener
                def send(room, cookie, text, **kwargs):
                    name = kwargs['account_display_name']
                    with mutex:
                        key = (room, name)
                        posts[key] = posts.get(key, 0) + 1
                        number = posts[key]
                    local.key, local.number = key, number
                    if key != ('first', 'one'):
                        local.listener.record_comment(text, unique_id=name, message_id=str(number))
                    return {'ok': True, 'response': {'data': {'msg_id': str(number)}}}
                def pause(*args):
                    if getattr(local, 'key', None) == ('first', 'one'):
                        self.assertTrue(missed[local.number].wait(3), 'expected real timeout notice')
                try:
                    with patch.object(runner, '_start_live_listener', side_effect=listen), \
                            patch.object(runner, 'room_heartbeat_session', side_effect=lambda *a, **kw: nullcontext()), \
                            patch.object(runner, 'send_live_message', side_effect=send), \
                            patch.object(runner, '_pause', side_effect=pause):
                        result = parallel_runner.run_parallel_targets(('first', 'second'), 'hello',
                            store_path=path, temporary_root=root/'tmp', report_dir=root/'reports',
                            selected_account_ids=None, message_count=5, message_interval_seconds=.1,
                            should_cancel=lambda: False, on_progress=snapshots.append,
                            runner_kwargs={'interval_seconds': 0, 'observation_timeout_seconds': .02,
                                           'observation_retries': attempts}, room_execution_mode=mode)
                finally:
                    logger.remove(handle)
                self.assertEqual(posts, {('first', 'one'): 3, ('first', 'two'): 5,
                                         ('second', 'one'): 5, ('second', 'two'): 5})
                self.assertFalse(result['cancelled'])
                self.assertEqual(result['sent'], 18)
                self.assertEqual(result['processed'], 18)
                self.assertEqual(result['acceptedNotObserved'], 3)
                self.assertEqual(result['rooms'][0]['accounts'][0]['status'], 'failed')
                self.assertIn('consecutive_not_observed', result['rooms'][0]['accounts'][0]['failure_reasons'])
                self.assertEqual(result['rooms'][0]['accounts'][1]['status'], 'completed')
                self.assertEqual(result['rooms'][1]['status'], 'completed')
                self.assertNotIn('consecutive_not_observed', result['rooms'][1]['accounts'][0]['failure_reasons'])
                self.assertEqual(path.read_bytes(), before)
                self.assertEqual(sum('classification=consecutive_not_observed' in line and '连续' in line
                                     for line in logs), 1)
                reports = [record for file in (root/'reports').rglob('failed_accounts.json')
                           for record in json.loads(file.read_text(encoding='utf-8'))['failed_accounts']]
                self.assertTrue(any(record['classification'] == 'consecutive_not_observed' for record in reports))

    def test_inflight_request_is_counted_but_no_next_post_starts_after_third_notice(self):
        listener = PassiveListener('host', show_comments=False)
        stopped = threading.Event()
        posts, logs = [], []
        def log(msg):
            text = msg.record['message']
            logs.append(text)
            if '连续' in text and 'classification=consecutive_not_observed' in text:
                stopped.set()
        handle = logger.add(log)
        self.addCleanup(logger.remove, handle)
        def send(*args, **kwargs):
            posts.append(len(posts) + 1)
            if len(posts) == 4:
                self.assertTrue(stopped.wait(2), 'third timeout did not stop during in-flight POST')
            return {'ok': True, 'response': {'data': {'msg_id': str(len(posts))}}}
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)/'accounts.json'
            store = load_account_store(path)
            account = upsert_account(store, [{'name':'uid_tt','value':'synthetic-one','domain':'.tiktok.com'}], datetime.now(timezone.utc))
            account['username'] = 'one'
            save_account_store_atomic(path, store)
            with patch.object(runner, '_start_live_listener', return_value=listener), \
                    patch.object(runner, 'room_heartbeat_session', return_value=nullcontext()), \
                    patch.object(runner, 'send_live_message', side_effect=send), patch.object(runner, '_pause'):
                result = runner.run_saved_accounts('host', 'hello', path, message_count=8,
                    observation_timeout_seconds=.1, observation_retries=3,
                    interval_seconds=0, on_progress=lambda *a: None)
        self.assertEqual(posts, [1, 2, 3, 4])
        self.assertEqual(result['sent'], 4)
        self.assertEqual(result['processed'], 4)
        self.assertEqual(result['acceptedNotObserved'], 4)
        self.assertEqual(result['failed'], 0)
        self.assertFalse(result['cancelled'])
        self.assertIn('consecutive_not_observed', result['failure_reasons'])


class ConsecutivePanelTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from PySide6.QtWidgets import QApplication
        cls.app = QApplication.instance() or QApplication([])

    def test_stop_reason_survives_safe_boundary_and_is_not_called_banned(self):
        from desktop.account_view import AccountTable, AccountSummary
        table = AccountTable()
        self.addCleanup(table.close)
        table.set_accounts([AccountSummary('one', 'a'*12, 'a'*64)])
        table.begin_delivery(('a'*64,), 10)
        snapshot = parallel_runner.safe_parallel_result({'rooms': [{'target': 'host', 'accounts': [
            {'id': 'a'*12, 'sent': 3, 'success': 3, 'acceptedNotObserved': 3, 'status': 'failed',
             'failure_reasons': ['consecutive_not_observed']}]}]})
        table.update_delivery(snapshot, finished=True)
        self.assertIn('连续未确认停发', table.item(0, 4).text())
        text = table.delivery_details('a'*64)
        self.assertIn('consecutive_not_observed', text)
        self.assertIn('连续 3', text)
        self.assertNotIn('已禁言', table.item(0, 4).text())
