import tempfile
import threading
import unittest
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from loguru import logger

from tiktok_live import runner, heartbeat, parallel_runner
from tiktok_live.account_store import load_account_store, upsert_account, save_account_store_atomic


class MessageBurstTests(unittest.TestCase):
    def run_burst(self, cancel_on_second=False, fail_on_second=False,
                  count=3, observed=True, disconnect_on_second=False,
                  observation_enabled=True, listener_lost=False, receipt_error=False,
                  raise_on_second=False, listener_start_failed=False,
                  malformed_response=False, save_failure=None, parallel=False):
        events = []
        cancelled = threading.Event()
        socket = SimpleNamespace(connected=True)
        @contextmanager
        def connected(*args, **kwargs):
            events.append('connected')
            try:
                yield SimpleNamespace(error=None, available=True,
                    connection=socket)
            finally:
                events.append('closed')
        class Result(dict):
            def retry_message(self):
                return post()
        posts = 0
        def post():
            nonlocal posts
            with heartbeat.room_heartbeat('host', '42', 'cookie', 'UA'):
                posts += 1
                events.append('post')
                if posts == 2 and cancel_on_second:
                    cancelled.set()
                if posts == 2 and fail_on_second:
                    return Result(ok=False, accountSilenced=True, statusCode=50001)
                if posts == 2 and save_failure == 'expiry':
                    return Result(ok=False, loginExpired=True,
                                  response={'data': {'msg_id': '2'}})
                if posts == 2 and raise_on_second:
                    raise RuntimeError('synthetic-private-error')
                if posts == 2 and disconnect_on_second:
                    socket.connected = False
                return Result(ok=True,
                              accountUsername='refreshed' if save_failure == 'response' else '',
                              response={'data': 'acknowledged'} if malformed_response
                              else {'data': {'msg_id': str(posts)}})
        def observe(*args, **kwargs):
            events.append('observe')
            self.assertNotIn('closed', events)
            if receipt_error:
                raise RuntimeError('synthetic-private-error')
            if listener_lost:
                listener.available = False
                return None
            seen = observed[len([event for event in events if event == 'observe']) - 1] if isinstance(observed, list) else observed
            self.assertEqual(kwargs.get('expected_message_ids'), {str(events.count('observe'))})
            return SimpleNamespace(unique_id='viewer') if seen else None
        listener = SimpleNamespace(room_id='42', available=True, error=None,
                                   wait_for_comment=observe, close=lambda: None)
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'accounts.json'
            store = load_account_store(path)
            upsert_account(store, [{'name': 'uid_tt', 'value': 'synthetic',
                                    'domain': '.tiktok.com'}], datetime.now(timezone.utc))
            save_account_store_atomic(path, store)
            original_store = path.read_bytes()
            def persist(save_path, saved_store):
                if posts and save_failure:
                    events.append('save_failed')
                    raise OSError('synthetic-private-error sessionid=private-cookie')
                save_account_store_atomic(save_path, saved_store)
            collect = parallel_runner.IsolatedAccountRuns.collect_outcome
            def collect_outcome(*args, **kwargs):
                events.append('collected')
                return collect(*args, **kwargs)
            with patch.object(heartbeat, '_room_heartbeat_connection', connected, create=True), \
                    patch.object(runner, 'send_live_message', side_effect=lambda *a, **kw: post()), \
                    patch.object(runner, '_start_live_listener', return_value=None if listener_start_failed else listener), \
                    patch.object(runner, 'save_account_store_atomic', side_effect=persist), \
                    patch.object(parallel_runner.IsolatedAccountRuns, 'collect_outcome', collect_outcome), \
                    patch.object(runner, '_pause', side_effect=lambda seconds, cancel: events.append(('pause', seconds))):
                if parallel:
                    result = parallel_runner.run_parallel_targets(('host',), 'hello',
                        store_path=path, temporary_root=Path(directory) / 'temporary',
                        report_dir=Path(directory) / 'reports', selected_account_ids=None,
                        message_count=count, message_interval_seconds=1,
                        should_cancel=cancelled.is_set,
                        on_progress=lambda snapshot: events.append(('progress', snapshot)),
                        runner_kwargs={'interval_seconds': 0,
                            'observation_enabled': observation_enabled, 'observation_retries': 1})
                    if save_failure:
                        self.assertEqual(path.read_bytes(), original_store)
                else:
                    result = runner.run_saved_accounts('host', 'hello', path,
                        message_count=count, message_interval_seconds=1,
                        should_cancel=cancelled.is_set, interval_seconds=0,
                        observation_enabled=observation_enabled, observation_retries=1)
        return result, events

    def assert_metadata_failure_preserves_completed(self, source, *, observed, posts, delivered, expired):
        lines = []
        handle = logger.add(lambda msg: lines.append(msg.record['message']))
        try:
            result, events = self.run_burst(save_failure=source, parallel=True, observed=observed)
        finally:
            logger.remove(handle)
        display = [line for line in lines if 'display_status=' in line]
        self.assertEqual(len(display), posts)
        for number, line in enumerate(display, start=1):
            self.assertIn(f'message_index={number}/3 | message_id={number} | ', line)
        self.assertEqual(events.count('post'), posts)
        self.assertEqual(result['processed'], posts)
        self.assertEqual(result['success'], posts - expired)
        self.assertEqual(result['expired'], expired)
        self.assertEqual(result['delivered'], delivered)
        self.assertEqual(result['acceptedNotObserved'], posts - expired - delivered)
        self.assertEqual(result['status'], 'failed')
        self.assertEqual(result['failed_tasks'], 1)
        account = result['rooms'][0]['accounts'][0]
        self.assertEqual(account['error']['classification'], 'runtime_error')
        self.assertEqual(account['error']['type'], '_SafeOperationError')
        self.assertNotIn('collected', events)
        self.assertIn('closed', events)
        progress = [event[1] for event in events if isinstance(event, tuple) and event[0] == 'progress']
        self.assertTrue(any(item['processed'] == posts and
                            item['rooms'][0]['accounts'][0]['status'] == 'running'
                            for item in progress))
        for secret in ('synthetic-private-error', 'private-cookie'):
            self.assertNotIn(secret, '\n'.join(lines) + repr(result) + repr(progress))

    def test_failure_classification_reaches_parallel_ui_snapshot(self):
        result, _ = self.run_burst(parallel=True, raise_on_second=True)
        account = result['rooms'][0]['accounts'][0]
        self.assertIn('send_exception', account.get('failure_reasons', []))
        self.assertNotIn('synthetic-private-error', repr(account))

    def test_response_name_save_failure_drains_completed_burst_before_task_failure(self):
        self.assert_metadata_failure_preserves_completed('response',
            observed=[True, False, True], posts=3, delivered=2, expired=0)

    def test_observed_name_save_failure_drains_completed_burst_before_task_failure(self):
        self.assert_metadata_failure_preserves_completed('observed',
            observed=[True, False, True], posts=3, delivered=2, expired=0)

    def test_expiry_save_failure_preserves_completed_and_expired_results(self):
        self.assert_metadata_failure_preserves_completed('expiry',
            observed=False, posts=2, delivered=0, expired=1)

    def display_logs(self, **kwargs):
        lines = []
        handle = logger.add(lambda msg: lines.append(msg.record['message']))
        try:
            result, events = self.run_burst(**kwargs)
        finally:
            logger.remove(handle)
        return result, events, [line for line in lines if 'display_status=' in line]

    def test_each_post_has_one_attributed_display_result_matching_its_receipt_id(self):
        result, events, lines = self.display_logs(observed=[True, False, True])
        self.assertEqual(len(lines), 3)
        for number, status in ((1, 'observed'), (2, 'not_observed'), (3, 'observed')):
            self.assertIn(f'message_index={number}/3 | message_id={number} | display_status={status}', lines[number - 1])
            self.assertIn('room=host | account_username=', lines[number - 1])
            self.assertIn('account_id=', lines[number - 1])
        self.assertEqual(result['delivered'], 2)
        self.assertEqual(events.count('post'), 3)

    def test_cancel_logs_only_already_posted_results(self):
        result, events, lines = self.display_logs(cancel_on_second=True)
        self.assertEqual(len(lines), 2)
        self.assertTrue(all('display_status=observed' in line for line in lines))
        self.assertEqual(result['success'], 2)

    def test_failed_post_is_attributed_without_logging_unsent_queue(self):
        result, events, lines = self.display_logs(fail_on_second=True)
        self.assertEqual(len(lines), 2)
        self.assertIn('message_index=2/3 | message_id=- | display_status=send_failed', lines[1])
        self.assertEqual(result['failed'], 1)

    def test_listener_lost_during_wait_is_unavailable_instead_of_timeout(self):
        result, events, lines = self.display_logs(listener_lost=True)
        self.assertEqual(len(lines), 3)
        self.assertTrue(all('display_status=listener_unavailable' in line for line in lines))
        self.assertEqual(result['listenerUnavailable'], 3)
        self.assertEqual(result['acceptedNotObserved'], 0)

    def test_receipt_exception_preserves_every_already_posted_result(self):
        result, events, lines = self.display_logs(receipt_error=True)
        self.assertEqual(len(lines), 3)
        self.assertEqual(result['success'], 3)
        self.assertTrue(all('display_status=listener_unavailable' in line for line in lines))
        self.assertTrue(all('synthetic-private-error' not in line for line in lines))

    def test_observation_disabled_does_not_claim_display(self):
        result, events, lines = self.display_logs(observation_enabled=False)
        self.assertEqual(len(lines), 3)
        self.assertTrue(all('display_status=listener_unavailable | classification=observation_disabled' in line for line in lines))
        self.assertEqual(result['success'], 3)

    def test_request_exception_logs_only_attempted_messages_without_exception_content(self):
        result, events, lines = self.display_logs(raise_on_second=True)
        self.assertEqual(events.count('post'), 2)
        self.assertEqual(len(lines), 2)
        self.assertIn('message_index=2/3 | message_id=- | display_status=send_failed | classification=send_exception', lines[1])
        self.assertNotIn('synthetic-private-error', '\n'.join(lines))
        self.assertEqual(result['success'], 1)
        self.assertEqual(result['failed'], 1)

    def test_listener_start_failure_logs_task_failure_without_fake_message_result(self):
        lines = []
        handle = logger.add(lambda msg: lines.append(msg.record['message']))
        try:
            with self.assertRaises(runner.SafeOperationError):
                self.run_burst(listener_start_failed=True)
        finally:
            logger.remove(handle)
        self.assertTrue(any('room=host | classification=listener_unavailable' in line for line in lines))
        self.assertFalse(any('display_status=' in line for line in lines))
        self.assertFalse(any('message_sending' in line for line in lines))

    def test_nonmapping_response_metadata_does_not_turn_success_into_send_failure(self):
        result, events, lines = self.display_logs(observation_enabled=False,
                                                malformed_response=True)
        self.assertEqual(result['success'], 3)
        self.assertEqual(result['failed'], 0)
        self.assertEqual(len(lines), 3)
        self.assertTrue(all('message_id=- | display_status=listener_unavailable' in line for line in lines))

    def test_exact_count_one_second_gaps_and_single_connection_through_observation(self):
        result, events = self.run_burst()
        self.assertEqual(events, ['connected', 'post', ('pause', 1), 'post',
                                 ('pause', 1), 'post', 'observe', 'observe', 'observe', 'closed'])
        self.assertEqual(result['success'], 3)
        self.assertEqual(result['delivered'], 3)

    def test_stop_preserves_already_posted_results_and_closes_socket(self):
        result, events = self.run_burst(cancel_on_second=True)
        self.assertEqual(events.count('post'), 2)
        self.assertEqual(result['success'], 2)
        self.assertTrue(result['cancelled'])
        self.assertEqual(events[-1], 'closed')

    def test_silenced_account_stops_remaining_messages_and_closes_socket(self):
        result, events = self.run_burst(fail_on_second=True)
        self.assertEqual(events.count('post'), 2)
        self.assertEqual(result['success'], 1)
        self.assertEqual(result['failed'], 1)
        self.assertEqual(events[-1], 'closed')

    def test_explicit_one_send_does_not_retry_when_not_observed(self):
        result, events = self.run_burst(count=1, observed=False)
        self.assertEqual(events.count('post'), 1)
        self.assertEqual(result['acceptedNotObserved'], 1)
        self.assertEqual(result['delivered'], 0)
        self.assertEqual(events[-1], 'closed')

    def test_disconnected_socket_stops_without_reconnecting(self):
        result, events = self.run_burst(disconnect_on_second=True)
        self.assertEqual(events.count('connected'), 1)
        self.assertEqual(events.count('post'), 2)
        self.assertEqual(result['failed'], 1)
        self.assertEqual(events[-1], 'closed')
