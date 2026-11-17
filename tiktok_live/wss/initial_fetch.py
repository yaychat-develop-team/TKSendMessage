"""Bounded decoding of TikTok's first-party initial webcast response."""
from dataclasses import dataclass, field
import re
import secrets
from urllib.parse import quote, urlencode, urlsplit
from loguru import logger

from ..config import browser_profile_from_user_agent
from .protocol import _fields, _text
from .sessions import validate_destination, validate_room_id
from ..signing import (
    browser_headers, cookie_with_ms_token_fallback, parse_cookie_string,
)
from ..reference_signing import sign_url


FETCH_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/150.0.0.0 Safari/537.36"
)
_FETCH_HOST = re.compile(r'webcast(?:\.[a-z0-9-]+)*\.tiktok\.com', re.IGNORECASE)
FETCH_SCREEN_RESOLUTIONS = (
    (1920, 1200), (2560, 1440), (1366, 768), (1536, 864),
    (1280, 720), (3840, 2160),
)


def _initial_fetch_params(
    room_id: str, user_agent: str,
    screen_size: tuple[int, int] | None = None,
) -> dict[str, object]:
    room_id = validate_room_id(room_id)
    if not isinstance(user_agent, str) or not user_agent.startswith("Mozilla/") or len(user_agent) > 2048:
        raise ValueError("Invalid fetch user agent")
    if screen_size is None:
        screen_width, screen_height = secrets.choice(FETCH_SCREEN_RESOLUTIONS)
    else:
        if (type(screen_size) is not tuple or len(screen_size) != 2
                or any(type(value) is not int for value in screen_size)
                or not all(100 <= value <= 10000 for value in screen_size)):
            raise ValueError("Invalid fetch screen size")
        screen_width, screen_height = screen_size
    return {
        # Initial HTTP fetch uses the webcast version once. Adding the legacy
        # 180800 value can reject otherwise identical room/account requests.
        "version_code": "270000",
        "device_platform": "web",
        "cookie_enabled": "true",
        "screen_width": str(screen_width),
        "screen_height": str(screen_height),
        "browser_language": "zh-CN",
        "browser_platform": "Win32",
        "browser_name": "Mozilla",
        "browser_version": user_agent.removeprefix("Mozilla/"),
        "browser_online": "true",
        "tz_name": "Asia/Shanghai",
        "ws_direct": "1",
        "aid": "1988",
        "app_name": "tiktok_web",
        "live_id": "12",
        "app_language": "zh-Hans",
        "client_enter": "1",
        "room_id": room_id,
        "identity": "audience",
        "history_comment_count": "6",
        "fetch_rule": "1",
        "last_rtt": "-1",
        "cursor": "0",
        "internal_ext": "0",
        "sup_ws_ds_opt": "1",
        "resp_content_type": "protobuf",
        "did_rule": "3",
        "webcast_language": "zh-Hans",
    }


def build_initial_fetch_url(room_id: str, user_agent: str) -> str:
    """Build the unsigned first-fetch URL with the example's ordered query shape."""
    return ("https://webcast.tiktok.com/webcast/im/fetch/?" +
            urlencode(_initial_fetch_params(room_id, user_agent), doseq=True))


def build_initial_fetch_request(
    session, *, device_id: str, ms_token: str,
    endpoint: str = "https://webcast.tiktok.com/webcast/im/fetch/",
    signer=None, account_id: str | None = None,
    screen_size: tuple[int, int] | None = None,
) -> tuple[str, dict[str, str]]:
    """Build one freshly signed fetch request without flattening the Cookie jar."""
    target = urlsplit(endpoint)
    if (target.scheme != "https" or not target.hostname or not _FETCH_HOST.fullmatch(target.hostname)
            or target.port not in (None, 443) or target.username or target.password
            or target.path != "/webcast/im/fetch/" or target.query or target.fragment):
        raise ValueError("Invalid initial fetch endpoint")
    if not isinstance(device_id, str) or not device_id.isdigit() or not ms_token:
        raise ValueError("Invalid initial fetch identity")
    if session.role not in ("account_heartbeat", "delivery_observer"):
        raise ValueError("Initial fetch browser profile mismatch")
    if session.role == 'delivery_observer':
        allowed = {'ttwid', 'tt_chain_token', 'tt_csrf_token'}
        if set(parse_cookie_string(session.cookie_header)) - allowed:
            raise ValueError('Unexpected observer credentials')
    profile = browser_profile_from_user_agent(session.user_agent)
    unsigned = endpoint + "?" + urlencode(
        _initial_fetch_params(
            session.room_id, session.user_agent, screen_size,
        ), doseq=True,
    )
    if signer is None:
        signed = sign_url(unsigned, ms_token, user_agent=session.user_agent)
    else:
        if not account_id:
            raise ValueError("Invalid signing account")
        signed = signer.sign(
            unsigned, account_id=account_id, room_id=session.room_id,
            query_token=ms_token, operation="initial_fetch",
        )
    headers = browser_headers({
        "accept": "*/*",
        "origin": "https://www.tiktok.com",
        "referer": f"https://www.tiktok.com/@{session.target_username}/live",
        "sec-fetch-dest": "empty",
        "sec-fetch-mode": "cors",
        "sec-fetch-site": "same-site",
    }, user_agent=session.user_agent, sec_ch_ua=profile.sec_ch_ua)
    # Only the validated endpoint is visible. The actual signed request is unchanged.
    logger.info('fetch 请求地址（查询参数已省略） | url={} | role={} '
                '| classification=initial_fetch_request', endpoint, session.role)
    return signed, headers


def fetch_initial_response(
    http_session, session, *, device_id: str, ms_token: str,
    endpoint: str = "https://webcast.tiktok.com/webcast/im/fetch/",
    timeout_seconds: float = 20,
    signer=None, account_id: str | None = None,
    screen_size: tuple[int, int] | None = None,
) -> "InitialFetch":
    """Fetch and decode a bounded first-party protobuf response."""
    if not 0 < float(timeout_seconds) <= 60:
        raise ValueError("Invalid initial fetch timeout")
    url, headers = build_initial_fetch_request(
        session, device_id=device_id, ms_token=ms_token, endpoint=endpoint,
        signer=signer, account_id=account_id, screen_size=screen_size)
    # The browser can send more than one scoped msToken while the signed query
    # uses the freshly issued report token.  A dict cookie jar would collapse
    # duplicate names and incorrectly force both channels to the same value.
    headers = dict(headers)
    headers["cookie"] = cookie_with_ms_token_fallback(
        session.cookie_header, ms_token,
    )
    raw = bytearray()

    def receive(chunk):
        if len(raw) + len(chunk) > 2 * 1024 * 1024:
            return 0
        raw.extend(chunk)
        return len(chunk)

    response = http_session.get(
        url, headers=headers, timeout=timeout_seconds,
        allow_redirects=False, content_callback=receive,
    )
    content_type = str(response.headers.get("content-type", "")).split(";", 1)[0].lower()
    if (response.status_code != 200 or content_type != "application/x-protobuf"
            or getattr(response, "history", None) or not raw):
        raise ValueError("Initial fetch rejected")
    return decode_initial_fetch(bytes(raw))


@dataclass(frozen=True)
class InitialFetch:
    cursor: str = field(repr=False)
    push_server: str = field(repr=False)
    route_params: tuple[tuple[str, str], ...] = field(repr=False)
    internal_ext: str = field(repr=False)
    historical_message_count: int = 0


def decode_initial_fetch(raw: bytes, *, max_bytes: int = 2 * 1024 * 1024) -> InitialFetch:
    if not isinstance(raw, bytes) or not 0 < len(raw) <= max_bytes <= 2 * 1024 * 1024:
        raise ValueError('Invalid initial fetch size/type')
    tree = _fields(raw)
    cursor = _text(tree, 2)
    push_server = _text(tree, 10)
    routes = []
    for encoded in tree.get(7, []):
        if not isinstance(encoded, bytes):
            raise ValueError('Invalid initial route')
        item = _fields(encoded)
        key, value = _text(item, 1), _text(item, 2)
        if not key or len(key) > 64 or len(value) > 8192:
            raise ValueError('Invalid initial route')
        routes.append((key, value))
    if not cursor or not push_server or not routes:
        raise ValueError('Missing initial webcast route')
    if len(cursor) > 8192 or len(push_server) > 4096 or len(routes) > 64:
        raise ValueError('Initial webcast route exceeds limit')
    return InitialFetch(cursor, push_server, tuple(routes), _text(tree, 5),
                        len(tree.get(1, [])))


def build_initialized_ws_url(
    initial: InitialFetch, *, room_id: str, user_agent: str,
    additional_params: tuple[tuple[str, str], ...],
) -> str:
    room_id = validate_room_id(room_id)
    target = urlsplit(initial.push_server)
    if target.query or target.fragment:
        raise ValueError('Unexpected WSS server query')
    if not isinstance(user_agent, str) or not user_agent or len(user_agent) > 2048:
        raise ValueError('Invalid WSS user agent')
    pairs = list(initial.route_params) + list(additional_params)
    pairs.extend((('browser_version', user_agent.split('Mozilla/', 1)[-1]),
                  ('room_id', room_id)))
    if len(pairs) > 200 or any(not isinstance(k, str) or not isinstance(v, str)
                              for k, v in pairs):
        raise ValueError('Invalid WSS parameters')
    query = '&'.join(f'{quote(k, safe="")}={quote(v, safe="")}' for k, v in pairs)
    url = initial.push_server + '?' + query
    validate_destination(url, room_id)
    return url
