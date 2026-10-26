import importlib
import json
import tempfile
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

from loguru import logger
from tiktok_live.account_store import load_account_store, save_account_store_atomic, upsert_account


class ParallelRunnerTests(unittest.TestCase):
    def test_failure_reasons_only_forward_known_codes_and_are_bounded(self):
        from tiktok_live.errors import safe_failure_reasons
        reasons = safe_failure_reasons(['initial_fetch_unavailable', 'cookie=secret',
                                       {'secret': 'value'}, 'initial_fetch_unavailable'])
        self.assertEqual(reasons, ['initial_fetch_unavailable', 'runtime_error'])
        self.assertNotIn('secret', repr(reasons))
        self.assertEqual(safe_failure_reasons('cookie=secret'), [])
        self.assertEqual(safe_failure_reasons(['send_failed'] * 32 + ['expired']), ['send_failed'])

    def setUp(self):
        self.module = importlib.import_module('tiktok_live.parallel_runner')
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.path = self.root / 'accounts.json'
        store = load_account_store(self.path)
        for name in ('one', 'two'):
            account = upsert_account(store, [{'name': 'uid_tt', 'value': 'synthetic-' + name,
                'domain': '.tiktok.com'}], datetime.now(timezone.utc))
            account['username'] = name
        save_account_store_atomic(self.path, store)
        self.ids = tuple(item['id'] for item in store['accounts'])
        self.cancel = threading.Event()

    def run_targets(self, targets=('room1',), **kwargs):
        return self.module.run_parallel_targets(targets, 'hello', store_path=self.path,
            temporary_root=self.root / 'temporary', report_dir=self.root / 'reports',
            selected_account_ids=self.ids, message_count=2, message_interval_seconds=0.5,
            should_cancel=self.cancel.is_set, runner_kwargs={'observation_enabled': False},
            on_progress=kwargs.get('on_progress'), room_execution_mode=kwargs.get('room_execution_mode', 'serial'))

    def launch(self, fake, targets, **kwargs):
        output = {}
        def run():
            try:
                output['result'] = self.run_targets(targets, **kwargs)
            except BaseException as exc:
                output['exception'] = exc
        patcher = patch.object(self.module, 'run_saved_accounts', side_effect=fake)
        patcher.start()
        self.addCleanup(patcher.stop)
        thread = threading.Thread(target=run)
        thread.start()
        return thread, output

    def finish(self, thread, output):
        thread.join(10)
        self.assertFalse(thread.is_alive(), 'coordinator did not drain')
        if 'exception' in output:
            raise output['exception']
        return output['result']

    def test_serial_accounts_advance_independently_after_their_own_cleanup(self):
        entered = {room: [threading.Event(), threading.Event()] for room in ('a', 'b', 'c', 'd')}
        releases = {room: [threading.Event(), threading.Event()] for room in entered}
        exited_first = threading.Event()
        def fake(room, message, **kw):
            index = self.ids.index(kw['selected_account_ids'][0])
            entered[room][index].set()
            self.assertTrue(releases[room][index].wait(10))
            if room == 'a' and index == 0:
                exited_first.set()
            return {'success': 2}
        thread, output = self.launch(fake, tuple(entered))
        try:
            for event in entered['a']:
                self.assertTrue(event.wait(5), 'accounts in one room must start together')
            self.assertFalse(any(event.is_set() for room in ('b','c','d') for event in entered[room]))
            releases['a'][0].set()
            self.assertTrue(exited_first.wait(5))
            self.assertTrue(entered['b'][0].wait(5), 'fast account waited for slow account')
            self.assertFalse(entered['b'][1].is_set(), 'slow account overlapped its own rooms')
            releases['a'][1].set()
            for room in ('b','c','d'):
                for event in entered[room]:
                    self.assertTrue(event.wait(5))
                for event in releases[room]:
                    event.set()
        finally:
            for events in releases.values():
                for event in events:
                    event.set()
            result = self.finish(thread, output)
        self.assertEqual(result['success'], 16)
        self.assertEqual(result['status'], 'completed')

    def test_parallel_mode_holds_three_room_slots_until_all_accounts_drain(self):
        entered = {room: [threading.Event(), threading.Event()] for room in ('a','b','c','d')}
        release = {room: [threading.Event(), threading.Event()] for room in entered}
        first_exited = threading.Event()
        paths = []
        def fake(room, message, **kw):
            index = self.ids.index(kw['selected_account_ids'][0])
            paths.append(kw['store_path'])
            entered[room][index].set()
            self.assertTrue(release[room][index].wait(5))
            if room == 'a' and index == 0:
                first_exited.set()
            return {'success': 2, 'delivered': 2}
        thread, output = self.launch(fake, tuple(entered), room_execution_mode='parallel')
        try:
            for room in ('a','b','c'):
                for event in entered[room]:
                    self.assertTrue(event.wait(3), 'parallel rooms did not start')
            self.assertFalse(any(e.is_set() for e in entered['d']))
            release['a'][0].set()
            self.assertTrue(first_exited.wait(3))
            self.assertFalse(entered['d'][0].wait(.05), 'room slot released before cleanup')
            release['a'][1].set()
            for event in entered['d']:
                self.assertTrue(event.wait(3))
        finally:
            for events in release.values():
                for event in events:
                    event.set()
            result = self.finish(thread, output)
        self.assertEqual(result['delivered'], 16)
        self.assertEqual([r['target'] for r in result['rooms']], ['a','b','c','d'])
        self.assertEqual(len(set(paths)), 8)
        self.assertTrue(all(not p.exists() for p in paths))

    def test_parallel_cancel_does_not_start_queued_fourth_room(self):
        barrier = threading.Barrier(7)
        started = []
        def fake(room, message, **kw):
            started.append(room)
            barrier.wait(5)
            self.assertTrue(self.cancel.wait(5))
            return {'cancelled': kw['should_cancel']()}
        thread, output = self.launch(fake, ('a','b','c','d'), room_execution_mode='parallel')
        try:
            barrier.wait(5)
        finally:
            self.cancel.set()
        result = self.finish(thread, output)
        self.assertEqual(sorted(started), ['a','a','b','b','c','c'])
        self.assertEqual(result['rooms'][3]['status'], 'cancelled')
        self.assertTrue(result['cancelled'])

    def test_parallel_room_restriction_only_stops_that_rooms_accounts(self):
        barrier = threading.Barrier(6)
        restricted = threading.Event()
        def fake(room, message, **kw):
            index = self.ids.index(kw['selected_account_ids'][0])
            if room in ('a','b','c'):
                barrier.wait(5)
            if room == 'a':
                if index == 0:
                    kw['on_progress']({'failed': 1, 'failure_reasons': ['room_comment_restricted']})
                    restricted.set()
                    return {'failed': 1, 'failure_reasons': ['room_comment_restricted']}
                self.assertTrue(restricted.wait(5))
                self.assertTrue(kw['should_cancel']())
                return {'cancelled': True}
            self.assertTrue(restricted.wait(5))
            self.assertFalse(kw['should_cancel']())
            return {'success': 2, 'delivered': 2}
        with patch.object(self.module, 'run_saved_accounts', side_effect=fake):
            result = self.run_targets(('a','b','c','d'), room_execution_mode='parallel')
        self.assertFalse(result['cancelled'])
        self.assertEqual(result['delivered'], 12)
        self.assertEqual(result['rooms'][0]['status'], 'failed')
        for room in result['rooms'][1:]:
            self.assertEqual(room['status'], 'completed')
            self.assertTrue(all(not a.get('failure_reasons') for a in room['accounts']))

    def test_single_room_is_parallel_with_private_stores_reports_and_log_context(self):
        barrier = threading.Barrier(2)
        paths, reports, records = [], [], []
        handler = logger.add(lambda item: records.append(item.record['extra'].copy()))
        self.addCleanup(lambda: logger.remove(handler))
        def fake(room, message, **kw):
            account_id, = kw['selected_account_ids']
            private = load_account_store(kw['store_path'])
            self.assertEqual([item['id'] for item in private['accounts']], [account_id])
            self.assertEqual((room, message, kw['message_count'], kw['message_interval_seconds']),
                             ('room1', 'hello', 2, 0.5))
            self.assertFalse(kw['add_new_account'])
            self.assertFalse(kw['observation_enabled'])
            paths.append(kw['store_path'])
            reports.append(kw['report_dir'])
            logger.info('task context')
            barrier.wait(5)
            return {'success': 2, 'cookies': 'synthetic-secret'}
        with patch.object(self.module, 'run_saved_accounts', side_effect=fake):
            result = self.run_targets()
        self.assertEqual(result['success'], 4)
        self.assertEqual(len(set(paths)), 2)
        self.assertEqual(len(set(reports)), 2)
        self.assertTrue(all(not path.exists() for path in paths))
        self.assertEqual({r['target_room'] for r in records}, {'room1'})
        self.assertEqual({r['sender_account'] for r in records}, {'one', 'two'})
        self.assertTrue(all(r['sender_id'] for r in records))
        self.assertNotIn('synthetic', json.dumps(result))

    def test_cancel_drains_running_tasks_and_never_starts_fourth_room(self):
        barrier = threading.Barrier(3)
        started, closed = [], []
        def fake(room, message, **kw):
            started.append(room)
            barrier.wait(5)
            self.assertTrue(self.cancel.wait(5))
            self.assertTrue(kw['should_cancel']())
            closed.append(room)
            return {'success': 1, 'cancelled': True}
        thread, output = self.launch(fake, ('a', 'b', 'c', 'd'))
        try:
            barrier.wait(5)
        finally:
            self.cancel.set()
        result = self.finish(thread, output)
        self.assertEqual(sorted(started), ['a', 'a'])
        self.assertEqual(sorted(closed), sorted(started))
        self.assertEqual(result['success'], 2)
        self.assertEqual(result['rooms'][3]['status'], 'cancelled')
        self.assertEqual(result['status'], 'cancelled')

    def test_latest_cumulative_progress_is_summed_once_and_callbacks_are_serial(self):
        barrier = threading.Barrier(2)
        callback_lock = threading.Lock()
        progress = []
        def observe(snapshot):
            self.assertTrue(callback_lock.acquire(blocking=False), 'callbacks overlapped')
            try:
                progress.append(snapshot)
            finally:
                callback_lock.release()
        def fake(room, message, **kw):
            barrier.wait(5)
            for count in (1, 1, 2):
                kw['on_progress']({'success': count, 'cookies': 'synthetic-secret'})
            return {'success': 2}
        with patch.object(self.module, 'run_saved_accounts', side_effect=fake):
            result = self.run_targets(on_progress=observe)
        values = [item['success'] for item in progress]
        self.assertEqual(values, sorted(values))
        self.assertEqual(result['success'], 4)
        self.assertEqual(progress[-1], result)
        self.assertNotIn('synthetic', json.dumps(progress))

    def test_account_failure_preserves_counts_skips_collection_and_other_tasks_finish(self):
        original = self.module.IsolatedAccountRuns.collect_outcome
        collected = []
        def collect(runs, account_id, path, counters):
            collected.append((account_id, counters))
            return original(runs, account_id, path, counters)
        def fake(room, message, **kw):
            kw['on_progress']({'success': 1})
            if room == 'a' and kw['selected_account_ids'] == (self.ids[0],):
                raise RuntimeError('Cookie: synthetic-secret')
            return {'success': 2}
        with patch.object(self.module, 'run_saved_accounts', side_effect=fake), \
                patch.object(self.module.IsolatedAccountRuns, 'collect_outcome', collect):
            result = self.run_targets(('a', 'b'))
        self.assertEqual(result['success'], 7)
        self.assertEqual(result['failed'], 0)
        self.assertEqual(result['failed_tasks'], 1)
        self.assertEqual(result['status'], 'failed')
        self.assertEqual(result['rooms'][1]['status'], 'completed')
        self.assertEqual(len(collected), 3)
        self.assertNotIn('synthetic', json.dumps(result))
        self.assertEqual(result['rooms'][0]['accounts'][0]['error'],
                         {'type': 'RuntimeError', 'classification': 'runtime_error'})

    def test_submission_failure_cancels_and_drains_started_task_before_commit(self):
        started, closed = threading.Event(), threading.Event()
        original_commit = self.module.IsolatedAccountRuns.commit
        class BrokenExecutor(ThreadPoolExecutor):
            def submit(inner, fn, *args, **kwargs):
                if inner._max_workers == 2 and getattr(inner, 'submitted', False):
                    self.assertTrue(started.wait(5))
                    raise RuntimeError('synthetic-submit-secret')
                inner.submitted = True
                return super().submit(fn, *args, **kwargs)
        def fake(room, message, **kw):
            started.set()
            try:
                for _ in range(500):
                    if kw['should_cancel']():
                        return {'cancelled': True}
                    closed.wait(0.01)
                self.fail('submission failure did not cancel running task')
            finally:
                closed.set()
        def commit(runs):
            self.assertTrue(closed.is_set(), 'commit happened before runner cleanup')
            return original_commit(runs)
        with patch.object(self.module, 'ThreadPoolExecutor', BrokenExecutor), \
                patch.object(self.module, 'run_saved_accounts', side_effect=fake), \
                patch.object(self.module.IsolatedAccountRuns, 'commit', commit):
            result = self.run_targets()
        self.assertTrue(closed.is_set())
        self.assertEqual(result['status'], 'failed')
        self.assertEqual(result['failed_tasks'], 1)
        self.assertNotIn('synthetic', json.dumps(result))

    def test_normal_failed_counts_mark_task_failed_but_unobserved_success_is_completed(self):
        def fake(room, message, **kw):
            if kw['selected_account_ids'] == (self.ids[0],):
                return {'failed': 1, 'expired': 1}
            return {'success': 2, 'acceptedNotObserved': 2}
        with patch.object(self.module, 'run_saved_accounts', side_effect=fake):
            result = self.run_targets()
        self.assertEqual(result['status'], 'failed')
        self.assertEqual(result['failed_tasks'], 1)
        self.assertEqual(result['processed'], 4)
        self.assertEqual(result['delivered'], 0)
        self.assertEqual(result['rooms'][0]['accounts'][1]['status'], 'completed')
        self.assertEqual(result['acceptedNotObserved'], 2)

    def test_collection_failure_preserves_normally_returned_final_counts(self):
        with patch.object(self.module, 'run_saved_accounts', return_value={'success': 2}), \
                patch.object(self.module.IsolatedAccountRuns, 'collect_outcome',
                             side_effect=OSError('synthetic-collect-secret')):
            result = self.run_targets()
        self.assertEqual(result['success'], 4)
        self.assertEqual(result['failed_tasks'], 2)
        self.assertEqual(result['status'], 'failed')
        self.assertNotIn('synthetic', json.dumps(result))

    def test_cancel_drains_current_room_before_commit_and_skips_next(self):
        started, closed = [], []
        barrier = threading.Barrier(3)
        original_commit = self.module.IsolatedAccountRuns.commit
        def fake(room, message, **kw):
            started.append(room)
            barrier.wait(5)
            self.assertTrue(self.cancel.wait(5))
            closed.append(room)
            return {'success': 1, 'cancelled': True}
        def commit(runs):
            self.assertEqual(closed,['a','a'])
            return original_commit(runs)
        with patch.object(self.module.IsolatedAccountRuns, 'commit', commit):
            thread,output=self.launch(fake,('a','b'))
            try:
                barrier.wait(5)
            finally:
                self.cancel.set()
            result=self.finish(thread,output)
        self.assertEqual(started,['a','a'])
        self.assertEqual(result['status'],'cancelled')
        self.assertEqual(result['rooms'][1]['status'],'cancelled')

    def test_cancelled_before_launch_and_empty_store_never_call_runner(self):
        self.cancel.set()
        with patch.object(self.module, 'run_saved_accounts', side_effect=AssertionError('network')):
            result = self.run_targets(('a', 'b', 'c', 'd'))
        self.assertEqual(result['status'], 'cancelled')
        self.assertEqual(result['processed'], 0)
        self.assertTrue(all(room['status'] == 'cancelled' for room in result['rooms']))
        self.cancel.clear()
        save_account_store_atomic(self.path, {'version': 1, 'accounts': []})
        with patch.object(self.module, 'run_saved_accounts', side_effect=AssertionError('login')):
            with self.assertRaises(ValueError):
                self.run_targets()

    def test_room_executor_thread_also_establishes_explicit_log_context(self):
        records = []
        handler = logger.add(lambda item: records.append(item.record['extra'].copy()))
        self.addCleanup(lambda: logger.remove(handler))
        class LoggingExecutor(ThreadPoolExecutor):
            def __init__(inner, *args, **kwargs):
                if kwargs.get('max_workers') == 2:
                    logger.info('room executor context')
                super().__init__(*args, **kwargs)
        with patch.object(self.module, 'ThreadPoolExecutor', LoggingExecutor), \
                patch.object(self.module, 'run_saved_accounts', return_value={'success': 2}):
            result = self.run_targets(room_execution_mode='parallel')
        self.assertEqual(result['success'], 4)
        self.assertEqual(records, [{'target_room': 'room1', 'sender_account': '', 'sender_id': ''}])

    def test_post_commit_callback_failure_returns_failed_state_and_completed_counts(self):
        committed = threading.Event()
        terminal_callbacks = []
        original_commit = self.module.IsolatedAccountRuns.commit
        def commit(runs):
            original_commit(runs)
            committed.set()
        def observe(snapshot):
            if committed.is_set():
                terminal_callbacks.append(snapshot)
                raise RuntimeError('synthetic-final-callback-secret')
        with patch.object(self.module, 'run_saved_accounts', return_value={'success': 2}), \
                patch.object(self.module.IsolatedAccountRuns, 'commit', commit):
            result = self.run_targets(on_progress=observe)
        self.assertTrue(committed.is_set())
        self.assertEqual(len(terminal_callbacks), 1, 'failed callback must not be invoked again')
        self.assertEqual(result['status'], 'failed')
        self.assertTrue(result['cancelled'])
        self.assertEqual(result['success'], 4)
        self.assertEqual(result['processed'], 4)
        self.assertEqual(result['failed_tasks'], 0)
        self.assertEqual(result['rooms'][0]['status'], 'completed')
        self.assertNotIn('synthetic', json.dumps(result))


if __name__ == '__main__':
    unittest.main()
