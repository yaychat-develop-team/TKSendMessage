import importlib.util
import io
import json
from urllib.error import HTTPError
from http.cookiejar import Cookie, CookieJar
import unittest
from unittest.mock import patch


def cookie(name='ttwid', value='synthetic-visitor', *, domain='.tiktok.com', secure=True, expires=None):
    return Cookie(0, name, value, None, False, domain, True, domain.startswith('.'),
                  '/', True, secure, expires, expires is None, None, None, {}, False)


class Response(io.BytesIO):
    status = 200
    headers = {'Content-Type': 'text/html', 'Content-Encoding': 'identity'}


def html(owner='host', status=2):
    state = {'LiveRoom': {'liveRoomUserInfo': {'user': {'uniqueId': owner, 'roomId': '42', 'status': status}}}}
    return ('<script id="SIGI_STATE">' + json.dumps(state) + '</script>').encode()


class DirectSessionTests(unittest.TestCase):
    def test_host_only_cookie_never_leaks_to_child_host(self):
        from tiktok_live.wss.sessions import visitor_cookie_header
        jar = CookieJar()
        item = cookie(domain='tiktok.com')
        item.domain_specified = False
        jar.set_cookie(item)
        with self.assertRaises(ValueError):
            visitor_cookie_header(jar, destination='wss://webcast-ws.us.tiktok.com/webcast/im/')

    def test_cookie_selection_tracks_actual_region(self):
        from tiktok_live.wss.sessions import visitor_cookie_header
        jar = CookieJar()
        jar.set_cookie(cookie(value='us', domain='.us.tiktok.com'))
        jar.set_cookie(cookie(value='eu', domain='.eu.tiktok.com'))
        for region in ('us', 'eu'):
            self.assertEqual(visitor_cookie_header(jar,
                destination=f'wss://webcast-ws.{region}.tiktok.com/webcast/im/'), 'ttwid=' + region)

    def setUp(self):
        self.assertIsNotNone(importlib.util.find_spec('tiktok_live.wss.sessions'))
        from tiktok_live.wss import sessions, direct_signer
        self.s, self.signer = sessions, direct_signer

    def test_only_fresh_secure_visitor_cookie_is_eligible_for_wss(self):
        jar = CookieJar()
        jar.set_cookie(cookie())
        self.assertEqual(self.s.visitor_cookie_header(jar), 'ttwid=synthetic-visitor')
        for item in (cookie('sessionid', 'synthetic-login'), cookie(secure=False),
                     cookie(domain='.evil.test'), cookie(expires=1)):
            with self.subTest(name=item.name), self.assertRaises(ValueError):
                jar = CookieJar()
                jar.set_cookie(item)
                self.s.visitor_cookie_header(jar)

    def test_legacy_public_parser_still_binds_owner_without_being_a_transport(self):
        self.assertEqual(self.s.parse_public_room(html().decode(), 'host')['room_id'], '42')
        for body in (b'SlardarWAF', b'<html>no state</html>', html('other'), html(status=4)):
            with self.subTest(body=body[:20]), self.assertRaises(ValueError):
                self.s.parse_public_room(body.decode(), 'host')

    def test_account_preparation_makes_no_public_http_request(self):
        with patch.object(self.s, 'prepare_observer', side_effect=AssertionError('No guest mixing')):
            session = self.s.prepare_account('host', '42', 'sessionid=synthetic-account', 'UA', proxy_setting='direct')
        self.assertEqual((session.role, session.room_id, session.cookie_header),
                         ('account_heartbeat', '42', 'sessionid=synthetic-account'))
        for cookie_value, ua in (('ttwid=guest', 'UA'), ('sessionid=x\r\nX:bad', 'UA'), ('sessionid=x', 'UA\nX')):
            with self.assertRaises(ValueError):
                self.s.prepare_account('host', '42', cookie_value, ua, proxy_setting='direct')

    def test_destination_rejects_host_path_room_and_header_injection(self):
        good = self.signer.build_direct_url('42', user_agent='UA')
        self.s.validate_destination(good, '42')
        for bad in (good.replace('wss:', 'ws:'), good.replace('.eu.tiktok.com', '.eu.tiktok.com.evil.test'),
                    good.replace('webcast-ws.eu.tiktok.com', 'x@webcast-ws.eu.tiktok.com'),
                    good.replace('webcast-ws.eu.tiktok.com', 'webcast-ws.eu.tiktok.com:444'),
                    good + '&room_id=43', good.replace('room_id=42', 'room_id=43'),
                    good.replace('/webcast/im/', '/other/'), good + '&sessionid=secret'):
            with self.subTest(url=bad.split('?')[0]), self.assertRaises(ValueError):
                self.s.validate_destination(bad, '42')
