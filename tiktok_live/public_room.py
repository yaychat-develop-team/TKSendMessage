"""Fresh anonymous curl_cffi live-page discovery; no account or browser state."""
import base64
import hashlib
from html import unescape
import json
import math
import re
import time
from contextlib import nullcontext
from html.parser import HTMLParser
from http.cookiejar import Cookie
from urllib.parse import urlsplit
from curl_cffi import requests
from curl_cffi.curl import CURL_WRITEFUNC_ERROR
from loguru import logger
from .room_state import room_state_flags
from .errors import SafeOperationError
from .request_fingerprint import request_flow, current_fingerprint
from .wss.sessions import PUBLIC_USER_AGENT, resolve_proxy
PUBLIC_PAGE_LIMIT = 4 * 1024 * 1024
_WEBCAST_HOST = re.compile(
    r'(?<![A-Za-z0-9.-])(webcast(?:\.[A-Za-z0-9-]+)*\.tiktok\.com)'
    r'(?![A-Za-z0-9.-])',
    re.IGNORECASE,
)


class RoomOwnerMismatch(ValueError):
    """Explicit owner evidence must never trigger another identity lookup."""


class RoomOffline(SafeOperationError, ValueError):
    """Offline evidence remains terminal even when no connectable ID exists."""
    def __init__(self):
        super().__init__('room_offline')


def reject_login_cookie_jar(jar):
    forbidden = {'sessionid', 'sessionid_ss', 'sid_tt', 'sid_guard', 'uid_tt',
                 'uid_tt_ss', 'multi_sids', 'sid_ucp_v1', 'ssid_ucp_v1'}
    if any(cookie.name.lower() in forbidden for cookie in jar):
        raise ValueError('Unexpected login cookie in anonymous public lookup')

def normalize_username(value):
    """Accept a single handle, never a URL or multiple targets."""
    if not isinstance(value, str):
        raise ValueError('Username must be text')
    value = value.strip()
    if value.startswith('@'):
        value = value[1:]
    if not re.fullmatch(r'[A-Za-z0-9._]{1,64}', value) or '..' in value:
        raise ValueError('Enter one TikTok username, without URL or separators')
    return value


def extract_webcast_fetch_endpoint(source):
    """Extract one TikTok-controlled webcast host from a live-page document."""
    if not isinstance(source, str) or len(source.encode('utf-8')) > PUBLIC_PAGE_LIMIT:
        raise ValueError('Invalid live-page endpoint source')
    decoded = unescape(source)
    decoded = (decoded.replace('\\u002F', '/').replace('\\u002f', '/')
               .replace('\\u003A', ':').replace('\\u003a', ':')
               .replace('\\/', '/'))
    hosts = {match.group(1).lower() for match in _WEBCAST_HOST.finditer(decoded)}
    if not hosts:
        return None
    if len(hosts) != 1:
        raise ValueError('Live-page webcast endpoint is ambiguous')
    return f'https://{hosts.pop()}/webcast/im/fetch/'


def _extract_live_page_app_context(source):
    if not isinstance(source, str) or len(source.encode('utf-8')) > PUBLIC_PAGE_LIMIT:
        raise ValueError('Invalid live-page context source')

    class UniversalDataParser(HTMLParser):
        def __init__(self):
            super().__init__()
            self.active, self.parts, self.count = False, [], 0
        def handle_starttag(self, tag, attrs):
            if tag == 'script' and dict(attrs).get('id') == '__UNIVERSAL_DATA_FOR_REHYDRATION__':
                self.active = True
                self.count += 1
        def handle_endtag(self, tag):
            if tag == 'script':
                self.active = False
        def handle_data(self, data):
            if self.active:
                self.parts.append(data)

    parser = UniversalDataParser()
    parser.feed(source)
    if parser.count == 0:
        return None
    if parser.count != 1:
        raise ValueError('Ambiguous canonical live-page context')
    try:
        context = json.loads(''.join(parser.parts))['__DEFAULT_SCOPE__']['webapp.app-context']
    except (ValueError, TypeError, KeyError, AttributeError, RecursionError):
        return None
    return context if isinstance(context, dict) else None


def extract_live_page_region(source):
    """Read the runtime region used by the page's own webcast requests."""
    context = _extract_live_page_app_context(source)
    if not context:
        return None
    region = str(context.get('region') or '').upper()
    if not re.fullmatch(r'[A-Z]{2}', region):
        return None
    return region


def extract_live_page_device_id(source):
    """Read the persistent web id used by the page's own webcast requests."""
    context = _extract_live_page_app_context(source)
    if not context:
        return None
    value = context.get('wid')
    if type(value) not in (str, int) or not re.fullmatch(r'[0-9]{10,20}', str(value)):
        return None
    if not 0 < int(value) < 2**64:
        return None
    return str(value)

def _public_waf_cookies(html, *, max_nonce=1000000, clock=time.monotonic,
                        timeout_seconds=3, should_cancel=None):
    """Port only the observed dd9808 SHA-256 proof; never execute remote JS.

    The public script hashes decoded v.a + decimal nonce until it equals v.c,
    preserves the signed challenge, adds base64(nonce) as d, and reloads with
    three 10-second document cookies. Unsupported templates fail closed.
    """
    if (not isinstance(html, str) or len(html.encode('utf-8')) > 65536
            or type(max_nonce) is not int or not 0 <= max_nonce <= 1000000):
        raise ValueError('Invalid public WAF proof input or budget')
    asset = ('https://sf16-website-login.neutral.ttwstatic.com/obj/'
             'tiktok_web_login_static/obj/waf-aiso/dd9808.js')

    class ChallengeParser(HTMLParser):
        def __init__(self):
            super().__init__()
            self.fields, self.scripts = {}, 0

        def handle_starttag(self, tag, attrs):
            values = dict(attrs)
            if tag == 'script' and values.get('src') == asset:
                self.scripts += 1
            key = values.get('id')
            if key in ('wci', 'cs', 'rci', 'rs', 'rs_id'):
                if key in self.fields:
                    raise ValueError('Duplicate public WAF field')
                self.fields[key] = values.get('class', '')

    parser = ChallengeParser()
    parser.feed(html)
    fields = parser.fields
    if (parser.scripts != 1 or not {'wci', 'cs', 'rci', 'rs'} <= fields.keys()
            or fields['wci'] != '_wafchallengeid' or fields['rci'] != 'waforiginalreid'):
        raise ValueError('Unsupported public WAF challenge template')

    def decode(value, limit):
        # Browser atob accepts omitted trailing padding (live cs uses this).
        if not isinstance(value, str) or not 1 <= len(value) <= limit:
            raise ValueError('Malformed public WAF challenge data')
        return base64.b64decode(value + '=' * (-len(value) % 4), validate=True)

    try:
        challenge = json.loads(decode(fields['cs'], 8192).decode('ascii'))
        values = challenge['v']
        prefix = decode(values['a'], 344)
        expected = decode(values['c'], 44)
        if (not isinstance(challenge, dict) or not isinstance(values, dict)
                or not 1 <= len(prefix) <= 256 or len(expected) != 32
                or not isinstance(challenge.get('s'), str) or not challenge['s']):
            raise ValueError('Malformed public WAF challenge data')
    except (ValueError, TypeError, KeyError, UnicodeError, RecursionError):
        raise ValueError('Malformed public WAF challenge data') from None
    for key in ('rs', 'rs_id'):
        value = fields.get(key, '')
        if len(value) > 4096 or any(ord(char) < 33 or ord(char) > 126 or char in ';,' for char in value):
            raise ValueError('Invalid public WAF cookie value')
    if not math.isfinite(timeout_seconds) or timeout_seconds <= 0:
        raise ValueError('Public WAF proof budget exhausted')
    deadline = clock() + min(3, timeout_seconds)
    for nonce in range(max_nonce + 1):
        if nonce % 256 == 0:
            if should_cancel is not None and should_cancel():
                raise TimeoutError('Public WAF proof cancelled')
            if clock() >= deadline:
                raise ValueError('Public WAF proof budget exhausted')
        answer = str(nonce).encode('ascii')
        if hashlib.sha256(prefix + answer).digest() == expected:
            challenge['d'] = base64.b64encode(answer).decode('ascii')
            encoded = base64.b64encode(json.dumps(challenge, ensure_ascii=True,
                                                  separators=(',', ':')).encode('ascii')).decode('ascii')
            if len(encoded) > 4096:
                raise ValueError('Public WAF proof cookie exceeds size limit')
            cookies = {'_wafchallengeid': encoded, 'waforiginalreid': fields['rs']}
            if fields.get('rs_id'):
                cookies['waforigin_id'] = fields['rs_id']
            return cookies
    raise ValueError('Public WAF proof budget exhausted')

def _read_public_html(url, session, *, timeout_seconds, headers=None):
    """One bounded public request using the caller's memory-only visitor jar."""
    raw = bytearray()
    too_large = False
    def receive(chunk):
        nonlocal too_large
        if len(raw) + len(chunk) > PUBLIC_PAGE_LIMIT:
            too_large = True
            return CURL_WRITEFUNC_ERROR
        raw.extend(chunk)
        return len(chunk)
    try:
        response = session.get(url, headers=headers or {
            'User-Agent': PUBLIC_USER_AGENT, 'Accept': 'text/html', 'Accept-Encoding': 'identity'},
            timeout=timeout_seconds, allow_redirects=False, content_callback=receive)
    except Exception:
        if too_large:
            raise ValueError('Public room response exceeds the size limit') from None
        raise
    if too_large:
        raise ValueError('Public room response exceeds the size limit')
    if response.status_code != 200:
        raise ValueError('Public room request did not return HTTP 200')
    if response.headers.get('Content-Encoding', 'identity').lower() not in ('identity', ''):
        raise ValueError('Unexpected compressed public response')
    if 'text/html' not in response.headers.get('Content-Type', '').lower():
        raise ValueError('Public room response is not HTML')
    try:
        text = raw.decode('utf-8-sig')
    except UnicodeError:
        raise ValueError('Unsupported public room response encoding') from None
    return text, response.status_code, len(raw)

def _current_room_state(html, username):
    """Only canonical SIGI_STATE/LiveRoom data is current room evidence."""
    class StateParser(HTMLParser):
        def __init__(self):
            super().__init__()
            self.active, self.parts, self.count = False, [], 0
        def handle_starttag(self, tag, attrs):
            if tag == 'script' and dict(attrs).get('id') == 'SIGI_STATE':
                self.active = True
                self.count += 1
        def handle_endtag(self, tag):
            if tag == 'script':
                self.active = False
        def handle_data(self, data):
            if self.active:
                self.parts.append(data)
    parser = StateParser()
    parser.feed(html)
    if parser.count != 1:
        raise ValueError('Missing or ambiguous canonical public room state')
    try:
        info = json.loads(''.join(parser.parts))['LiveRoom']['liveRoomUserInfo']
        user, live = info['user'], info.get('liveRoom') or {}
        owner = normalize_username(user['uniqueId'])
        if owner.casefold() != username.casefold():
            raise RoomOwnerMismatch('Current room owner does not match target')
        status, is_live = room_state_flags(user.get('status'), live.get('status'))
        room_id = user.get('roomId')
        if (type(room_id) not in (str, int) or not re.fullmatch(r'[0-9]{1,20}', str(room_id))
                or not 0 < int(room_id) < 2**64):
            if is_live is False:
                raise RoomOffline()
            raise ValueError()
        live_id = live.get('id')
        if live_id is not None and (type(live_id) not in (str, int)
                or not re.fullmatch(r'[0-9]{1,20}', str(live_id))
                or int(live_id) != int(room_id)):
            if is_live is False:
                raise RoomOffline()
            raise ValueError()
        status, is_live = room_state_flags(user.get('status'), live.get('status'))
        return dict(roomId=str(room_id), username=owner, nickname=user.get('nickname') or user.get('nickName') or '-',
                    liveStatus=status, isLive=is_live)
    except (RoomOwnerMismatch, RoomOffline):
        raise
    except (ValueError, TypeError, KeyError, AttributeError, RecursionError):
        raise ValueError('Invalid canonical current room or owner mismatch') from None

@request_flow()
def query_live_page(username, *, timeout_seconds=30, proxy_setting='system', should_cancel=None,
                    _session=None, _headers=None):
    """Bounded page/WAF flow; borrowed account jars are never closed or cleared."""
    username = normalize_username(username)
    timeout_seconds = float(timeout_seconds)
    if not math.isfinite(timeout_seconds) or timeout_seconds <= 0:
        raise ValueError('Invalid room lookup timeout')
    deadline = time.monotonic() + timeout_seconds

    def remaining():
        if should_cancel is not None and should_cancel():
            raise TimeoutError('Public room lookup cancelled')
        budget = deadline - time.monotonic()
        if budget <= 0:
            raise TimeoutError('Public room lookup timeout budget exhausted')
        return budget

    url = 'https://www.tiktok.com/@' + username + '/live'
    remaining()
    anonymous = _session is None
    mode = 'anonymous' if anonymous else 'account'
    logger.info('直播页查询当前房间 | username={} | session_mode={} | transport=curl_cffi | classification=room_page_lookup', username, mode)
    solved = False
    context = (requests.Session(impersonate=current_fingerprint(), proxy=resolve_proxy(proxy_setting), trust_env=False)
               if anonymous else nullcontext(_session))
    with context as session:
        try:
            if anonymous:
                reject_login_cookie_jar(session.cookies.jar)
            for attempt in range(1, 4):
                html, status, response_bytes = _read_public_html(url, session,
                    timeout_seconds=min(15, remaining()), headers=_headers)
                remaining()
                if anonymous:
                    reject_login_cookie_jar(session.cookies.jar)
                if re.search(r'SlardarWAF|_wafchallengeid', html, re.I):
                    if attempt == 3:
                        raise ValueError('Public WAF request budget exhausted')
                    try:
                        cookies = _public_waf_cookies(html, timeout_seconds=remaining(),
                                                     should_cancel=should_cancel)
                    except ValueError:
                        remaining()
                        raise
                    remaining()
                    path = urlsplit(url).path.rpartition('/')[0] or '/'
                    expires = int(time.time()) + 10
                    for name, value in cookies.items():
                        session.cookies.jar.set_cookie(Cookie(0, name, value, None, False,
                            'www.tiktok.com', False, False, path, True, True, expires,
                            False, None, None, {}, False))
                    solved = True
                    logger.info('直播页 WAF 验证计算完成，同会话重新请求 | username={} | attempt={}/3 '
                                '| session_mode={} | classification=public_waf_ready', username, attempt, mode)
                    continue
                state = _current_room_state(html, username)
                if state is None or not state.get('roomId'):
                    raise ValueError('Missing public room state for the requested host')
                remaining()
                room = dict(state, exists=True, accountUsername=None, accountNickname=None,
                    deviceId=None, url=url, status=status,
                    querySource='live_page_waf' if solved else 'live_page')
                endpoint = extract_webcast_fetch_endpoint(html)
                if endpoint:
                    room['fetchEndpoint'] = endpoint
                region = extract_live_page_region(html)
                if region:
                    room['region'] = region
                device_id = extract_live_page_device_id(html)
                if device_id:
                    room['deviceId'] = device_id
                if not anonymous:
                    room['querySource'] = 'account_' + room['querySource']
                logger.info('直播页房间查询完成 | username={} | room_id={} | live_status={} '
                            '| session_mode={} | classification=room_page_ready', username, room['roomId'], room['liveStatus'], mode)
                return room
        finally:
            if anonymous:
                session.cookies.jar.clear()

