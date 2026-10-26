import hashlib
import json
import re
import subprocess
import os
import tempfile
import sys
from types import SimpleNamespace
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import call, patch
from contextlib import ExitStack, nullcontext

import send_live_message as subject
from tiktok_live import browser_auth
from tiktok_live import cli as package_cli


class OfflineSession:
    """HTTP boundary double; no network, with the real close lifecycle."""
    def __init__(self, **kwargs):
        self.closed = False

    def close(self):
        self.closed = True


class AccountStoreTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / 'accounts.json'
        self.now = datetime(2026, 10, 9, tzinfo=timezone.utc)

    def cookies(self):
        return [{'name': 'uid_tt', 'value': 'synthetic-user', 'domain': '.tiktok.com',
                 'path': '/', 'expires': -1, 'httpOnly': True, 'secure': True,
                 'sameSite': 'Lax'}]

    def test_missing_store_returns_versioned_empty_store(self):
        self.assertEqual(subject.load_account_store(self.path), {'version': 1, 'accounts': []})
        self.assertFalse(self.path.exists())

    def test_malformed_store_is_preserved_and_rejected(self):
        for raw in ('{broken', '[]', '{"version": 2, "accounts": []}',
                    '{"version": 1, "accounts": {}}'):
            self.path.write_text(raw, encoding='utf-8')
            with self.assertRaises(ValueError):
                subject.load_account_store(self.path)
            self.assertTrue(self.path.read_text(encoding='utf-8') == raw)

    def test_upsert_same_account_updates_without_duplicate(self):
        store = {'version': 1, 'accounts': []}
        cookies = self.cookies()
        first = subject.upsert_account(store, cookies, self.now)
        cookies.append({'name': 'sessionid', 'value': 'synthetic-session', 'domain': 'www.tiktok.com'})
        later = datetime(2026, 10, 10, tzinfo=timezone.utc)
        second = subject.upsert_account(store, cookies, later)
        self.assertEqual(len(store['accounts']), 1)
        self.assertEqual(first['id'], second['id'])
        self.assertEqual(second['id'], hashlib.sha256(b'synthetic-user').hexdigest())
        self.assertEqual(second['created_at'], self.now.isoformat())
        self.assertEqual(second['updated_at'], later.isoformat())
        self.assertTrue(second['cookies'] == cookies)
        cookies.clear()
        self.assertTrue(bool(second['cookies']))
        self.assertTrue(subject.remove_account(store, second['id']))
        self.assertFalse(subject.remove_account(store, second['id']))
        with self.assertRaises(ValueError):
            subject.account_id_from_cookies([])
        identifiers = [{'name': name, 'value': 'synthetic-' + name}
                       for name in ('sid_tt', 'sessionid_ss', 'sessionid', 'uid_tt')]
        for name in ('uid_tt', 'sessionid', 'sessionid_ss', 'sid_tt'):
            self.assertEqual(subject.account_id_from_cookies(identifiers),
                             hashlib.sha256(('synthetic-' + name).encode()).hexdigest())
            identifiers = [item for item in identifiers if item['name'] != name]

    def test_upsert_assigns_stable_environment_seed_per_account(self):
        store = {'version': 1, 'accounts': []}
        first = subject.upsert_account(store, self.cookies(), self.now)
        original_seed = first['environment_seed']

        refreshed = subject.upsert_account(
            store,
            self.cookies() + [{'name': 'sessionid', 'value': 'rotated',
                               'domain': '.tiktok.com'}],
            datetime(2026, 10, 10, tzinfo=timezone.utc),
        )
        other = subject.upsert_account(
            store,
            [{'name': 'uid_tt', 'value': 'another-account',
              'domain': '.tiktok.com'}],
            self.now,
        )

        self.assertEqual(refreshed['environment_seed'], original_seed)
        self.assertNotEqual(other['environment_seed'], original_seed)
        self.assertRegex(original_seed, r'^[a-f0-9]{16}$')

    def test_upsert_repairs_stale_historical_id_before_adding_new_account(self):
        old_uid = '1111111111111111111'
        new_uid = '2222222222222222222'
        stale_id = hashlib.sha256(new_uid.encode()).hexdigest()
        store = {'version': 1, 'accounts': [{
            'id': stale_id,
            'created_at': self.now.isoformat(),
            'updated_at': self.now.isoformat(),
            'cookies': [{
                'name': 'multi_sids', 'value': f'{old_uid}%3Aold',
                'domain': '.tiktok.com',
            }],
        }]}
        new_cookies = [{
            'name': 'multi_sids', 'value': f'{new_uid}%3Anew',
            'domain': '.tiktok.com',
        }]

        added = subject.upsert_account(store, new_cookies, self.now)

        self.assertEqual(len(store['accounts']), 2)
        self.assertEqual(added['id'], stale_id)
        self.assertEqual(store['accounts'][0]['id'], hashlib.sha256(old_uid.encode()).hexdigest())

    def test_relogin_with_rotated_sessions_merges_by_numeric_tiktok_uid(self):
        store = {'version': 1, 'accounts': []}
        first_cookies = [
            {'name': 'multi_sids', 'value': '7686807371494851604%3Afirst-login',
             'domain': '.tiktok.com'},
            {'name': 'uid_tt', 'value': 'first-uid-token', 'domain': '.tiktok.com'},
            {'name': 'sessionid', 'value': 'first-session', 'domain': '.tiktok.com'},
        ]
        second_cookies = [
            {'name': 'multi_sids', 'value': '7686807371494851604%3Asecond-login',
             'domain': '.tiktok.com'},
            {'name': 'uid_tt', 'value': 'second-uid-token', 'domain': '.tiktok.com'},
            {'name': 'sessionid', 'value': 'second-session', 'domain': '.tiktok.com'},
        ]

        first = subject.upsert_account(store, first_cookies, self.now)
        second = subject.upsert_account(
            store, second_cookies, datetime(2026, 10, 10, tzinfo=timezone.utc))

        self.assertEqual(len(store['accounts']), 1)
        self.assertEqual(first['id'], second['id'])
        self.assertEqual(second['id'], hashlib.sha256(b'7686807371494851604').hexdigest())
        self.assertEqual(second['cookies'], second_cookies)

    def test_conflicting_numeric_tiktok_uids_never_merge_via_shared_session(self):
        store = {'version': 1, 'accounts': []}
        first_cookies = [
            {'name': 'multi_sids', 'value': '1111111111111111111%3Afirst',
             'domain': '.tiktok.com'},
            {'name': 'sessionid', 'value': 'shared-session', 'domain': '.tiktok.com'},
        ]
        second_cookies = [
            {'name': 'multi_sids', 'value': '2222222222222222222%3Asecond',
             'domain': '.tiktok.com'},
            {'name': 'sessionid', 'value': 'shared-session', 'domain': '.tiktok.com'},
        ]

        subject.upsert_account(store, first_cookies, self.now)
        subject.upsert_account(
            store, second_cookies, datetime(2026, 10, 10, tzinfo=timezone.utc))

        self.assertEqual(len(store['accounts']), 2)
    def test_atomic_save_persists_cookie_as_single_string(self):
        store = {'version': 1, 'accounts': []}
        subject.upsert_account(store, self.cookies(), self.now)
        subject.save_account_store_atomic(self.path, store)
        persisted = json.loads(self.path.read_text(encoding='utf-8'))
        account = persisted['accounts'][0]
        self.assertEqual(account['cookie'], 'uid_tt=synthetic-user')
        self.assertNotIn('cookies', account)
        loaded = subject.load_account_store(self.path)
        self.assertEqual(subject.cookie_header_from_playwright(
            loaded['accounts'][0]['cookies']), 'uid_tt=synthetic-user')
        self.assertEqual(list(Path(self.tmp.name).iterdir()), [self.path])
        original = self.path.read_bytes()
        with patch.object(subject.os, 'replace', side_effect=OSError('synthetic failure')):
            with self.assertRaises(OSError):
                subject.save_account_store_atomic(self.path, loaded)
        self.assertEqual(self.path.read_bytes(), original)
        self.assertEqual(list(Path(self.tmp.name).iterdir()), [self.path])

    def test_legacy_cookie_array_is_read_and_resaved_as_string(self):
        legacy = {'version': 1, 'accounts': [{
            'id': 'legacy', 'created_at': self.now.isoformat(),
            'updated_at': self.now.isoformat(), 'cookies': self.cookies(),
        }]}
        self.path.write_text(json.dumps(legacy), encoding='utf-8')
        loaded = subject.load_account_store(self.path)
        self.assertIn('cookies', loaded['accounts'][0])
        subject.save_account_store_atomic(self.path, loaded)
        persisted = json.loads(self.path.read_text(encoding='utf-8'))
        self.assertEqual(persisted['accounts'][0]['cookie'], 'uid_tt=synthetic-user')
        self.assertNotIn('cookies', persisted['accounts'][0])


    def test_cookie_header_keeps_only_tiktok_cookie_name_values(self):
        cookies = self.cookies() + [
            {'name': 'theme', 'value': 'dark', 'domain': 'www.tiktok.com'},
            {'name': 'bad', 'value': 'excluded', 'domain': 'eviltiktok.com'},
            {'name': 'bad', 'value': 'excluded', 'domain': 'tiktok.com.evil.test'},
            {'name': 'bad', 'value': 'excluded', 'domain': 'example.com'}]
        self.assertTrue(subject.cookie_header_from_playwright(cookies) == 'uid_tt=synthetic-user; theme=dark')


class LoginPollingRegressionTests(unittest.TestCase):
    def setUp(self):
        self.browser = SimpleNamespace(states=SimpleNamespace(is_alive=True))
        self.cookies = [
            {'name': 'sessionid', 'value': 'synthetic-session',
             'domain': '.tiktok.com', 'expires': -1},
            {'name': 's_v_web_id', 'value': 'verify_synthetic',
             'domain': '.tiktok.com', 'expires': -1},
        ]
        self.tab = SimpleNamespace(url='https://www.tiktok.com/foryou')

    def test_transient_cookie_read_error_recovers_during_login(self):
        with patch.object(browser_auth, '_browser_cookies', side_effect=[
                RuntimeError('transient'), self.cookies, self.cookies]), \
                patch.object(browser_auth, '_tabs', return_value=[self.tab]), \
                patch.object(browser_auth.time, 'sleep'):
            result = browser_auth.wait_for_tiktok_login(
                self.browser, timeout_seconds=5, poll_seconds=0.01)
        self.assertEqual(result, self.cookies)

    def test_repeated_cookie_read_errors_still_fail_safely(self):
        with patch.object(browser_auth, '_browser_cookies',
                          side_effect=RuntimeError('persistent')), \
                patch.object(browser_auth, '_tabs', return_value=[self.tab]), \
                patch.object(browser_auth.time, 'sleep'):
            with self.assertRaises(subject.BrowserError) as caught:
                browser_auth.wait_for_tiktok_login(
                    self.browser, timeout_seconds=5, poll_seconds=0.01)
        self.assertEqual(caught.exception.classification, 'login_state_unavailable')


class AccountRunnerTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / 'accounts.json'
        self.now = datetime(2026, 10, 9, tzinfo=timezone.utc)
        self.events = []
        self.login_count = 0

    def add_account(self, name):
        store = subject.load_account_store(self.path)
        cookies = [{'name': 'uid_tt', 'value': 'synthetic-' + name,
                    'domain': '.tiktok.com', 'path': '/', 'expires': -1},
                   {'name': 'sessionid', 'value': 'synthetic-session-' + name,
                    'domain': '.tiktok.com', 'path': '/', 'expires': -1}]
        account = subject.upsert_account(store, cookies, self.now)
        subject.save_account_store_atomic(self.path, store)
        return account

    def run_accounts(self, outcomes=None, add_new=False):
        outcomes = iter(outcomes or [])

        def login(path, timeout_seconds=300):
            self.assertEqual(path, self.path)
            self.assertEqual(timeout_seconds, 300)
            self.login_count += 1
            return self.add_account('new')

        def send(username, cookie, message, **kwargs):
            self.assertEqual((username, message), ('host', 'hello'))
            self.events.append(('send', cookie))
            outcome = next(outcomes, {'ok': True})
            if isinstance(outcome, Exception):
                raise outcome
            return outcome

        with patch.object(subject, 'login_new_account', side_effect=login), \
                patch.object(subject, 'send_live_message', side_effect=send), \
                patch.object(subject.time, 'sleep', side_effect=lambda seconds:
                             self.events.append(('sleep', seconds))):
            return subject.run_saved_accounts('host', 'hello', self.path,
                                              add_new_account=add_new,
                                              interval_seconds=3)

    def test_missing_blank_or_empty_store_starts_login(self):
        for store_state in ('missing', 'blank', 'empty'):
            with self.subTest(store_state=store_state):
                self.path.unlink(missing_ok=True)
                if store_state == 'blank':
                    self.path.write_text(' \r\n\t', encoding='utf-8')
                elif store_state == 'empty':
                    subject.save_account_store_atomic(self.path, {'version': 1, 'accounts': []})
                self.events.clear()
                self.login_count = 0
                result = self.run_accounts()
                self.assertEqual(self.login_count, 1)
                self.assertEqual(result, {'success': 1, 'failed': 0, 'expired': 0,
                                          'temporaryError': 0})
                self.assertEqual(len(subject.load_account_store(self.path)['accounts']), 1)
                self.assertEqual([event[0] for event in self.events], ['send'])

    def test_add_new_account_always_starts_login(self):
        self.add_account('saved')
        result = self.run_accounts(add_new=True)
        self.assertEqual(self.login_count, 1)
        self.assertEqual(result['success'], 1)
        self.assertEqual(len(subject.load_account_store(self.path)['accounts']), 2)
        sent = [event[1] for event in self.events if event[0] == 'send']
        self.assertEqual(sent, ['uid_tt=synthetic-new; sessionid=synthetic-session-new'])
        # Logging in the same new account on the next run still happens once.
        self.events.clear()
        second = self.run_accounts(add_new=True)
        self.assertEqual(self.login_count, 2)
        self.assertEqual(second['success'], 1)
        self.assertEqual(len(subject.load_account_store(self.path)['accounts']), 2)
        self.assertEqual([event[0] for event in self.events], ['send'])

    def test_false_switch_uses_valid_saved_accounts_without_login(self):
        account = self.add_account('saved')
        result = self.run_accounts()
        self.assertEqual(self.login_count, 0)
        self.assertEqual(result['success'], 1)
        saved = subject.load_account_store(self.path)['accounts'][0]
        self.assertEqual(subject.cookie_header_from_playwright(saved['cookies']),
                         subject.cookie_header_from_playwright(account['cookies']))
        self.assertEqual(saved['id'], account['id'])
        self.assertEqual(saved['created_at'], self.now.isoformat())
        self.assertEqual(saved['updated_at'], self.now.isoformat())
        sent = [event[1] for event in self.events if event[0] == 'send']
        self.assertTrue(sent == ['uid_tt=synthetic-saved; sessionid=synthetic-session-saved'])

    def test_saved_account_is_sent_without_browser_validation(self):
        self.add_account('saved')
        with patch.object(subject, 'validate_saved_account',
                          side_effect=AssertionError('browser validation must not run')), \
                patch.object(subject, 'send_live_message', return_value={'ok': True}) as send:
            result = subject.run_saved_accounts(
                'host', 'hello', self.path, add_new_account=False, interval_seconds=0)
        self.assertEqual(send.call_count, 1)
        self.assertEqual(result['success'], 1)

    def test_login_required_response_removes_only_that_account(self):
        expired = self.add_account('expired')
        valid = self.add_account('valid')
        result = self.run_accounts(outcomes=[
            {'ok': False, 'loginExpired': True, 'message': '请登录'},
            {'ok': True},
        ])
        self.assertEqual(result, {'success': 1, 'failed': 0, 'expired': 1,
                                  'temporaryError': 0})
        self.assertEqual([item['id'] for item in subject.load_account_store(self.path)['accounts']],
                         [valid['id']])

    def test_expired_account_is_removed_and_next_account_is_not_skipped(self):
        expired = self.add_account('expired')
        valid = self.add_account('valid')
        result = self.run_accounts(outcomes=[
            {'ok': False, 'loginExpired': True, 'message': '请登录'}, {'ok': True}])
        self.assertEqual(result, {'success': 1, 'failed': 0, 'expired': 1,
                                  'temporaryError': 0})
        self.assertEqual([account['id'] for account in subject.load_account_store(self.path)['accounts']],
                         [valid['id']])
        self.assertEqual([event[0] for event in self.events], ['send', 'sleep', 'send'])

    def test_comment_disabled_without_observation_fails_but_continues_accounts(self):
        first = self.add_account('first')
        self.add_account('second')
        result = self.run_accounts(outcomes=[{
            'ok': False,
            'statusCode': 4010048,
            'classification': 'comment_delivery_uncertain',
            'requiresObservation': True,
            'stopProcessing': False,
        }, {'ok': True}])
        self.assertEqual(result, {'success': 1, 'failed': 1, 'expired': 0,
                                  'temporaryError': 0})
        self.assertEqual([event[0] for event in self.events], ['send', 'sleep', 'send'])
        report = json.loads(
            self.path.with_name('failed_accounts.json').read_text(encoding='utf-8')
        )
        self.assertEqual(report['failed_accounts'][0], {
            'account_id': first['id'][:12],
            'username': '-',
            'classification': 'observation_required',
        })

    def test_silenced_account_is_written_to_separate_report(self):
        account = self.add_account('silenced')
        result = self.run_accounts(outcomes=[{
            'ok': False,
            'statusCode': 50001,
            'classification': 'account_silenced',
            'accountSilenced': True,
        }])
        self.assertEqual(result, {'success': 0, 'failed': 1, 'expired': 0,
                                  'temporaryError': 0})
        report = json.loads(
            self.path.with_name('silenced_accounts.json').read_text(encoding='utf-8')
        )
        self.assertEqual(report, {'silenced_accounts': [{
            'account_id': account['id'][:12],
            'username': '-',
            'classification': 'account_silenced',
            'status_code': 50001,
        }]})
        self.assertEqual(len(subject.load_account_store(self.path)['accounts']), 1)

    def test_generic_send_failure_preserves_account(self):
        self.add_account('temporary')
        original = self.path.read_bytes()
        result = self.run_accounts(outcomes=[{'ok': False}])
        self.assertEqual(result, {'success': 0, 'failed': 1, 'expired': 0,
                                  'temporaryError': 0})
        self.assertTrue(self.path.read_bytes() == original)
        self.assertEqual([event[0] for event in self.events], ['send'])

    def test_failure_uses_resolved_account_username_and_writes_json_report(self):
        account = self.add_account('temporary')
        result = self.run_accounts(outcomes=[{
            'ok': False,
            'accountUsername': 'spider_king017',
        }])

        self.assertEqual(result['failed'], 1)
        saved = subject.load_account_store(self.path)['accounts'][0]
        self.assertEqual(saved['username'], 'spider_king017')
        report = json.loads(
            self.path.with_name('failed_accounts.json').read_text(encoding='utf-8')
        )
        self.assertEqual(report, {
            'failed_accounts': [{
                'account_id': account['id'][:12],
                'username': 'spider_king017',
                'classification': 'send_failed',
            }],
        })

    def test_each_valid_account_sends_exactly_once_with_interval_between_accounts(self):
        first = self.add_account('first')
        temporary = self.add_account('temporary')
        expired = self.add_account('expired')
        last = self.add_account('last')
        result = self.run_accounts(outcomes=[
            {'ok': True}, {'ok': False},
            {'ok': False, 'loginExpired': True, 'message': '请登录'}, {'ok': True}])
        self.assertEqual(result, {'success': 2, 'failed': 1, 'expired': 1,
                                  'temporaryError': 0})
        self.assertEqual([event[0] for event in self.events],
                         ['send', 'sleep', 'send', 'sleep', 'send', 'sleep', 'send'])
        self.assertEqual([event[1] for event in self.events if event[0] == 'sleep'], [3, 3, 3])
        sent = [event[1] for event in self.events if event[0] == 'send']
        self.assertEqual(len(sent), 4)
        self.assertEqual([account['id'] for account in subject.load_account_store(self.path)['accounts']],
                         [first['id'], temporary['id'], last['id']])

    def test_send_exception_counts_failure_and_continues_without_removing_account(self):
        self.add_account('first')
        self.add_account('last')
        result = self.run_accounts(outcomes=[RuntimeError('synthetic-sensitive'), {'ok': True}])
        self.assertEqual(result, {'success': 1, 'failed': 1, 'expired': 0,
                                  'temporaryError': 0})
        self.assertEqual(len(subject.load_account_store(self.path)['accounts']), 2)
        self.assertEqual([event[0] for event in self.events],
                         ['send', 'sleep', 'send'])


class LoggingTests(unittest.TestCase):
    secrets = ('synthetic-cookie-private', 'synthetic-ms-private',
               'synthetic-csrf-private', 'synthetic-signature-private',
               'synthetic-ticket-private')
    sensitive = ('Cookie=sessionid=synthetic-cookie-private; msToken=synthetic-ms-private '
                 'tt-csrf-token=synthetic-csrf-private X-Gnarly=synthetic-signature-private '
                 'Ticket-Guard=synthetic-ticket-private')

    def setUp(self):
        self.logs = []
        sink = subject.logger.add(self.logs.append, format='{message}')
        self.addCleanup(subject.logger.remove, sink)
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / 'accounts.json'
        self.cookies = [{'name': 'sessionid', 'value': self.secrets[0],
                         'domain': '.tiktok.com', 'path': '/', 'expires': -1},
                        {'name': 's_v_web_id', 'value': 'synthetic-fingerprint-private',
                         'domain': '.tiktok.com', 'path': '/', 'expires': -1}]
        store = {'version': 1, 'accounts': []}
        self.account = subject.upsert_account(store, self.cookies, datetime.now(timezone.utc))
        subject.save_account_store_atomic(self.path, store)

    def assert_no_credentials(self):
        text = ''.join(self.logs)
        # Use boolean assertions so even a regression does not print log contents.
        for value in self.secrets + ('synthetic-fingerprint-private',):
            self.assertTrue(value not in text, 'credential appeared in captured logs')
        return text

    def send_dependencies(self, stack, response):
        stack.enter_context(patch.object(subject._client, 'room_heartbeat',
                                         side_effect=lambda *a, **kw: nullcontext()))
        stack.enter_context(patch.object(subject._client, 'fetch_initial_response',
                                         return_value=object()))
        stack.enter_context(patch.object(subject.requests, 'Session', side_effect=OfflineSession))
        stack.enter_context(patch.object(subject, 'get_fresh_ms_token', return_value=self.secrets[1]))
        stack.enter_context(patch.object(subject, 'get_sec_sdk_csrf_token', return_value=self.secrets[2]))
        stack.enter_context(patch.object(subject, 'get_live_room', return_value={
            'exists': True, 'roomId': '7694434643340888852',
            'username': 'resolved_host', 'nickname': 'Resolved Host',
            'accountUsername': 'sender_unique_id',
            'accountNickname': 'Current Account',
            'url': 'https://www.tiktok.com/@host/live'}))
        stack.enter_context(patch.object(subject, 'build_chat_request', return_value=('url', b'{}')))
        stack.enter_context(patch.object(subject, 'post_chat_request', return_value=response))

    def test_account_runner_logs_message_room_and_result_without_credentials(self):
        response = SimpleNamespace(status_code=200, json=lambda: {'status_code': 0})
        with ExitStack() as stack:
            self.send_dependencies(stack, response)
            result = subject.run_saved_accounts(
                'host', 'hello logging', self.path,
                add_new_account=False, interval_seconds=0)
        self.assertEqual(result['success'], 1)
        text = self.assert_no_credentials()
        for field in ('account_index=1', 'account_id=' + self.account['id'][:12],
                      'username=host', 'message=hello logging', 'room_id=7694434643340888852',
                      'username=resolved_host', 'nickname=Current Account',
                      'http_status=200', 'status_code=0',
                      'processed=1', 'success=1', 'failed=0', 'expired=0', 'temporaryError=0'):
            self.assertIn(field, text)

    def test_room_log_uses_saved_sender_name_when_room_lookup_lacks_account_identity(self):
        response = SimpleNamespace(status_code=200, json=lambda: {'status_code': 0})
        store = subject.load_account_store(self.path)
        store['accounts'][0].update(username='boaluodyblala1', nickname='Current Sender')
        subject.save_account_store_atomic(self.path, store)
        with ExitStack() as stack:
            self.send_dependencies(stack, response)
            stack.enter_context(patch.object(subject, 'get_live_room', return_value={
                'exists': True, 'roomId': '7696094502439684865',
                'username': 'anggikwodz31', 'nickname': 'Host Display',
                'accountUsername': None, 'accountNickname': None,
                'url': 'https://www.tiktok.com/@anggikwodz31/live',
            }))
            result = subject.run_saved_accounts(
                'anggikwodz31', 'hello logging', self.path,
                add_new_account=False, interval_seconds=0,
            )
        self.assertEqual(result['success'], 1)
        text = self.assert_no_credentials()
        self.assertIn('username=anggikwodz31 | nickname=Current Sender', text)

    def test_room_log_uses_saved_username_when_saved_nickname_is_missing(self):
        response = SimpleNamespace(status_code=200, json=lambda: {'status_code': 0})
        store = subject.load_account_store(self.path)
        store['accounts'][0]['username'] = 'boaluodyblala1'
        subject.save_account_store_atomic(self.path, store)
        with ExitStack() as stack:
            self.send_dependencies(stack, response)
            stack.enter_context(patch.object(subject, 'get_live_room', return_value={
                'exists': True, 'roomId': '7696094502439684865',
                'username': 'anggikwodz31', 'nickname': 'Host Display',
                'accountUsername': None, 'accountNickname': None,
                'url': 'https://www.tiktok.com/@anggikwodz31/live',
            }))
            result = subject.run_saved_accounts(
                'anggikwodz31', 'hello logging', self.path,
                add_new_account=False, interval_seconds=0,
            )
        self.assertEqual(result['success'], 1)
        text = self.assert_no_credentials()
        self.assertIn('username=anggikwodz31 | nickname=boaluodyblala1', text)

    def test_wrapper_exception_logs_no_request_credentials(self):
        error = RuntimeError(self.sensitive)
        with patch.object(subject, '_send_live_message', side_effect=error):
            with self.assertRaises(RuntimeError) as caught:
                subject.send_live_message('host', 'unused', 'hello')
        self.assertIs(caught.exception, error)
        text = self.assert_no_credentials()
        self.assertIn('classification=send_exception', text)

    def test_non_json_error_does_not_expose_response_preview(self):
        def invalid_json():
            raise ValueError(self.sensitive)
        response = SimpleNamespace(status_code=403, text=self.sensitive, json=invalid_json)
        with ExitStack() as stack:
            self.send_dependencies(stack, response)
            with self.assertRaises(RuntimeError) as caught:
                subject.send_live_message('host', 'sessionid=synthetic-cookie-private; s_v_web_id=synthetic-fingerprint-private', 'hello')
        self.assertTrue(all(value not in str(caught.exception) for value in self.secrets))
        self.assertIsNone(caught.exception.__cause__)
        self.assert_no_credentials()

    def test_non_json_login_required_response_is_classified_for_removal(self):
        def invalid_json():
            raise ValueError('not json')
        response = SimpleNamespace(status_code=401, text='请登录后再试', json=invalid_json)
        with ExitStack() as stack:
            self.send_dependencies(stack, response)
            result = subject.send_live_message(
                'host', 'sessionid=synthetic-cookie-private; s_v_web_id=synthetic-fingerprint-private', 'hello')
        self.assertFalse(result['ok'])
        self.assertTrue(result['loginExpired'])
        self.assertEqual(result['message'], '账号登录状态已失效')
        self.assert_no_credentials()

    def test_business_response_is_logged_with_credentials_redacted(self):
        response = SimpleNamespace(status_code=200, json=lambda: {
            'status_code': 123,
            'data': {'message': 'message rejected', 'sessionid': self.secrets[0]},
        })
        with ExitStack() as stack:
            self.send_dependencies(stack, response)
            result = subject.send_live_message('host', 'sessionid=synthetic-cookie-private; s_v_web_id=synthetic-fingerprint-private', 'hello')
        self.assertFalse(result['ok'])
        self.assertTrue(result['message'] == 'message rejected')
        text = self.assert_no_credentials()
        self.assertIn('status_code=123', text)
        self.assertIn('直播接口响应内容', text)
        self.assertIn('message rejected', text)
        self.assertIn('sessionid', text)
        self.assertIn('<redacted>', text)

    def test_false_switch_logs_empty_store_before_automatic_login(self):
        self.path.unlink()
        with patch.object(subject, 'login_new_account', return_value=self.account):
            subject.run_saved_accounts(
                'host', 'hello', self.path, add_new_account=False,
                interval_seconds=0,
            )
        text = self.assert_no_credentials()
        self.assertIn('账号库中没有账号，正在打开登录界面新增账号', text)
        self.assertIn('classification=account_store_empty', text)
    def test_login_timeout_is_forwarded_to_browser_login(self):
        with patch.object(subject, 'login_new_account', return_value=self.account) as login, \
                patch.object(subject, 'send_live_message', return_value={'ok': True}):
            subject.run_saved_accounts(
                'host', 'hello', self.path, add_new_account=True,
                interval_seconds=0, login_timeout_seconds=77,
            )
        login.assert_called_once_with(self.path, timeout_seconds=77)
        self.assertNotIn('classification=account_store_empty', self.assert_no_credentials())

    def test_scan_logs_start_and_safe_failure(self):
        with patch.object(subject, 'login_new_account', side_effect=RuntimeError(self.sensitive)):
            with self.assertRaises(RuntimeError):
                subject.run_saved_accounts('host', 'hello', self.path, add_new_account=True)
        text = self.assert_no_credentials()
        self.assertIn('classification=scan_start', text)
        self.assertIn('classification=scan_failed', text)

    def test_scan_success_and_account_outcomes_are_logged_without_raw_reasons(self):
        store = subject.load_account_store(self.path)
        temporary = subject.upsert_account(store, [dict(self.cookies[0], value='synthetic-temp')],
                                           datetime.now(timezone.utc))
        subject.save_account_store_atomic(self.path, store)
        with patch.object(subject, 'login_new_account', return_value=self.account), \
                patch.object(subject, 'send_live_message', side_effect=[
                    {'ok': False, 'loginExpired': True, 'message': '请登录'},
                    {'ok': False, 'message': 'temporary'}]):
            result = subject.run_saved_accounts('host', 'hello', self.path, add_new_account=True)
        self.assertEqual(result, {'success': 0, 'failed': 0, 'expired': 1, 'temporaryError': 0})
        text = self.assert_no_credentials()
        for field in ('classification=scan_saved', 'classification=expired', 'processed=1'):
            self.assertIn(field, text)
        self.assertEqual(subject.load_account_store(self.path)['accounts'][0]['id'], temporary['id'])

    def test_http_403_logs_proxy_ip_risk_hint(self):
        subject.log_send_result({'ok': False, 'statusCode': None}, 403)
        text = self.assert_no_credentials()
        self.assertIn('当前代理 IP 被风控，请更换代理节点', text)
    def test_non_numeric_status_fields_and_untrusted_account_id_are_not_logged(self):
        subject.log_send_result({'ok': False, 'statusCode': self.sensitive,
                                 'message': self.sensitive}, self.sensitive)
        store = subject.load_account_store(self.path)
        store['accounts'][0]['id'] = self.sensitive
        subject.save_account_store_atomic(self.path, store)
        with patch.object(subject, 'send_live_message', return_value={
                'ok': False, 'statusCode': self.sensitive, 'message': self.sensitive}):
            subject.run_saved_accounts(
                'host', 'hello', self.path, add_new_account=False, interval_seconds=0)
        self.assert_no_credentials()


class AccountRequestCookieTests(unittest.TestCase):
    def setUp(self):
        gate = patch.object(subject._client, 'room_heartbeat',
                            side_effect=lambda *a, **kw: nullcontext())
        gate.start()
        self.addCleanup(gate.stop)
        fetch = patch.object(subject._client, 'fetch_initial_response', return_value=object())
        fetch.start()
        self.addCleanup(fetch.stop)
        entry = patch.object(subject._client, 'enter_live_room')
        entry.start()
        self.addCleanup(entry.stop)

    def test_random_chrome_profiles_are_version_aligned(self):
        profiles = subject._client.CHROME_BROWSER_PROFILES
        self.assertGreaterEqual(len(profiles), 7)
        for profile in profiles:
            major = re.search(r"Chrome/(\d+)", profile.user_agent).group(1)
            self.assertIn(f'v="{major}"', profile.sec_ch_ua)
            expected_fingerprint = "chrome145" if major == "146" else f"chrome{major}"
            self.assertEqual(profile.impersonate, expected_fingerprint)

    def test_send_flow_reuses_saved_login_chrome_profile(self):
        profile = SimpleNamespace(
            user_agent='Mozilla/5.0 Chrome/123.0.0.0 Safari/537.36',
            sec_ch_ua='"Chromium";v="123", "Google Chrome";v="123"',
            impersonate='chrome123',
        )
        session = OfflineSession()
        fetch_session = OfflineSession()
        response = SimpleNamespace(status_code=200, json=lambda: {'status_code': 0})
        room = {'exists': True, 'roomId': '123456789012', 'username': 'host',
                'nickname': '主播昵称', 'accountUsername': 'sender_unique_id',
                'accountNickname': '当前账号昵称',
                'deviceId': '7000000000000000001',
                'url': 'https://www.tiktok.com/@host/live'}
        with patch('tiktok_live.request_fingerprint._choose', return_value='safari2601'), \
                patch.object(subject._client, 'browser_profile_from_user_agent', return_value=profile) as profile_builder, \
                patch.object(subject._client.requests, 'Session', side_effect=[session, fetch_session]) as session_factory, \
                patch.object(subject._client, 'get_fresh_ms_token', return_value='fresh') as token, \
                patch.object(subject._client, 'get_live_room', return_value=room) as live, \
                patch.object(subject._client, 'get_sec_sdk_csrf_token', return_value='csrf') as csrf, \
                patch.object(subject._client, 'build_chat_request', return_value=('url', b'{}')) as build, \
                patch.object(subject._client, 'post_chat_request', return_value=response) as post, \
                patch.object(subject._client, 'log_room_ready') as room_log:
            result = subject._client._send_live_message(
                'host', 's_v_web_id=verify_test; sessionid=session_test', 'hello',
                user_agent=profile.user_agent)
            retried = result.retry_message()
        self.assertTrue(result['ok'])
        self.assertTrue(retried['ok'])
        self.assertEqual(result['accountNickname'], '当前账号昵称')
        room_log.assert_called_once_with('123456789012', 'host', '当前账号昵称')
        profile_builder.assert_called_once_with(profile.user_agent)
        self.assertEqual(session_factory.call_args_list,
                         [call(impersonate='safari2601'), call(impersonate='safari2601')])
        self.assertTrue(fetch_session.closed)
        self.assertFalse(session.closed)  # retained for message retry
        csrf.assert_called_once()
        for mocked in (token, live, csrf, build, post):
            self.assertIs(mocked.call_args.kwargs.get('profile'), profile)
        self.assertEqual(post.call_args.args[5], 'csrf')

    def test_observation_retry_reuses_room_and_only_reposts_chat_message(self):
        profile = SimpleNamespace(
            user_agent='Mozilla/5.0 Chrome/123.0.0.0 Safari/537.36',
            sec_ch_ua='"Chromium";v="123", "Google Chrome";v="123"',
            impersonate='chrome123',
        )
        session = OfflineSession()
        room = {'exists': True, 'roomId': '123456789012', 'username': 'host',
                'nickname': 'Host', 'accountUsername': 'sender',
                'accountNickname': 'Sender', 'deviceId': '7000000000000000001',
                'url': 'https://www.tiktok.com/@host/live'}
        responses = [
            SimpleNamespace(status_code=200, json=lambda: {'status_code': 0}),
            SimpleNamespace(status_code=200, json=lambda: {'status_code': 0}),
        ]
        with patch.object(subject._client, 'choose_browser_profile', return_value=profile), \
                patch.object(subject._client.requests, 'Session', side_effect=[session, OfflineSession()]), \
                patch.object(subject._client, 'get_fresh_ms_token', return_value='fresh') as token, \
                patch.object(subject._client, 'get_live_room', return_value=room) as live, \
                patch.object(subject._client, 'get_sec_sdk_csrf_token', return_value='csrf') as csrf, \
                patch.object(subject._client, 'build_chat_request', return_value=('url', b'{}')) as build, \
                patch.object(subject._client, 'post_chat_request', side_effect=responses) as post:
            first = subject._client._send_live_message(
                'host', 's_v_web_id=verify_test; sessionid=session_test', 'hello')
            second = first.retry_message()

        self.assertTrue(first['ok'])
        self.assertTrue(second['ok'])
        token.assert_called_once()
        live.assert_called_once()
        csrf.assert_called_once()
        self.assertTrue(all(call.args[5] == 'csrf' for call in post.call_args_list))
        self.assertEqual(build.call_count, 2)
        self.assertEqual(post.call_count, 2)

    def test_pre_send_token_failure_retries_at_most_three_times(self):
        profile = SimpleNamespace(
            user_agent='Mozilla/5.0 Chrome/123.0.0.0 Safari/537.36',
            sec_ch_ua='"Chromium";v="123", "Google Chrome";v="123"',
            impersonate='chrome123',
        )
        room = {'exists': True, 'roomId': '123456789012', 'username': 'host',
                'nickname': 'Host', 'deviceId': '7000000000000000001',
                'url': 'https://www.tiktok.com/@host/live'}
        response = SimpleNamespace(status_code=200, json=lambda: {'status_code': 0})
        with patch.object(subject._client, 'choose_browser_profile', return_value=profile), \
                patch.object(subject._client.requests, 'Session', side_effect=OfflineSession), \
                patch.object(subject._client, 'get_fresh_ms_token', side_effect=[
                    RuntimeError('temporary-1'), RuntimeError('temporary-2'),
                    RuntimeError('temporary-3'), 'fresh']) as token, \
                patch.object(subject._client, 'get_live_room', return_value=room), \
                patch.object(subject._client, 'get_sec_sdk_csrf_token', return_value='csrf'), \
                patch.object(subject._client, 'build_chat_request', return_value=('url', b'{}')), \
                patch.object(subject._client, 'post_chat_request', return_value=response), \
                patch.object(subject._client.time, 'sleep') as sleep:
            result = subject._client._send_live_message(
                'host', 's_v_web_id=verify_test; sessionid=session_test', 'hello')
        self.assertTrue(result['ok'])
        self.assertEqual(token.call_count, 4)
        self.assertEqual(sleep.call_count, 3)
        sleep.assert_has_calls([call(2.0), call(2.0), call(2.0)])

    def test_chat_post_failure_is_not_retried(self):
        profile = SimpleNamespace(
            user_agent='Mozilla/5.0 Chrome/123.0.0.0 Safari/537.36',
            sec_ch_ua='"Chromium";v="123", "Google Chrome";v="123"',
            impersonate='chrome123',
        )
        room = {'exists': True, 'roomId': '123456789012', 'username': 'host',
                'nickname': 'Host', 'deviceId': '7000000000000000001',
                'url': 'https://www.tiktok.com/@host/live'}
        with patch.object(subject._client, 'choose_browser_profile', return_value=profile), \
                patch.object(subject._client.requests, 'Session', side_effect=OfflineSession), \
                patch.object(subject._client, 'get_fresh_ms_token', return_value='fresh'), \
                patch.object(subject._client, 'get_live_room', return_value=room), \
                patch.object(subject._client, 'get_sec_sdk_csrf_token', return_value='csrf'), \
                patch.object(subject._client, 'build_chat_request', return_value=('url', b'{}')), \
                patch.object(subject._client, 'post_chat_request',
                             side_effect=RuntimeError('uncertain post failure')) as post:
            with self.assertRaises(RuntimeError):
                subject._client._send_live_message(
                    'host', 's_v_web_id=verify_test; sessionid=session_test', 'hello')
        post.assert_called_once()

    def test_login_required_response_is_classified_for_account_removal(self):
        result = subject.interpret_chat_response({
            'status_code': 10000,
            'data': {'prompts': '请登录后再试'},
        })
        self.assertFalse(result['ok'])
        self.assertTrue(result['loginExpired'])

    def test_user_silence_response_is_classified_as_silenced_account(self):
        result = subject.interpret_chat_response({
            'status_code': 50001,
            'data': {'message': 'user silence', 'prompts': '您目前已被禁言'},
        })
        self.assertFalse(result['ok'])
        self.assertTrue(result['accountSilenced'])
        self.assertEqual(result['classification'], 'account_silenced')
        self.assertEqual(result['statusCode'], 50001)

    def test_comment_disabled_prompt_stops_room_without_live_observation(self):
        result = subject.interpret_chat_response({
            'status_code': 4010048,
            'data': {'prompts': '\u7531\u4e8e\u672c\u573a\u76f4\u64ad\u7684\u8bbe\u7f6e\uff0c\u65e0\u6cd5\u53d1\u5e03\u8bc4\u8bba'},
        })
        self.assertFalse(result['ok'])
        self.assertEqual(result['classification'], 'room_comment_restricted')
        self.assertFalse(result.get('requiresObservation'))
        self.assertTrue(result['stopProcessing'])
        self.assertTrue(result['roomCommentsRestricted'])
    def test_other_4010048_prompt_does_not_require_live_observation(self):
        result = subject.interpret_chat_response({
            'status_code': 4010048,
            'data': {'prompts': '其他服务端提示'},
        })
        self.assertFalse(result['ok'])
        self.assertNotIn('requiresObservation', result)
        self.assertNotIn('stopProcessing', result)

    def test_chat_request_builds_json_body(self):
        url, body = subject.build_chat_request(
            room_id='7693939745713326862',
            message='hello',
            cookies={'s_v_web_id': 'verify_synthetic', 'store-country-code': 'sg'},
            device_id='7123456789012345678',
            referer='https://www.tiktok.com/@host/live',
            now_ms=1791546000000,
        )
        payload = json.loads(body.decode('utf-8'))
        self.assertEqual(payload['room_id'], '7693939745713326862')
        self.assertEqual(payload['content'], 'hello')
        self.assertIn('verifyFp=verify_synthetic', url)

    def test_missing_verification_cookie_proceeds_to_request_preparation(self):
        with patch.object(subject._client, 'get_fresh_ms_token', side_effect=RuntimeError('preparation reached')):
            with self.assertRaisesRegex(RuntimeError, 'preparation reached'):
                subject._client._send_live_message('host', 'sessionid=synthetic-session', 'hello')

    def test_missing_verification_cookie_is_omitted(self):
        for value in (None, ''):
            cookies = {} if value is None else {'s_v_web_id': value}
            url, _ = subject._client.build_chat_request(
                room_id='42', message='hello', cookies=cookies,
                device_id='123', referer='https://www.tiktok.com/@host/live')
            self.assertNotIn('verifyFp', url)

    def test_ticket_guard_generation_failure_is_safe_and_never_posts(self):
        from cryptography.exceptions import InvalidTag
        posts = []
        session = SimpleNamespace(post=lambda *args, **kwargs: posts.append(kwargs))
        with patch.object(subject._client, 'build_headers_from_cookie_state', side_effect=InvalidTag('synthetic-secret')):
            with self.assertRaises(Exception) as caught:
                subject.post_chat_request(session, subject.CHAT_URL, b'{}', 'sessionid=synthetic',
                    'synthetic-token', None, 'https://synthetic.test', ticket_guard_state={'ts_sign': 'synthetic'})
        self.assertIsInstance(caught.exception, subject.SafeOperationError)
        self.assertEqual(caught.exception.classification, 'ticket_guard_unavailable')
        self.assertEqual(posts, [])
        self.assertNotIn('synthetic', str(caught.exception))

    def test_chat_request_without_runtime_ticket_guard_never_posts(self):
        for state in (None, {}, {'ts_sign': 'synthetic-private-sign'},
                      {'ts_sign': 'synthetic-private-sign', 'encrypt_ticket': 'invalid-ticket'}):
            with self.subTest(state_present=bool(state)):
                posts = []
                session = SimpleNamespace(post=lambda *args, **kwargs: posts.append(kwargs))
                with self.assertRaises(subject.SafeOperationError) as caught:
                    subject.post_chat_request(session, subject.CHAT_URL, b'{}',
                        'sessionid=synthetic', 'synthetic-token', None, 'https://synthetic.test',
                        ticket_guard_state=state)
                self.assertEqual(caught.exception.classification, 'ticket_guard_unavailable')
                self.assertEqual(posts, [])
                self.assertNotIn('synthetic', str(caught.exception))

    def test_chat_request_carries_selected_account_session_cookie(self):
        captured = {}
        response = SimpleNamespace(status_code=200)
        def post(url, **kwargs):
            captured.update(kwargs)
            return response
        session = SimpleNamespace(post=post)
        with patch.object(subject, 'sign_url', return_value='https://synthetic.test/chat'), \
                patch.object(subject._client, 'build_headers_from_cookie_state', return_value={
                    'tt-ticket-guard-client-data': 'synthetic-fresh-data',
                    'tt-ticket-guard-public-key': 'synthetic-public-key',
                    'tt-ticket-guard-iteration-version': '0',
                    'tt-ticket-guard-version': '2', 'tt-ticket-guard-web-version': '1'}):
            actual = subject.post_chat_request(
                session, subject.CHAT_URL, b'{}',
                'sessionid=synthetic-account-session; msToken=synthetic-old',
                'synthetic-fresh', None, 'https://www.tiktok.com/@synthetic/live',
                ticket_guard_state={'ts_sign': 'synthetic', 'encrypt_ticket': 'synthetic'})
        self.assertIs(actual, response)
        self.assertTrue(captured['headers'].get('cookie') ==
                        'sessionid=synthetic-account-session; msToken=synthetic-fresh')
        self.assertIn('tt-ticket-guard-client-data', captured['headers'])
        self.assertIn('tt-ticket-guard-public-key', captured['headers'])
        self.assertEqual(captured['headers']['tt-ticket-guard-iteration-version'], '0')
        self.assertEqual(captured['headers']['tt-ticket-guard-version'], '2')
        self.assertEqual(captured['headers']['tt-ticket-guard-web-version'], '1')

    def test_live_room_request_uses_current_account_cookie_first(self):
        cookies = []
        def get(url, **kwargs):
            cookies.append(kwargs['headers']['cookie'])
            return SimpleNamespace(
                status_code=200,
                text='{"roomId":"123456789012","deviceId":"7000000000000000001"}',
            )
        session = SimpleNamespace(get=get)
        with patch.object(subject, 'ROOM_ID_COOKIE', 'sessionid=synthetic-room-fallback'):
            room = subject._client.request_live_room(session, 'synthetic', 'sessionid=current-account')
        self.assertEqual(cookies, ['sessionid=current-account'])
        self.assertEqual(room['roomId'], '123456789012')
        self.assertEqual(room['deviceId'], '7000000000000000001')

    def test_live_room_fallback_only_supplies_room_id(self):
        cookies = []
        def get(url, **kwargs):
            selected_cookie = kwargs['headers']['cookie']
            cookies.append(selected_cookie)
            if selected_cookie == 'sessionid=current-account':
                return SimpleNamespace(
                    status_code=200,
                    text='{"deviceId":"7000000000000000001"}',
                )
            return SimpleNamespace(
                status_code=200,
                text='{"roomId":"123456789012","deviceId":"7999999999999999999"}',
            )
        session = SimpleNamespace(get=get)
        with patch.object(subject, 'ROOM_ID_COOKIE', 'sessionid=synthetic-room-fallback'):
            room = subject._client.resolve_live_room(
                lambda selected: subject._client.request_live_room(session, 'synthetic', selected),
                'sessionid=current-account', 'sessionid=synthetic-room-fallback')
        self.assertEqual(cookies, [
            'sessionid=current-account',
            'sessionid=synthetic-room-fallback',
        ])
        self.assertEqual(room['roomId'], '123456789012')
        self.assertEqual(room['deviceId'], '7000000000000000001')

    def test_live_room_fallback_preserves_public_host_identity(self):
        def fetch_page(selected_cookie):
            if selected_cookie == 'sessionid=current-account':
                raise RuntimeError('synthetic WAF')
            return {
                'exists': True,
                'roomId': '123456789012',
                'username': 'target_host',
                'nickname': 'Target Host',
                'accountUsername': 'fallback_account',
                'deviceId': '7999999999999999999',
                'url': 'https://www.tiktok.com/@target_host/live',
                'status': 200,
            }

        with patch.object(subject._client, 'ROOM_ID_COOKIE', 'sessionid=synthetic-room-fallback'):
            room = subject._client.resolve_live_room(fetch_page, 'sessionid=current-account')

        self.assertEqual(room['roomId'], '123456789012')
        self.assertEqual(room['username'], 'target_host')
        self.assertEqual(room['nickname'], 'Target Host')
        self.assertIsNone(room['accountUsername'])
        self.assertIsNone(room['deviceId'])

    def test_live_room_fallback_preserves_current_account_nickname(self):
        def fetch_page(selected_cookie):
            if selected_cookie == 'sessionid=current-account':
                return {
                    'exists': False,
                    'roomId': None,
                    'username': 'target_host',
                    'nickname': '-',
                    'accountUsername': 'sender_unique_id',
                    'accountNickname': '当前账号昵称',
                    'deviceId': '7000000000000000001',
                    'url': 'https://www.tiktok.com/@target_host/live',
                    'status': 200,
                }
            return {
                'exists': True,
                'roomId': '123456789012',
                'username': 'target_host',
                'nickname': '主播昵称',
                'accountUsername': 'fallback_account',
                'accountNickname': '兜底账号',
                'deviceId': '7999999999999999999',
                'url': 'https://www.tiktok.com/@target_host/live',
                'status': 200,
            }

        with patch.object(subject._client, 'ROOM_ID_COOKIE', 'sessionid=synthetic-room-fallback'):
            room = subject._client.resolve_live_room(fetch_page, 'sessionid=current-account')

        self.assertEqual(room['roomId'], '123456789012')
        self.assertEqual(room['accountUsername'], 'sender_unique_id')
        self.assertEqual(room['accountNickname'], '当前账号昵称')

    def test_live_room_extracts_current_account_nickname(self):
        app_context = {
            "__DEFAULT_SCOPE__": {
                "webapp.live-detail": {"liveRoomUserInfo": {"user": {
                    "uniqueId": "target_host", "nickname": "主播昵称",
                    "roomId": "123456789012", "status": 2}}},
                "webapp.app-context": {
                    "user": {
                        "uniqueId": "sender_unique_id",
                        "nickName": "当前账号昵称",
                    }
                }
            }
        }
        html = (
            '<script id="__UNIVERSAL_DATA_FOR_REHYDRATION__" type="application/json">'
            + json.dumps(app_context, ensure_ascii=False)
            + '</script>'
        )
        session = SimpleNamespace(get=lambda *args, **kwargs:
                                  SimpleNamespace(status_code=200, text=html))
        with patch.object(subject, 'ROOM_ID_COOKIE', ''):
            room = subject._client.request_live_room(session, 'target_host', 'sessionid=fallback')

        self.assertEqual(room['username'], 'target_host')
        self.assertEqual(room['nickname'], '主播昵称')
        self.assertEqual(room['accountUsername'], 'sender_unique_id')
        self.assertEqual(room['accountNickname'], '当前账号昵称')

    def test_live_room_extracts_owner_username_and_nickname(self):
        html = (
            '{"roomId":"123456789012","ownerInfo":'
            '{"uniqueId":"actual_host","nickname":"主播昵称"}}'
        )
        session = SimpleNamespace(get=lambda *args, **kwargs:
                                  SimpleNamespace(status_code=200, text=html))
        with patch.object(subject, 'ROOM_ID_COOKIE', ''):
            room = subject._client.request_live_room(session, 'requested_host', 'sessionid=fallback')
        self.assertEqual(room['username'], 'actual_host')
        self.assertEqual(room['nickname'], '主播昵称')

    def test_live_room_extracts_identity_from_live_room_user_info(self):
        html = (
            '{"roomId":"7694780620325210901","liveRoomUserInfo":{"user":'
            '{"id":"7556705170421302292","nickname":"饺子皮🐔",'
            '"uniqueId":"888jz0910"}}}'
        )
        session = SimpleNamespace(get=lambda *args, **kwargs:
                                  SimpleNamespace(status_code=200, text=html))
        with patch.object(subject, 'ROOM_ID_COOKIE', ''):
            room = subject._client.request_live_room(session, '888jz0910', 'sessionid=fallback')
        self.assertEqual(room['username'], '888jz0910')
        self.assertEqual(room['nickname'], '饺子皮🐔')

    def test_live_room_extracts_camel_case_nickname(self):
        html = (
            '{"roomId":"7696035739356039937","liveRoomUserInfo":{"user":'
            '{"uniqueId":"jiaxin888__","nickName":"Host Display"}}}'
        )
        session = SimpleNamespace(get=lambda *args, **kwargs:
                                  SimpleNamespace(status_code=200, text=html))
        with patch.object(subject, 'ROOM_ID_COOKIE', ''):
            room = subject._client.request_live_room(session, 'jiaxin888__', 'sessionid=fallback')
        self.assertEqual(room['username'], 'jiaxin888__')
        self.assertEqual(room['nickname'], 'Host Display')
    def test_live_room_extracts_current_account_username_from_app_context(self):
        html = (
            '<script id="__UNIVERSAL_DATA_FOR_REHYDRATION__" type="application/json">'
            '{"__DEFAULT_SCOPE__":{"webapp.app-context":{"user":'
            '{"uid":"7582599467250435093","nickName":"Spider",'
            '"uniqueId":"spider_king017"}}},'
            '"liveRoomUserInfo":{"user":{"uniqueId":"target_host",'
            '"nickname":"Target Host","roomId":"7694780620325210901"}}}'
            '</script>'
        )
        session = SimpleNamespace(get=lambda *args, **kwargs:
                                  SimpleNamespace(status_code=200, text=html))
        with patch.object(subject, 'ROOM_ID_COOKIE', ''):
            room = subject._client.request_live_room(session, 'target_host', 'sessionid=current')

        self.assertEqual(room['roomId'], '7694780620325210901')
        self.assertEqual(room['username'], 'target_host')
        self.assertEqual(room['accountUsername'], 'spider_king017')
    def test_live_room_identity_falls_back_when_page_omits_owner_fields(self):
        session = SimpleNamespace(get=lambda *args, **kwargs: SimpleNamespace(
            status_code=200, text='{"roomId":"123456789012"}'))
        with patch.object(subject, 'ROOM_ID_COOKIE', ''):
            room = subject._client.request_live_room(session, '@requested_host', 'sessionid=fallback')
        self.assertEqual(room['username'], 'requested_host')
        self.assertEqual(room['nickname'], '-')
    def test_csrf_request_carries_account_cookie(self):
        captured = {}
        def head(url, **kwargs):
            captured.update(kwargs)
            return SimpleNamespace(headers={
                'x-ware-csrf-token':
                    '1,synthetic-request-token,1.2.22,1,synthetic-session-token'
            })
        token = subject.get_sec_sdk_csrf_token(
            SimpleNamespace(head=head), 'sessionid=synthetic-account-session')
        self.assertTrue(captured['headers'].get('cookie') == 'sessionid=synthetic-account-session')
        self.assertEqual(token, 'synthetic-request-token,synthetic-session-token')

    def test_token_report_request_carries_seed_cookie(self):
        captured = {}
        def post(url, **kwargs):
            captured.update(kwargs)
            return SimpleNamespace(headers={'x-ms-token': 'synthetic-fresh'}, cookies={})
        with patch.object(subject, 'generate_ms_seed_token', return_value='synthetic-seed'), \
                patch.object(subject, 'build_ms_token_environment', return_value='{}'), \
                patch.object(subject, 'build_ms_token_report_json', return_value=b'{}'), \
                patch.object(subject, 'sign_url', return_value='https://synthetic.test/report'):
            token = subject.get_fresh_ms_token(SimpleNamespace(post=post))
        self.assertTrue(captured['headers'].get('cookie') == 'msToken=synthetic-seed')
        self.assertTrue(token == 'synthetic-fresh')

    def test_browser_environment_is_stable_per_account_and_uses_cookie_ttwid(self):
        first = json.loads(subject.build_ms_token_environment(
            user_agent='Synthetic-UA', account_seed='0000000000000000',
            ttwid='account-one-ttwid'))
        repeat = json.loads(subject.build_ms_token_environment(
            user_agent='Synthetic-UA', account_seed='0000000000000000',
            ttwid='account-one-ttwid'))
        second = json.loads(subject.build_ms_token_environment(
            user_agent='Synthetic-UA', account_seed='1111111111111111',
            ttwid='account-two-ttwid'))

        self.assertEqual(first['screen'], repeat['screen'])
        self.assertEqual(first['navigator']['hardwareConcurrency'],
                         repeat['navigator']['hardwareConcurrency'])
        self.assertNotEqual(first['screen'], second['screen'])
        self.assertEqual(first['customInit']['ttwid'], 'account-one-ttwid')
        self.assertEqual(second['customInit']['ttwid'], 'account-two-ttwid')



class TicketGuardRegressionTests(unittest.TestCase):
    def test_upstream_ticket_guard_builds_fresh_verifiable_headers(self):
        import base64
        from cryptography.hazmat.primitives import hashes, serialization
        from cryptography.hazmat.primitives.asymmetric import ec
        from cryptography.hazmat.primitives.ciphers.aead import AESGCM
        from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC
        from tiktok_live.algorithms import ticket_guard

        private_key = ec.generate_private_key(ec.SECP256R1())
        private_pem = private_key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        ).decode("utf-8")
        key = PBKDF2HMAC(
            algorithm=hashes.SHA256(), length=16,
            salt=b"secure-salt", iterations=1000,
        ).derive(b"tt-ticket-guard-iv")
        nonce = b"0123456789ab"
        encrypted = nonce + AESGCM(key).encrypt(nonce, b"synthetic-ticket", None)

        headers = ticket_guard.build_headers(
            private_key_pem=private_pem,
            encrypt_ticket=base64.b64encode(encrypted).decode("ascii"),
            ts_sign="ts.1.synthetic",
            path="/webcast/room/chat/",
            timestamp=1791871957,
        )

        payload = ticket_guard.decode_client_data(
            headers["tt-ticket-guard-client-data"]
        )
        self.assertEqual(payload["ts_sign"], "ts.1.synthetic")
        self.assertEqual(payload["timestamp"], 1791871957)
        self.assertEqual(payload["req_content"], "ticket,path,timestamp")
        signed = b"ticket=synthetic-ticket&path=/webcast/room/chat/&timestamp=1791871957"
        private_key.public_key().verify(
            base64.b64decode(payload["req_sign"]), signed, ec.ECDSA(hashes.SHA256())
        )
        self.assertEqual(headers["tt-ticket-guard-version"], "2")
        self.assertEqual(headers["tt-ticket-guard-iteration-version"], "0")

    def test_cookie_key_material_accepts_browser_origin_crypt_field(self):
        import base64
        from urllib.parse import quote
        from tiktok_live.algorithms import ticket_guard

        outer = {
            "tt-ticket-guard-version": 2,
            "tt-ticket-guard-iteration-version": 1,
            "tt-ticket-guard-public-key": "synthetic-public-key",
            "tt-ticket-guard-origin-crypt": json.dumps({
                "ec_privateKey": "synthetic-private-key",
                "ec_publicKey": "synthetic-public-pem",
            }),
        }
        encoded = quote(base64.b64encode(
            json.dumps(outer).encode("utf-8")
        ).decode("ascii"))
        material = ticket_guard.cookie_key_material(
            "tt_ticket_guard_client_data=" + encoded
        )
        self.assertEqual(material["private_key_pem"], "synthetic-private-key")
        self.assertEqual(material["public_key"], "synthetic-public-key")
        self.assertEqual(material["version"], "2")
        self.assertEqual(material["iteration_version"], "1")

    def test_browser_ticket_guard_storage_is_parsed_without_logging_secrets(self):
        state = browser_auth.ticket_guard_state_from_tab(SimpleNamespace(
            run_js=lambda script: {
                "fetch": json.dumps({"data": json.dumps({
                    "ts_sign": "ts.1.synthetic",
                    "encrypt_ticket": "synthetic-encrypted-ticket",
                })}),
                "crypt": json.dumps({"data": json.dumps({
                    "ec_privateKey": "synthetic-private-key",
                    "ec_publicKey": "synthetic-public-key",
                    "ec_csr": "synthetic-csr",
                })}),
            }
        ))
        self.assertEqual(state, {
            "ts_sign": "ts.1.synthetic",
            "encrypt_ticket": "synthetic-encrypted-ticket",
            "private_key_pem": "synthetic-private-key",
        })

    def test_dynamic_headers_can_use_same_session_storage_private_key_without_cookie_field(self):
        import base64
        from cryptography.hazmat.primitives import hashes, serialization
        from cryptography.hazmat.primitives.asymmetric import ec
        from cryptography.hazmat.primitives.ciphers.aead import AESGCM
        from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC
        from tiktok_live.algorithms import ticket_guard

        private_key = ec.generate_private_key(ec.SECP256R1())
        private_pem = private_key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        ).decode("utf-8")
        key = PBKDF2HMAC(
            algorithm=hashes.SHA256(), length=16,
            salt=b"secure-salt", iterations=1000,
        ).derive(b"tt-ticket-guard-iv")
        nonce = b"0123456789ab"
        encrypted = nonce + AESGCM(key).encrypt(nonce, b"synthetic-ticket", None)
        state = {
            "ts_sign": "ts.1.synthetic",
            "encrypt_ticket": base64.b64encode(encrypted).decode("ascii"),
            "private_key_pem": private_pem,
        }

        headers = ticket_guard.build_headers_from_cookie_state(
            "sessionid=synthetic", state, "/webcast/room/chat/"
        )

        self.assertEqual(
            headers["tt-ticket-guard-public-key"],
            ticket_guard.public_key_base64(private_pem),
        )

    def test_chat_request_uses_dynamic_ticket_guard_headers(self):
        captured = {}
        response = SimpleNamespace(status_code=200)
        session = SimpleNamespace(post=lambda url, **kwargs: (
            captured.update({"url": url, **kwargs}) or response
        ))
        generated = {
            "tt-ticket-guard-client-data": "fresh-client-data",
            "tt-ticket-guard-iteration-version": "0",
            "tt-ticket-guard-public-key": "fresh-public-key",
            "tt-ticket-guard-version": "2",
            "tt-ticket-guard-web-version": "1",
        }
        state = {"ts_sign": "ts.1.synthetic", "encrypt_ticket": "ciphertext"}
        with patch.object(subject._client, "build_headers_from_cookie_state",
                          return_value=generated) as build, \
                patch.object(subject, "sign_url", return_value="https://synthetic.test/chat"):
            actual = subject.post_chat_request(
                session, subject.CHAT_URL, b"{}",
                "sessionid=synthetic; tt_ticket_guard_client_data=synthetic",
                "fresh-ms", None, "https://www.tiktok.com/@host/live",
                ticket_guard_state=state,
            )

        self.assertIs(actual, response)
        build.assert_called_once_with(
            "sessionid=synthetic; tt_ticket_guard_client_data=synthetic",
            state,
            "/webcast/room/chat/",
        )
        for key, value in generated.items():
            self.assertEqual(captured["headers"][key], value)

    def test_runner_forwards_only_the_matching_accounts_ticket_guard_state(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "accounts.json"
            store = {"version": 1, "accounts": []}
            account = subject.upsert_account(store, [{
                "name": "uid_tt", "value": "synthetic-user",
                "domain": ".tiktok.com", "path": "/", "expires": -1,
            }], datetime.now(timezone.utc))
            state = {
                "ts_sign": "ts.1.synthetic",
                "encrypt_ticket": "synthetic-ciphertext",
            }
            account["ticket_guard"] = state
            subject.save_account_store_atomic(path, store)
            captured = {}

            def send(username, cookie, message, **kwargs):
                captured.update(kwargs)
                return {"ok": True}

            with patch.object(subject, "send_live_message", side_effect=send):
                result = subject.run_saved_accounts(
                    "host", "hello", path, add_new_account=False,
                    interval_seconds=0,
                )

        self.assertEqual(result["success"], 1)
        self.assertEqual(captured["ticket_guard_state"], state)

    def test_account_store_preserves_ticket_guard_state(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "accounts.json"
            store = {"version": 1, "accounts": []}
            account = subject.upsert_account(store, [{
                "name": "uid_tt", "value": "synthetic-user",
                "domain": ".tiktok.com", "path": "/", "expires": -1,
            }], datetime.now(timezone.utc))
            account["ticket_guard"] = {
                "ts_sign": "ts.1.synthetic",
                "encrypt_ticket": "synthetic-ciphertext",
            }
            subject.save_account_store_atomic(path, store)
            loaded = subject.load_account_store(path)
        self.assertEqual(loaded["accounts"][0]["ticket_guard"], account["ticket_guard"])


class IdentityRegressionTests(unittest.TestCase):
    # These regressions fail if matching uses only the historical stored ID.
    setUp = AccountStoreTests.setUp
    cookies = AccountStoreTests.cookies
    def session(self, value='synthetic-session'):
        return {'name': 'sessionid', 'value': value, 'domain': '.tiktok.com',
                'path': '/', 'expires': -1}

    def test_session_then_uid_updates_original_record(self):
        store = {'version': 1, 'accounts': []}
        original = subject.upsert_account(store, [self.session()], self.now)
        original_id = original['id']
        later = datetime(2026, 10, 10, tzinfo=timezone.utc)
        refreshed = [self.session()] + self.cookies()
        updated = subject.upsert_account(store, refreshed, later)
        self.assertEqual(len(store['accounts']), 1)
        self.assertEqual(updated['id'], original_id)
        self.assertEqual(updated['created_at'], self.now.isoformat())
        self.assertEqual(updated['updated_at'], later.isoformat())
        self.assertTrue(updated['cookies'] == refreshed)

    def test_runner_refresh_then_identical_scan_keeps_one_record(self):
        store = {'version': 1, 'accounts': []}
        original = subject.upsert_account(store, [self.session()], self.now)
        original_id = original['id']
        subject.save_account_store_atomic(self.path, store)
        refreshed = [self.session('synthetic-rotated')] + self.cookies()
        with patch.object(subject, 'send_live_message', return_value={'ok': True}):
            subject.run_saved_accounts(
                'host', 'hello', self.path, add_new_account=False, interval_seconds=0)
        store = subject.load_account_store(self.path)
        updated = subject.upsert_account(store, refreshed, datetime.now(timezone.utc))
        self.assertEqual(len(store['accounts']), 2)
        self.assertNotEqual(updated['id'], original_id)

    def test_existing_duplicates_runner_sends_once_and_keeps_oldest_creation(self):
        refreshed = [self.session()] + self.cookies()
        store = {'version': 1, 'accounts': [
            {'id': 'a' * 64, 'created_at': self.now.isoformat(),
             'updated_at': self.now.isoformat(), 'cookies': [self.session()]},
            {'id': 'b' * 64, 'created_at': '2026-10-10T00:00:00+00:00',
             'updated_at': '2026-10-10T00:00:00+00:00', 'cookies': refreshed}]}
        subject.save_account_store_atomic(self.path, store)
        with patch.object(subject, 'send_live_message', return_value={'ok': True}) as send:
            counters = subject.run_saved_accounts(
                'host', 'hello', self.path, add_new_account=False, interval_seconds=0)
        self.assertEqual(send.call_count, 1)
        self.assertEqual(counters['success'], 1)
        saved = subject.load_account_store(self.path)['accounts']
        self.assertEqual(len(saved), 1)
        self.assertEqual(saved[0]['created_at'], self.now.isoformat())
        self.assertEqual(subject.cookie_header_from_playwright(saved[0]['cookies']),
                         subject.cookie_header_from_playwright(refreshed))

    def test_distinct_uid_or_unrelated_sessions_are_not_merged(self):
        for second in ([self.session('synthetic-other')],
                       [self.session(), dict(self.cookies()[0], value='synthetic-other-user')]):
            with self.subTest(conflicting_uid=len(second) == 2):
                store = {'version': 1, 'accounts': []}
                subject.upsert_account(store, [self.session()] + self.cookies(), self.now)
                subject.upsert_account(store, second, self.now)
                self.assertEqual(len(store['accounts']), 2)

    def test_uid_overlap_with_rotated_session_updates_one_record(self):
        store = {'version': 1, 'accounts': []}
        subject.upsert_account(store, [self.session()] + self.cookies(), self.now)
        subject.upsert_account(store, [self.session('synthetic-rotated')] + self.cookies(), self.now)
        self.assertEqual(len(store['accounts']), 1)

    def test_separate_legacy_sessions_are_sent_without_browser_refresh(self):
        store = {'version': 1, 'accounts': []}
        for value in ('synthetic-first', 'synthetic-second'):
            subject.upsert_account(store, [self.session(value)], self.now)
        subject.save_account_store_atomic(self.path, store)
        with patch.object(subject, 'send_live_message', return_value={'ok': True}) as send:
            result = subject.run_saved_accounts(
                'host', 'hello', self.path, add_new_account=False, interval_seconds=0)
        self.assertEqual(send.call_count, 2)
        self.assertEqual(result['success'], 2)
        self.assertEqual(len(subject.load_account_store(self.path)['accounts']), 2)

    def test_conflicting_uid_with_shared_session_runner_keeps_both_accounts(self):
        store = {'version': 1, 'accounts': []}
        for uid in ('synthetic-first', 'synthetic-second'):
            subject.upsert_account(store, [self.session(), dict(self.cookies()[0], value=uid)], self.now)
        subject.save_account_store_atomic(self.path, store)
        with patch.object(subject, 'send_live_message', return_value={'ok': True}) as send:
            result = subject.run_saved_accounts(
                'host', 'hello', self.path, add_new_account=False, interval_seconds=0)
        self.assertEqual(send.call_count, 2)
        self.assertEqual(result['success'], 2)
        self.assertEqual(len(subject.load_account_store(self.path)['accounts']), 2)

    def test_unrelated_accounts_with_shared_nonidentity_cookie_stay_separate(self):
        store = {'version': 1, 'accounts': []}
        theme = {'name': 'theme', 'value': 'same', 'domain': '.tiktok.com'}
        for value in ('synthetic-first', 'synthetic-second'):
            subject.upsert_account(store, [self.session(value), theme], self.now)
        self.assertEqual(len(store['accounts']), 2)


class CliSafetyRegressionTests(unittest.TestCase):
    # Run the real __main__ AST in a child process with fake external boundaries.
    # Never print captured output on regression: it may contain synthetic secrets.
    sensitive = ('synthetic-cookie-private synthetic-ms-private synthetic-csrf-private '
                 'synthetic-signature-private synthetic-ticket-private '
                 'https://synthetic.test/private-request')

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / 'accounts.json'

    def run_cli(self, scenario):
        bootstrap = '''
import ast, sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
import send_live_message as subject
subject.logger.remove()
subject.logger.add(sys.stderr, format='{message}')
path = Path(sys.argv[1])
original_runner = subject.run_saved_accounts
subject.run_saved_accounts = lambda username, message: original_runner(
    username, message, path, add_new_account=False, interval_seconds=0)
'''
        invoke = '''
tree = ast.parse(Path(subject.__file__).read_text(encoding='utf-8-sig'))
entry = next(node for node in tree.body if isinstance(node, ast.If) and isinstance(node.test, ast.Compare) and isinstance(node.test.left, ast.Name) and node.test.left.id == '__name__')
subject.__dict__['__name__'] = '__main__'
exec(compile(ast.Module(body=[entry], type_ignores=[]), subject.__file__, 'exec'), subject.__dict__)
'''
        result = subprocess.run([sys.executable, '-c', bootstrap + scenario + invoke, str(self.path)],
                                cwd=Path(subject.__file__).parent, capture_output=True,
                                text=True, encoding='utf-8', errors='replace',
                                env=dict(os.environ, PYTHONUTF8='1'))
        output = result.stdout + result.stderr
        for value in self.sensitive.split():
            self.assertTrue(value not in output, 'sensitive output leaked')
        self.assertTrue('Traceback' not in output, 'raw traceback leaked')
        self.assertNotEqual(result.returncode, 0)
        return output

    def save_account(self):
        store = {'version': 1, 'accounts': []}
        subject.upsert_account(store, [{'name': 'sessionid', 'value': 'synthetic-cookie-private',
            'domain': '.tiktok.com', 'path': '/', 'expires': -1}], datetime.now(timezone.utc))
        subject.save_account_store_atomic(self.path, store)
        return self.path.read_bytes()

    def test_corrupt_store_cli_reports_error_and_preserves_bytes(self):
        for raw in (b'{broken synthetic-cookie-private', b'\xffinvalid',
                    b'{"version": 2, "accounts": []}'):
            with self.subTest(invalid_utf8=raw.startswith(b'\xff')):
                self.path.write_bytes(raw)
                output = self.run_cli('')
                self.assertTrue('classification=account_store_corrupt' in output)
                self.assertTrue('备份' in output)
                self.assertTrue(self.path.read_bytes() == raw)

    def test_save_failure_cli_reports_safe_recovery_and_preserves_file(self):
        original = self.save_account()
        scenario = '''
def replace(*args):
    raise OSError('SENSITIVE')
subject.os.replace = replace
subject.send_live_message = lambda *args, **kwargs: {'ok': False, 'loginExpired': True, 'message': '请登录'}
'''.replace('SENSITIVE', self.sensitive)
        output = self.run_cli(scenario)
        self.assertTrue('classification=runtime_error' in output)
        self.assertTrue('已保留完成消息的结果' in output)
        self.assertTrue('display_status=send_failed | classification=expired' in output)
        self.assertTrue('未发送当前账户消息' not in output)
        self.assertTrue(self.path.read_bytes() == original)

    def test_manual_interrupt_is_reported_as_run_cancelled(self):
        output = self.run_cli(
            "subject.run_saved_accounts = lambda *args: (_ for _ in ()).throw(KeyboardInterrupt())\n"
        )
        self.assertIn('classification=run_cancelled', output)
        self.assertNotIn('classification=login_cancelled', output)

    def test_package_cli_reports_manual_interrupt_as_run_cancelled(self):
        with patch.object(package_cli, 'run_saved_accounts', side_effect=KeyboardInterrupt), \
                patch.object(package_cli, 'log_safe_error') as log_error:
            self.assertEqual(package_cli.run_cli(), 1)
        log_error.assert_called_once_with('run_cancelled')

    def test_unexpected_cli_error_is_fixed_and_safe(self):
        output = self.run_cli("subject.run_saved_accounts = lambda *args: (_ for _ in ()).throw(RuntimeError('" + self.sensitive + "'))\n")
        self.assertTrue('classification=runtime_error' in output)


if __name__ == '__main__':
    unittest.main()









