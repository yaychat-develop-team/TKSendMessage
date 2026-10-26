import tempfile
import threading
import time
import unittest
from datetime import datetime, timezone
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
from loguru import logger
from desktop.settings_store import DesktopSettings, load_desktop_settings, save_desktop_settings
from tiktok_live import runner, parallel_runner, heartbeat
from tiktok_live.account_store import load_account_store, save_account_store_atomic, upsert_account


class SettingsValueTests(unittest.TestCase):
    def test_large_ui_values_survive_reload(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)/'settings.json'
            values = DesktopSettings(observation_timeout_seconds=1000, message_retry_delay_seconds=1000,
                listener_connect_timeout_seconds=1000, listener_retry_delay_seconds=1000)
            save_desktop_settings(path, values)
            self.assertEqual(load_desktop_settings(path), values)

    def test_nonfinite_numeric_fields_fall_back_without_startup_error(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)/'settings.json'
            for value in ('1e309','NaN','-Infinity'):
                path.write_text('{"login_timeout_seconds":'+value+',"listener_connect_timeout_seconds":'+value+'}',encoding='utf-8')
                loaded = load_desktop_settings(path)
                self.assertEqual(loaded.login_timeout_seconds,300)
                self.assertEqual(loaded.listener_connect_timeout_seconds,15)

    def test_new_desktop_settings_default_to_no_stagger(self):
        self.assertEqual(DesktopSettings().account_interval_seconds,0)

    def test_comment_is_delivered_to_log_sink_with_context_not_only_stdout(self):
        from listen_msg import LiveMessageListener
        from tiktok_live.wss.direct_models import ChatEvent
        lines = []
        handle = logger.add(lambda message:lines.append(message.record),format='{message}')
        try:
            with logger.contextualize(target_room='host',sender_account='alice',sender_id='123'), \
                    patch('builtins.print') as stdout:
                LiveMessageListener('host',show_comments=True)._on_comment(ChatEvent('1','2','viewer','Viewer','hello-world'))
            self.assertTrue(any('hello-world' in line['message'] and line['extra']['target_room']=='host' for line in lines))
            stdout.assert_not_called()
        finally:
            logger.remove(handle)


class RetryBehaviorTests(unittest.TestCase):
    def test_prepared_exception_callback_is_used_in_burst_and_legacy_modes(self):
        from tiktok_live.retry_policy import RetryableMessageError
        for count in (None,1):
            retried=[]
            def retry():
                retried.append(True)
                return self.accepted(2)
            with self.subTest(count=count):
                result,calls,waits,_=self.run_case([RetryableMessageError(retry)],observed=True,count=count)
                self.assertEqual(calls,[1],'preparation must not run again')
                self.assertEqual(retried,[True])
                self.assertEqual(waits,[2])
                self.assertEqual(result['delivered'],1)

    def test_html_login_expiration_is_terminal_even_with_503(self):
        from tiktok_live import client
        from tiktok_live.retry_policy import transient_failure
        from tests.test_send_heartbeat import SendHeartbeatTests
        def invalid():
            raise ValueError()
        with patch.object(client,'build_chat_request',return_value=('https://example.invalid',b'x')), \
                patch.object(client,'post_chat_request',return_value=SimpleNamespace(
                    status_code=503,json=invalid,text='<html>Please log in</html>')):
            result=client._post_chat_with_heartbeat(**SendHeartbeatTests().arguments())
        self.assertTrue(result['loginExpired'])
        self.assertFalse(transient_failure(result))

    def test_html_503_retains_prepared_retry_callback(self):
        from tiktok_live import client
        from tests.test_send_heartbeat import SendHeartbeatTests
        def invalid_json():
            raise ValueError('not JSON')
        with patch.object(client,'build_chat_request',return_value=('https://example.invalid',b'x')), \
                patch.object(client,'post_chat_request',return_value=SimpleNamespace(
                    status_code=503,json=invalid_json,text='<html>temporary upstream</html>')):
            result=client._post_chat_with_heartbeat(**SendHeartbeatTests().arguments())
        self.assertFalse(result['ok'])
        self.assertTrue(result['temporaryError'])
        self.assertTrue(callable(result.retry_message))

    def test_first_post_timeout_retry_reuses_prepared_session_and_wss(self):
        from tiktok_live import client
        from tiktok_live.retry_policy import transient_failure
        from tests.test_send_heartbeat import SendHeartbeatTests
        arguments=SendHeartbeatTests().arguments()
        arguments['session']=object()
        starts,calls=[],[]
        @contextmanager
        def connection(*args,**kw):
            starts.append(args)
            yield SimpleNamespace(available=True,error=None,connection=SimpleNamespace(connected=True))
        def wire(*args,**kw):
            calls.append((args[0],args[3]))
            if len(calls)==1:
                raise TimeoutError('synthetic private network error')
            return SimpleNamespace(status_code=200,json=lambda:{'status_code':0,'data':{}})
        with patch.object(client,'build_chat_request',return_value=('https://example.invalid',b'x')), \
                patch.object(client,'post_chat_request',side_effect=wire), \
                patch.object(heartbeat,'_room_heartbeat_connection',connection), \
                heartbeat.room_heartbeat_session():
            try:
                client._post_prepared_chat(**arguments)
                self.fail('first request should fail')
            except Exception as exc:
                self.assertTrue(transient_failure(exc))
                callback=getattr(exc,'retry_message',None)
                self.assertTrue(callable(callback),'prepared callback missing after first POST failure')
                result=callback()
            self.assertTrue(result['ok'])
        self.assertEqual(len(starts),1)
        self.assertEqual(calls,[(arguments['session'],'sessionid=synthetic')]*2)

    def test_503_response_with_success_body_is_still_transient_failure(self):
        from tiktok_live import client
        from tests.test_send_heartbeat import SendHeartbeatTests
        arguments = SendHeartbeatTests().arguments()
        with patch.object(client,'build_chat_request',return_value=('https://example.invalid',b'x')), \
                patch.object(client,'post_chat_request',return_value=SimpleNamespace(
                    status_code=503,json=lambda:{'status_code':0,'data':{}})):
            result = client._post_chat_with_heartbeat(**arguments)
        self.assertFalse(result['ok'])
        self.assertTrue(result['temporaryError'])

    def run_case(self, outcomes, *, observed=False, attempts=3, count=1, cancel_on_retry=False,
                 connection_options=None):
        calls, waits, receipts = [], [], []
        cancelled = threading.Event()
        sequence = iter(outcomes)
        def send(*args, **kw):
            calls.append(len(calls)+1)
            item = next(sequence)
            if isinstance(item,Exception):
                raise item
            return item
        def pause(seconds, cancel):
            waits.append(seconds)
            if cancel_on_retry and seconds==2:
                cancelled.set()
        def observe(*args, **kw):
            receipts.append(kw.get('expected_message_ids'))
            return SimpleNamespace(unique_id='viewer') if observed else None
        listener = SimpleNamespace(room_id='42',available=True,error=None,
            wait_for_comment=observe,close=lambda:None)
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)/'accounts.json'
            store = load_account_store(path)
            upsert_account(store,[{'name':'uid_tt','value':'100001','domain':'.tiktok.com'}],datetime.now(timezone.utc))
            save_account_store_atomic(path,store)
            with patch.object(runner,'send_live_message',side_effect=send), \
                    patch.object(runner,'_start_live_listener',return_value=listener), \
                    patch.object(runner,'_pause',side_effect=pause):
                result = runner.run_saved_accounts('host','hi',path,message_count=count,
                    observation_retries=attempts,observation_retry_delay_seconds=2,
                    interval_seconds=0,should_cancel=cancelled.is_set,**(connection_options or {}))
        return result,calls,waits,receipts

    @staticmethod
    def accepted(mid):
        return {'ok':True,'response':{'data':{'msg_id':str(mid)}}}

    def test_unobserved_message_retries_real_request_up_to_total_limit(self):
        result,calls,waits,receipts = self.run_case([self.accepted(1),self.accepted(2),self.accepted(3)])
        self.assertEqual(calls,[1,2,3])
        self.assertEqual(waits,[2,2])
        self.assertEqual(receipts,[{'1'},{'2'},{'3'}])
        self.assertEqual(result['success'],1)
        self.assertEqual(result['acceptedNotObserved'],1)

    def test_observed_message_never_uses_remaining_attempt_budget(self):
        result,calls,waits,_ = self.run_case([self.accepted(1)],observed=True)
        self.assertEqual(calls,[1])
        self.assertEqual(waits,[])
        self.assertEqual(result['delivered'],1)

    def test_each_planned_message_has_independent_attempt_budget(self):
        result,calls,waits,receipts = self.run_case([self.accepted(i) for i in (1,2,3,4)],attempts=2,count=2)
        self.assertEqual(calls,[1,2,3,4])
        self.assertEqual(receipts,[{'1'},{'3'},{'2'},{'4'}])
        self.assertEqual(result['acceptedNotObserved'],2)

    def test_transport_failure_and_missing_receipt_share_one_budget(self):
        result,calls,waits,_ = self.run_case([TimeoutError('private'),self.accepted(2),self.accepted(3)])
        self.assertEqual(calls,[1,2,3])
        self.assertEqual(waits,[2,2])
        self.assertEqual(result['success'],1)

    def test_retryable_http_failure_recovers_without_counting_extra_planned_message(self):
        result,calls,waits,_ = self.run_case([{'ok':False,'httpStatus':503},self.accepted(2)],observed=True)
        self.assertEqual(calls,[1,2])
        self.assertEqual(waits,[2])
        self.assertEqual(result['success'],1)
        self.assertEqual(result['failed'],0)

    def test_cancellation_during_retry_delay_never_posts_again(self):
        result,calls,waits,_ = self.run_case([self.accepted(1)],cancel_on_retry=True)
        self.assertEqual(calls,[1])
        self.assertTrue(result['cancelled'])

    def test_terminal_account_failures_are_not_retried(self):
        for terminal in ('loginExpired','accountSilenced','stopProcessing'):
            with self.subTest(terminal=terminal):
                result,calls,waits,_ = self.run_case([{'ok':False,terminal:True}])
                self.assertEqual(calls,[1])
                self.assertEqual(waits,[])

    def test_later_terminal_result_blocks_earlier_unobserved_retries(self):
        for terminal in ({'loginExpired':True},{'accountSilenced':True},
                         {'stopProcessing':True},{'roomExists':False}):
            with self.subTest(terminal=next(iter(terminal))):
                result,calls,waits,_ = self.run_case([self.accepted(1),{'ok':False,**terminal}],count=2)
                self.assertEqual(calls,[1,2])
                self.assertEqual(waits,[1])
                self.assertEqual(result['success'],1)
                self.assertEqual(result['failed']+result['expired'],1)


class StaggerBehaviorTests(unittest.TestCase):
    def test_stagger_delays_start_but_does_not_serialize_account_completion(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            path = root/'accounts.json'
            store = load_account_store(path)
            for uid in ('100001','100002'):
                upsert_account(store,[{'name':'uid_tt','value':uid,'domain':'.tiktok.com'}],datetime.now(timezone.utc))
            save_account_store_atomic(path,store)
            ids = [item['id'] for item in store['accounts']]
            starts = {}
            second = threading.Event()
            def progress(snapshot):
                for index, account in enumerate(snapshot['rooms'][0]['accounts']):
                    if account['status']=='running':
                        starts.setdefault(index,time.monotonic())
            def run(room,message,**kw):
                index = ids.index(kw['selected_account_ids'][0])
                if index==0:
                    self.assertTrue(second.wait(2),'account completion was serialized')
                else:
                    second.set()
                return {'success':1}
            with patch.object(parallel_runner,'run_saved_accounts',side_effect=run):
                result = parallel_runner.run_parallel_targets(('host',),'hi',store_path=path,
                    temporary_root=root/'temporary',report_dir=root/'reports',selected_account_ids=None,
                    message_count=1,message_interval_seconds=1,should_cancel=lambda:False,on_progress=progress,
                    runner_kwargs={'interval_seconds':.1})
            self.assertGreaterEqual(starts[1]-starts[0],.08)
            self.assertEqual(result['success'],2)


class HeartbeatSettingsTests(unittest.TestCase):
    def test_real_listener_start_can_cancel_while_connection_is_pending(self):
        from listen_msg import LiveMessageListener
        from tests.support.direct_fakes import WaitingConnection, visitor
        cancel = threading.Event()
        class Pending(WaitingConnection):
            def run(self,stop):
                self.stop=stop
                stop.wait(2)
        listener=LiveMessageListener('host',session_factory=visitor,connection_factory=Pending)
        timer=threading.Timer(.03,cancel.set)
        timer.start()
        started=time.monotonic()
        try:
            self.assertFalse(listener.start(connect_timeout=10,should_cancel=cancel.is_set))
            listener.close()
            self.assertLess(time.monotonic()-started,.5)
            self.assertFalse(listener._thread.is_alive())
        finally:
            cancel.set()
            listener.close()
            timer.join()

    def test_account_connection_uses_configured_timeout_retries_and_delay(self):
        attempts,delays,closed = [],[],[]
        def listener(*args,**kw):
            def start(**options):
                attempts.append(options['connect_timeout'])
                return False
            return SimpleNamespace(start=start,close=lambda:closed.append(True))
        with patch.object(heartbeat,'LiveMessageListener',side_effect=listener), \
                patch.object(heartbeat,'_pause',side_effect=lambda delay,cancel:delays.append(delay),create=True):
            with self.assertRaises(RuntimeError):
                with heartbeat.room_heartbeat_session(connect_timeout=7,connect_retries=2,retry_delay_seconds=5):
                    with heartbeat.room_heartbeat('host','42','sessionid=SYNTHETIC','UA'):
                        pass
        self.assertEqual(attempts,[7,7,7])
        self.assertEqual(delays,[5,5])
        self.assertEqual(len(closed),3)

    def test_cancelling_retry_delay_skips_remaining_account_connections(self):
        cancel = threading.Event()
        attempts = []
        def listener(*a,**kw):
            def start(**options):
                attempts.append(1)
                return False
            return SimpleNamespace(start=start,close=lambda:None)
        with patch.object(heartbeat,'LiveMessageListener',side_effect=listener), \
                patch.object(heartbeat,'_pause',side_effect=lambda *a:cancel.set(),create=True):
            with self.assertRaises(RuntimeError):
                with heartbeat.room_heartbeat_session(cancel.is_set,connect_timeout=7,connect_retries=5,retry_delay_seconds=5):
                    with heartbeat.room_heartbeat('host','42','sessionid=SYNTHETIC','UA'):
                        pass
        self.assertEqual(attempts,[1])
