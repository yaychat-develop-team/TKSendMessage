"""Delivery evidence must be published before the next POST, not after a burst."""
from contextlib import nullcontext
from datetime import datetime, timezone
from pathlib import Path
import tempfile
import threading
import time
import unittest
from unittest.mock import patch
from loguru import logger
from listen_msg import LiveMessageListener
from tiktok_live import runner
from tiktok_live.client import interpret_chat_response
from tiktok_live.parallel_runner import safe_parallel_result
from tiktok_live.parallel_runner import run_parallel_targets
from tiktok_live.realtime_delivery import RealtimeDelivery
from tiktok_live.account_store import load_account_store, upsert_account, save_account_store_atomic


class PassiveListener(LiveMessageListener):
    @property
    def available(self):
        return True
    @property
    def room_id(self):
        return '42'
    def close(self):
        pass


class RealtimeDeliveryTests(unittest.TestCase):
    def test_early_receipt_cache_survives_legacy_queue_consumption(self):
        listener = PassiveListener('host',show_comments=False)
        with RealtimeDelivery(listener,account={'username':'sender'},message='hello',lock=threading.RLock(),
                notify=lambda:None,log_observed=lambda *a:None) as progress:
            listener.record_comment('hello',unique_id='sender',message_id='7')
            listener._comments.get_nowait()
            progress.register(1,dict(ok=True,response={'data':{'msg_id':'7'}}),0,set())
            self.assertIsNotNone(progress.get(1))
    def test_timeout_notice_can_be_followed_by_late_real_evidence_before_finalization(self):
        listener = PassiveListener('host',show_comments=False)
        timeout_event = threading.Event()
        logs = []
        with RealtimeDelivery(listener,account={'username':'sender'},message='hello',lock=threading.RLock(),
                notify=lambda:None,log_observed=lambda *a:logs.append('observed'),timeout=.04,
                log_timeout=lambda *a:(logs.append('timeout'),timeout_event.set())) as progress:
            progress.register(1,dict(ok=True,response={'data':{'msg_id':'7'}}),0,set())
            self.assertTrue(timeout_event.wait(1))
            self.assertEqual(progress.remaining(1,10),0)
            listener.record_comment('hello',unique_id='sender',message_id='7')
            self.assertIsNotNone(progress.resolve(1,None))
            self.assertEqual(logs,['timeout','observed'])
        self.assertFalse(progress.monitor.is_alive())
        self.assertFalse(listener._comment_callbacks)

    def test_cancelled_or_closed_monitor_does_not_emit_timeout_failure(self):
        for cancelled in (False,True):
            listener = PassiveListener('host',show_comments=False)
            timeout_event = threading.Event()
            with RealtimeDelivery(listener,account={'username':'sender'},message='hello',lock=threading.RLock(),
                    notify=lambda:None,log_observed=lambda *a:None,timeout=.05,
                    should_cancel=lambda:cancelled,log_timeout=lambda *a:timeout_event.set()) as progress:
                progress.register(1,dict(ok=True,response={'data':{'msg_id':'7'}}),0,set())
                if cancelled:
                    self.assertFalse(timeout_event.wait(.12))
            self.assertFalse(timeout_event.wait(.08))
            self.assertFalse(progress.monitor.is_alive())
    def test_default_message_attempts_is_one_without_overwriting_saved_settings(self):
        import settings
        from desktop.settings_store import DesktopSettings,load_desktop_settings,save_desktop_settings
        self.assertEqual(settings.MESSAGE_NOT_OBSERVED_RETRIES,1)
        self.assertEqual(DesktopSettings().message_attempts,1)
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)/'settings.json'
            save_desktop_settings(path,DesktopSettings(message_attempts=3))
            self.assertEqual(load_desktop_settings(path).message_attempts,3)
    def test_unobserved_timeout_is_logged_while_later_post_is_still_blocked(self):
        listener = PassiveListener('host',show_comments=False)
        blocked = threading.Event()
        release = threading.Event()
        timed_out = threading.Event()
        lines,completed = [],[]
        def log(msg):
            text = msg.record['message']
            lines.append(text)
            if '监听期内未发现实际弹幕' in text and 'classification=accepted_not_observed' in text:
                timed_out.set()
        sink = logger.add(log)
        self.addCleanup(logger.remove,sink)
        posts = 0
        def send(*args,**kwargs):
            nonlocal posts
            posts += 1
            if posts == 2:
                blocked.set()
                release.wait(3)
            return dict(ok=True,response={'data':{'msg_id':str(posts)}})
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)/'accounts.json'
            store = load_account_store(path)
            account = upsert_account(store,[{'name':'uid_tt','value':'synthetic','domain':'.tiktok.com'}],datetime.now(timezone.utc))
            account['username'] = 'sender'
            save_account_store_atomic(path,store)
            with patch.object(runner,'_start_live_listener',return_value=listener), \
                    patch.object(runner,'room_heartbeat_session',return_value=nullcontext()), \
                    patch.object(runner,'send_live_message',side_effect=send),patch.object(runner,'_pause'):
                thread = threading.Thread(target=lambda:completed.append(runner.run_saved_accounts('host','hello',path,
                    message_count=2,interval_seconds=0,observation_retries=1,observation_timeout_seconds=.1)))
                thread.start()
                try:
                    self.assertTrue(blocked.wait(2))
                    printed_while_sending = timed_out.wait(.7)
                finally:
                    release.set()
                    thread.join(3)
                self.assertFalse(thread.is_alive())
        self.assertTrue(printed_while_sending,'Unobserved result was withheld until the burst finished')
        self.assertEqual(completed[0]['acceptedNotObserved'],2)
        self.assertEqual(len([line for line in lines if '监听期内未发现实际弹幕' in line and
            'classification=accepted_not_observed' in line]),2)
        self.assertEqual(len([line for line in lines if 'display_status=not_observed' in line]),2)
    def test_preparation_failure_is_not_reported_as_sent(self):
        listener = PassiveListener('host',show_comments=False)
        with RealtimeDelivery(listener,account={},message='hello',lock=threading.RLock(),
                notify=lambda:None,log_observed=lambda *a:None) as progress:
            progress.register(1,runner.SafeOperationError('initial_fetch_unavailable'),0,set())
            self.assertEqual(len(progress.sent),0)

    def test_receipt_arriving_at_timeout_boundary_wins_before_result_is_sealed(self):
        listener = PassiveListener('host',show_comments=False)
        with RealtimeDelivery(listener,account={'username':'sender'},message='hello',lock=threading.RLock(),
                notify=lambda:None,log_observed=lambda *a:None) as progress:
            progress.register(1,dict(ok=True,response={'data':{'msg_id':'7'}}),0,set())
            self.assertIsNone(progress.get(1))
            listener.record_comment('hello',unique_id='sender',message_id='7')
            resolve = getattr(progress,'resolve',lambda index,event:event)
            self.assertIsNotNone(resolve(1,None))
    def test_receipt_after_final_timeout_does_not_reopen_result_and_subscription_closes(self):
        listener = PassiveListener('host',show_comments=False)
        logged = []
        with RealtimeDelivery(listener,account={'username':'sender'},message='hello',
                lock=threading.RLock(),notify=lambda:None,log_observed=lambda *args:logged.append(args)) as progress:
            progress.register(1,dict(ok=True,response={'data':{'msg_id':'7'}}),0,set())
            progress.finalize_display(1,'not_observed')
            listener.record_comment('hello',unique_id='sender',message_id='7')
            self.assertIsNone(progress.get(1))
        listener.record_comment('hello',unique_id='sender',message_id='7')
        self.assertFalse(logged)
        self.assertFalse(listener._comment_callbacks)

    def test_listener_subscription_does_not_consume_queue_and_callback_error_is_isolated(self):
        listener = PassiveListener('host',show_comments=False)
        received = []
        def fail(event):
            raise RuntimeError('secret must never be logged')
        remove_bad = listener.subscribe_comments(fail)
        remove_good = listener.subscribe_comments(received.append)
        listener.record_comment('hello',unique_id='sender',message_id='8')
        self.assertEqual(len(received),1)
        self.assertEqual(listener.wait_for_comment('hello',since=0,timeout=.01).message_id,'8')
        remove_bad()
        remove_good()
        remove_good()
        listener.record_comment('hello',unique_id='sender',message_id='9')
        self.assertEqual(len(received),1)
    def test_one_receipt_cannot_confirm_two_logical_messages(self):
        listener = PassiveListener('host',show_comments=False)
        with RealtimeDelivery(listener,account={'username':'sender'},message='hello',
                lock=threading.RLock(),notify=lambda:None,log_observed=lambda *a:None) as progress:
            result = dict(ok=True,response={'data':{'msg_id':'7'}})
            progress.register(1,result,0,set())
            progress.register(2,result,0,set())
            listener.record_comment('hello',unique_id='sender',message_id='7')
            self.assertEqual(len(progress.observed),1)

    def test_restrictions_stop_only_affected_account_and_are_published_immediately(self):
        local = threading.local()
        posts = {}
        snapshots = []
        guard = threading.Lock()
        def listener_factory(target,**kwargs):
            local.listener = PassiveListener(target,show_comments=False)
            return local.listener
        def send(target,cookie,message,**kwargs):
            sender = kwargs['account_display_name']
            with guard:
                posts[sender] = posts.get(sender,0)+1
                number = posts[sender]
            if number == 2 and sender != 'healthy':
                text = 'Your account has been banned' if sender == 'banned' else 'user silence'
                return interpret_chat_response({'status_code':50001,'data':{'message':text}})
            local.listener.record_comment(message,unique_id=sender,message_id=str(number))
            return dict(ok=True,response={'data':{'msg_id':str(number)}})
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            path = root/'accounts.json'
            store = load_account_store(path)
            for sender in ('banned','muted','healthy'):
                account = upsert_account(store,[{'name':'uid_tt','value':'synthetic-'+sender,
                    'domain':'.tiktok.com'}],datetime.now(timezone.utc))
                account['username'] = sender
            save_account_store_atomic(path,store)
            with patch.object(runner,'_start_live_listener',side_effect=listener_factory), \
                    patch.object(runner,'room_heartbeat_session',side_effect=lambda *a,**kw:nullcontext()), \
                    patch.object(runner,'send_live_message',side_effect=send),patch.object(runner,'_pause'):
                result = run_parallel_targets(('host',),'hello',store_path=path,
                    temporary_root=root/'temporary',report_dir=root/'reports',selected_account_ids=None,
                    message_count=3,message_interval_seconds=.1,should_cancel=lambda:False,
                    on_progress=snapshots.append,runner_kwargs={'interval_seconds':0,'observation_retries':3,
                        'observation_timeout_seconds':.01})
        self.assertEqual(posts,{'banned':2,'muted':2,'healthy':3})
        accounts = {a['username']:a for a in result['rooms'][0]['accounts']}
        self.assertIn('account_banned',accounts['banned']['failure_reasons'])
        self.assertIn('account_silenced',accounts['muted']['failure_reasons'])
        self.assertEqual(accounts['healthy']['delivered'],3)
        self.assertFalse(accounts['healthy']['failure_reasons'])
        for name,code in (('banned','account_banned'),('muted','account_silenced')):
            self.assertTrue(any(a['username']==name and a['status']=='running' and
                code in a.get('failure_reasons',[]) for s in snapshots for r in s['rooms'] for a in r['accounts']))

    def test_two_rooms_two_accounts_publish_while_posts_are_blocked_without_cross_talk(self):
        local = threading.local()
        condition = threading.Condition()
        blocked = {}
        release = threading.Event()
        snapshots, records, completed = [], [], []
        sink = logger.add(lambda msg: records.append((msg.record['message'],dict(msg.record['extra']))))
        self.addCleanup(logger.remove, sink)
        def listener_factory(target, **kwargs):
            local.listener = PassiveListener(target, show_comments=False)
            local.posts = 0
            return local.listener
        def send(target, cookie, message, **kwargs):
            local.posts += 1
            number = local.posts
            sender = kwargs['account_display_name']
            # Identical message IDs deliberately exercise task-local isolation.
            if number == 2:
                with condition:
                    blocked[(target,sender)] = local.listener
                    condition.notify_all()
                if not release.wait(5):
                    raise TimeoutError('Test gate timed out')
            if number != 1 or target == 'host2':
                local.listener.record_comment(message, unique_id=sender, message_id=str(number))
            return dict(ok=True,response={'data':{'msg_id':str(number)}})
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            path = root/'accounts.json'
            store = load_account_store(path)
            for number in range(2):
                account = upsert_account(store,[{'name':'uid_tt','value':f'synthetic-{number}',
                    'domain':'.tiktok.com'}],datetime.now(timezone.utc))
                account['username'] = f'sender{number}'
            save_account_store_atomic(path,store)
            def run():
                completed.append(run_parallel_targets(('host1','host2'),'hello',store_path=path,
                    temporary_root=root/'temporary',report_dir=root/'reports',selected_account_ids=None,
                    message_count=3,message_interval_seconds=.1,should_cancel=lambda:False,
                    on_progress=lambda result:snapshots.append(result),
                    runner_kwargs={'interval_seconds':0,'observation_retries':1,'observation_timeout_seconds':.05}))
            with patch.object(runner,'_start_live_listener',side_effect=listener_factory), \
                    patch.object(runner,'room_heartbeat_session',side_effect=lambda *a,**kw:nullcontext()), \
                    patch.object(runner,'send_live_message',side_effect=send),patch.object(runner,'_pause'):
                task = threading.Thread(target=run)
                task.start()
                try:
                    with condition:
                        self.assertTrue(condition.wait_for(lambda:len(blocked)==2,timeout=5))
                    self.assertEqual({key[0] for key in blocked},{'host1'})
                    def receive():
                        for (target,sender),listener in blocked.items():
                            listener.record_comment('hello',unique_id='someone_else',message_id='1')
                            listener.record_comment('different',unique_id=sender,message_id='1')
                            listener.record_comment('hello',unique_id=sender,message_id='1')
                            listener.record_comment('hello',unique_id=sender,message_id='1')
                    receiver = threading.Thread(target=receive)
                    receiver.start()
                    receiver.join(3)
                    self.assertFalse(receiver.is_alive(), 'Receipt callback deadlocked')
                    live = snapshots[-1]
                    self.assertEqual(live['delivered'],2)
                    self.assertEqual(live['sent'],2)
                    self.assertTrue(all(a['delivered']==1 and a['status']=='running'
                        for a in live['rooms'][0]['accounts']))
                    self.assertTrue(all(a['status']=='queued' for a in live['rooms'][1]['accounts']))
                    display = [(line,context) for line,context in records if 'display_status=observed' in line]
                    self.assertEqual(len(display),2)
                    for line,context in display:
                        self.assertIn('room='+context['target_room']+' |',line)
                        self.assertIn('account_username='+context['sender_account']+' |',line)
                finally:
                    release.set()
                    task.join(6)
                self.assertFalse(task.is_alive())
        self.assertEqual(completed[0]['sent'],12)
        self.assertEqual(completed[0]['delivered'],12)
        self.assertEqual(completed[0]['failed'],0)
        self.assertEqual(len([line for line,_ in records if 'display_status=observed' in line]),12)
        self.assertTrue(all(not listener._comment_callbacks for listener in blocked.values()))

    def test_explicit_account_ban_is_terminal_but_network_failure_is_not_a_ban(self):
        result = interpret_chat_response({'status_code': 50001, 'data': {'message': 'Your account has been banned'}})
        self.assertTrue(result.get('accountBanned'))
        self.assertEqual(result.get('classification'), 'account_banned')
        self.assertTrue(runner.terminal_failure(result))
        for text in ('Forbidden', 'Request timed out', 'This comment contains a banned word'):
            result = interpret_chat_response({'status_code': 403, 'data': {'message': text}})
            self.assertFalse(result.get('accountBanned'))
            self.assertFalse(result.get('accountSilenced'))

    def test_parallel_boundary_preserves_sent_progress(self):
        result = safe_parallel_result({'rooms':[{'target':'host','accounts':[
            {'id':'a'*64,'sent':4,'delivered':3,'status':'running'}]}]})
        self.assertEqual(result['rooms'][0]['accounts'][0].get('sent'), 4)

    def test_early_receipt_is_logged_and_counted_before_next_post_once(self):
        listener = PassiveListener('host', show_comments=False)
        lines, progress = [], []
        sink = logger.add(lambda msg: lines.append(msg.record['message']))
        self.addCleanup(logger.remove, sink)
        posts = 0
        def send(*args, **kwargs):
            nonlocal posts
            posts += 1
            if posts == 2:
                successes = [line for line in lines if 'display_status=observed' in line]
                self.assertEqual(len(successes), 1, 'First receipt was withheld until the burst ended')
                self.assertTrue(any(p.get('sent') == 1 and p.get('delivered') == 1 for p in progress))
            # Both copies arrive before the HTTP response returns its message ID.
            for _ in range(2):
                listener.record_comment('hello', unique_id='sender', message_id=str(posts))
            return dict(ok=True, response={'data': {'msg_id': str(posts)}})
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'accounts.json'
            store = load_account_store(path)
            account = upsert_account(store, [{'name':'uid_tt','value':'synthetic','domain':'.tiktok.com'}], datetime.now(timezone.utc))
            account['username'] = 'sender'
            save_account_store_atomic(path, store)
            with patch.object(runner, '_start_live_listener', return_value=listener), \
                    patch.object(runner, 'room_heartbeat_session', return_value=nullcontext()), \
                    patch.object(runner, 'send_live_message', side_effect=send), \
                    patch.object(runner, '_pause'):
                result = runner.run_saved_accounts('host', 'hello', path, message_count=2,
                    interval_seconds=0, observation_retries=1, observation_timeout_seconds=.01,
                    on_progress=lambda counts: progress.append(dict(counts)))
        self.assertEqual(posts, 2)
        self.assertEqual(result['delivered'], 2)
        self.assertEqual(len([line for line in lines if 'display_status=observed' in line]), 2)
        self.assertEqual(result['failed'], 0)


class RealtimeDeliveryPanelTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from PySide6.QtWidgets import QApplication
        cls.app = QApplication.instance() or QApplication([])

    def test_running_counts_and_restrictions_render_on_correct_account_rows(self):
        from desktop.account_view import AccountTable,AccountSummary
        table = AccountTable()
        table.set_accounts([AccountSummary('sender0','a'*12,'a'*64),AccountSummary('sender1','b'*12,'b'*64)])
        table.begin_delivery(('a'*64,'b'*64),10)
        result = safe_parallel_result({'rooms':[{'target':'host','accounts':[
            {'id':'a'*64,'sent':4,'delivered':3,'status':'running'},
            {'id':'b'*64,'sent':2,'delivered':1,'status':'running'}]}]})
        table.update_delivery(result)
        self.assertEqual(table.item(0,4).text(),'正在运行 · 已发 4/10 · 展示 3/10')
        self.assertEqual(table.item(1,4).text(),'正在运行 · 已发 2/10 · 展示 1/10')
        for code,label in (('account_banned','已封禁'),('account_silenced','已禁言')):
            result['rooms'][0]['accounts'][0]['failure_reasons'] = [code]
            table.update_delivery(result)
            self.assertIn('正在运行',table.item(0,4).text())
            self.assertEqual(table.item(0,4).foreground().color().name(),'#175cd3')
            self.assertIn(code,table.delivery_details('a'*64))
            self.assertEqual(table.item(1,4).text(),'正在运行 · 已发 2/10 · 展示 1/10')
        table.close()
