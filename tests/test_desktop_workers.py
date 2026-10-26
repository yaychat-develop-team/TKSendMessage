import importlib
import json
import os
import tempfile
import threading
import time
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

from tiktok_live import runner
from tiktok_live.account_store import load_account_store, save_account_store_atomic, upsert_account
from tiktok_live.errors import BrowserError, LoginTimeoutError


class RunnerBoundaryTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / 'accounts.json'
        self.event = threading.Event()
        store = load_account_store(self.path)
        for name in ('one', 'two'):
            upsert_account(store, [{'name': 'uid_tt', 'value': 'synthetic-' + name,
                                   'domain': '.tiktok.com'}], datetime.now(timezone.utc))
        save_account_store_atomic(self.path, store)

    def run_accounts(self, **kwargs):
        return runner.run_saved_accounts('host', 'hello', self.path,
            add_new_account=False, interval_seconds=0,
            should_cancel=self.event.is_set, **kwargs)

    def test_explicit_account_filter_sends_only_selected_account(self):
        accounts = load_account_store(self.path)['accounts']
        sent = []
        with patch.object(runner, 'send_live_message', side_effect=lambda *a, **kw: sent.append(a[1]) or {'ok': True}):
            result = self.run_accounts(observation_enabled=False,
                selected_account_ids=(accounts[1]['id'],))
        self.assertEqual(result['success'], 1)
        self.assertEqual(len(sent), 1)
        self.assertIn('synthetic-two', sent[0])
        self.assertNotIn('synthetic-one', sent[0])

    def test_empty_account_filter_does_not_connect_or_send(self):
        with patch.object(runner, 'send_live_message', side_effect=AssertionError('send')), \
                patch.object(runner, '_start_live_listener', side_effect=AssertionError('listen')):
            with self.assertRaises(ValueError):
                self.run_accounts(observation_enabled=True, selected_account_ids=())

    def test_pre_send_persistence_failure_closes_started_listener(self):
        class Listener:
            closed = False
            def close(inner):
                inner.closed = True
        listener = Listener()
        for preparation in ('_deduplicate_accounts', '_ensure_environment_seeds'):
            with self.subTest(preparation=preparation):
                listener.closed = False
                with patch.object(runner, '_start_live_listener', return_value=listener), \
                        patch.object(runner, preparation, return_value=True), \
                        patch.object(runner, 'save_account_store_atomic', side_effect=OSError('synthetic-save-failure')), \
                        patch.object(runner, 'send_live_message', side_effect=AssertionError('must not send')):
                    with self.assertRaises(OSError):
                        self.run_accounts(observation_enabled=True)
                self.assertTrue(listener.closed, 'started listener leaked on setup failure')

    def test_cancel_before_start_does_not_connect_or_send(self):
        self.event.set()
        with patch.object(runner, 'send_live_message', side_effect=AssertionError('send')), \
                patch.object(runner, '_start_live_listener', side_effect=AssertionError('connect')):
            result = self.run_accounts(observation_enabled=True)
        self.assertEqual(result['processed'], 0)
        self.assertTrue(result['cancelled'])

    def test_cancel_after_account_preserves_completed_result_and_skips_next(self):
        events = []
        def send(*args, **kwargs):
            events.append('sent')
            self.event.set()
            return {'ok': True}
        progress = []
        with patch.object(runner, 'send_live_message', side_effect=send):
            result = self.run_accounts(observation_enabled=False, on_progress=progress.append,
                                       report_dir=Path(self.tmp.name) / 'reports')
        self.assertEqual(events, ['sent'])
        self.assertEqual(result['success'], 1)
        self.assertEqual(result['processed'], 1)
        self.assertTrue(result['cancelled'])
        self.assertEqual(progress[-1]['processed'], 1)
        self.assertNotIn('cookies', json.dumps(progress))
        self.assertTrue((Path(self.tmp.name) / 'reports' / 'failed_accounts.json').exists())
        self.assertFalse(self.path.with_name('failed_accounts.json').exists())

    def test_cancel_at_observation_retry_boundary_does_not_repost(self):
        class Listener:
            closed = False
            def wait_for_comment(inner, *args, **kwargs):
                self.event.set()
                return None
            def close(inner):
                inner.closed = True
        listener = Listener()
        sends = []
        def send(*args, **kwargs):
            sends.append('sent')
            return {'ok': True}
        with patch.object(runner, 'send_live_message', side_effect=send), \
                patch.object(runner, '_start_live_listener', return_value=listener):
            result = self.run_accounts(observation_enabled=True, observation_retries=3,
                                       observation_retry_delay_seconds=0)
        self.assertEqual(sends, ['sent'])
        self.assertEqual(result['acceptedNotObserved'], 1)
        self.assertTrue(result['cancelled'])
        self.assertTrue(listener.closed)

    def test_cancel_during_listener_retry_closes_candidate(self):
        events = []
        class Listener:
            def __init__(inner, *args, **kwargs):
                events.append('connect')
            def start(inner, **kwargs):
                self.event.set()
                return False
            def close(inner):
                events.append('close')
        with patch.object(runner, 'LiveMessageListener', Listener), \
                patch.object(runner, '_resolve_observer_room_id', return_value='42'), \
                patch.object(runner, 'send_live_message', side_effect=AssertionError('send')):
            result = self.run_accounts(observation_enabled=True, observer_connect_retries=3,
                                       observer_retry_delay_seconds=0)
        self.assertEqual(events, ['connect', 'close'])
        self.assertTrue(result['cancelled'])

    def test_cancel_during_account_interval_skips_unsent_account(self):
        sends = []
        def send(*args, **kwargs):
            sends.append('sent')
            return {'ok': True}
        def pause(seconds):
            self.event.set()
        with patch.object(runner, 'send_live_message', side_effect=send), \
                patch.object(runner.time, 'sleep', side_effect=pause):
            result = runner.run_saved_accounts('host', 'hello', self.path,
                add_new_account=False, interval_seconds=3, observation_enabled=False,
                should_cancel=self.event.is_set)
        self.assertEqual(sends, ['sent'])
        self.assertEqual(result['processed'], 1)
        self.assertTrue(result['cancelled'])

    def test_cancel_during_message_retry_delay_finalizes_first_request(self):
        class Listener:
            def wait_for_comment(inner, *args, **kwargs):
                return None
            def close(inner):
                pass
        sends = []
        def send(*args, **kwargs):
            sends.append('sent')
            return {'ok': True}
        with patch.object(runner, 'send_live_message', side_effect=send), \
                patch.object(runner, '_start_live_listener', return_value=Listener()), \
                patch.object(runner.time, 'sleep', side_effect=lambda seconds: self.event.set()):
            result = self.run_accounts(observation_enabled=True,
                observation_retries=3, observation_retry_delay_seconds=3)
        self.assertEqual(sends, ['sent'])
        self.assertEqual(result['processed'], 1)
        self.assertEqual(result['acceptedNotObserved'], 1)
        self.assertTrue(result['cancelled'])


class DesktopWorkerTests(unittest.TestCase):
    def test_login_failure_logs_safe_classification_and_error_type(self):
        from loguru import logger
        lines = []
        sink = logger.add(lambda message: lines.append(str(message)))
        try:
            def fail(*args, **kwargs):
                raise RuntimeError('synthetic-private-browser-error')
            events = self.login_events(fail)
        finally:
            logger.remove(sink)
        self.assertIn(('failed', 'runtime_error'), events)
        self.assertIn('classification=runtime_error', ''.join(lines))
        self.assertNotIn('synthetic-private-browser-error', ''.join(lines))

    @classmethod
    def setUpClass(cls):
        os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
        from PySide6.QtWidgets import QApplication
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.workers = importlib.import_module('desktop.workers')
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        from desktop.paths import RuntimePaths
        self.paths = RuntimePaths.from_application(Path(self.tmp.name))
        self.event = threading.Event()

    def add_account(self):
        store = load_account_store(self.paths.account_store)
        account = upsert_account(store, [{'name': 'uid_tt', 'value': 'synthetic-user',
            'domain': '.tiktok.com'}], datetime.now(timezone.utc))
        account.update(username='demo', ticket_guard={'private_key_pem': 'synthetic-secret'})
        save_account_store_atomic(self.paths.account_store, store)
        return account

    def login_events(self, action):
        worker = self.workers.LoginWorker(self.paths.account_store, 17, self.event,
                                           profile_root=self.paths.profile_root)
        events = []
        worker.browser_ready.connect(lambda pid: events.append(('browser', pid)))
        worker.account_saved.connect(lambda account: events.append(('saved', account)))
        worker.failed.connect(lambda error: events.append(('failed', error)))
        worker.finished.connect(lambda: events.append(('finished',)))
        with patch.object(self.workers, 'login_new_account', side_effect=action):
            worker.run()
        return events

    def test_login_worker_does_not_require_browser_embedding_acknowledgement(self):
        worker = self.workers.LoginWorker(self.paths.account_store, 17, self.event,
                                          profile_root=self.paths.profile_root)
        self.assertFalse(hasattr(worker, 'browser_embedded'))

    def test_login_success_signal_order_and_safe_summary(self):
        def login(path, timeout_seconds, **kwargs):
            self.assertEqual(timeout_seconds, 17)
            self.assertEqual(kwargs['profile_root'], self.paths.profile_root)
            kwargs['on_browser_ready'](123)
            return self.add_account()
        events = self.login_events(login)
        self.assertEqual([item[0] for item in events], ['browser', 'saved', 'finished'])
        self.assertEqual(events[1][1]['username'], 'demo')
        self.assertFalse(events[1][1]['duplicate'])
        self.assertEqual(len(events[1][1]['id']), 12)
        self.assertNotIn('synthetic', json.dumps(events))
        self.assertNotIn('ticket_guard', json.dumps(events))

    def test_login_duplicate_preserves_safe_identity(self):
        account = self.add_account()
        events = self.login_events(lambda *args, **kwargs: account)
        self.assertEqual([item[0] for item in events], ['saved', 'finished'])
        self.assertTrue(events[0][1]['duplicate'])

    def test_login_cancellation_finishes_without_account(self):
        self.event.set()
        events = self.login_events(lambda *args, **kwargs: self.fail('browser started'))
        self.assertEqual(events, [('failed', 'login_cancelled'), ('finished',)])

    def test_login_timeout_and_unknown_exception_are_safe(self):
        for exc, expected in [(LoginTimeoutError('login_timeout'), 'login_timeout'),
                              (RuntimeError('Cookie: synthetic-secret'), 'runtime_error')]:
            with self.subTest(expected=expected):
                events = self.login_events(lambda *args, **kwargs: (_ for _ in ()).throw(exc))
                self.assertEqual(events, [('failed', expected), ('finished',)])

    def send_worker(self, username='host', message='hello'):
        worker = self.workers.SendWorker(username, message, self.paths, self.event)
        events = []
        worker.progress.connect(lambda value: events.append(('progress', value)))
        worker.failed.connect(lambda value: events.append(('failed', value)))
        worker.finished.connect(lambda value: events.append(('finished', value)))
        return worker, events

    def test_blank_fields_fail_before_business_calls(self):
        for username, message in [(' ', 'hello'), ('host', '\n')]:
            worker, events = self.send_worker(username, message)
            with patch.object(self.workers, 'run_parallel_targets', side_effect=AssertionError('run')):
                worker.run()
            self.assertEqual(events[0], ('failed', 'invalid_input'))
            self.assertEqual(events[-1][1]['status'], 'failed')

    def test_empty_store_requests_login(self):
        worker, events = self.send_worker()
        with patch.object(self.workers, 'run_parallel_targets', side_effect=AssertionError('run')):
            worker.run()
        self.assertEqual(events[0], ('progress', {'phase': 'login_required'}))
        self.assertEqual(events[-1][1]['status'], 'login_required')

    def test_send_maps_result_and_preserves_runtime_paths(self):
        self.add_account()
        worker, events = self.send_worker(' @host ', ' hello ')
        def run(username, message, **kwargs):
            self.assertEqual((username, message), (('host',), 'hello'))
            self.assertEqual(kwargs['store_path'], self.paths.account_store)
            self.assertEqual(kwargs['report_dir'], self.paths.report_dir)
            self.assertEqual(kwargs['temporary_root'], self.paths.runtime_dir / 'temporary')
            self.assertFalse(kwargs['should_cancel']())
            kwargs['on_progress']({'processed': 2, 'cookies': ['synthetic-secret']})
            return {'success': 2, 'failed': 3, 'expired': 4, 'temporaryError': 1,
                    'delivered': 1, 'acceptedNotObserved': 1, 'listenerUnavailable': 2,
                    'silenced': 2, 'cookies': ['synthetic-secret']}
        with patch.object(self.workers, 'run_parallel_targets', side_effect=run):
            worker.run()
        final = events[-1][1]
        self.assertEqual(final['processed'], 10)
        self.assertEqual(final['silenced'], 2)
        self.assertEqual(final['status'], 'completed')
        self.assertNotIn('synthetic-secret', json.dumps(events))
        self.assertNotIn('cookies', json.dumps(events))

    def test_send_worker_forwards_desktop_runtime_settings(self):
        from desktop.settings_store import DesktopSettings
        self.add_account()
        values = DesktopSettings(
            room_execution_mode='parallel',
            login_timeout_seconds=91,
            account_interval_seconds=7.5,
            observation_enabled=False,
            show_live_comments=True,
            observation_timeout_seconds=12,
            message_attempts=4,
            message_retry_delay_seconds=3,
            listener_connect_timeout_seconds=16,
            listener_connect_retries=2,
            listener_retry_delay_seconds=1.5,
            room_id_cookie='fixture-room-cookie',
        )
        worker = self.workers.SendWorker(
            'host', 'hello', self.paths, self.event, settings=values
        )
        captured = {}
        def run(*args, **kwargs):
            captured.update(kwargs)
            return {'success': 1}
        with patch.object(self.workers, 'run_parallel_targets', side_effect=run):
            worker.run()
        self.assertEqual(captured['runner_kwargs']['interval_seconds'], 7.5)
        self.assertEqual(captured['room_execution_mode'], 'parallel')
        self.assertEqual(captured['runner_kwargs']['observation_retries'], 4)
        self.assertEqual(captured['runner_kwargs']['login_timeout_seconds'], 91)
        self.assertEqual(captured['runner_kwargs']['room_id_cookie'], 'fixture-room-cookie')

    def test_send_cancellation_before_start_is_safe(self):
        self.event.set()
        worker, events = self.send_worker()
        with patch.object(self.workers, 'run_parallel_targets', side_effect=AssertionError('run')):
            worker.run()
        self.assertEqual(events[-1][1]['status'], 'cancelled')
        self.assertEqual(events[-1][1]['processed'], 0)

    def test_send_error_finishes_with_safe_classification(self):
        self.add_account()
        worker, events = self.send_worker()
        with patch.object(self.workers, 'run_parallel_targets', side_effect=BrowserError('chromium_unavailable')):
            worker.run()
        self.assertEqual(events[-2], ('failed', 'chromium_unavailable'))
        self.assertEqual(events[-1][1]['status'], 'failed')

    def test_late_report_error_preserves_completed_account_counts(self):
        self.add_account()
        worker, events = self.send_worker()
        def fail_report(*args, **kwargs):
            kwargs['on_progress']({'success': 1})
            raise OSError('Cookie: synthetic-secret')
        with patch.object(self.workers, 'run_parallel_targets', side_effect=fail_report):
            worker.run()
        self.assertEqual(events[-3][1]['processed'], 1)
        self.assertEqual(events[-2], ('failed', 'runtime_error'))
        terminal = events[-1][1]
        self.assertEqual(terminal['processed'], 1)
        self.assertEqual(terminal['success'], 1)
        self.assertEqual(terminal['status'], 'failed')
        self.assertFalse(terminal['cancelled'])
        self.assertNotIn('synthetic-secret', json.dumps(events))

    def test_late_report_error_preserves_cancellation_flag_and_completed_counts(self):
        self.add_account()
        worker, events = self.send_worker()
        def fail_report(*args, **kwargs):
            kwargs['on_progress']({'success': 1})
            self.event.set()
            raise OSError('synthetic-report-failure')
        with patch.object(self.workers, 'run_parallel_targets', side_effect=fail_report):
            worker.run()
        terminal = events[-1][1]
        self.assertEqual(terminal['processed'], 1)
        self.assertEqual(terminal['success'], 1)
        self.assertEqual(terminal['status'], 'failed')
        self.assertTrue(terminal['cancelled'])

    def test_all_targets_parse_before_runner_and_room_metadata_is_safely_preserved(self):
        self.add_account()
        worker, events = self.send_worker(' @host ; @other；HOST ')
        payload = {'success': 1, 'failed_tasks': 1, 'status': 'failed', 'rooms': [
            {'target': 'host', 'status': 'failed', 'success': 1, 'cookies': 'synthetic-secret',
             'accounts': [{'id': 'abcdef012345', 'username': 'demo', 'status': 'failed',
                 'success': 1, 'cookies': 'synthetic-secret',
                 'error': {'type': 'OSError', 'classification': 'runtime_error',
                           'message': 'synthetic-secret'}}]}]}
        def run(targets, message, **kwargs):
            self.assertEqual(targets, ('host', 'other'))
            kwargs['on_progress'](payload)
            return payload
        with patch.object(self.workers, 'run_parallel_targets', side_effect=run):
            worker.run()
        final = events[-1][1]
        self.assertEqual(final['status'], 'failed')
        self.assertEqual(final['failed_tasks'], 1)
        self.assertEqual(final['rooms'][0]['accounts'][0]['id'], 'abcdef012345')
        self.assertEqual(final['rooms'], worker._last_counts['rooms'])
        self.assertNotIn('synthetic', json.dumps(events))

    def test_invalid_later_target_and_empty_selection_never_start_coordinator(self):
        self.add_account()
        for username, selected, expected in [('host;https://bad', None, 'invalid_targets'),
                                              ('host', (), 'no_accounts_selected')]:
            worker, events = self.send_worker(username)
            worker.selected_account_ids = selected
            with patch.object(self.workers, 'run_parallel_targets', side_effect=AssertionError('run')):
                worker.run()
            self.assertEqual(events[0], ('failed', expected))

    def test_worker_preserves_positive_count_clamps_interval_and_forwards_selection(self):
        account = self.add_account()
        worker = self.workers.SendWorker('host', 'hello', self.paths,
            selected_account_ids=(account['id'],), message_count=101,
            message_interval_seconds=4000)
        with patch.object(self.workers, 'run_parallel_targets', return_value={'success':101}) as run:
            worker.run()
        run.assert_called_once()
        kwargs = run.call_args.kwargs
        self.assertEqual(kwargs['message_count'], 101)
        self.assertEqual(kwargs['message_interval_seconds'], 3600)
        self.assertEqual(kwargs['selected_account_ids'], (account['id'],))

    def test_worker_runs_on_owned_qthread_and_finishes(self):
        from PySide6.QtCore import QObject, QThread, Signal, Slot
        class Probe(QObject):
            finished = Signal()
            thread_seen = None
            @Slot()
            def run(inner):
                inner.thread_seen = QThread.currentThread()
                inner.finished.emit()
        worker = Probe()
        thread = self.workers.create_worker_thread(worker)
        thread.start()
        deadline = time.monotonic() + 5
        while thread.isRunning() and time.monotonic() < deadline:
            self.app.processEvents()
            time.sleep(0.005)
        self.assertTrue(thread.wait(1000))
        self.assertIs(worker.thread_seen, thread)


class LogSafetyTests(unittest.TestCase):
    def test_sensitive_assignments_are_removed_before_signal_emission(self):
        from desktop.log_bridge import QtLogSink
        sink = QtLogSink()
        output = []
        sink.message.connect(output.append)
        for line in ['Cookie: sessionid=synthetic-secret; uid_tt=synthetic-secret',
                     'url?msToken=synthetic-secret&room_id=123',
                     "{'cookies': [{'name': 'x', 'value': 'synthetic-secret'}]}",
                     'X-Sec-Sdk-Csrf-Token: synthetic-secret',
                     'csrf_token=synthetic-secret',
                     'Ticket-Guard-Public-Key: synthetic-secret',
                     'ticket_guard={private_key_pem: synthetic-secret}',
                     '-----BEGIN PRIVATE KEY-----\nsynthetic-secret\n-----END PRIVATE KEY-----',
                     'ec_privateKey: synthetic-secret', 'encrypt_ticket=synthetic-secret',
                     'sessionid=synthetic-secret', 'ts_sign=synthetic-secret']:
            sink(line)
        self.assertEqual(len(output), 12)
        self.assertTrue(all('synthetic-secret' not in item for item in output))

    def test_browser_name_value_secret_records_are_redacted_in_all_representations(self):
        from desktop.log_bridge import QtLogSink
        sink = QtLogSink()
        output = []
        sink.message.connect(output.append)
        for name in ('msToken', 'sessionid', 'csrf_token', 'X-Sec-Sdk-Csrf-Token',
                     'Ticket-Guard-Public-Key', 'tt-ticket-guard-client-data',
                     'private_key_pem', 'encrypt_ticket', 'ts_sign'):
            # Value-first ordering also occurs in persisted/logged mappings.
            record = {'value': 'synthetic-secret', 'name': name}
            for representation in (record, [record], str(record), json.dumps(record),
                    json.dumps([record]), {'envelope': {'records': [record]}},
                    json.dumps({'envelope': {'records': json.dumps([record])}}),
                    repr({'envelope': {'records': [record]}}),
                    'browser response: ' + json.dumps({'payload': {'records': [record]}}),
                    f"Cookie(name='{name}', value='synthetic-secret')"):
                with self.subTest(name=name, representation=representation):
                    sink(representation)
                    self.assertEqual(output[-1], '[redacted sensitive log]')

    def test_cookie_shape_with_unrecognized_name_is_redacted(self):
        from desktop.log_bridge import QtLogSink
        sink = QtLogSink()
        output = []
        sink.message.connect(output.append)
        record = {'name': 'custom_auth', 'value': 'synthetic-secret',
                  'domain': '.tiktok.com', 'path': '/', 'secure': True}
        for representation in (record, json.dumps([record]),
                                'browser records: ' + repr({'records': [record]})):
            sink(representation)
        self.assertEqual(output, ['[redacted sensitive log]'] * 3)

    def test_noncredential_name_value_record_remains_readable(self):
        from desktop.log_bridge import QtLogSink
        sink = QtLogSink()
        output = []
        sink.message.connect(output.append)
        sink({'name': 'phase', 'value': 'complete'})
        self.assertEqual(output, ["{'name': 'phase', 'value': 'complete'}"])

    def test_spaced_ticket_guard_and_private_key_labels_are_redacted(self):
        from desktop.log_bridge import QtLogSink
        sink = QtLogSink()
        output = []
        sink.message.connect(output.append)
        sink('Ticket Guard key: synthetic-secret')
        sink('private key: synthetic-secret')
        self.assertEqual(output, ['[redacted sensitive log]', '[redacted sensitive log]'])

    def test_safe_loguru_message_is_delivered(self):
        from desktop.log_bridge import QtLogSink
        from loguru import logger
        sink = QtLogSink()
        output = []
        sink.message.connect(output.append)
        handler = logger.add(sink, format='{message}')
        try:
            logger.info('账户发送完成 | classification=delivered | account_id=123')
        finally:
            logger.remove(handler)
        self.assertEqual(output, ['账户发送完成 | classification=delivered | account_id=123'])
