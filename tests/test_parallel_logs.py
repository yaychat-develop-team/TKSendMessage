import tempfile
import threading
import unittest
import asyncio
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace

from loguru import logger
from PySide6.QtCore import Qt

from desktop.log_bridge import DailyLogSink, QtLogSink
from tiktok_live import runner
from tiktok_live.client import log_chat_response
from listen_msg import LiveMessageListener
from tests.support.direct_fakes import visitor, WaitingConnection


class ParallelLogTests(unittest.TestCase):
    def test_only_observed_message_uses_success_level(self):
        records = []
        handle = logger.add(lambda message: records.append(message.record))
        try:
            for status in ('observed', 'not_observed', 'listener_unavailable', 'send_failed'):
                runner.log_message_display(room='host', account={'id': 'a' * 64, 'username': 'alice'},
                    index=1, total=1, message_id='123', status=status, classification='delivered')
        finally:
            logger.remove(handle)
        self.assertEqual(records[0]['level'].name, 'SUCCESS')
        self.assertTrue(all(record['level'].name != 'SUCCESS' for record in records[1:]))

    def test_native_listener_threads_preserve_both_sink_contexts_and_standalone_logs(self):
        Client = WaitingConnection

        output = []
        qt_sink = QtLogSink()
        qt_sink.message.connect(output.append, Qt.ConnectionType.DirectConnection)
        with tempfile.TemporaryDirectory() as directory:
            handles = [logger.add(qt_sink, format='{message}'),
                       logger.add(DailyLogSink(directory), format='{message}')]
            try:
                for name, role in (('alice', 'delivery_observer'), ('bob', 'account_heartbeat')):
                    listener = LiveMessageListener('host', session_factory=visitor, connection_factory=Client, show_comments=False)
                    listener.connection_role = role
                    try:
                        with logger.contextualize(target_room='room1', sender_account=name,
                                                  sender_id='a' * 12):
                            self.assertTrue(listener.start(connect_timeout=2))
                    finally:
                        # Cleanup logs must retain the captured context after its caller exits.
                        listener.close()
                    self.assertFalse(listener._thread.is_alive())
                standalone = LiveMessageListener('standalone', session_factory=visitor, connection_factory=Client, show_comments=False)
                try:
                    self.assertTrue(standalone.start(connect_timeout=2))
                finally:
                    standalone.close()
                logger.info('plain record')
            finally:
                for handle in handles:
                    logger.remove(handle)
            file_text = next(Path(directory).glob('*.log')).read_text(encoding='utf-8')
        for name, role in (('alice', 'delivery_observer'), ('bob', 'account_heartbeat')):
            lines = [line for line in output if f'role={role}' in line and 'username=host' in line]
            self.assertEqual(len(lines), 3)
            for line in lines:
                self.assertTrue(line.startswith(
                    f'room=room1 | account_username={name} | account_id=aaaaaaaaaaaa | '))
                self.assertIn(line, file_text)
        standalone_lines = [line for line in output if 'username=standalone' in line]
        self.assertEqual(len(standalone_lines), 3)
        self.assertTrue(all('account_username=' not in line for line in standalone_lines))
        self.assertEqual(output[-1], 'plain record')
        self.assertNotIn('room=', file_text.splitlines()[-1])

    def test_concurrent_sinks_keep_room_account_and_leave_plain_records_unchanged(self):
        output = []
        qt_sink = QtLogSink()
        qt_sink.message.connect(output.append, Qt.ConnectionType.DirectConnection)
        barrier = threading.Barrier(4)
        with tempfile.TemporaryDirectory() as directory:
            handles = [logger.add(qt_sink, format='{message}'),
                       logger.add(DailyLogSink(directory), format='{message}')]
            def emit(room, account, account_id):
                with logger.contextualize(target_room=room, sender_account=account,
                                          sender_id=account_id):
                    barrier.wait(timeout=5)
                    logger.info('identical body')
            try:
                with ThreadPoolExecutor(max_workers=4) as pool:
                    futures = [pool.submit(emit, room, name, aid)
                        for room in ('room1', 'room2')
                        for name, aid in (('alice', 'a' * 12), ('bob', 'b' * 12))]
                    for future in futures:
                        future.result(timeout=10)
                logger.info('plain record')
            finally:
                for handle in handles:
                    logger.remove(handle)
            file_text = next(Path(directory).glob('*.log')).read_text(encoding='utf-8')
        expected = {f'room={room} | account_username={name} | account_id={aid} | identical body'
                    for room in ('room1', 'room2')
                    for name, aid in (('alice', 'a' * 12), ('bob', 'b' * 12))}
        self.assertEqual(set(output[:-1]), expected)
        for line in expected:
            self.assertEqual(file_text.count(line), 1)
        self.assertEqual(output[-1], 'plain record')
        self.assertNotIn('room=', file_text.splitlines()[-1])

    def test_display_metadata_rejects_response_text_and_falls_back_to_short_id(self):
        self.assertTrue(callable(getattr(runner, 'log_message_display', None)),
                        'runner must expose per-message display logging')
        lines = []
        handle = logger.add(lambda msg: lines.append(msg.record['message']))
        try:
            runner.log_message_display(room='@room1', account={'id': 'a' * 64},
                index=1, total=3, message_id='sessionid=private',
                status='listener_unavailable', classification='listener_unavailable')
        finally:
            logger.remove(handle)
        self.assertEqual(len(lines), 1)
        self.assertIn('room=room1 | account_username=aaaaaaaaaaaa | account_id=aaaaaaaaaaaa', lines[0])
        self.assertIn('message_index=1/3 | message_id=- | display_status=listener_unavailable', lines[0])
        self.assertNotIn('private', lines[0])

    def test_response_preview_truncation_keeps_attribution_on_both_sinks(self):
        output = []
        qt_sink = QtLogSink()
        qt_sink.message.connect(output.append)
        with tempfile.TemporaryDirectory() as directory:
            handles = [logger.add(qt_sink, format='{message}'),
                       logger.add(DailyLogSink(directory), format='{message}')]
            try:
                with logger.contextualize(target_room='room1', sender_account='alice',
                                          sender_id='a' * 12):
                    log_chat_response({'data': '中' * 200})
                    runner.log_message_display(room='room1',
                        account={'id': 'a' * 64, 'username': 'alice'},
                        index=1, total=1, message_id='123',
                        status='observed', classification='delivered')
            finally:
                for handle in handles:
                    logger.remove(handle)
            file_text = next(Path(directory).glob('*.log')).read_text(encoding='utf-8')
        self.assertEqual(len(output), 2)
        for line in output:
            self.assertTrue(line.startswith('room=room1 | account_username=alice | account_id=aaaaaaaaaaaa | '))
            self.assertIn(line, file_text)
        self.assertEqual(output[0].split('response=', 1)[1], '{"data":"' + '中' * 141 + '…（已截断）')
        self.assertIn('message_index=1/1 | message_id=123 | display_status=observed', output[1])
