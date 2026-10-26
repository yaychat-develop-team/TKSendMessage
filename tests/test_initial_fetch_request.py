"""Offline contract for the first-party fetch request; all identities are synthetic."""
import unittest
from types import SimpleNamespace
from urllib.parse import parse_qs, parse_qsl, urlsplit
from unittest.mock import patch

from tiktok_live.wss.direct_models import DirectSession
from tiktok_live.wss.initial_fetch import (
    FETCH_USER_AGENT, build_initial_fetch_request, fetch_initial_response,
)


class InitialFetchRequestTests(unittest.TestCase):
    def test_each_fetch_selects_one_common_screen_pair_before_signing_for_both_roles(self):
        profiles = ((1920,1200),(2560,1440),(1366,768),(1536,864),(1280,720),(3840,2160))
        ua = FETCH_USER_AGENT.replace('Chrome/150.','Chrome/145.')
        for role,cookie in (('account_heartbeat','sessionid=synthetic'),('delivery_observer','ttwid=synthetic')):
            with self.subTest(role=role):
                pending = iter(profiles)
                def choose(options):
                    self.assertEqual(set(profiles),set(options))
                    return next(pending)
                session = DirectSession(role,'host','42',ua,cookie)
                with patch('secrets.choice',side_effect=choose):
                    for width,height in profiles:
                        url,headers = build_initial_fetch_request(session,device_id='1',ms_token='synthetic-token')
                        query = parse_qs(urlsplit(url).query)
                        self.assertEqual([str(width)],query['screen_width'])
                        self.assertEqual([str(height)],query['screen_height'])
                        self.assertEqual(['270000'],query['version_code'])
                        self.assertEqual(['12'],query['live_id'])
                        self.assertEqual([ua.removeprefix('Mozilla/')],query['browser_version'])
                        self.assertEqual(ua,headers['user-agent'])

    def test_failed_fetch_logs_actual_business_url_without_credentials(self):
        from loguru import logger
        from desktop.log_bridge import sanitize_log_text
        captured, messages = {}, []
        class Http:
            def get(self, url, **kwargs):
                captured['url'] = url
                return SimpleNamespace(status_code=403,headers={'content-type':'application/json'},history=[])
        session = DirectSession('account_heartbeat','host','42',FETCH_USER_AGENT,
                                'sessionid=synthetic-private-login')
        sink = logger.add(lambda message:messages.append(message.record['message']))
        try:
            with self.assertRaises(ValueError):
                fetch_initial_response(Http(),session,device_id='1',ms_token='synthetic-private-token',
                    endpoint='https://webcast.us.tiktok.com/webcast/im/fetch/')
        finally:
            logger.remove(sink)
        requests = [m for m in messages if 'classification=initial_fetch_request' in m]
        self.assertEqual(1,len(requests))
        message = requests[0]
        visible_url = message.split('url=',1)[1].split(' |',1)[0]
        self.assertTrue(captured['url'].startswith(visible_url+'?'))
        self.assertEqual('webcast.us.tiktok.com',urlsplit(visible_url).hostname)
        self.assertEqual('https://webcast.us.tiktok.com/webcast/im/fetch/', visible_url)
        self.assertEqual('', urlsplit(visible_url).query)
        self.assertEqual(['270000'],parse_qs(urlsplit(captured['url']).query)['version_code'])
        self.assertEqual(['12'],parse_qs(urlsplit(captured['url']).query)['live_id'])
        self.assertEqual(['42'],parse_qs(urlsplit(captured['url']).query)['room_id'])
        self.assertNotIn('synthetic-private',message)
        self.assertNotIn('msToken=',message)
        self.assertNotIn('X-Gnarly=',message)
        self.assertEqual(message,sanitize_log_text(message))

    def test_observer_refuses_login_credentials(self):
        session = DirectSession('delivery_observer', 'host', '42', FETCH_USER_AGENT,
                                'ttwid=synthetic; sessionid=synthetic-login')
        with self.assertRaises(ValueError):
            build_initial_fetch_request(session, device_id='1', ms_token='synthetic-token')

    def test_request_uses_session_user_agent_for_header_signature_and_browser_version(self):
        account_user_agent = (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/145.0.0.0 Safari/537.36"
        )
        session = DirectSession(
            'account_heartbeat', 'host', '7699044408762616590',
            account_user_agent, 'sessionid=synthetic',
        )

        url, headers = build_initial_fetch_request(
            session,
            device_id='7770000000000000001',
            ms_token='synthetic-ms-token',
        )
        params = parse_qs(urlsplit(url).query)

        self.assertEqual(account_user_agent, headers['user-agent'])
        self.assertEqual(
            account_user_agent.removeprefix('Mozilla/'),
            params['browser_version'][0],
        )
        self.assertIn('v="145"', headers['sec-ch-ua'])

    def test_request_targets_same_room_and_session_with_web_initial_flags(self):
        session = DirectSession('account_heartbeat', 'host', '7699044408762616590',
                                FETCH_USER_AGENT, 'sessionid=synthetic')
        url, headers = build_initial_fetch_request(session,
            device_id='7770000000000000001', ms_token='synthetic-ms-token',
            endpoint='https://webcast.us.tiktok.com/webcast/im/fetch/')
        parts = urlsplit(url)
        params = parse_qs(parts.query)
        self.assertEqual('https', parts.scheme)
        self.assertEqual('webcast.us.tiktok.com', parts.hostname)
        self.assertEqual('/webcast/im/fetch/', parts.path)
        self.assertEqual(['7699044408762616590'], params['room_id'])
        self.assertEqual(['1'], params['client_enter'])
        self.assertEqual(['1'], params['ws_direct'])
        self.assertEqual(['0'], params['cursor'])
        self.assertEqual(['protobuf'], params['resp_content_type'])
        self.assertEqual(['270000'], params['version_code'])
        self.assertEqual(['12'], params['live_id'])
        # The fresh web fetch has no device_id query key; only room/enter does.
        self.assertNotIn('device_id', params)
        self.assertEqual([
            'version_code', 'device_platform', 'cookie_enabled',
            'screen_width', 'screen_height', 'browser_language',
            'browser_platform', 'browser_name', 'browser_version',
            'browser_online', 'tz_name', 'ws_direct', 'aid', 'app_name',
            'live_id', 'app_language', 'client_enter',
            'room_id', 'identity', 'history_comment_count', 'fetch_rule',
            'last_rtt', 'cursor', 'internal_ext', 'sup_ws_ds_opt',
            'resp_content_type', 'did_rule', 'webcast_language',
            'X-Dynosaur', 'msToken', 'X-Bogus', 'X-Gnarly',
        ], [key for key, _ in parse_qsl(parts.query, keep_blank_values=True)])
        self.assertIn('X-Dynosaur', params)
        self.assertIn('X-Gnarly', params)
        self.assertNotIn('cookie', headers)
        self.assertEqual(FETCH_USER_AGENT, headers['user-agent'])
        self.assertEqual(FETCH_USER_AGENT.removeprefix('Mozilla/'),
                         params['browser_version'][0])

    def test_request_fails_closed_without_token_or_allowed_endpoint(self):
        session = DirectSession('account_heartbeat', 'host', '42',
                                FETCH_USER_AGENT, 'sessionid=synthetic')
        for endpoint, token in (
            ('https://webcast.us.tiktok.com/webcast/im/fetch/', ''),
            ('https://evil.example/webcast/im/fetch/', 'synthetic-ms-token'),
            ('http://webcast.us.tiktok.com/webcast/im/fetch/', 'synthetic-ms-token'),
        ):
            with self.subTest(endpoint=endpoint, token_present=bool(token)):
                with self.assertRaises(ValueError):
                    build_initial_fetch_request(session, device_id='7770000000000000001',
                        ms_token=token, endpoint=endpoint)

    def test_request_accepts_page_declared_tiktok_webcast_region(self):
        session = DirectSession('account_heartbeat', 'host', '42',
                                FETCH_USER_AGENT, 'sessionid=synthetic')
        url, _ = build_initial_fetch_request(
            session,
            device_id='7770000000000000001',
            ms_token='synthetic-ms-token',
            endpoint='https://webcast.eu.tiktok.com/webcast/im/fetch/',
        )
        self.assertEqual('webcast.eu.tiktok.com', urlsplit(url).hostname)

    def test_fetch_uses_cookie_jar_not_explicit_cookie_header(self):
        def varint(value):
            out = bytearray()
            while value > 127:
                out.append((value & 127) | 128)
                value >>= 7
            out.append(value)
            return bytes(out)

        def field(number, value):
            value = value.encode() if isinstance(value, str) else value
            return varint(number << 3 | 2) + varint(len(value)) + value

        raw = (field(2, 'cursor') +
               field(7, field(1, 'wrss') + field(2, 'routing')) +
               field(10, 'wss://webcast-ws.tiktok.com/webcast/im/ws_proxy/ws_reuse_supplement/'))
        captured = {}

        class Http:
            def get(self, url, **kwargs):
                captured.update(url=url, **kwargs)
                for offset in range(0, len(raw), 7):
                    kwargs['content_callback'](raw[offset:offset + 7])
                return SimpleNamespace(status_code=200,
                    headers={'content-type': 'application/x-protobuf'}, history=[])

        session = DirectSession('account_heartbeat', 'host', '42', FETCH_USER_AGENT,
                                'sessionid=synthetic; msToken=old-cookie-token')
        initial = fetch_initial_response(Http(), session,
            device_id='7770000000000000001', ms_token='synthetic-query-token',
            endpoint='https://webcast.tiktok.com/webcast/im/fetch/', timeout_seconds=3)
        self.assertEqual('cursor', initial.cursor)
        self.assertNotIn('cookie', {k.lower(): v for k, v in captured['headers'].items()})
        self.assertEqual('synthetic-query-token', captured['cookies']['msToken'])
        self.assertEqual('synthetic', captured['cookies']['sessionid'])


if __name__ == '__main__':
    unittest.main()
