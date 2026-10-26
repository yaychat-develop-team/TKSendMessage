"""Anonymous live-page WAF discovery; no account session or external network."""
import base64
from copy import deepcopy
from http.cookiejar import Cookie, CookieJar
import json
from types import SimpleNamespace
import unittest
from unittest.mock import patch
from urllib.request import Request
from tiktok_live import client

HOST = 'hungcamdayne'
ROOM = '7698987141979736852'
CHALLENGE = {'v': {'a': 'YWJj', 'b': 100,
                  'c': 'ghasd/XJ7WaQewPx14t5XnzdMVzs0T30LNckXUXhawY='},
             's': 'synthetic-signature'}
ASSET = 'https://sf16-website-login.neutral.ttwstatic.com/obj/tiktok_web_login_static/obj/waf-aiso/dd9808.js'
def waf():
    cs = base64.b64encode(json.dumps(CHALLENGE, separators=(',', ':')).encode()).decode().rstrip('=')
    return ('<html>SlardarWAF<script src="' + ASSET + '"></script>'
        '<div id="wci" class="_wafchallengeid"></div><div id="cs" class="' + cs + '"></div>'
        '<div id="rci" class="waforiginalreid"></div><div id="rs" class=""></div>'
        '<div id="rs_id" class="eu"></div></html>')

def page(owner=HOST, status=2, room=ROOM):
    state = '<script id="SIGI_STATE">' + json.dumps({'LiveRoom': {'liveRoomUserInfo': {
        'user': {'uniqueId': owner, 'roomId': room, 'status': status, 'nickname': 'Host'},
        'liveRoom': {'status': status}}}}) + '</script>'
    context = '<script id="__UNIVERSAL_DATA_FOR_REHYDRATION__">' + json.dumps({
        '__DEFAULT_SCOPE__': {'webapp.app-context': {
            'region': 'JP', 'wid': '7123456789012345678',
            'user': {'region': 'JP'},
        }},
    }) + '</script>'
    return state + context + '<script>window.__webcast="https:\\/\\/webcast.eu.tiktok.com";</script>'

class PublicRoomPageTests(unittest.TestCase):
    def setUp(self):
        self.pages, self.sessions, self.calls = [waf(), page()], [], []
        self.status, self.response_headers = 200, {'Content-Type': 'text/html'}
        owner = self
        class Session:
            def __init__(self, **kwargs):
                self.options, self.closed = kwargs, False
                jar = CookieJar()
                self.cookies = SimpleNamespace(jar=jar, clear=jar.clear)
                owner.sessions.append(self)
            def __enter__(self):
                return self
            def __exit__(self, *args):
                self.closed = True
            def get(self, url, **kwargs):
                req = Request(url)
                self.cookies.jar.add_cookie_header(req)
                owner.calls.append((url, deepcopy(kwargs), req.get_header('Cookie', '')))
                body = owner.pages.pop(0)
                if isinstance(body, Exception):
                    raise body
                raw = body.encode()
                callback = kwargs['content_callback']
                if callback(raw) != len(raw):
                    raise ValueError('Receive aborted')
                return SimpleNamespace(status_code=owner.status, headers=owner.response_headers)
        self.factory = Session

    def lookup(self, timeout=12):
        from tiktok_live.public_room import query_live_page
        with patch.object(client.requests, 'Session', self.factory):
            return query_live_page('@' + HOST, timeout_seconds=timeout)

    def test_default_lookup_uses_fresh_anonymous_curl_and_dynamic_waf_proof(self):
        try:
            result = self.lookup()
        except (ValueError, AssertionError):
            result = None
        self.assertIsNotNone(result, 'Default discovery must use the anonymous live-page WAF flow')
        self.assertEqual(result['roomId'], ROOM)
        self.assertTrue(result['isLive'])
        self.assertEqual(result['querySource'], 'live_page_waf')
        self.assertIsNone(result['accountUsername'])
        self.assertEqual(
            result['fetchEndpoint'],
            'https://webcast.eu.tiktok.com/webcast/im/fetch/',
        )
        self.assertEqual(result['region'], 'JP')
        self.assertEqual(result['deviceId'], '7123456789012345678')
        self.assertEqual([c[0] for c in self.calls], ['https://www.tiktok.com/@hungcamdayne/live'] * 2)
        self.assertNotIn('sessionid', self.calls[1][2])
        proof = self.calls[1][2].split('_wafchallengeid=', 1)[1].split(';', 1)[0]
        self.assertEqual(json.loads(base64.b64decode(proof))['d'], 'NDI=')
        self.assertFalse(self.sessions[0].options['trust_env'])
        self.assertTrue(self.sessions[0].closed)
        self.assertEqual(list(self.sessions[0].cookies.jar), [])
        self.assertTrue(all(0 < c[1]['timeout'] <= 12 and c[1]['allow_redirects'] is False for c in self.calls))
        self.assertTrue(all(not any(k.lower() == 'cookie' for k in c[1]['headers']) for c in self.calls))

    def test_offline_room_remains_offline_after_waf(self):
        self.pages = [waf(), page(status=4)]
        result = self.lookup()
        self.assertFalse(result['isLive'])
        self.assertEqual(result['liveStatus'], 4)

    def test_wrong_owner_is_rejected_without_generic_roomid_fallback(self):
        self.pages = [waf(), page(owner='somebody_else')]
        with self.assertRaises(ValueError):
            self.lookup()
        self.assertTrue(self.sessions[0].closed)
        self.assertEqual(list(self.sessions[0].cookies.jar), [])

    def test_unrecognized_waf_or_exhausted_challenge_has_no_unbounded_retry(self):
        for pages, count in ((['SlardarWAF'], 1), ([waf()] * 3, 3)):
            self.pages, self.calls = list(pages), []
            with self.assertRaises(ValueError):
                self.lookup()
            self.assertEqual(len(self.calls), count)

    def test_http_redirect_or_oversized_html_is_rejected(self):
        for status, body in ((302, page()), (200, 'x' * (4 * 1024 * 1024 + 1))):
            self.status, self.pages = status, [body]
            with self.assertRaises(ValueError):
                self.lookup()
            self.assertTrue(self.sessions[-1].closed)

    def test_expired_total_deadline_stops_before_next_get(self):
        from tiktok_live import public_room
        with patch.object(public_room.time, 'monotonic', side_effect=[100, 100, 120]):
            with self.assertRaises(TimeoutError):
                self.lookup()
        self.assertLessEqual(len(self.calls), 1)

    def test_waf_search_is_bounded_and_keeps_server_signature(self):
        from tiktok_live.public_room import _public_waf_cookies
        with self.assertRaisesRegex(ValueError, 'budget'):
            _public_waf_cookies(waf(), max_nonce=10)
        proof = json.loads(base64.b64decode(_public_waf_cookies(waf())['_wafchallengeid']))
        self.assertEqual(proof['s'], 'synthetic-signature')
        self.assertEqual(proof['v'], CHALLENGE['v'])

    def test_conflicting_current_room_ids_are_rejected(self):
        html = page().replace('"liveRoom": {"status": 2}', '"liveRoom": {"status": 2, "id": "123456789012"}')
        self.pages = [html]
        with self.assertRaises(ValueError):
            self.lookup()

    def test_recommended_same_owner_cannot_replace_canonical_current_room(self):
        historical = json.loads(page().split('>', 1)[1].split('</script>', 1)[0])['LiveRoom']
        canonical = deepcopy(historical)
        canonical['liveRoomUserInfo']['user']['roomId'] = '123456789012'
        self.pages = ['<script id="SIGI_STATE">' + json.dumps({
            'recommendations': {'historical': historical}, 'LiveRoom': canonical}) + '</script>']
        self.assertEqual(self.lookup()['roomId'], '123456789012')

    def test_cancel_after_waf_response_prevents_any_followup_get(self):
        from tiktok_live import public_room
        state = {'cancel': False}
        original = self.factory.get
        def get(session, *args, **kwargs):
            result = original(session, *args, **kwargs)
            state['cancel'] = True
            return result
        with patch.object(self.factory, 'get', get), \
                patch.object(client.requests, 'Session', self.factory), self.assertRaises(TimeoutError):
            client.get_live_room(None, HOST, '', should_cancel=lambda: state['cancel'])
        self.assertEqual(len(self.calls), 1)
        self.assertEqual(list(self.sessions[0].cookies.jar), [])

    def test_waf_compute_respects_short_remaining_deadline(self):
        from tiktok_live.public_room import _public_waf_cookies
        clock = iter([0, 2])
        with self.assertRaisesRegex(ValueError, 'budget'):
            _public_waf_cookies(waf(), timeout_seconds=1, clock=lambda: next(clock))

    def test_webcast_endpoint_parser_rejects_lookalikes_and_ambiguity(self):
        from tiktok_live.public_room import extract_webcast_fetch_endpoint
        self.assertIsNone(extract_webcast_fetch_endpoint(
            '<script>"https://webcast.tiktok.com.evil.example"</script>'
        ))
        with self.assertRaisesRegex(ValueError, 'ambiguous'):
            extract_webcast_fetch_endpoint(
                '<script>"https://webcast.eu.tiktok.com";'
                '"https://webcast.us.tiktok.com"</script>'
            )

    def test_page_region_comes_only_from_canonical_app_context(self):
        from tiktok_live.public_room import extract_live_page_region
        canonical = '<script id="__UNIVERSAL_DATA_FOR_REHYDRATION__">' + json.dumps({
            '__DEFAULT_SCOPE__': {'webapp.app-context': {'region': 'US'}},
        }) + '</script>'
        self.assertEqual(extract_live_page_region(
            '<script>{"region":"DE"}</script>' + canonical
        ), 'US')
        self.assertIsNone(extract_live_page_region('<script>{"region":"DE"}</script>'))

    def test_page_device_id_comes_only_from_canonical_app_context(self):
        from tiktok_live.public_room import extract_live_page_device_id
        canonical = '<script id="__UNIVERSAL_DATA_FOR_REHYDRATION__">' + json.dumps({
            '__DEFAULT_SCOPE__': {'webapp.app-context': {'wid': '7123456789012345678'}},
        }) + '</script>'
        self.assertEqual(extract_live_page_device_id(
            '<script>{"device_id":"7999999999999999999"}</script>' + canonical
        ), '7123456789012345678')
        self.assertIsNone(extract_live_page_device_id(
            '<script>{"device_id":"7999999999999999999"}</script>'
        ))

if __name__ == '__main__':
    unittest.main()

