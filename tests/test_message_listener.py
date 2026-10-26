import tempfile
import time
import unittest
import asyncio
import io
import json
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from loguru import logger

import listen_msg
from tiktok_live import account_store, runner
from tiktok_live.errors import SafeOperationError


class EventUserLabelTests(unittest.TestCase):
    def test_missing_event_user_uses_placeholder(self):
        event = SimpleNamespace(user=None)

        self.assertEqual(listen_msg._event_user_label(event), "-")

    def test_event_user_prefers_unique_id_then_nickname_then_id(self):
        self.assertEqual(
            listen_msg._event_user_label(SimpleNamespace(user=SimpleNamespace(
                unique_id="@sender", nickname="Sender", id="123"
            ))),
            "sender",
        )
        self.assertEqual(
            listen_msg._event_user_label(SimpleNamespace(user=SimpleNamespace(
                unique_id="", nickname="Sender", id="123"
            ))),
            "Sender",
        )


class MessageListenerBufferTests(unittest.TestCase):
    def test_wss_logs_distinguish_observer_from_account_heartbeat(self):
        from tests.support.direct_fakes import visitor, WaitingConnection
        output = io.StringIO()
        sink = logger.add(output, format='{message}')
        try:
            for role in ('delivery_observer', 'account_heartbeat'):
                listener = listen_msg.LiveMessageListener('host', show_comments=False,
                    session_factory=visitor, connection_factory=WaitingConnection, connection_role=role)
                self.assertTrue(listener.start(1))
                listener.close()
        finally:
            logger.remove(sink)
        self.assertIn('role=delivery_observer', output.getvalue())
        self.assertIn('role=account_heartbeat', output.getvalue())
        self.assertNotIn('SYNTHETIC', output.getvalue())

    def test_repeated_content_receipts_match_message_ids_and_preserve_later_receipts(self):
        listener = listen_msg.LiveMessageListener('host', show_comments=False)
        listener.record_comment('hello', unique_id='viewer', message_id='18446744073709551614')
        listener.record_comment('hello', unique_id='viewer', message_id='first')
        first = listener.wait_for_comment('hello', since=0, timeout=.1, expected_message_ids={'first'})
        second = listener.wait_for_comment('hello', since=0, timeout=.1,
            expected_message_ids={'18446744073709551614'})
        self.assertEqual(first.message_id, 'first')
        self.assertEqual(second.message_id, '18446744073709551614')

    def test_queue_overflow_is_bounded_and_nonblocking(self):
        listener = listen_msg.LiveMessageListener('host', show_comments=False)
        for i in range(5000):
            listener.record_comment('hello', message_id=str(i))
        self.assertEqual(listener._comments.qsize(), 4096)
        self.assertEqual(listener._comments.get_nowait().message_id, '904')

    def test_comment_console_output_can_be_disabled_without_disabling_capture(self):
        from tiktok_live.wss.direct_models import ChatEvent
        listener = listen_msg.LiveMessageListener('host', show_comments=False)
        with patch('builtins.print') as output:
            listener._on_comment(ChatEvent('123', '2222222222222222222', 'spider_king017', 'Spider', 'hello'))
        output.assert_not_called()
        self.assertIsNotNone(listener.wait_for_comment('hello', since=0, timeout=.01))

    def test_observer_default_never_receives_account_cookie(self):
        from tests.support.direct_fakes import visitor, WaitingConnection
        with patch.object(listen_msg, 'prepare_observer', return_value=visitor()) as prepare:
            listener = listen_msg.LiveMessageListener('host', connection_factory=WaitingConnection)
            try:
                self.assertTrue(listener.start(1))
                self.assertEqual(prepare.call_args.args, ('host',))
                self.assertIs(prepare.call_args.kwargs['startup_scope'], listener._startup_scope)
                self.assertEqual(listener.connection.session.cookie_header, 'ttwid=SYNTHETIC')
            finally:
                listener.close()

    def test_observer_preparation_failure_reports_safe_stage(self):
        output = io.StringIO()
        sink = logger.add(output, format='{message}')
        try:
            with patch.object(listen_msg, 'prepare_observer', side_effect=ValueError('private URL with cookie')):
                listener = listen_msg.LiveMessageListener('host', show_comments=False)
                self.assertFalse(listener.start(1))
                self.assertEqual(listener.failure_stage, 'observer_prepare')
                listener.close()
        finally:
            logger.remove(sink)
        self.assertIn('stage=observer_prepare', output.getvalue())
        self.assertNotIn('private URL with cookie', output.getvalue())

    def test_wait_for_comment_matches_content_time_and_sender_username(self):
        listener = listen_msg.LiveMessageListener("host")
        since = time.monotonic()
        listener.record_comment(
            "hello", unique_id="another_user", observed_at=since + 0.1
        )
        listener.record_comment(
            "hello", unique_id="spider_king017", observed_at=since + 0.2
        )

        observed = listener.wait_for_comment(
            "hello", since=since,
            expected_usernames={"spider_king017"}, timeout=0.01,
        )

        self.assertIsNotNone(observed)
        self.assertEqual(observed.unique_id, "spider_king017")

    def test_numeric_comment_matches_sender_nickname_when_unique_id_is_missing(self):
        listener = listen_msg.LiveMessageListener("host")
        since = time.monotonic()
        listener.record_comment(
            "6662",
            unique_id="",
            nickname="boaluodyblala1",
            user_id="7686807371494851604",
            observed_at=since + 0.1,
        )

        observed = listener.wait_for_comment(
            "6662",
            since=since,
            expected_usernames={"boaluodyblala1"},
            expected_user_ids={"7686807371494851604"},
            timeout=0.01,
        )

        self.assertIsNotNone(observed)
        self.assertEqual(observed.nickname, "boaluodyblala1")

    def test_numeric_comment_normalizes_fullwidth_digits(self):
        listener = listen_msg.LiveMessageListener("host")
        since = time.monotonic()
        listener.record_comment(
            "６６６２",
            unique_id="boaluodyblala1",
            observed_at=since + 0.1,
        )

        observed = listener.wait_for_comment(
            "6662",
            since=since,
            expected_usernames={"boaluodyblala1"},
            timeout=0.01,
        )

        self.assertIsNotNone(observed)

    def test_emoji_comment_ignores_presentation_variation_selector(self):
        listener = listen_msg.LiveMessageListener("host")
        since = time.monotonic()
        listener.record_comment(
            "❤️",
            unique_id="boaluodyblala1",
            observed_at=since + 0.01,
        )

        observed = listener.wait_for_comment(
            "❤",
            since=since,
            expected_usernames={"boaluodyblala1"},
            timeout=0.01,
        )

        self.assertIsNotNone(observed)
    def test_wait_for_comment_does_not_accept_old_or_wrong_sender_message(self):
        listener = listen_msg.LiveMessageListener("host")
        since = time.monotonic()
        listener.record_comment(
            "hello", unique_id="spider_king017", observed_at=since - 1
        )
        listener.record_comment(
            "hello", unique_id="another_user", observed_at=since + 0.1
        )

        observed = listener.wait_for_comment(
            "hello", since=since,
            expected_usernames={"spider_king017"}, timeout=0.01,
        )

        self.assertIsNone(observed)

    def test_wait_for_comment_uses_numeric_uid_when_username_is_not_saved_yet(self):
        listener = listen_msg.LiveMessageListener("host")
        since = time.monotonic()
        listener.record_comment(
            "hello",
            unique_id="spider_king017",
            user_id="2222222222222222222",
            observed_at=since + 0.1,
        )

        observed = listener.wait_for_comment(
            "hello",
            since=since,
            expected_user_ids={"2222222222222222222"},
            timeout=0.01,
        )

        self.assertIsNotNone(observed)
        self.assertEqual(observed.unique_id, "spider_king017")


class RunnerObservationTests(unittest.TestCase):
    def setUp(self):
        token_patch = patch.object(runner, 'get_fresh_ms_token', return_value='synthetic-fresh')
        room_patch = patch.object(runner, 'get_live_room', return_value={
            'exists': True, 'roomId': '123456789012', 'username': 'host'})
        token_patch.start()
        room_patch.start()
        self.addCleanup(token_patch.stop)
        self.addCleanup(room_patch.stop)
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / "accounts.json"
        store = {"version": 1, "accounts": []}
        account_store.upsert_account(store, [
            {"name": "multi_sids", "value": "2222222222222222222%3Asession", "domain": ".tiktok.com"},
            {"name": "sessionid", "value": "session", "domain": ".tiktok.com"},
        ], datetime(2026, 10, 10, tzinfo=timezone.utc))
        account_store.save_account_store_atomic(self.path, store)

    def test_observer_uses_account_room_before_opening_guest_connection(self):
        second = SimpleNamespace(start=lambda **kw: True, error=None,
                                 failure_stage=None, close=lambda: None)
        with patch.object(runner, 'LiveMessageListener', return_value=second) as factory:
            result = runner._start_live_listener('host', connect_timeout=2,
                connect_retries=0, retry_delay_seconds=1, show_comments=False,
                resolve_room_id=lambda: '42')
        self.assertIs(result, second)
        self.assertEqual(factory.call_count, 1)
        self.assertEqual(factory.call_args.kwargs['room_id'], '42')

    def test_observer_preserves_page_fetch_endpoint_for_sender(self):
        endpoint = 'https://webcast.eu.tiktok.com/webcast/im/fetch/'
        listener = SimpleNamespace(start=lambda **kw: True, error=None,
                                   failure_stage=None, close=lambda: None)
        with patch.object(runner, 'LiveMessageListener', return_value=listener) as factory:
            result = runner._start_live_listener(
                'host', connect_timeout=2, connect_retries=0,
                retry_delay_seconds=1, show_comments=False,
                resolve_room_id=lambda: {
                    'roomId': '42', 'fetchEndpoint': endpoint, 'region': 'JP',
                    'deviceId': '7123456789012345678',
                },
            )
        self.assertIs(result, listener)
        self.assertEqual('42', factory.call_args.kwargs['room_id'])
        self.assertEqual(endpoint, result.fetch_endpoint)
        self.assertEqual('JP', result.region)
        self.assertEqual('7123456789012345678', result.device_id)

    def test_transport_failure_does_not_repeat_account_room_lookup(self):
        first = SimpleNamespace(start=lambda **kw: False, error=RuntimeError('transport'),
                                failure_stage='wss_transport', close=lambda: None)
        lookup = unittest.mock.Mock(return_value='42')
        with patch.object(runner, 'LiveMessageListener', return_value=first):
            result = runner._start_live_listener('host', connect_timeout=2,
                connect_retries=0, retry_delay_seconds=1, show_comments=False,
                resolve_room_id=lookup)
        self.assertIsNone(result)
        lookup.assert_called_once()

    def test_unconfirmed_room_lookup_stops_before_guest_connection(self):
        failed = SimpleNamespace(start=lambda **kw: False, error=ValueError('WAF'),
                                 failure_stage='observer_prepare', close=lambda: None)
        lookup = unittest.mock.Mock(return_value=None)
        with patch.object(runner, 'LiveMessageListener', return_value=failed) as factory:
            with self.assertRaises(SafeOperationError):
                runner._start_live_listener('tt.yam1', connect_timeout=2,
                    connect_retries=3, retry_delay_seconds=0, show_comments=False,
                    resolve_room_id=lookup)
        self.assertEqual(factory.call_count, 0)
        self.assertEqual(lookup.call_count, 1)

    def test_room_lookup_uses_only_selected_account_and_closes_session(self):
        store = account_store.load_account_store(self.path)
        selected = store['accounts'][0]
        other = dict(selected, id='not-selected')
        session = SimpleNamespace(close=unittest.mock.Mock())
        with patch.object(runner.requests, 'Session', return_value=session), \
                patch.object(runner, 'get_live_room', return_value={
                    'exists': True, 'roomId': '123456789012'}) as get_room:
            room = runner._resolve_observer_room_id('host', [other, selected],
                {selected['id']}, 'sessionid=room-lookup', None, connect_timeout=5)
        self.assertEqual(room['roomId'], '123456789012')
        self.assertEqual(get_room.call_count, 1)
        self.assertIn('sessionid=session', get_room.call_args.args[2])
        self.assertEqual(get_room.call_args.kwargs['fallback_cookie'], 'sessionid=room-lookup')
        self.assertLessEqual(get_room.call_args.kwargs['timeout_seconds'], 5)
        session.close.assert_called_once()

    def test_room_lookup_rejects_an_explicitly_different_owner(self):
        store = account_store.load_account_store(self.path)
        session = SimpleNamespace(close=unittest.mock.Mock())
        with patch.object(runner.requests, 'Session', return_value=session), \
                patch.object(runner, 'get_live_room', return_value={
                    'exists': True, 'roomId': '123456789012', 'username': 'another_host'}):
            with self.assertRaises(SafeOperationError):
                runner._resolve_observer_room_id('host', store['accounts'],
                    None, '', None, connect_timeout=5)

    def test_runner_reuses_fallback_room_id_for_send(self):
        endpoint = 'https://webcast.eu.tiktok.com/webcast/im/fetch/'
        second = SimpleNamespace(start=lambda **kw: True, error=None, room_id='123456789012',
                                 fetch_endpoint=endpoint,
                                 region='JP',
                                 device_id='7123456789012345678',
                                 failure_stage=None, close=lambda: None,
                                 wait_for_comment=lambda *args, **kwargs: None)
        response = {'ok': True, 'roomId': '123456789012',
                    'response': {'status_code': 0, 'data': {'msg_id': '987'}}}
        with patch.object(runner, 'LiveMessageListener', return_value=second) as listeners, \
                patch.object(runner, '_resolve_observer_room_id', return_value='123456789012'), \
                patch.object(runner, 'send_live_message', return_value=response) as send:
            runner.run_saved_accounts('host', 'hello', self.path, add_new_account=False,
                observation_enabled=True, observer_connect_retries=0,
                observation_timeout_seconds=.01, observation_retries=1)
        self.assertEqual(listeners.call_args.kwargs['room_id'], '123456789012')
        self.assertEqual(send.call_args.kwargs['known_room_id'], '123456789012')
        self.assertEqual(send.call_args.kwargs['known_fetch_endpoint'], endpoint)
        self.assertEqual(send.call_args.kwargs['known_region'], 'JP')
        self.assertEqual(send.call_args.kwargs['known_device_id'], '7123456789012345678')

    def _run(self, *, connected=True, observed=True, show_comments=True, response=None):
        instance = SimpleNamespace(
            start=lambda **kwargs: connected,
            wait_for_comment=lambda *args, **kwargs: (
                SimpleNamespace(
                    user_id="2222222222222222222",
                    unique_id="spider_king017",
                    comment="hello",
                ) if observed else None
            ),
            close=lambda: None,
        )
        response = response or {
            "ok": True,
            "roomId": "123456789012",
            "response": {"status_code": 0, "data": {"msg_id": "987"}},
        }
        with patch.object(runner, "LiveMessageListener", return_value=instance, create=True) as factory, \
                patch.object(runner, "send_live_message", return_value=response):
            result = runner.run_saved_accounts(
                "host", "hello", self.path,
                add_new_account=False,
                interval_seconds=0,
                observation_enabled=True,
                observation_timeout_seconds=0.01,
                observer_connect_retries=0,
                show_live_comments_in_console=show_comments,
            )
        self.listener_factory = factory
        return result

    def test_comment_disabled_prompt_is_success_when_comment_is_observed(self):
        result = self._run(observed=True, response={
            "ok": False,
            "statusCode": 4010048,
            "classification": "comment_delivery_uncertain",
            "requiresObservation": True,
            "stopProcessing": False,
        })
        self.assertEqual(result["success"], 1)
        self.assertEqual(result["failed"], 0)
        self.assertEqual(result["delivered"], 1)

    def test_comment_disabled_prompt_is_failure_when_comment_is_not_observed(self):
        result = self._run(observed=False, response={
            "ok": False,
            "statusCode": 4010048,
            "classification": "comment_delivery_uncertain",
            "requiresObservation": True,
            "stopProcessing": False,
        })
        self.assertEqual(result["success"], 0)
        self.assertEqual(result["failed"], 1)
        self.assertEqual(result["delivered"], 0)
        self.assertEqual(result["acceptedNotObserved"], 1)

    def test_successful_request_is_marked_delivered_when_comment_is_observed(self):
        result = self._run(observed=True)
        self.assertEqual(result["success"], 1)
        self.assertEqual(result["delivered"], 1)
        self.assertEqual(result["acceptedNotObserved"], 0)
        self.assertEqual(result["listenerUnavailable"], 0)
        saved = account_store.load_account_store(self.path)
        self.assertEqual(saved["accounts"][0]["username"], "spider_king017")

    def test_successful_request_is_marked_not_observed_after_timeout(self):
        result = self._run(observed=False)
        self.assertEqual(result["success"], 1)
        self.assertEqual(result["delivered"], 0)
        self.assertEqual(result["acceptedNotObserved"], 1)

    def test_not_observed_attempts_three_times_total_with_two_second_delays(self):
        listener = SimpleNamespace(
            start=lambda **kwargs: True,
            wait_for_comment=unittest.mock.Mock(side_effect=[None, None, None]),
            close=lambda: None,
        )
        retry_post = unittest.mock.Mock()

        class RetryableResult(dict):
            def retry_message(self):
                retry_post()
                return self

        response = RetryableResult({
            "ok": True,
            "roomId": "123456789012",
            "response": {"status_code": 0, "data": {"msg_id": "987"}},
        })
        with patch.object(runner, "LiveMessageListener", return_value=listener), \
                patch.object(runner, "send_live_message", return_value=response) as send, \
                patch.object(runner.time, "sleep") as sleep:
            result = runner.run_saved_accounts(
                "host", "hello", self.path,
                add_new_account=False,
                interval_seconds=0,
                observation_enabled=True,
                observation_timeout_seconds=0.01,
                observation_retries=3,
                observation_retry_delay_seconds=2,
                observer_connect_retries=0,
            )

        send.assert_called_once()
        self.assertEqual(retry_post.call_count, 2)
        self.assertEqual(listener.wait_for_comment.call_count, 3)
        self.assertEqual([item.args for item in sleep.call_args_list], [(2,), (2,)])
        self.assertEqual(result["success"], 1)
        self.assertEqual(result["delivered"], 0)
        self.assertEqual(result["acceptedNotObserved"], 1)

    def test_observed_retry_stops_after_message_is_seen(self):
        observed = SimpleNamespace(
            user_id="2222222222222222222",
            unique_id="spider_king017",
            comment="hello",
        )
        listener = SimpleNamespace(
            start=lambda **kwargs: True,
            wait_for_comment=unittest.mock.Mock(side_effect=[None, observed]),
            close=lambda: None,
        )
        retry_post = unittest.mock.Mock()

        class RetryableResult(dict):
            def retry_message(self):
                retry_post()
                return self

        response = RetryableResult({
            "ok": True,
            "roomId": "123456789012",
            "response": {"status_code": 0, "data": {"msg_id": "987"}},
        })
        with patch.object(runner, "LiveMessageListener", return_value=listener), \
                patch.object(runner, "send_live_message", return_value=response) as send, \
                patch.object(runner.time, "sleep") as sleep:
            result = runner.run_saved_accounts(
                "host", "hello", self.path,
                add_new_account=False,
                interval_seconds=0,
                observation_enabled=True,
                observation_timeout_seconds=0.01,
                observation_retries=3,
                observation_retry_delay_seconds=2,
                observer_connect_retries=0,
            )

        send.assert_called_once()
        retry_post.assert_called_once()
        self.assertEqual(listener.wait_for_comment.call_count, 2)
        sleep.assert_called_once_with(2)
        self.assertEqual(result["success"], 1)
        self.assertEqual(result["delivered"], 1)
        self.assertEqual(result["acceptedNotObserved"], 0)

    def test_explicit_send_failure_is_not_retried_for_observation(self):
        listener = SimpleNamespace(
            start=lambda **kwargs: True,
            wait_for_comment=unittest.mock.Mock(),
            close=lambda: None,
        )
        response = {"ok": False, "classification": "send_failed"}
        with patch.object(runner, "LiveMessageListener", return_value=listener), \
                patch.object(runner, "send_live_message", return_value=response) as send, \
                patch.object(runner.time, "sleep") as sleep:
            result = runner.run_saved_accounts(
                "host", "hello", self.path,
                add_new_account=False,
                interval_seconds=0,
                observation_enabled=True,
                observation_timeout_seconds=0.01,
                observation_retries=3,
                observation_retry_delay_seconds=2,
                observer_connect_retries=0,
            )

        send.assert_called_once()
        listener.wait_for_comment.assert_not_called()
        sleep.assert_not_called()
        self.assertEqual(result["failed"], 1)

    def test_listener_failure_aborts_sending(self):
        from tiktok_live.errors import SafeOperationError
        with self.assertRaises(SafeOperationError) as caught:
            self._run(connected=False, observed=False)
        self.assertEqual(caught.exception.classification, 'listener_unavailable')

    def test_exhausted_listener_retries_abort_before_any_account_send(self):
        for raises in (False, True):
            with self.subTest(start_raises=raises):
                listener = SimpleNamespace(
                    start=unittest.mock.Mock(
                        side_effect=RuntimeError("private detail") if raises else None,
                        return_value=False,
                    ),
                    close=unittest.mock.Mock(),
                )
                original_store = self.path.read_bytes()
                with patch.object(runner, "LiveMessageListener", return_value=listener) as factory, \
                        patch.object(runner, "send_live_message") as send, \
                        patch.object(runner, "get_live_room", return_value={
                            "exists": True, "roomId": "123456789012", "username": "wrong_host"}), \
                        patch.object(runner.time, "sleep") as sleep:
                    with self.assertRaises(SafeOperationError) as caught:
                        runner.run_saved_accounts(
                            "wrong_host", "hello", self.path,
                            add_new_account=False, observation_enabled=True,
                            observer_connect_retries=3,
                            observer_retry_delay_seconds=2,
                        )
                    send.assert_not_called()
                self.assertEqual(caught.exception.classification, "listener_unavailable")
                self.assertEqual(factory.call_count, 4)
                self.assertEqual(listener.close.call_count, 4)
                self.assertEqual(sleep.call_count, 3)
                self.assertEqual(self.path.read_bytes(), original_store)

    def test_runner_passes_console_comment_switch_to_listener(self):
        self._run(show_comments=False)
        self.listener_factory.assert_called_once_with("host", room_id='123456789012',
            show_comments=False, heartbeat_interval_seconds=10.0)

    def test_final_summary_includes_observation_and_lists_failed_account_identity(self):
        store = account_store.load_account_store(self.path)
        store["accounts"][0]["username"] = "spider_king017"
        account_store.save_account_store_atomic(self.path, store)
        output = io.StringIO()
        sink = logger.add(output, format="{message}")
        try:
            result = self._run(observed=False)
        finally:
            logger.remove(sink)

        text = output.getvalue()
        self.assertEqual(result["acceptedNotObserved"], 1)
        self.assertIn(
            "失败账号 | account_id=5af2b986527e | username=spider_king017 "
            "| classification=accepted_not_observed",
            text,
        )
        report = json.loads(
            self.path.with_name("failed_accounts.json").read_text(encoding="utf-8")
        )
        self.assertEqual(report["failed_accounts"], [{
            "account_id": "5af2b986527e",
            "username": "spider_king017",
            "classification": "accepted_not_observed",
        }])
        self.assertEqual(text.count("本次处理完成 |"), 1)
        self.assertIn("delivered=0 | acceptedNotObserved=1", text)
        self.assertNotIn("展示确认汇总 |", text)

    def test_listener_connection_retries_three_times_with_two_second_delays(self):
        closed = []

        def instance(connected, number):
            return SimpleNamespace(
                start=lambda **kwargs: connected,
                wait_for_comment=lambda *args, **kwargs: SimpleNamespace(
                    user_id="2222222222222222222",
                    unique_id="spider_king017",
                    comment="hello",
                ),
                close=lambda: closed.append(number),
            )

        listeners = [
            instance(False, 1),
            instance(False, 2),
            instance(False, 3),
            instance(True, 4),
        ]
        response = {"ok": True, "response": {"status_code": 0}}
        with patch.object(runner, "LiveMessageListener", side_effect=listeners) as factory, \
                patch.object(runner, "send_live_message", return_value=response), \
                patch.object(runner.time, "sleep") as sleep:
            result = runner.run_saved_accounts(
                "host", "hello", self.path,
                add_new_account=False,
                interval_seconds=0,
                observation_enabled=True,
                observation_timeout_seconds=0.01,
                observer_connect_retries=3,
                observer_retry_delay_seconds=2,
            )

        self.assertEqual(factory.call_count, 4)
        self.assertEqual([item.args[0] for item in sleep.call_args_list], [2, 2, 2])
        self.assertEqual(closed, [1, 2, 3, 4])
        self.assertEqual(result["delivered"], 1)


if __name__ == "__main__":
    unittest.main()
