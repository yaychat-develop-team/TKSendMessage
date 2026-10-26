"""Native observer bootstrap must use isolated, bounded curl_cffi JSON requests."""
import asyncio
import base64
from copy import deepcopy
from http.cookiejar import CookieJar
import json
import threading
import time
from types import SimpleNamespace
import unittest
from unittest.mock import patch
from urllib.parse import urlsplit
from urllib.request import Request

from curl_cffi import requests
from tiktok_live.wss import sessions
from tiktok_live.wss.startup import StartupScope
from tests.test_direct_sessions import cookie


class VisitorApiBootstrapTests(unittest.TestCase):
    def test_observer_bootstraps_without_page_report_or_fetch(self):
        result = self.prepare()
        self.assertIsNone(result.initial_fetch)
        self.assertEqual([p for p,c in self.calls],['/ttwid/check/','/ttwid/register/'])

    def setUp(self):
        self.calls, self.instances = [], []
        self.answers = [{'status_code': 1002}, {'status_code': 0}]
        self.cookies = [cookie()]
        self.http_status = 200
        self.content_type = 'application/json'
        self.cancel_after = None
        self.check_cookies = []
        self.room_payload = {'data': {'user': {'uniqueId': 'host', 'roomId': '42', 'status': 2},
                                      'liveRoom': {'status': 2}}}
        self.route_host = 'webcast-ws.us.tiktok.com'
        self.fetch_kind = 'application/x-protobuf'
        self.page_endpoint = 'https://webcast.us.tiktok.com/webcast/im/fetch/'
        self.page_challenges = []
        self.page_cookie_headers = []
        self.after_page = None
        owner = self
        class FakeSession:
            def __init__(self, **kwargs):
                self.options = kwargs
                self.cookies = SimpleNamespace(jar=CookieJar())
                self.closed = False
                owner.instances.append(self)
            async def __aenter__(self):
                return self
            async def __aexit__(self, *args):
                self.closed = True
            async def post(self, url, **kwargs):
                owner.calls.append((urlsplit(url).path, deepcopy(kwargs)))
                if urlsplit(url).path == '/web/report':
                    return SimpleNamespace(status_code=200, headers={'x-ms-token': 'synthetic-token'})
                if urlsplit(url).path == '/ttwid/check/':
                    for item in owner.check_cookies:
                        self.cookies.jar.set_cookie(item)
                if urlsplit(url).path == '/ttwid/register/':
                    for item in owner.cookies:
                        self.cookies.jar.set_cookie(item)
                answer = owner.answers.pop(0)
                if owner.cancel_after:
                    owner.cancel_after()
                if kwargs.get('content_callback'):
                    kwargs['content_callback'](json.dumps(answer).encode())
                return SimpleNamespace(status_code=owner.http_status,
                    headers={'content-type': owner.content_type}, content=json.dumps(answer).encode(),
                    json=lambda: deepcopy(answer))
            async def get(self, url, **kwargs):
                owner.calls.append((urlsplit(url).path, deepcopy(kwargs)))
                if urlsplit(url).path == '/@host/live':
                    req = Request(url)
                    self.cookies.jar.add_cookie_header(req)
                    owner.page_cookie_headers.append(req.get_header('Cookie', ''))
                    state = {'LiveRoom': {'liveRoomUserInfo': owner.room_payload['data']}}
                    raw = ('<script id="SIGI_STATE">' + json.dumps(state) + '</script>' + owner.page_endpoint).encode()
                    if owner.page_challenges:
                        raw = owner.page_challenges.pop(0).encode()
                    kwargs['content_callback'](raw)
                    if owner.after_page:
                        owner.after_page()
                    return SimpleNamespace(status_code=200, headers={'content-type': 'text/html'})
                if urlsplit(url).path == '/webcast/im/fetch/':
                    from tiktok_live.wss.protocol import _wire
                    raw = (_wire(2, 'server-cursor') + _wire(7, _wire(1, 'wrss') + _wire(2, 'route'))
                           + _wire(10, 'wss://' + owner.route_host + '/webcast/im/ws_proxy/ws_reuse_supplement/'))
                    kwargs['content_callback'](raw)
                    return SimpleNamespace(status_code=200, headers={'content-type': owner.fetch_kind})
                payload = deepcopy(owner.room_payload)
                if kwargs.get('content_callback'):
                    kwargs['content_callback'](json.dumps(payload).encode())
                return SimpleNamespace(status_code=200, headers={'content-type': 'application/json'},
                    content=json.dumps(payload).encode(), json=lambda: payload)
        self.factory = FakeSession

    def prepare(self, *, scope=None, room='42'):
        with patch.object(requests, 'AsyncSession', self.factory):
            return sessions.prepare_observer('host', known_room_id=room,
                proxy_setting='direct', startup_scope=scope)

    def prepare_legacy_route(self, *, scope=None):
        # The optional route helper is still tested independently; production
        # observer bootstrap no longer calls its page/report/fetch chain.
        from tiktok_live.wss.observer_route import initialize_observer_route
        from tiktok_live.wss.direct_models import DirectSession
        async def run(active):
            async with self.factory(impersonate='chrome') as http:
                try:
                    for item in self.cookies:
                        http.cookies.jar.set_cookie(item)
                    guest=DirectSession('delivery_observer','host','42',sessions.PUBLIC_USER_AGENT,
                                        sessions.visitor_cookie_header(http.cookies.jar))
                    return await initialize_observer_route(http,guest,active)
                finally:
                    http.cookies.jar.clear()
        if scope is not None:
            return asyncio.run(run(scope))
        with StartupScope(15) as active:
            return asyncio.run(run(active))

    def test_legacy_route_waf_then_page_uses_same_visitor_ua_and_scoped_proof(self):
        from tests.test_public_room_page import waf
        self.page_challenges = [waf()]
        try:
            result = self.prepare_legacy_route()
        except ValueError:
            result = None
        self.assertIsNotNone(result, 'Known WAF must allow bounded same-session page retry')
        self.assertEqual(len(self.instances), 1)
        self.assertEqual(len(self.page_cookie_headers), 2)
        proof = self.page_cookie_headers[1].split('_wafchallengeid=', 1)[1].split(';', 1)[0]
        self.assertEqual(json.loads(base64.b64decode(proof))['d'], 'NDI=')
        self.assertNotIn('_waf', result.cookie_header)
        page_calls = [c for p,c in self.calls if p == '/@host/live']
        self.assertEqual(page_calls[0]['headers'], page_calls[1]['headers'])
        self.assertTrue(self.instances[0].closed)
        self.assertEqual(list(self.instances[0].cookies.jar), [])

    def test_legacy_route_repeated_waf_stops_after_three_pages_without_fetch(self):
        from tests.test_public_room_page import waf
        self.page_challenges = [waf()] * 4
        with self.assertRaises(ValueError):
            self.prepare_legacy_route()
        self.assertEqual(len(self.page_cookie_headers), 3)
        self.assertNotIn('/webcast/im/fetch/', [p for p,c in self.calls])

    def test_legacy_route_cancel_after_waf_prevents_another_page(self):
        from tests.test_public_room_page import waf
        self.page_challenges = [waf()]
        with StartupScope(3) as scope:
            self.after_page = scope.cancel
            with self.assertRaises(TimeoutError):
                self.prepare_legacy_route(scope=scope)
        self.assertEqual(len(self.page_cookie_headers), 1)
        self.assertTrue(self.instances[0].closed)

    def test_legacy_route_unknown_waf_never_retries_or_fetches(self):
        self.page_challenges = ['SlardarWAF unsupported']
        with self.assertRaises(ValueError):
            self.prepare_legacy_route()
        self.assertEqual(len(self.page_cookie_headers), 1)
        self.assertNotIn('/webcast/im/fetch/', [p for p,c in self.calls])

    def test_known_room_only_bootstraps_anonymous_cookie(self):
        try:
            result = self.prepare()
        except Exception:
            result = None
        self.assertIsNotNone(result, 'Observer must initialize through browser-free HTTP')
        self.assertEqual((result.role, result.room_id, result.cookie_header),
                         ('delivery_observer', '42', 'ttwid=synthetic-visitor'))
        self.assertEqual([p for p, _ in self.calls], ['/ttwid/check/', '/ttwid/register/'])
        self.assertIsNone(result.initial_fetch)
        for _, call in self.calls[:2]:
            self.assertFalse(call['allow_redirects'])
            self.assertNotIn('cookie', {k.lower() for k in call['headers']})
            self.assertLessEqual(call['timeout'], 15)
            self.assertIs(call.get('verify', True), True)
        first = json.loads(self.calls[0][1]['data'])
        second = json.loads(self.calls[1][1]['data'])
        self.assertEqual(first, {'aid': 1988, 'service': 'www.tiktok.com', 'union': False,
            'unionHost': '', 'needFid': False, 'fid': '', 'migrate_priority': 0})
        self.assertNotIn('migrate_info', second)
        self.assertTrue(self.instances[0].closed)

    def test_each_observer_gets_its_own_empty_curl_cookie_jar(self):
        self.prepare()
        self.answers = [{'status_code': 1002}, {'status_code': 0}]
        self.prepare()
        self.assertIsNot(self.instances[0].cookies.jar, self.instances[1].cookies.jar)
        self.assertTrue(all(s.options.get('trust_env') is False for s in self.instances))

    def test_migration_state_is_forwarded_only_to_same_origin_register(self):
        self.answers[0]['migrate_info'] = {'ticket': 'synthetic-migration'}
        self.prepare()
        self.assertEqual(json.loads(self.calls[1][1]['data'])['migrate_info'],
                         {'ticket': 'synthetic-migration'})

    def test_insecure_expired_login_or_wrong_domain_cookie_is_rejected(self):
        for item in (cookie(secure=False), cookie(expires=1),
                     cookie(domain='.evil.test'), cookie('sessionid', 'synthetic-login')):
            with self.subTest(name=item.name, domain=item.domain):
                self.answers = [{'status_code': 1002}, {'status_code': 0}]
                self.cookies = [item]
                with self.assertRaises(ValueError):
                    self.prepare()
                self.assertTrue(self.instances[-1].closed)

    def test_non_json_http_error_or_invalid_status_is_not_a_success(self):
        for status, kind, answer in ((403, 'application/json', {'status_code': 1002}),
                                    (200, 'text/html', {'status_code': 1002}),
                                    (200, 'application/json', {'status_code': True})):
            with self.subTest(http=status, content_type=kind):
                self.http_status, self.content_type, self.answers = status, kind, [answer]
                self.calls.clear()
                with self.assertRaises(ValueError):
                    self.prepare()
                self.assertEqual(len(self.calls), 1)
                self.assertTrue(self.instances[-1].closed)

    def test_failed_registration_does_not_use_issued_cookie(self):
        self.answers = [{'status_code': 1002}, {'status_code': 1234}]
        with self.assertRaises(ValueError):
            self.prepare()

    def test_login_cookie_from_check_is_rejected_before_another_http_request(self):
        self.check_cookies = [cookie('sessionid', 'synthetic-login')]
        with self.assertRaises(ValueError):
            self.prepare()
        self.assertEqual([p for p, _ in self.calls], ['/ttwid/check/'])

    def test_cancel_between_requests_prevents_registration_and_closes_session(self):
        with StartupScope(3) as scope:
            self.cancel_after = scope.cancel
            with self.assertRaises(TimeoutError):
                self.prepare(scope=scope)
        self.assertEqual([p for p, _ in self.calls], ['/ttwid/check/'])
        self.assertTrue(self.instances[0].closed)

    def test_deadline_cancels_pending_curl_request_before_its_own_timeout(self):
        entered, aborted, closed = threading.Event(), threading.Event(), threading.Event()
        class PendingSession(self.factory):
            async def post(self, *args, **kwargs):
                entered.set()
                try:
                    await asyncio.Future()
                finally:
                    aborted.set()
            async def __aexit__(self, *args):
                await super().__aexit__(*args)
                closed.set()
        self.factory = PendingSession
        started = time.monotonic()
        with StartupScope(.05) as scope:
            with self.assertRaises(TimeoutError):
                self.prepare(scope=scope)
        self.assertTrue(entered.is_set() and aborted.is_set() and closed.is_set())
        self.assertLess(time.monotonic() - started, .4)

    def test_missing_known_id_queries_room_before_route_page(self):
        result = self.prepare(room=None)
        self.assertEqual(result.room_id, '42')
        room_call = next(c for p,c in self.calls if p == '/api-live/user/room/')
        self.assertEqual(room_call['params']['uniqueId'], 'host')

    def test_legacy_route_bad_route_or_empty_json_fetch_has_no_fallback(self):
        for field, value in (('route_host', 'evil.test'), ('fetch_kind', 'application/json'),
                             ('page_endpoint', '')):
            with self.subTest(field=field):
                self.setUp()
                setattr(self, field, value)
                with self.assertRaises(ValueError):
                    self.prepare_legacy_route()
                self.assertTrue(self.instances[-1].closed)

    def test_room_api_rejects_bad_owner_live_state_id_and_present_noninteger_status(self):
        valid = deepcopy(self.room_payload)
        bad_owner = deepcopy(valid)
        bad_owner['data']['user']['uniqueId'] = 'other'
        offline = deepcopy(valid)
        offline['data']['liveRoom']['status'] = 4
        bad_id = deepcopy(valid)
        bad_id['data']['user']['roomId'] = '0'
        for payload in ({**valid, 'status_code': False}, {**valid, 'status_code': 0.0},
                        {**valid, 'status_code': 123}, bad_owner, offline, bad_id, {'data': []}):
            with self.subTest(payload=payload):
                self.answers = [{'status_code': 1002}, {'status_code': 0}]
                self.room_payload = payload
                with self.assertRaises(ValueError):
                    self.prepare(room=None)

    def test_account_heartbeat_preparation_remains_http_free(self):
        with patch.object(requests, 'AsyncSession', side_effect=AssertionError('No visitor mixing')):
            result = sessions.prepare_account('host', '42', 'sessionid=synthetic', 'UA', proxy_setting='direct')
        self.assertEqual((result.role, result.cookie_header), ('account_heartbeat', 'sessionid=synthetic'))

    def test_oversize_body_is_aborted_during_receive_not_after_buffering(self):
        from curl_cffi.curl import CURL_WRITEFUNC_ERROR
        received = []
        class OversizeSession(self.factory):
            async def post(self, url, **kwargs):
                chunks = []
                for _ in range(20):
                    chunk = b'x' * 16384
                    received.append(len(chunk))
                    callback = kwargs.get('content_callback')
                    if callback and callback(chunk) == CURL_WRITEFUNC_ERROR:
                        raise ValueError('Synthetic write aborted')
                    chunks.append(chunk)
                return SimpleNamespace(status_code=200, headers={'content-type': 'application/json'},
                    content=b''.join(chunks), json=lambda: {})
        self.factory = OversizeSession
        with self.assertRaises(ValueError):
            self.prepare()
        self.assertLessEqual(sum(received), 65536 + 16384)
