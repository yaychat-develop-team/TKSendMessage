"""Anonymous, bounded first-party route discovery in the visitor's HTTP session."""
from dataclasses import replace
from http.cookiejar import Cookie
import re
import time
from urllib.parse import urlsplit

from curl_cffi.curl import CURL_WRITEFUNC_ERROR

from ..config import browser_profile_from_user_agent, MS_TOKEN_URL
from ..public_room import extract_webcast_fetch_endpoint, _current_room_state, _public_waf_cookies
from ..signing import (browser_headers, build_ms_token_environment, sign_url, parse_cookie_string)
from ..algorithms.ms_token import generate_ms_seed_token, build_ms_token_report_json
from .initial_fetch import build_initial_fetch_request, decode_initial_fetch, build_initialized_ws_url
from .sessions import visitor_cookie_header, reject_login_cookie_jar


async def _read(http, method, url, scope, *, limit, **kwargs):
    data = bytearray()
    def receive(chunk):
        if len(data) + len(chunk) > limit:
            return CURL_WRITEFUNC_ERROR
        data.extend(chunk)
        return len(chunk)
    reject_login_cookie_jar(http.cookies.jar)
    response = await getattr(http, method)(url, timeout=scope.remaining(),
        allow_redirects=False, content_callback=receive, **kwargs)
    scope.remaining()
    reject_login_cookie_jar(http.cookies.jar)
    if response.status_code != 200 or getattr(response, 'history', None):
        raise ValueError('Anonymous route request rejected')
    return response, bytes(data)


async def initialize_observer_route(http, session, scope):
    profile = browser_profile_from_user_agent(session.user_agent)
    page_url = 'https://www.tiktok.com/@' + session.target_username + '/live'
    headers = browser_headers({'accept': 'text/html', 'accept-encoding': 'identity'},
        user_agent=session.user_agent, sec_ch_ua=profile.sec_ch_ua)
    def check_cancel():
        scope.remaining()
        return False

    for attempt in range(3):
        response, raw = await _read(http, 'get', page_url, scope, limit=4 * 1024 * 1024, headers=headers)
        if 'text/html' not in response.headers.get('content-type', '').lower():
            raise ValueError('Anonymous route page is not HTML')
        html = raw.decode('utf-8-sig')
        if not re.search(r'SlardarWAF|_wafchallengeid', html, re.I):
            break
        if attempt == 2:
            raise ValueError('Anonymous route WAF request budget exhausted')
        proof = _public_waf_cookies(html, timeout_seconds=scope.remaining(),
                                   should_cancel=check_cancel)
        scope.remaining()
        path = urlsplit(page_url).path.rpartition('/')[0] or '/'
        for name, value in proof.items():
            http.cookies.jar.set_cookie(Cookie(0, name, value, None, False,
                'www.tiktok.com', False, False, path, True, True,
                int(time.time()) + 10, False, None, None, {}, False))
    state = _current_room_state(html, session.target_username)
    if not state or str(state.get('roomId')) != session.room_id or state.get('isLive') is not True:
        raise ValueError('Anonymous route room mismatch or offline')
    endpoint = extract_webcast_fetch_endpoint(html)
    if not endpoint:
        raise ValueError('Anonymous route endpoint missing')

    seed = generate_ms_seed_token()
    body = build_ms_token_report_json(user_agent=session.user_agent,
        env_plaintext=build_ms_token_environment(user_agent=session.user_agent))
    report_url = sign_url(MS_TOKEN_URL + '?msToken=' + seed, seed, body, user_agent=session.user_agent)
    response, _ = await _read(http, 'post', report_url, scope, limit=65536, data=body,
        headers=browser_headers({'content-type': 'text/plain;charset=UTF-8',
            'cookie': 'msToken=' + seed, 'origin': 'https://www.tiktok.com',
            'referer': 'https://www.tiktok.com/'},
            user_agent=session.user_agent, sec_ch_ua=profile.sec_ch_ua))
    token = response.headers.get('x-ms-token')
    if not token:
        raise ValueError('Anonymous route token missing')
    fetch_cookie = visitor_cookie_header(http.cookies.jar, destination=endpoint)
    fetch_session = replace(session, cookie_header=fetch_cookie)
    # The current initial-fetch query does not transmit device_id; do not invent
    # a browser identity. This placeholder only satisfies the shared validator.
    url, headers = build_initial_fetch_request(fetch_session, device_id='1', ms_token=token, endpoint=endpoint)
    cookies = parse_cookie_string(fetch_cookie)
    cookies['msToken'] = token
    response, raw = await _read(http, 'get', url, scope, limit=2 * 1024 * 1024,
                                headers=headers, cookies=cookies)
    if response.headers.get('content-type', '').split(';')[0].lower() != 'application/x-protobuf':
        raise ValueError('Anonymous initial fetch rejected')
    initial = decode_initial_fetch(raw)
    # Validate the server's host/path and bind the same room before selecting cookies.
    build_initialized_ws_url(initial, room_id=session.room_id, user_agent=session.user_agent,
                             additional_params=(('live_id', '12'),))
    cookie = visitor_cookie_header(http.cookies.jar, destination=initial.push_server)
    return replace(session, cookie_header=cookie, initial_fetch=initial)
