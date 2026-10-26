"""Isolated curl_cffi visitor bootstrap and explicit account handshakes.
Pure validation helpers adapted from hash-checked standalone_live_comments.py.
"""
import json
import re
from html.parser import HTMLParser
from http.cookiejar import CookieJar, DefaultCookiePolicy
from urllib.request import Request, getproxies
from urllib.parse import parse_qs, urlsplit
from .direct_models import DirectSession
from .direct_signer import WS_PATH
from .startup import StartupScope

PUBLIC_PAGE_LIMIT = 4 * 1024 * 1024
PUBLIC_USER_AGENT = ('Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 '
                     '(KHTML, like Gecko) Chrome/153.0.0.0 Safari/537.36')

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

def parse_public_room(html, username):
    """Read the known SIGI_STATE live-room schema, binding it to its owner.

    Unsupported hydration schemas fail rather than picking an unrelated roomId
    from advertisements, recommendations, or historical room records.
    """
    username = normalize_username(username)
    if not isinstance(html, str) or len(html.encode('utf-8')) > PUBLIC_PAGE_LIMIT:
        raise ValueError('Public page exceeds the size limit')
    if re.search(r'SlardarWAF|_wafchallengeid', html, re.I):
        raise ValueError('Public page returned a WAF challenge')

    class StateParser(HTMLParser):
        def __init__(self):
            super().__init__()
            self.active = False
            self.parts = []
            self.count = 0

        def handle_starttag(self, tag, attrs):
            if tag == 'script' and dict(attrs).get('id') == 'SIGI_STATE':
                self.count += 1
                self.active = True

        def handle_endtag(self, tag):
            if tag == 'script':
                self.active = False

        def handle_data(self, data):
            if self.active:
                self.parts.append(data)

    parser = StateParser()
    parser.feed(html)
    if parser.count != 1:
        raise ValueError('Expected one public live-room state; schema unsupported or blocked')
    try:
        state = json.loads(''.join(parser.parts))
        user = state['LiveRoom']['liveRoomUserInfo']['user']
        owner = normalize_username(user['uniqueId'])
        room_id = user['roomId']
        status = user['status']
    except (KeyError, TypeError, ValueError, RecursionError):
        raise ValueError('Malformed public live-room state') from None
    if owner.casefold() != username.casefold():
        raise ValueError('Public room belongs to a different username')
    if type(status) is not int:
        raise ValueError('Missing or malformed room status')
    if status == 4:
        raise ValueError('The requested user is offline')
    if not isinstance(room_id, str) or not re.fullmatch(r'[0-9]{1,20}', room_id) or not 0 < int(room_id) < 2**64:
        raise ValueError('Missing or invalid current room ID')
    return {'username': owner, 'room_id': room_id, 'status': status}

def resolve_proxy(value):
    """Explicit choice: use configured OS proxy, an HTTP proxy, or direct."""
    if value == 'system':
        proxies = getproxies()
        value = proxies.get('https') or proxies.get('http') or 'direct'
    if value in (None, '', 'direct'):
        return None
    if not isinstance(value, str) or any(ord(char) <= 32 for char in value):
        raise ValueError('Invalid HTTP proxy setting')
    if '://' not in value:
        value = 'http://' + value
    try:
        parsed = urlsplit(value)
        port = parsed.port or 80
        if (parsed.scheme != 'http' or not parsed.hostname or parsed.username is not None
                or parsed.password is not None or parsed.path not in ('', '/')
                or parsed.query or parsed.fragment or not 1 <= port <= 65535):
            raise ValueError('Only credential-free HTTP CONNECT proxies are supported')
    except ValueError:
        raise ValueError('Invalid HTTP proxy; use http://host:port or direct') from None
    host = parsed.hostname
    if ':' in host:
        host = '[' + host + ']'
    return f'http://{host}:{port}'

_LOGIN_COOKIE_NAMES = frozenset({'sessionid', 'sessionid_ss', 'sid_tt', 'sid_guard', 'uid_tt',
    'uid_tt_ss', 'multi_sids', 'sid_ucp_v1', 'ssid_ucp_v1'})

def reject_login_cookie_jar(jar):
    if any(cookie.name.lower() in _LOGIN_COOKIE_NAMES for cookie in jar):
        raise ValueError('Unexpected login cookie in anonymous session')

def visitor_cookie_header(jar, *, destination='https://www.tiktok.com/'):
    """Only fresh, secure, domain/path-scoped anonymous visitor cookies."""
    allowed = {'ttwid', 'tt_chain_token', 'tt_csrf_token'}
    reject_login_cookie_jar(jar)
    target = urlsplit(destination)
    host = target.hostname or ''
    if (target.scheme not in ('https', 'wss') or target.port not in (None, 443)
            or target.username or target.password
            or not (host == 'tiktok.com' or host.endswith('.tiktok.com'))):
        raise ValueError('Invalid visitor Cookie destination')
    selected = CookieJar(policy=DefaultCookiePolicy(
        strict_ns_domain=DefaultCookiePolicy.DomainStrictNonDomain))
    for cookie in jar:
        if (cookie.name in allowed and cookie.secure
                and (cookie.domain.lstrip('.').lower() == 'tiktok.com'
                     or cookie.domain.lstrip('.').lower().endswith('.tiktok.com'))):
            selected.set_cookie(cookie)
    request = Request('https://' + target.netloc + (target.path or '/'))
    selected.add_cookie_header(request)
    header = request.get_header('Cookie')
    if not header:
        raise ValueError('The visitor API did not issue usable anonymous cookies')
    return header

def validate_header(value, *, limit=65536):
    if not isinstance(value, str) or not value or len(value) > limit:
        raise ValueError('Invalid handshake header')
    if any(ord(c) < 32 or ord(c) >= 127 for c in value):
        raise ValueError('Invalid handshake header')
    return value

def validate_room_id(value):
    value = str(value)
    if not re.fullmatch(r'[0-9]{1,20}', value) or not 0 < int(value) < 2**64:
        raise ValueError('Invalid room ID')
    return value

def validate_destination(url, expected_room_id):
    expected = validate_room_id(expected_room_id)
    if not isinstance(url, str) or len(url) > 65536 or any(ord(c) <= 32 or ord(c) >= 127 for c in url):
        raise ValueError('Invalid WSS destination')
    try:
        target = urlsplit(url)
        host = target.hostname or ''
        allowed_host = host == 'webcast-ws.tiktok.com' or (host.startswith('webcast-ws.') and host.endswith('.tiktok.com'))
        if (target.scheme != 'wss' or not allowed_host or target.port not in (None, 443)
                or target.username is not None or target.password is not None
                or target.path != WS_PATH or target.fragment):
            raise ValueError('Invalid WSS destination')
        query = parse_qs(target.query, keep_blank_values=True, max_num_fields=200)
        if not query.get('room_id') or any(room != expected for room in query['room_id']):
            raise ValueError('WSS room mismatch')
        if query.get('live_id') != ['12']:
            raise ValueError('WSS live ID mismatch')
        forbidden = {'cookie', 'authorization', 'sessionid', 'sessionid_ss', 'sid_tt', 'uid_tt', 'uid_tt_ss'}
        if any(name.lower() in forbidden for name in query):
            raise ValueError('Authentication fields are not URL parameters')
    except ValueError:
        raise ValueError('WSS destination not authorized') from None

def prepare_observer(username, *, known_room_id=None, proxy_setting='system', startup_scope=None,
                     observer_proxy=''):
    if startup_scope is None:
        with StartupScope(15) as scope:
            return prepare_observer(username, known_room_id=known_room_id,
                                    proxy_setting=proxy_setting, startup_scope=scope,
                                    observer_proxy=observer_proxy)
    username = normalize_username(username)
    if known_room_id is not None:
        known_room_id = validate_room_id(known_room_id)
    startup_scope.remaining()
    from .visitor_api import bootstrap_visitor
    from .proxy import normalize_observer_proxy
    proxy = normalize_observer_proxy(observer_proxy) or resolve_proxy(proxy_setting)
    return bootstrap_visitor(username, known_room_id, proxy, startup_scope)

def prepare_account(username, room_id, cookie_header, user_agent, *,
                    proxy_setting='system', initial_fetch=None):
    username = normalize_username(username)
    room_id = validate_room_id(room_id)
    header = validate_header(cookie_header)
    user_agent = validate_header(user_agent, limit=2048)
    pairs = [part.strip().partition('=') for part in header.split(';') if part.strip()]
    if any(not sep or not re.fullmatch(r'[!#$%&\\\'*+.^_\x60|~0-9A-Za-z-]+', name) for name, sep, value in pairs):
        raise ValueError('Invalid account Cookie')
    if not any(name in {'sessionid', 'sessionid_ss', 'sid_tt'} and value for name, sep, value in pairs):
        raise ValueError('Account session Cookie required')
    return DirectSession('account_heartbeat', username, room_id, user_agent, header,
                         resolve_proxy(proxy_setting), initial_fetch=initial_fetch)
