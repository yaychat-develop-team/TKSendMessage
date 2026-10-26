"""A room-wide comment restriction must drain that room and advance in order."""
import tempfile
import threading
import time
import unittest
from datetime import datetime,timezone
from pathlib import Path
from unittest.mock import patch
from loguru import logger
from contextlib import nullcontext
from tiktok_live import parallel_runner
from tiktok_live import runner
from tiktok_live.client import interpret_chat_response
from tiktok_live.account_store import load_account_store,upsert_account,save_account_store_atomic


class RoomRestrictionTests(unittest.TestCase):
    def test_stop_interrupts_long_receipt_wait_without_waiting_for_timeout(self):
        from listen_msg import LiveMessageListener
        listener=LiveMessageListener('host',show_comments=False)
        cancel=threading.Event()
        timer=threading.Timer(.05,cancel.set)
        timer.start()
        try:
            started=time.monotonic()
            self.assertIsNone(listener.wait_for_comment('hello',since=0,timeout=30,should_cancel=cancel.is_set))
            self.assertLess(time.monotonic()-started,.5)
        finally:
            timer.cancel()
            timer.join()

    def test_real_runner_stops_new_posts_in_restricted_room_and_sends_in_next_room(self):
        from tests.test_realtime_delivery import PassiveListener
        local=threading.local()
        first_posts=threading.Barrier(2)
        restricted=threading.Event()
        posts={}
        guard=threading.Lock()
        logs=[]
        sink=logger.add(lambda message:logs.append(message.record['message']))
        self.addCleanup(logger.remove,sink)
        def start(room,**kwargs):
            local.listener=PassiveListener(room,show_comments=False)
            return local.listener
        def send(room,cookie,message,**kwargs):
            sender=kwargs['account_display_name']
            with guard:
                key=(room,sender)
                posts[key]=posts.get(key,0)+1
                number=posts[key]
            if room=='first':
                first_posts.wait(3)
                if sender=='one':
                    return interpret_chat_response({'data':{'prompts':'由于本场直播的设置，无法发布评论'},'status_code':4010048})
                if not restricted.wait(3): raise TimeoutError('Room stop was not published')
                return dict(ok=True,response={'data':{'msg_id':str(number)}})
            local.listener.record_comment(message,unique_id=sender,message_id=str(number))
            return dict(ok=True,response={'data':{'msg_id':str(number)}})
        def progress(snapshot):
            if any('room_comment_restricted' in account.get('failure_reasons',[])
                    for room in snapshot['rooms'] for account in room['accounts']):
                restricted.set()
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory)
            path=root/'accounts.json'
            store=load_account_store(path)
            for name in ('one','two'):
                account=upsert_account(store,[{'name':'uid_tt','value':'synthetic-'+name,'domain':'.tiktok.com'}],datetime.now(timezone.utc))
                account['username']=name
            save_account_store_atomic(path,store)
            with patch.object(runner,'_start_live_listener',side_effect=start), \
                    patch.object(runner,'room_heartbeat_session',side_effect=lambda *a,**kw:nullcontext()), \
                    patch.object(runner,'send_live_message',side_effect=send),patch.object(runner,'_pause'):
                result=parallel_runner.run_parallel_targets(('first','second'),'hello',store_path=path,
                    temporary_root=root/'tmp',report_dir=root/'reports',selected_account_ids=None,
                    message_count=3,message_interval_seconds=.1,should_cancel=lambda:False,
                    on_progress=progress,runner_kwargs={'interval_seconds':0,'observation_retries':3,
                        'observation_timeout_seconds':.05})
        self.assertEqual(posts,{('first','one'):1,('first','two'):1,('second','one'):3,('second','two'):3})
        self.assertFalse(result['cancelled'])
        self.assertEqual(result['rooms'][1]['status'],'completed')
        self.assertEqual(result['rooms'][1]['delivered'],6)
        self.assertFalse(any('监听期内未发现实际弹幕 |' in line for line in logs),
            'Room-stop interruption must not masquerade as a full observation timeout')

    def test_exact_live_settings_response_is_terminal_room_restriction(self):
        result=interpret_chat_response({'data':{'prompts':'由于本场直播的设置，无法发布评论'},'status_code':4010048})
        self.assertTrue(result.get('roomCommentsRestricted'))
        self.assertTrue(result.get('stopProcessing'))
        self.assertFalse(result.get('requiresObservation'))
        self.assertEqual(result.get('classification'),'room_comment_restricted')

    def test_room_restriction_cancels_only_current_room_and_next_starts_after_cleanup(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory)
            path=root/'accounts.json'
            store=load_account_store(path)
            for name in ('one','two'):
                account=upsert_account(store,[{'name':'uid_tt','value':'synthetic-'+name,'domain':'.tiktok.com'}],datetime.now(timezone.utc))
                account['username']=name
            save_account_store_atomic(path,store)
            ids=[a['id'] for a in store['accounts']]
            barriers={name:threading.Barrier(2) for name in ('first','second')}
            closed=set()
            lock=threading.Lock()
            progress=[]
            def fake(room,message,**kwargs):
                index=ids.index(kwargs['selected_account_ids'][0])
                if room=='second':
                    with lock:
                        self.assertIn(index,closed,'Account advanced before its own connection cleanup')
                barriers[room].wait(2)
                if room=='first':
                    if index==0:
                        kwargs['on_progress']({'failed':1,'failure_reasons':['room_comment_restricted']})
                        self.assertTrue(kwargs['should_cancel']())
                    else:
                        deadline=time.monotonic()+2
                        while not kwargs['should_cancel']() and time.monotonic()<deadline:
                            time.sleep(.005)
                        self.assertTrue(kwargs['should_cancel']())
                    with lock: closed.add(index)
                    return {'failed':1 if index==0 else 0,'cancelled':True,
                            'failure_reasons':['room_comment_restricted'] if index==0 else []}
                self.assertFalse(kwargs['should_cancel']())
                return {'success':2,'delivered':2}
            with patch.object(parallel_runner,'run_saved_accounts',side_effect=fake):
                result=parallel_runner.run_parallel_targets(('first','second'),'hello',store_path=path,
                    temporary_root=root/'tmp',report_dir=root/'reports',selected_account_ids=tuple(ids),
                    message_count=2,message_interval_seconds=.1,should_cancel=lambda:False,
                    on_progress=progress.append,runner_kwargs={'interval_seconds':0})
        self.assertFalse(result['cancelled'],'Room restriction must not cancel the whole run')
        self.assertEqual(result['rooms'][1]['status'],'completed')
        self.assertEqual(result['rooms'][1]['delivered'],4)
        for account in result['rooms'][0]['accounts']:
            self.assertIn('room_comment_restricted',account['failure_reasons'])
            self.assertFalse(account['cancelled'])
