"""Offline regressions for long tasks, room summaries and observer recovery."""
import threading
import time
import unittest
from types import SimpleNamespace
from unittest.mock import patch

import listen_msg
from tests.support.direct_fakes import visitor


class LongTaskTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from PySide6.QtWidgets import QApplication
        cls.app = QApplication.instance() or QApplication([])

    def test_count_keeps_typed_positive_integers_beyond_qt_int_range(self):
        from desktop.positive_count import PositiveCountBox
        from desktop.workers import SendWorker
        from PySide6.QtGui import QValidator
        box = PositiveCountBox()
        self.addCleanup(box.close)
        box.setSuffix(' 次')
        for value in (101, 10000, 2**63, 10**40):
            box.lineEdit().setText(str(value) + ' 次')
            self.assertEqual(box.value(), value)
            box.stepBy(1)
            self.assertEqual(box.value(), value+1)
            worker = SendWorker('host', 'hello', None, message_count=value)
            self.assertEqual(worker.message_count, value)
        for invalid in ('-1 次', '1.5 次', 'abc 次'):
            self.assertEqual(box.validate(invalid, 0)[0], QValidator.State.Invalid)
        box.setValue(1)
        box.stepBy(-1)
        self.assertEqual(box.value(), 1)

    def test_real_runner_completes_more_than_one_hundred_posts(self):
        from tests.test_message_burst import MessageBurstTests
        case = MessageBurstTests()
        result, events = case.run_burst(count=201)
        self.assertEqual(events.count('post'), 201)
        self.assertEqual(result['sent'], 201)
        self.assertEqual(result['delivered'], 201)

    def test_large_count_can_be_cancelled_without_preallocating_requested_count(self):
        from tests.test_message_burst import MessageBurstTests
        result, events = MessageBurstTests().run_burst(count=10**30, cancel_on_second=True)
        self.assertEqual(events.count('post'), 2)
        self.assertEqual(result['sent'], 2)
        self.assertTrue(result['cancelled'])

    def test_serial_list_processes_all_rooms_and_closes_before_advancing(self):
        from tests.test_parallel_runner import ParallelRunnerTests
        from tiktok_live.targets import parse_targets
        case = ParallelRunnerTests()
        case.setUp()
        self.addCleanup(case.doCleanups)
        names = tuple('host'+str(n) for n in range(105))
        self.assertEqual(parse_targets(';'.join(names)), names)
        seen = []
        active = set()
        lock = threading.Lock()
        def fake(target, message, **kwargs):
            with lock:
                account_id = kwargs['selected_account_ids'][0]
                self.assertNotIn(account_id, active)
                active.add(account_id)
                seen.append((account_id, target))
            # Each task returns only after its owned connection was closed.
            with lock:
                active.discard(account_id)
            return {'success': 1, 'sent': 1, 'delivered': 1}
        with patch.object(case.module, 'run_saved_accounts', side_effect=fake):
            result = case.run_targets(names)
        self.assertEqual(len(seen), 210)
        for account_id in case.ids:
            self.assertEqual([room for account,room in seen if account == account_id], list(names))
        self.assertEqual(len(result['rooms']), 105)

    def test_partial_stop_and_other_rooms_running_never_show_red_cross(self):
        from desktop.account_view import AccountTable, AccountSummary
        table = AccountTable()
        self.addCleanup(table.close)
        table.set_accounts([AccountSummary('one', 'a'*12, 'a'*64)])
        table.begin_delivery(('a'*64,), 1100)
        first = {'id':'a'*12, 'status':'failed', 'sent':26, 'delivered':23,
                 'success':26, 'acceptedNotObserved':3,
                 'failure_reasons':['consecutive_not_observed']}
        second = {'id':'a'*12, 'status':'running'}
        snapshot = {'rooms':[{'target':'first','accounts':[first]},
                             {'target':'second','accounts':[second]}]}
        table.update_delivery(snapshot)
        self.assertEqual(table.item(0,3).text(), '正在运行')
        self.assertIn('正在运行', table.item(0,4).text())
        self.assertEqual(table.item(0,4).foreground().color().name(), '#175cd3')
        second['status'] = 'completed'
        table.update_delivery(snapshot, finished=True)
        self.assertIn('部分展示 23/1100', table.item(0,4).text())
        self.assertEqual(table.item(0,4).foreground().color().name(), '#b54708')
        self.assertIn('consecutive_not_observed', table.delivery_details('a'*64))
        first.update(delivered=0, success=0, acceptedNotObserved=0, failed=1,
                     failure_reasons=['initial_fetch_unavailable'])
        second.update(status='failed', failed=1, failure_reasons=['initial_fetch_unavailable'])
        table.update_delivery(snapshot, finished=True)
        self.assertEqual(table.item(0,4).foreground().color().name(), '#b42318')
        second.update(failed=0, listenerUnavailable=1, failure_reasons=['listener_unavailable'])
        table.update_delivery(snapshot, finished=True)
        self.assertEqual(table.item(0,4).foreground().color().name(), '#b54708')


class ObserverRecoveryTests(unittest.TestCase):
    def make_listener(self, *, fail_reconnect=False, delay=.001, role='delivery_observer'):
        drop = threading.Event()
        instances = []
        restored = threading.Event()
        class Connection:
            def __init__(inner, session, *, on_comment, on_connected, connect_timeout):
                inner.session = session
                inner.room_id = session.room_id
                inner.connected = False
                inner.closed = threading.Event()
                inner.on_connected = on_connected
                inner.on_comment = on_comment
                instances.append(inner)
            def run(inner, stop):
                if len(instances) > 1 and fail_reconnect:
                    raise ConnectionError('sessionid=DO_NOT_LOG')
                inner.connected = True
                inner.on_connected()
                if len(instances) > 1:
                    restored.set()
                while not stop.is_set() and not inner.closed.wait(.005):
                    if instances[0] is inner and drop.is_set():
                        inner.connected = False
                        raise ConnectionError('sessionid=DO_NOT_LOG')
            def close(inner):
                inner.connected = False
                inner.closed.set()
        listener = listen_msg.LiveMessageListener('host', show_comments=False,
            session_factory=visitor, connection_factory=Connection, reconnect_delay=delay,
            connection_role=role)
        self.addCleanup(listener.close)
        self.assertTrue(listener.start(1))
        return listener, drop, restored, instances

    def wait_for(self, predicate):
        deadline = time.monotonic()+2
        while not predicate() and time.monotonic() < deadline:
            time.sleep(.005)
        self.assertTrue(predicate())

    def test_reconnect_preserves_room_guest_session_and_receipt_subscription(self):
        listener, drop, restored, instances = self.make_listener()
        received = []
        listener.subscribe_comments(received.append)
        before = time.monotonic()
        drop.set()
        self.assertTrue(restored.wait(2))
        self.assertTrue(listener.available)
        self.assertIsNone(listener.error)
        self.assertTrue(instances[0].closed.is_set())
        self.assertIs(instances[0].session, instances[1].session)
        self.assertFalse(listener.observation_available_since(before))
        self.assertTrue(listener.observation_available_since(time.monotonic()))
        instances[1].on_comment(SimpleNamespace(text='hello', unique_id='viewer',
            nickname='', user_id='1', message_id='new'))
        self.assertEqual(received[0].message_id, 'new')
        self.assertEqual(listener.wait_for_comment('hello', since=before, timeout=.1).message_id, 'new')
        listener.close()
        self.assertTrue(all(c.closed.is_set() for c in instances))
        self.assertFalse(listener._thread.is_alive())

    def test_exhaustion_is_bounded_and_does_not_leak_exception_secrets(self):
        lines = []
        sink = listen_msg.logger.add(lambda message: lines.append(str(message)))
        self.addCleanup(listen_msg.logger.remove, sink)
        listener, drop, _, instances = self.make_listener(fail_reconnect=True)
        drop.set()
        self.assertTrue(listener._stopped.wait(2))
        self.assertEqual(len(instances), 4)
        self.assertFalse(listener.available)
        self.assertIsNotNone(listener.error)
        self.assertTrue(all(c.closed.is_set() for c in instances))
        self.assertNotIn('DO_NOT_LOG', ''.join(lines)+str(listener.error))

    def test_leaving_room_during_reconnect_delay_stops_thread_and_next_attempt(self):
        listener, drop, _, instances = self.make_listener(delay=20)
        drop.set()
        self.wait_for(lambda: listener.recovering)
        started = time.monotonic()
        listener.close()
        self.assertLess(time.monotonic()-started, 1)
        self.assertEqual(len(instances), 1)
        self.assertTrue(instances[0].closed.is_set())
        self.assertFalse(listener._thread.is_alive())

    def test_account_heartbeat_does_not_use_observer_reconnection_policy(self):
        listener, drop, _, instances = self.make_listener(role='account_heartbeat')
        drop.set()
        self.assertTrue(listener._stopped.wait(2))
        self.assertEqual(len(instances), 1)

    def test_recovered_gap_is_not_counted_as_consecutive_missing_comment(self):
        from tiktok_live.realtime_delivery import RealtimeDelivery
        from tiktok_live.runner import _listener_unavailable
        listener, drop, restored, _ = self.make_listener()
        outcomes = []
        started = time.monotonic()
        with RealtimeDelivery(listener, account={'username':'viewer'}, message='hello',
                lock=threading.RLock(), notify=lambda:None, log_observed=lambda *a:None,
                timeout=.15, log_timeout=lambda *a:outcomes.append('timeout'),
                on_outcome=lambda *a:outcomes.append(a)) as live:
            live.register(1, {'ok':True, 'response':{'data':{'msg_id':'one'}}}, started, set())
            drop.set()
            self.assertTrue(restored.wait(2))
            self.wait_for(lambda: 1 in live.expired)
            with live.lock:
                self.assertEqual(outcomes, [(1, 'other')])
            self.assertTrue(_listener_unavailable(listener, started))
            self.assertFalse(_listener_unavailable(listener, time.monotonic()))
