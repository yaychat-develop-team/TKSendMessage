"""TikTok live-room discovery and message request client."""

import json
import math
from http.cookiejar import Cookie
import hashlib
import re
import secrets
import time
from typing import Any, Callable
from urllib.parse import parse_qsl, quote, unquote, urlencode, urlsplit

from curl_cffi import requests
from curl_cffi.requests.exceptions import (
    ConnectionError as CurlConnectionError, Timeout as CurlTimeout, CertificateVerifyError, ProxyError,
)
from loguru import logger

from .account_store import (
    _cookie_records_from_header,
    account_id_from_cookies,
    environment_seed_for_account_id,
)
from .algorithms.reference_ms_token import build_ms_token_report_json, generate_ms_seed_token
from .algorithms.ticket_guard import build_headers_from_cookie_state
from .errors import SafeOperationError
from .room_state import extract_bound_room_state, room_state_flags
from .heartbeat import room_heartbeat
from .wss.initial_fetch import fetch_initial_response
from .wss.sessions import prepare_account
from .retry_policy import RetryableMessageError, transient_failure
from .request_fingerprint import request_flow
from .config import (
    CHAT_URL, ENTER_URL, CHROME_BROWSER_PROFILES, DEFAULT_BROWSER_PROFILE, MS_TOKEN_URL,
    ROOM_ID_COOKIE, BrowserProfile, browser_profile_from_user_agent,
)
from .signing import (
    browser_headers,
    build_ms_token_environment,
    cookie_with_ms_token_fallback,
    cookie_with_ms_token,
    first_saved_ms_token,
    parse_cookie_string,
)
from .reference_signing import sign_url
from .signing_session import SigningSession, create_sdk_state

def choose_browser_profile() -> BrowserProfile:
    return secrets.choice(CHROME_BROWSER_PROFILES)


def _request_profile(profile: BrowserProfile | None) -> BrowserProfile:
    return profile or DEFAULT_BROWSER_PROFILE


_LOGIN_REQUIRED_PATTERN = re.compile(
    r"请.{0,8}登录|登录.{0,8}(?:后|失效|过期)|login\s+required|please\s+log\s*in|not\s+logged\s+in",
    re.IGNORECASE,
)
_SENSITIVE_RESPONSE_KEY = re.compile(
    r"cookie|session|token|csrf|signature|ticket|x[-_]?bogus|x[-_]?gnarly|x[-_]?dynosaur",
    re.IGNORECASE,
)
_SENSITIVE_RESPONSE_TEXT = re.compile(
    r"(?i)(cookie|session(?:id)?|msToken|csrf|tt_csrf_token|x-gnarly|x-bogus|"
    r"x-dynosaur|ticket-guard)\s*[:=]\s*[^\s,;]+"
)

# Only retry idempotent preparation requests. The chat POST is deliberately
# excluded because a lost response does not prove that TikTok rejected it.
_PRE_SEND_RETRY_DELAYS_SECONDS = (2.0, 2.0, 2.0)


class _RoomEnterForbidden(SafeOperationError):
    def __init__(self) -> None:
        super().__init__("room_enter_unavailable")


def _run_pre_send_with_retry(
    stage: str, operation: Callable[[], Any], *,
    checkpoint: Callable[[], Any] | None = None,
    restore: Callable[[Any], None] | None = None,
) -> Any:
    """Run a safe pre-send operation and retry up to three times."""
    if (checkpoint is None) != (restore is None):
        raise ValueError("checkpoint and restore must be supplied together")
    attempts = (*_PRE_SEND_RETRY_DELAYS_SECONDS, None)
    for retry_index, delay_seconds in enumerate(attempts, start=1):
        saved_state = checkpoint() if checkpoint is not None else None
        try:
            return operation()
        except _RoomEnterForbidden:
            raise
        except Exception:
            if restore is not None:
                restore(saved_state)
            if delay_seconds is None:
                raise
            logger.warning(
                "发送前请求失败，准备重试 | stage={} | retry={}/{} | delay_seconds={}",
                stage,
                retry_index,
                len(_PRE_SEND_RETRY_DELAYS_SECONDS),
                delay_seconds,
            )
            time.sleep(delay_seconds)
    raise AssertionError("unreachable")


def _safe_response_for_log(value: Any) -> Any:
    """Keep business response fields visible while removing credential material."""
    if isinstance(value, dict):
        return {
            str(key): ("<redacted>" if _SENSITIVE_RESPONSE_KEY.search(str(key))
                       else _safe_response_for_log(item))
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [_safe_response_for_log(item) for item in value]
    if isinstance(value, tuple):
        return [_safe_response_for_log(item) for item in value]
    if isinstance(value, str):
        return _SENSITIVE_RESPONSE_TEXT.sub(lambda match: f"{match.group(1)}=<redacted>", value)
    return value


def log_chat_response(payload: dict[str, Any]) -> None:
    safe_payload = _safe_response_for_log(payload)
    text = json.dumps(safe_payload, ensure_ascii=False, separators=(",", ":"))
    preview = text[:150] + ('…（已截断）' if len(text) > 150 else '')
    logger.info(
        "直播接口响应内容 | response={}",
        preview,
    )

def log_send_start(username: str, message: str) -> None:
    logger.info(
        "开始发送直播消息 | username={} | message={}",
        str(username).strip().lstrip("@"),
        str(message),
    )


def _single_line_log_value(value: Any, fallback: str) -> str:
    cleaned = re.sub(r"\s+", " ", str(value or "")).strip()
    return cleaned[:200] if cleaned else fallback


def log_room_ready(room_id: str, username: str, nickname: str) -> None:
    logger.info(
        "直播间识别成功 | room_id={} | username={} | nickname={}",
        room_id,
        _single_line_log_value(username, "-"),
        _single_line_log_value(nickname, "-"),
    )

def log_send_result(result: dict[str, Any], http_status: int | None) -> None:
    values = (
        http_status if type(http_status) is int else None,
        result.get("statusCode") if type(result.get("statusCode")) is int else None,
        str(result.get("classification") or ("success" if result.get("ok") else "failed")),
    )
    if result.get("ok"):
        logger.info(
            "消息请求成功 | http_status={} | status_code={} | classification={}",
            *values,
        )
    elif result.get("requiresObservation") is True:
        logger.warning(
            "接口返回评论限制提示，将以直播间实际弹幕为准 | "
            "http_status={} | status_code={} | classification={}",
            *values,
        )
    else:
        logger.error(
            "消息请求失败 | http_status={} | status_code={} | classification={}",
            *values,
        )
        if http_status == 403:
            logger.warning("当前代理 IP 被风控，请更换代理节点")


def get_fresh_ms_token(
    session: requests.Session, *, cookie: str = "",
    signer: SigningSession, timeout_seconds: float = 30,
    environment_plaintext: str | None = None,
) -> str:
    profile = signer.profile
    cookie_values = parse_cookie_string(cookie)
    for name, value in cookie_values.items():
        session.cookies.jar.set_cookie(Cookie(
            0, name, value, None, False, ".tiktok.com", True, True,
            "/", True, True, None, True, None, None, {}, False,
        ))
    if environment_plaintext is None:
        try:
            account_id = account_id_from_cookies(_cookie_records_from_header(cookie))
        except ValueError:
            account_id = hashlib.sha256(str(cookie or "").encode("utf-8")).hexdigest()
        environment_seed = environment_seed_for_account_id(account_id)
        environment_plaintext = build_ms_token_environment(
            user_agent=profile.user_agent,
            account_seed=environment_seed,
            ttwid=cookie_values.get("ttwid", ""),
            page_url=signer.page_url,
        )
    seed = generate_ms_seed_token()
    session.cookies.jar.set_cookie(Cookie(
        0, "msToken", seed, None, False, ".tiktok.com", True, True,
        "/", True, True, None, True, None, None, {}, False,
    ))
    body = build_ms_token_report_json(
        user_agent=profile.user_agent,
        env_plaintext=environment_plaintext,
    )
    report_url = signer.sign(
        f"{MS_TOKEN_URL}?msToken={seed}",
        account_id=signer.account_id, room_id=None, query_token=seed,
        body=body, operation="report",
    )
    response = session.post(
        report_url,
        data=body,
        headers=browser_headers({
            "content-type": "text/plain;charset=UTF-8",
            "origin": "https://www.tiktok.com",
            "referer": signer.page_url,
        }, user_agent=profile.user_agent, sec_ch_ua=profile.sec_ch_ua),
        timeout=timeout_seconds,
    )
    token = response.headers.get("x-ms-token") or response.cookies.get("msToken")
    if not token:
        match = re.search(r"(?:^|[,;]\s*)msToken=([^;,]+)", response.headers.get("set-cookie", ""))
        token = match.group(1) if match else None
    if not token:
        response_headers = sorted(str(name).lower() for name in response.headers)
        content_type = str(response.headers.get("content-type", "")).split(";", 1)[0]
        response_size = len(getattr(response, "content", b"") or b"")
        logger.warning(
            "msToken 刷新响应无有效 Token | http_status={} | content_type={} | "
            "response_size={} | response_headers={}",
            getattr(response, "status_code", None), content_type or "-",
            response_size, response_headers,
        )
        raise RuntimeError("/web/report 没有返回新的 msToken")
    token = str(token)
    session.cookies.jar.set_cookie(Cookie(
        0, "msToken", token, None, False, ".tiktok.com", True, True,
        "/", True, True, None, True, None, None, {}, False,
    ))
    return token


def get_sec_sdk_csrf_token(
    session: requests.Session, cookie: str, *,
    referer: str = "https://www.tiktok.com/",
    profile: BrowserProfile | None = None,
) -> str | None:
    profile = _request_profile(profile)
    preflight = session.options(
        CHAT_URL,
        headers=browser_headers({
            "accept": "*/*",
            "access-control-request-headers":
                "x-secsdk-csrf-request,x-secsdk-csrf-version",
            "access-control-request-method": "HEAD",
            "origin": "https://www.tiktok.com",
            "referer": referer,
            "sec-fetch-dest": "empty",
            "sec-fetch-mode": "cors",
            "sec-fetch-site": "same-site",
        }, user_agent=profile.user_agent, sec_ch_ua=profile.sec_ch_ua),
        timeout=30,
    )
    if preflight.status_code != 200:
        return None
    response = session.head(
        CHAT_URL,
        headers=browser_headers({
            "accept": "*/*",
            "cookie": cookie,
            "origin": "https://www.tiktok.com",
            "referer": referer,
            "x-secsdk-csrf-request": "1",
            "x-secsdk-csrf-version": "1.2.22",
        }, user_agent=profile.user_agent, sec_ch_ua=profile.sec_ch_ua),
        timeout=30,
    )
    raw = response.headers.get("x-ware-csrf-token", "").strip()
    if not raw:
        return None
    parts = [part.strip() for part in raw.split(",")]
    # SecSDK 1.2.22 response format:
    # [statusCode, token, maxAge, message, sessionId].  A successful browser
    # sends the request token and session ID together in the chat header.
    # It uses the literal downgrade token when KMS/token bootstrap fails.
    if len(parts) >= 5:
        if parts[0] == "0" and parts[1]:
            return ",".join(value for value in (parts[1], parts[4]) if value)
        return "DOWNGRADE"
    return None


def extract_room_id(source: str) -> str | None:
    text = str(source or "")
    decoded = (
        text.replace('\\"', '"')
        .replace("\\u002F", "/")
        .replace("\\u002f", "/")
        .replace("\\u0026", "&")
        .replace("&quot;", '"')
    )
    patterns = (
        r'["\\](?:roomId|room_id|room_id_str)["\\]\s*:\s*["\\]?(\d{10,})',
        r'(?:roomId|room_id|room_id_str)%22%3A%22(\d{10,})',
        r'/webcast/room/(\d{10,})',
    )
    for candidate in (text, decoded):
        for pattern in patterns:
            match = re.search(pattern, candidate, re.IGNORECASE)
            if match:
                return match.group(1)
    return None


def extract_device_id(source: str) -> str | None:
    match = re.search(
        r'["\\](?:deviceId|device_id|webId)["\\]\s*:\s*["\\]?(\d{10,})',
        str(source or ""),
        re.IGNORECASE,
    )
    return match.group(1) if match else None


def _first_text_field(value: Any, names: tuple[str, ...]) -> str | None:
    if isinstance(value, dict):
        for name in names:
            item = value.get(name)
            if isinstance(item, str) and item.strip():
                return item.strip()
        for item in value.values():
            found = _first_text_field(item, names)
            if found:
                return found
    elif isinstance(value, list):
        for item in value:
            found = _first_text_field(item, names)
            if found:
                return found
    return None


def _owner_identity_from_json(value: Any) -> tuple[str | None, str | None]:
    if isinstance(value, dict):
        for key in ("liveRoomUserInfo", "live_room_user_info", "ownerInfo", "owner_info", "owner"):
            owner = value.get(key)
            if isinstance(owner, dict):
                username = _first_text_field(owner, ("uniqueId", "unique_id", "display_id"))
                nickname = _first_text_field(owner, ("nickname", "nickName"))
                if username or nickname:
                    return username, nickname
        for item in value.values():
            username, nickname = _owner_identity_from_json(item)
            if username or nickname:
                return username, nickname
    elif isinstance(value, list):
        for item in value:
            username, nickname = _owner_identity_from_json(item)
            if username or nickname:
                return username, nickname
    return None, None


def _decode_json_text(value: str) -> str:
    try:
        return str(json.loads(f'"{value}"'))
    except (TypeError, ValueError):
        return str(value)


def extract_live_identity(source: str) -> tuple[str | None, str | None]:
    text = str(source or "")
    decoded = text.replace('\\"', '"').replace("&quot;", '"')
    candidates = [text, decoded]
    candidates.extend(re.findall(
        r'<script[^>]+(?:id=["\'](?:__UNIVERSAL_DATA_FOR_REHYDRATION__|SIGI_STATE)["\']|type=["\']application/json["\'])[^>]*>(.*?)</script>',
        text,
        re.IGNORECASE | re.DOTALL,
    ))
    for candidate in candidates:
        try:
            payload = json.loads(candidate)
        except (TypeError, ValueError):
            continue
        username, nickname = _owner_identity_from_json(payload)
        if username or nickname:
            return username, nickname
    for match in re.finditer(r'"(?:liveRoomUserInfo|live_room_user_info|ownerInfo|owner_info|owner)"\s*:\s*\{', decoded, re.IGNORECASE):
        segment = decoded[match.end():match.end() + 50000]
        username_match = re.search(r'"(?:uniqueId|unique_id|display_id)"\s*:\s*"((?:\\.|[^"\\])*)"', segment, re.IGNORECASE)
        nickname_match = re.search(r'"nickname"\s*:\s*"((?:\\.|[^"\\])*)"', segment, re.IGNORECASE)
        username = _decode_json_text(username_match.group(1)) if username_match else None
        nickname = _decode_json_text(nickname_match.group(1)) if nickname_match else None
        if username or nickname:
            return username, nickname
    return None, None


def extract_current_account_identity(source: str) -> tuple[str | None, str | None]:
    """Extract the logged-in account uniqueId and nickname from app context."""
    text = str(source or "")
    candidates = [text]
    candidates.extend(re.findall(
        r'<script[^>]+id=["\']__UNIVERSAL_DATA_FOR_REHYDRATION__["\'][^>]*>(.*?)</script>',
        text,
        re.IGNORECASE | re.DOTALL,
    ))
    for candidate in candidates:
        try:
            payload = json.loads(candidate.replace("&quot;", '"'))
        except (TypeError, ValueError):
            continue
        if not isinstance(payload, dict):
            continue
        scope = payload.get("__DEFAULT_SCOPE__")
        context = scope.get("webapp.app-context") if isinstance(scope, dict) else None
        user = context.get("user") if isinstance(context, dict) else None
        if not isinstance(user, dict):
            continue
        username = _first_text_field(user, ("uniqueId", "unique_id"))
        nickname = _first_text_field(user, ("nickName", "nickname"))
        if username or nickname:
            return username, nickname
    return None, None


def extract_current_account_username(source: str) -> str | None:
    return extract_current_account_identity(source)[0]

def create_device_id() -> str:
    return "7" + str(secrets.randbelow(10**18)).zfill(18)


def web_id_last_time(cookies: dict[str, str]) -> str:
    match = re.search(r"\.(\d{10,13})$", cookies.get("_ttp", ""))
    if match:
        value = match.group(1)
        return value[:10] if len(value) == 13 else value
    sid_guard = unquote(cookies.get("sid_guard", ""))
    parts = sid_guard.split("|")
    if len(parts) >= 2 and re.fullmatch(r"\d{10,13}", parts[1]):
        return parts[1][:10]
    return str(int(time.time()))


def build_live_page_headers(
    username: str, cookie: str, *, profile: BrowserProfile | None = None
) -> dict[str, str]:
    profile = _request_profile(profile)
    clean_username = username.strip().lstrip("@")
    live_url = f"https://www.tiktok.com/@{quote(clean_username)}/live"
    return browser_headers({
        "accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8,application/signed-exchange;v=b3;q=0.7",
        "accept-language": "zh-CN,zh;q=0.9,en-US;q=0.8,en-GB;q=0.7,en;q=0.6",
        "cache-control": "no-cache",
        "cookie": cookie,
        "pragma": "no-cache",
        "priority": "u=0, i",
        "referer": live_url,
        "sec-fetch-dest": "document",
        "sec-fetch-mode": "navigate",
        "sec-fetch-site": "same-origin",
        "sec-fetch-user": "?1",
        "upgrade-insecure-requests": "1",
    }, user_agent=profile.user_agent, sec_ch_ua=profile.sec_ch_ua)


class _RoomQueryError(ValueError):
    """Only fixed reasons, never a response body or signed request URL."""
    def __init__(self, reason, *, retryable=False):
        self.reason = reason
        self.retryable = retryable
        super().__init__("Room lookup failed: " + reason)


def _transient_room_error(error):
    if isinstance(error, CertificateVerifyError):
        return False
    return (isinstance(error, (TimeoutError, ConnectionError, CurlConnectionError, CurlTimeout))
            or isinstance(error, ProxyError) and getattr(error, 'code', None) in (5, 7, 28, 52, 55, 56)
            or isinstance(error, _RoomQueryError) and error.retryable)


def request_live_room(
    session: requests.Session, username: str, cookie: str, *,
    profile: BrowserProfile | None = None,
    timeout_seconds: float = 30,
) -> dict[str, Any]:
    profile = _request_profile(profile)
    clean_username = username.strip().lstrip("@")
    live_url = f"https://www.tiktok.com/@{quote(clean_username)}/live"
    response = session.get(
        live_url,
        headers=build_live_page_headers(clean_username, cookie, profile=profile),
        timeout=timeout_seconds,
    )
    html = response.text
    if re.search(r"SlardarWAF|_wafchallengeid|Please wait", html, re.IGNORECASE):
        raise RuntimeError("TikTok 公开直播页被 WAF 风控拦截，暂时无法获取 roomId")
    if response.status_code != 200:
        raise _RoomQueryError("page_http_error")
    state = extract_bound_room_state(html, clean_username)
    if state is None:
        room_id = extract_room_id(html)
        owner_id = None
        resolved_username, nickname = extract_live_identity(html)
        status, is_live = None, None
    else:
        room_id = state["roomId"]
        owner_id = state.get("ownerId")
        resolved_username, nickname = state["username"], state["nickname"]
        status, is_live = state["liveStatus"], state["isLive"]
    account_username, account_nickname = extract_current_account_identity(html)
    return {
        "exists": bool(room_id),
        "roomId": room_id,
        "ownerId": owner_id,
        "username": resolved_username or clean_username,
        "nickname": nickname or "-",
        "accountUsername": account_username,
        "accountNickname": account_nickname,
        "deviceId": extract_device_id(html),
        "url": live_url,
        "status": response.status_code,
        "liveStatus": status,
        "isLive": is_live,
        "querySource": "live_page",
    }

def resolve_live_room(
    fetch_page: Callable[[str], dict[str, Any]], account_cookie: str,
    fallback_cookie: str | None = None,
) -> dict[str, Any]:
    account_result: dict[str, Any] | None = None
    account_error: Exception | None = None
    selected_account_cookie = account_cookie.strip()
    if selected_account_cookie:
        try:
            account_result = fetch_page(selected_account_cookie)
            if account_result.get("exists"):
                return account_result
        except Exception as error:
            account_error = error

    fallback_cookie = (ROOM_ID_COOKIE if fallback_cookie is None else fallback_cookie).strip()
    if fallback_cookie and fallback_cookie != selected_account_cookie:
        fallback_result = fetch_page(fallback_cookie)
        if fallback_result.get("exists"):
            if account_result is not None:
                merged = dict(account_result)
                merged["exists"] = True
                merged["roomId"] = fallback_result.get("roomId")
                for key in ("username", "nickname", "liveStatus", "isLive", "querySource"):
                    if key in fallback_result:
                        merged[key] = fallback_result[key]
                return merged
            fallback_only = dict(fallback_result)
            # Preserve public host username/nickname from the fallback page.
            # Do not reuse fallback account identity or device data.
            fallback_only["deviceId"] = None
            fallback_only["accountUsername"] = None
            fallback_only["accountNickname"] = None
            return fallback_only
        if account_result is not None:
            return account_result

    if account_error:
        raise account_error
    if account_result is not None:
        return account_result
    return {"exists": False, "roomId": None, "username": None, "nickname": "-", "accountUsername": None, "accountNickname": None, "deviceId": None, "url": "", "status": None}


def request_live_room_api(
    session: requests.Session, username: str, cookie: str, *,
    profile: BrowserProfile | None = None, timeout_seconds: float = 30,
    refresh: bool = False,
) -> dict[str, Any]:
    """Query current host state with the caller's session, never a browser."""
    clean_username = username.strip().lstrip("@")
    headers = build_live_page_headers(clean_username, cookie, profile=profile)
    headers.update({"accept": "application/json", "sec-fetch-dest": "empty",
                    "sec-fetch-mode": "cors"})
    for name in ("sec-fetch-user", "upgrade-insecure-requests"):
        headers.pop(name, None)
    params = {"aid": 1988, "uniqueId": clean_username, "sourceType": 54}
    if refresh:
        params['_'] = str(time.time_ns() // 1_000_000)
        headers.update({'cache-control': 'no-cache', 'pragma': 'no-cache'})
    response = session.get("https://www.tiktok.com/api-live/user/room/",
        params=params,
        headers=headers, timeout=timeout_seconds)
    if response.status_code != 200:
        raise _RoomQueryError("api_http_error", retryable=response.status_code in (408, 425, 429, 500, 502, 503, 504))
    try:
        payload = response.json()
        if not isinstance(payload, dict) or payload.get("status_code", 0) != 0:
            raise ValueError()
        data = payload["data"]
        user = data["user"]
        live = data.get("liveRoom") or {}
        owner = user["uniqueId"]
        room_id = user["roomId"]
        status, is_live = room_state_flags(user.get("status"), live.get("status"))
        if (not isinstance(owner, str) or owner.casefold() != clean_username.casefold()
                or type(room_id) not in (str, int)
                or not re.fullmatch(r"[0-9]{1,20}", str(room_id))
                or not 0 < int(room_id) < 2**64):
            raise ValueError()
    except (KeyError, TypeError, ValueError, AttributeError, RecursionError):
        # Response text and request URLs may contain private state. Do not echo them.
        raise _RoomQueryError("invalid_api_state") from None
    return {"exists": True, "roomId": str(room_id), "username": owner,
            "nickname": user.get("nickname") or user.get("nickName") or "-",
            "accountUsername": None, "accountNickname": None, "deviceId": None,
            "url": f"https://www.tiktok.com/@{quote(clean_username)}/live",
            "status": response.status_code, "liveStatus": status,
            "isLive": is_live, "querySource": "room_api"}


def request_room_alive(session, username, cookie, room_id, *, profile, timeout_seconds):
    """Require one boolean result bound to the exact room, never list position."""
    headers = build_live_page_headers(username, cookie, profile=profile)
    headers.update({'accept': 'application/json', 'sec-fetch-dest': 'empty',
                    'sec-fetch-mode': 'cors', 'cache-control': 'no-cache'})
    for name in ('sec-fetch-user', 'upgrade-insecure-requests'):
        headers.pop(name, None)
    response = session.get('https://webcast.tiktok.com/webcast/room/check_alive/',
        params={'aid': 1988, 'room_ids': room_id}, headers=headers, timeout=timeout_seconds)
    try:
        payload = response.json()
        if response.status_code != 200 or payload.get('status_code', 0) != 0:
            raise ValueError()
        entries = payload['data']
        if not isinstance(entries, list) or len(entries) != 1:
            raise ValueError()
        entry = entries[0]
        if str(entry['room_id']) != room_id or type(entry['alive']) is not bool:
            raise ValueError()
        return entry['alive']
    except (KeyError, TypeError, ValueError, AttributeError, RecursionError):
        raise _RoomQueryError('invalid_alive_state') from None


def get_api_live_room(
    session: requests.Session, username: str, cookie: str, *,
    profile: BrowserProfile | None = None, fallback_cookie: str | None = None,
    timeout_seconds: float = 30,
) -> dict[str, Any]:
    profile = _request_profile(profile)
    clean_username = str(username or "").strip().lstrip("@")
    if not clean_username:
        raise ValueError("username 不能为空")
    deadline = time.monotonic() + float(timeout_seconds)
    selected_cookie = str(cookie or "").strip()

    def remaining():
        budget = deadline - time.monotonic()
        if budget <= 0:
            raise TimeoutError("Room lookup timeout budget exhausted")
        return budget

    # The compatibility fallback_cookie argument never switches account context.
    # Reserve one slot for refresh and one for independent room-state evidence.
    logger.info('使用原会话纯接口查询当前房间 | classification=room_api_lookup')
    for attempt in (1, 2):
        api_budget = remaining() / (4 - attempt)
        try:
            api_room = request_live_room_api(session, clean_username, selected_cookie,
                                            profile=profile, timeout_seconds=api_budget,
                                            refresh=attempt > 1)
            remaining()
            if api_room['isLive'] is True or attempt == 2:
                break
            logger.info('房间状态未确认，刷新主播当前房间 | classification=room_state_refreshing')
        except Exception as exc:
            transient = _transient_room_error(exc)
            logger.warning("room_id 接口查询未完成 | attempt={}/2 | error_type={} | curl_code={} "
                           "| classification=room_api_attempt_failed", attempt, type(exc).__name__,
                           int(getattr(exc, "code", 0) or 0))
            if not transient:
                if isinstance(exc, CertificateVerifyError):
                    raise _RoomQueryError("certificate_error") from None
                raise
            if attempt == 2:
                raise _RoomQueryError("network_error") from None
            logger.info("在剩余查询预算内重试 room_id 接口 | classification=room_lookup_retrying")
    remaining()
    if api_room['isLive'] is not True:
        try:
            api_room['isLive'] = request_room_alive(session, clean_username, selected_cookie,
                api_room['roomId'], profile=profile, timeout_seconds=remaining())
            remaining()
            api_room['querySource'] = 'room_api+check_alive'
        except Exception as exc:
            api_room['isLive'] = None
            logger.warning('独立开播状态校验未完成 | error_type={} '
                           '| classification=room_state_unconfirmed', type(exc).__name__)
        logger.info('独立开播状态校验结果 | room_id={} | is_live={} '
                    '| classification=room_state_checked', api_room['roomId'], api_room['isLive'])
    logger.info("room_id 接口查询成功 | room_id={} | live_status={} | classification=room_api_ready",
                api_room["roomId"], api_room["liveStatus"] if api_room["liveStatus"] is not None else "unknown")
    return api_room


def get_live_room(
    session: requests.Session, username: str, cookie: str, *,
    profile: BrowserProfile | None = None, fallback_cookie: str | None = None,
    timeout_seconds: float = 30,
    should_cancel: Callable[[], bool] | None = None,
) -> dict[str, Any]:
    """Try the caller's account session first, with an isolated guest fallback."""
    from .public_room import query_live_page, normalize_username, RoomOwnerMismatch
    username = normalize_username(username)
    timeout_seconds = float(timeout_seconds)
    if not math.isfinite(timeout_seconds) or timeout_seconds <= 0:
        raise ValueError('Invalid room lookup timeout')
    deadline = time.monotonic() + timeout_seconds
    def remaining():
        if should_cancel is not None and should_cancel():
            raise TimeoutError('Room lookup cancelled')
        budget = deadline - time.monotonic()
        if budget <= 0:
            raise TimeoutError('Room lookup timeout budget exhausted')
        return budget
    remaining()
    if session is not None and str(cookie or '').strip():
        logger.info('优先使用账号会话查询直播页 room_id | transport=curl_cffi '
                    '| classification=account_room_lookup_start')
        # Cookies belong in the jar so Set-Cookie and WAF proof survive reloads.
        for name, value in parse_cookie_string(cookie).items():
            session.cookies.jar.set_cookie(Cookie(0, name, value, None, False,
                '.tiktok.com', True, True, '/', True, True, None,
                True, None, None, {}, False))
        headers = build_live_page_headers(username, '', profile=profile)
        headers = {k: v for k, v in headers.items() if k.lower() != 'cookie'}
        headers['accept-encoding'] = 'identity'
        try:
            # Reserve half the single configured deadline for a guest fallback.
            return query_live_page(username, timeout_seconds=remaining()/2,
                should_cancel=should_cancel, _session=session, _headers=headers)
        except (SafeOperationError, RoomOwnerMismatch, CertificateVerifyError):
            raise
        except Exception as exc:
            remaining()
            if not isinstance(exc, ValueError) and not _transient_room_error(exc):
                raise
            logger.warning('账号会话查询未完成，使用独立匿名会话兜底 | error_type={} '
                           '| classification=account_room_lookup_fallback', type(exc).__name__)
    return query_live_page(username, timeout_seconds=remaining(), should_cancel=should_cancel)


def build_chat_request(
    room_id: str,
    message: str,
    cookies: dict[str, str],
    device_id: str,
    referer: str,
    now_ms: int | None = None,
    *,
    profile: BrowserProfile | None = None,
    region: str | None = None,
    environment_plaintext: str | None = None,
    history_len: int | None = None,
) -> tuple[str, bytes]:
    profile = _request_profile(profile)
    timestamp = int(now_ms if now_ms is not None else time.time() * 1000)
    if history_len is None:
        # TikTok's web common parameter is window.history.length.  It is not
        # the number of historical chat messages decoded from room/fetch.
        # This protocol flow opens one canonical live page.
        history_len = 1
    if type(history_len) is not int or not 0 <= history_len <= 100000:
        raise ValueError("Invalid chat history length")
    request_region = str(
        region or cookies.get("store-country-code") or "SG"
    ).upper()
    if not re.fullmatch(r"[A-Z]{2}", request_region):
        request_region = "SG"
    screen_width, screen_height = 1707, 1067
    if environment_plaintext is not None:
        try:
            environment = json.loads(environment_plaintext)
            screen_width = int(environment["screen"]["sizeWidth"])
            screen_height = int(environment["screen"]["sizeHeight"])
        except (KeyError, TypeError, ValueError, json.JSONDecodeError):
            raise ValueError("signing_state_unsupported") from None
        if not (100 <= screen_width <= 10000
                and 100 <= screen_height <= 10000):
            raise ValueError("signing_state_unsupported")
    # TikTok currently accepts the desktop tool flow only when the comment
    # fields are mirrored in its JSON body as well as the query.
    body = json.dumps({
        "room_id": str(room_id),
        "content": str(message),
        "emotes_with_index": "",
        "input_type": 0,
        "client_start_timestamp_millisecond": timestamp,
    }, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    params = {
        "WebIdLastTime": web_id_last_time(cookies),
        "aid": "1988",
        "app_language": "zh-Hans",
        "app_name": "tiktok_web",
        "browser_language": "zh-CN",
        "browser_name": "Mozilla",
        "browser_online": "true",
        "browser_platform": "Win32",
        "browser_version": profile.user_agent.replace("Mozilla/", "", 1),
        "channel": "tiktok_web",
        "client_start_timestamp_millisecond": str(timestamp),
        "content": str(message),
        "cookie_enabled": "true",
        "data_collection_enabled": "true",
        "device_id": str(device_id),
        "device_platform": "web_pc",
        "emotes_with_index": "",
        "focus_state": "true",
        "from_page": "",
        "history_len": str(history_len),
        "input_type": "0",
        "is_fullscreen": "false",
        "is_page_visible": "true",
        "os": "windows",
        "priority_region": request_region,
        "referer": referer,
        "region": request_region,
        "room_id": str(room_id),
        "root_referer": "",
        "screen_height": str(screen_height),
        "screen_width": str(screen_width),
        "tz_name": "Asia/Shanghai",
        "user_is_login": "true",
        "webcast_language": "zh-Hans",
    }
    if cookies.get("s_v_web_id"):
        params["verifyFp"] = cookies["s_v_web_id"]
    return f"{CHAT_URL}?{urlencode(params, doseq=True)}", body


def build_epiphron_request(
    cookies: dict[str, str],
    device_id: str,
    referer: str,
    *,
    profile: BrowserProfile | None = None,
    region: str | None = None,
    environment_plaintext: str | None = None,
    history_len: int | None = None,
) -> str:
    """Build the feature-upload query emitted before a web comment."""
    chat_url, _ = build_chat_request(
        "0", "", cookies, device_id, referer, profile=profile,
        region=region, environment_plaintext=environment_plaintext,
        history_len=history_len,
    )
    parts = urlsplit(chat_url)
    chat_only = {
        "client_start_timestamp_millisecond", "content",
        "emotes_with_index", "input_type", "room_id",
    }
    params = [
        (name, value)
        for name, value in parse_qsl(parts.query, keep_blank_values=True)
        if name not in chat_only
    ]
    return (
        "https://webcast.tiktok.com/webcast/epiphron/feature/upload/?"
        + urlencode(params, doseq=True)
    )


def _browser_pre_comment_params(
    cookies: dict[str, str],
    device_id: str,
    referer: str,
    *,
    profile: BrowserProfile | None = None,
    region: str | None = None,
    environment_plaintext: str | None = None,
) -> list[tuple[str, str]]:
    base = build_epiphron_request(
        cookies, device_id, referer, profile=profile, region=region,
        environment_plaintext=environment_plaintext, history_len=1,
    )
    return parse_qsl(urlsplit(base).query, keep_blank_values=True)


def build_browser_check_alive_request(
    room_id: str,
    cookies: dict[str, str],
    device_id: str,
    referer: str,
    *,
    profile: BrowserProfile | None = None,
    region: str | None = None,
    environment_plaintext: str | None = None,
) -> str:
    params = _browser_pre_comment_params(
        cookies, device_id, referer, profile=profile, region=region,
        environment_plaintext=environment_plaintext,
    )
    params.append(("room_ids", str(room_id)))
    return (
        "https://webcast.tiktok.com/webcast/room/check_alive/?"
        + urlencode(params, doseq=True)
    )


def build_browser_event_feed_request(
    cookies: dict[str, str],
    device_id: str,
    referer: str,
    *,
    profile: BrowserProfile | None = None,
    region: str | None = None,
    environment_plaintext: str | None = None,
) -> str:
    params = _browser_pre_comment_params(
        cookies, device_id, referer, profile=profile, region=region,
        environment_plaintext=environment_plaintext,
    )
    params.extend((("count", "5"), ("query_type", "3")))
    return (
        "https://www.tiktok.com/tiktok/event/feed/v1?"
        + urlencode(params, doseq=True)
    )


def _active_tiktok_user_id(cookies: dict[str, str]) -> str | None:
    decoded = unquote(str(cookies.get("multi_sids") or ""))
    entries = re.findall(r"(?:^|,)\s*(\d{5,30}):([^,;]+)", decoded)
    active_sid = str(cookies.get("sessionid") or cookies.get("sessionid_ss")
                     or cookies.get("sid_tt") or "")
    if active_sid:
        matched = [user_id for user_id, sid in entries if sid == active_sid]
        if len(set(matched)) == 1:
            return matched[0]
    user_ids = {user_id for user_id, _ in entries}
    return next(iter(user_ids)) if len(user_ids) == 1 else None


def build_epiphron_body(
    room_id: str,
    owner_id: str,
    user_id: str,
    device_id: str,
    *,
    now_ms: int | None = None,
    watch_duration_ms: int = 30000,
) -> bytes:
    """Serialize the browser's dynamic Epiphron feature payload."""
    timestamp = int(time.time() * 1000) if now_ms is None else int(now_ms)
    watched = max(30000, int(watch_duration_ms))
    duration_bucket = watched // 30000 * 30000
    payload = {
        "base_info": {
            "room_id": str(room_id),
            "owner_id": str(owner_id),
            "user_id": str(user_id),
            "local_timeregi_stamp": str(timestamp),
            "device_id": str(device_id),
            "durationc": str(duration_bucket),
        },
        "biz_params": {"webapp_watch_duration": str(watched)},
    }
    return json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")


def enter_live_room(
    session: requests.Session,
    room: dict[str, Any],
    cookies: dict[str, str],
    device_id: str,
    runtime_cookie: str,
    ms_token: str,
    profile: BrowserProfile,
    *,
    signer: SigningSession | None = None,
    account_id: str | None = None,
) -> dict[str, str]:
    """Perform the page's HTTP room entry before starting chat transport."""
    region = str(room.get("region") or cookies.get("store-country-code") or "SG").upper()
    if not re.fullmatch(r"[A-Z]{2}", region):
        region = "SG"
    params = {
        "WebIdLastTime": web_id_last_time(cookies),
        "aid": "1988",
        "app_language": "zh-Hans",
        "app_name": "tiktok_web",
        "browser_language": "zh-CN",
        "browser_name": "Mozilla",
        "browser_online": "true",
        "browser_platform": "Win32",
        "browser_version": profile.browser_version,
        "channel": "tiktok_web",
        "cookie_enabled": "true",
        "data_collection_enabled": "true",
        "device_id": str(device_id),
        "device_platform": "web_pc",
        "device_type": "web_h265",
        "focus_state": "true",
        "from_page": "",
        "history_len": "7",
        "is_fullscreen": "false",
        "is_page_visible": "true",
        "os": "windows",
        "priority_region": region,
        "referer": room["url"],
        "region": region,
        "root_referer": "https://www.tiktok.com/",
        "screen_height": "1080",
        "screen_width": "1920",
        "tz_name": "Asia/Shanghai",
        "user_is_login": "true",
    }
    if cookies.get("s_v_web_id"):
        params["verifyFp"] = cookies["s_v_web_id"]
    params["webcast_language"] = "zh-Hans"
    # Keep the exact form order used by the standalone, known-good API
    # example.  The signature hashes these bytes, so request construction and
    # signing must share this one encoded value.
    body = urlencode({
        "enter_source": "others-others",
        "room_id": room["roomId"],
    }, doseq=True).encode("utf-8")
    url = f"{ENTER_URL}?{urlencode(params, doseq=True)}"
    headers = browser_headers({
        "accept": "*/*",
        "accept-language": "zh-CN,zh;q=0.9",
        "content-type": "application/x-www-form-urlencoded; charset=UTF-8",
        "origin": "https://www.tiktok.com",
        "priority": "u=1, i",
        "referer": "https://www.tiktok.com/",
        "sec-fetch-dest": "empty",
        "sec-fetch-mode": "cors",
        "sec-fetch-site": "same-site",
    }, user_agent=profile.user_agent, sec_ch_ua=profile.sec_ch_ua)
    if signer is None:
        signed_url = sign_url(url, ms_token, body, user_agent=profile.user_agent)
    else:
        if not account_id:
            raise ValueError("Invalid signing identity")
        signed_url = signer.sign(
            url, account_id=account_id, room_id=str(room["roomId"]),
            query_token=ms_token, body=body, operation="enter",
        )
    response = session.post(
        signed_url,
        data=body,
        headers=headers,
        timeout=30,
    )
    try:
        payload = response.json()
        accepted = response.status_code == 200 and int(payload.get("status_code", -1)) == 0
    except (TypeError, ValueError, AttributeError):
        accepted = False
    if not accepted:
        response_headers = sorted(str(name).lower() for name in response.headers)
        content_type = str(response.headers.get("content-type", "")).split(";", 1)[0]
        response_size = len(getattr(response, "content", b"") or b"")
        logger.warning(
            "HTTP 进入直播间未成功 | room_id={} | http_status={} | "
            "content_type={} | response_size={} | response_headers={}",
            room["roomId"], response.status_code, content_type or "-",
            response_size, response_headers,
        )
        if response.status_code == 403:
            raise _RoomEnterForbidden()
        raise SafeOperationError("room_enter_unavailable")
    logger.info("HTTP 进入直播间成功 | room_id={} | http_status={}",
                room["roomId"], response.status_code)
    cookie_jar = getattr(session, "cookies", None)
    session_updates = (
        cookie_jar.get_dict()
        if cookie_jar is not None and hasattr(cookie_jar, "get_dict")
        else {}
    )
    return {
        str(name): str(value)
        for name, value in session_updates.items()
        if str(name or "").strip() and str(value or "").strip()
    }


def _merge_runtime_cookie(cookie: str, updates: dict[str, str]) -> str:
    values: list[tuple[str, str]] = []
    for part in str(cookie or "").split(";"):
        item = part.strip()
        if not item or "=" not in item:
            continue
        name, value = item.split("=", 1)
        name = name.strip()
        if name:
            values.append((name, value.strip()))
    for raw_name, raw_value in updates.items():
        name = str(raw_name or "").strip()
        value = str(raw_value or "").strip()
        if (name and value
                and re.fullmatch(r"[!#$%&'*+\-.^_`|~0-9A-Za-z]+", name)
                and not re.search(r"[;\r\n]", value)):
            matching = [index for index, item in enumerate(values)
                        if item[0] == name]
            # msToken has independent scoped values in the browser.  The
            # report token belongs in the signed query, not in this flattened
            # Cookie header.
            if name == "msToken" and matching:
                continue
            if matching:
                values[matching[-1]] = (name, value)
            else:
                values.append((name, value))
    return "; ".join(f"{name}={value}" for name, value in values)


def interpret_chat_response(payload: dict[str, Any]) -> dict[str, Any]:
    try:
        status_code = int(payload.get("status_code"))
    except (TypeError, ValueError):
        status_code = None
    data = payload.get("data") if isinstance(payload.get("data"), dict) else {}
    server_message = str(data.get("message") or "")
    prompt = str(data.get("prompts") or "")
    root_message = str(payload.get("message") or payload.get("status_msg") or "")
    restriction_text = ' '.join((server_message, prompt, root_message))
    if status_code != 0 and re.search(
            r'\b(?:your\s+)?account\s+(?:(?:has\s+been|is|was)\s+)?'
            r'(?:(?:permanently|temporarily)\s+)?(?:banned|suspended)\b'
            r'|(?:账号|帳號|账户|帳戶)(?:已|已经|已經)?(?:被)?(?:永久|暂时|暫時)?(?:封禁|封停|停用)',
            restriction_text, re.I):
        return {'ok': False, 'roomExists': True, 'accountBanned': True,
                'statusCode': status_code, 'classification': 'account_banned',
                'message': '服务端明确提示账号已封禁，已停止该账号发送'}
    if _LOGIN_REQUIRED_PATTERN.search(" ".join((server_message, prompt, root_message))):
        return {
            "ok": False,
            "roomExists": True,
            "loginExpired": True,
            "statusCode": status_code,
            "message": "账号登录状态已失效",
        }
    account_silenced = status_code == 50001 and (
        "user silence" in restriction_text.lower() or "已被禁言" in restriction_text
    )
    if account_silenced:
        return {
            "ok": False,
            "roomExists": True,
            "accountSilenced": True,
            "statusCode": status_code,
            "classification": "account_silenced",
            "message": prompt or server_message or "当前账号已被禁言",
        }
    room_missing = status_code == 30001 or bool(
        re.search(r"room.*(?:doesn.t|not).*exist", server_message, re.IGNORECASE)
    ) or "不存在" in prompt
    if room_missing:
        return {"ok": False, "roomExists": False, "statusCode": status_code, "message": "直播间不存在"}
    if status_code == 4010048 and "由于本场直播的设置，无法发布评论" in prompt:
        return {
            "ok": False,
            "roomExists": True,
            "statusCode": status_code,
            "classification": "room_comment_restricted",
            "roomCommentsRestricted": True,
            "stopProcessing": True,
            "message": prompt,
        }
    if status_code == 0:
        return {
            "ok": True,
            "roomExists": True,
            "statusCode": 0,
            "message": "消息发送成功",
        }
    return {
        "ok": False,
        "roomExists": True,
        "statusCode": status_code,
        "message": prompt or server_message or "消息发送失败",
    }


def post_chat_request(
    session: requests.Session,
    url: str,
    body: bytes,
    cookie: str,
    ms_token: str,
    csrf_token: str | None,
    referer: str,
    *,
    profile: BrowserProfile | None = None,
    ticket_guard_state: dict[str, str] | None = None,
    ticket_guard_headers: dict[str, str] | None = None,
    signer=None,
    account_id: str | None = None,
    room_id: str | None = None,
) -> requests.Response:
    profile = _request_profile(profile)
    effective_cookie = cookie_with_ms_token_fallback(cookie, ms_token)
    cookie_jar = getattr(session, "cookies", None)
    session_updates = (
        cookie_jar.get_dict()
        if cookie_jar is not None and hasattr(cookie_jar, "get_dict")
        else {}
    )
    if session_updates:
        # The CSRF HEAD response creates/rotates csrf_session_id.  An explicit
        # Cookie header suppresses the session jar, so merge the latest
        # same-session values before the chat POST just as the browser does.
        effective_cookie = _merge_runtime_cookie(
            effective_cookie, session_updates,
        )
    headers = browser_headers({
        "accept": "*/*",
        "accept-language": "zh-CN,zh;q=0.9,en-US;q=0.8,en-GB;q=0.7,en;q=0.6",
        "cache-control": "no-cache",
        "content-type": "application/json; charset=UTF-8",
        "cookie": effective_cookie,
        "origin": "https://www.tiktok.com",
        "pragma": "no-cache",
        "priority": "u=1, i",
        "referer": referer,
        "sec-fetch-dest": "empty",
        "sec-fetch-mode": "cors",
        "sec-fetch-site": "same-site",
    }, user_agent=profile.user_agent, sec_ch_ua=profile.sec_ch_ua)
    # The browser preflight and POST include the freshly signed Ticket Guard
    # client-data together with its public key/version headers.
    if ticket_guard_state:
        try:
            ticket_headers = build_headers_from_cookie_state(
                effective_cookie, ticket_guard_state, urlsplit(url).path,
            )
            headers.update(ticket_headers)
        except Exception:
            raise SafeOperationError("ticket_guard_unavailable") from None
    else:
        raise SafeOperationError("ticket_guard_unavailable")
    if csrf_token:
        headers["x-secsdk-csrf-token"] = csrf_token
    signature_body = body or None
    if signer is None:
        signed_url = sign_url(
            url, ms_token, signature_body, user_agent=profile.user_agent
        )
    else:
        if not account_id or not room_id:
            raise ValueError("Invalid signing identity")
        signed_url = signer.sign(
            url, account_id=account_id, room_id=room_id,
            query_token=ms_token, body=signature_body, operation="chat",
        )
    requested_header_names = [
        name for name in (
            "content-type",
            "tt-ticket-guard-client-data",
            "tt-ticket-guard-iteration-version",
            "tt-ticket-guard-public-key",
            "tt-ticket-guard-version",
            "tt-ticket-guard-web-version",
            "x-secsdk-csrf-token",
        )
        if name in {header_name.lower() for header_name in headers}
    ]
    preflight = session.options(
        signed_url,
        headers=browser_headers({
            "accept": "*/*",
            "access-control-request-headers": ",".join(requested_header_names),
            "access-control-request-method": "POST",
            "origin": "https://www.tiktok.com",
            "priority": "u=1, i",
            "referer": referer,
            "sec-fetch-dest": "empty",
            "sec-fetch-mode": "cors",
            "sec-fetch-site": "same-site",
        }, user_agent=profile.user_agent, sec_ch_ua=profile.sec_ch_ua),
        timeout=30,
    )
    if preflight.status_code != 200:
        raise SafeOperationError("chat_preflight_unavailable")
    request_options = {"headers": headers, "timeout": 30}
    if body:
        request_options["data"] = body
    return session.post(signed_url, **request_options)


def post_epiphron_feature(
    session: requests.Session,
    url: str,
    cookie: str,
    ms_token: str,
    referer: str,
    *,
    profile: BrowserProfile | None = None,
    signer: SigningSession,
    account_id: str,
    room_id: str,
    body: bytes = b"{}",
) -> requests.Response:
    """Replay the browser's JSON-object Epiphron step in the signing session."""
    profile = _request_profile(profile)
    page_header_referer = "https://www.tiktok.com/"
    signed_url = signer.sign(
        url, account_id=account_id, room_id=room_id,
        query_token=ms_token, body=body, operation="epiphron",
    )
    preflight = session.options(
        signed_url,
        headers=browser_headers({
            "accept": "*/*",
            "access-control-request-headers": "content-type",
            "access-control-request-method": "POST",
            "origin": "https://www.tiktok.com",
            "referer": page_header_referer,
            "sec-fetch-dest": "empty",
            "sec-fetch-mode": "cors",
            "sec-fetch-site": "same-site",
        }, user_agent=profile.user_agent, sec_ch_ua=profile.sec_ch_ua),
        timeout=30,
    )
    logger.info(
        "Epiphron 预检响应 | endpoint=/webcast/epiphron/feature/upload/ "
        "| http_status={} | classification=epiphron_preflight_response",
        preflight.status_code,
    )
    if preflight.status_code != 200:
        raise SafeOperationError("epiphron_preflight_unavailable")
    response = session.post(
        signed_url,
        data=body,
        headers=browser_headers({
            "accept": "*/*",
            "accept-language": "zh-CN,zh;q=0.9,en-US;q=0.8,en-GB;q=0.7,en;q=0.6",
            "cache-control": "no-cache",
            "content-type": "application/json",
            "cookie": cookie_with_ms_token_fallback(cookie, ms_token),
            "origin": "https://www.tiktok.com",
            "pragma": "no-cache",
            "priority": "u=1, i",
            "referer": page_header_referer,
            "sec-fetch-dest": "empty",
            "sec-fetch-mode": "cors",
            "sec-fetch-site": "same-site",
        }, user_agent=profile.user_agent, sec_ch_ua=profile.sec_ch_ua),
        timeout=30,
    )
    logger.info(
        "Epiphron 接口响应 | endpoint=/webcast/epiphron/feature/upload/ "
        "| http_status={} | classification=epiphron_response",
        response.status_code,
    )
    if response.status_code != 200:
        raise SafeOperationError("epiphron_unavailable")
    try:
        payload = response.json()
    except Exception:
        raise SafeOperationError("epiphron_unavailable") from None
    logger.info(
        "Epiphron 业务响应 | endpoint=/webcast/epiphron/feature/upload/ "
        "| status_code={} | classification=epiphron_business_response",
        payload.get("status_code"),
    )
    if payload.get("status_code") != 0:
        # This endpoint uploads page features; the browser proceeds to CSRF and
        # chat even when that auxiliary upload is not accepted.  Preserve the
        # request and evidence, but never reinterpret its business code as a
        # failed comment submission.
        logger.warning(
            "Epiphron 特征上报未接受，继续评论流程 | status_code={} "
            "| classification=epiphron_business_nonblocking",
            payload.get("status_code"),
        )
    return response


def _get_signed_browser_prerequisite(
    session: requests.Session,
    url: str,
    cookie: str,
    ms_token: str,
    referer: str,
    *,
    profile: BrowserProfile,
    signer: SigningSession,
    account_id: str,
    room_id: str,
    operation: str,
) -> requests.Response:
    signed_url = signer.sign(
        url, account_id=account_id, room_id=room_id,
        query_token=ms_token, operation=operation,
    )
    response = session.get(
        signed_url,
        headers=browser_headers({
            "accept": "*/*",
            "accept-language": "zh-CN,zh;q=0.9,en-US;q=0.8,en;q=0.7",
            "cache-control": "no-cache",
            "cookie": cookie_with_ms_token_fallback(cookie, ms_token),
            "pragma": "no-cache",
            "priority": "u=1, i",
            "referer": referer,
            "sec-fetch-dest": "empty",
            "sec-fetch-mode": "cors",
            "sec-fetch-site": "same-site",
        }, user_agent=profile.user_agent, sec_ch_ua=profile.sec_ch_ua),
        timeout=30,
    )
    logger.info(
        "网页评论预请求响应 | stage={} | http_status={} | response_size={} "
        "| classification=browser_prerequisite_response",
        operation, response.status_code,
        len(getattr(response, "content", b"") or b""),
    )
    if response.status_code != 200:
        raise SafeOperationError(f"{operation}_unavailable")
    return response


def get_browser_room_alive(
    session: requests.Session, url: str, cookie: str, ms_token: str,
    referer: str, *, profile: BrowserProfile, signer: SigningSession,
    account_id: str, room_id: str,
) -> requests.Response:
    return _get_signed_browser_prerequisite(
        session, url, cookie, ms_token, referer, profile=profile,
        signer=signer, account_id=account_id, room_id=room_id,
        operation="check_alive",
    )


def get_browser_event_feed(
    session: requests.Session, url: str, cookie: str, ms_token: str,
    referer: str, *, profile: BrowserProfile, signer: SigningSession,
    account_id: str, room_id: str,
) -> requests.Response:
    return _get_signed_browser_prerequisite(
        session, url, cookie, ms_token, referer, profile=profile,
        signer=signer, account_id=account_id, room_id=room_id,
        operation="event_feed",
    )


class RetryableMessageResult(dict[str, Any]):
    """Mapping result with a private callback that repeats only the chat POST."""

    def __init__(self, values: dict[str, Any], retry_callback: Callable[[], "RetryableMessageResult"]):
        super().__init__(values)
        self._retry_callback = retry_callback

    def retry_message(self) -> "RetryableMessageResult":
        return self._retry_callback()


def _post_prepared_chat(**kwargs):
    prepare_before_chat = kwargs.pop('prepare_before_chat', None)
    room = kwargs['room']
    username = room.get('username') or room['url'].split('/@', 1)[1].split('/', 1)[0]
    wss_user_agent = kwargs.get('wss_user_agent') or kwargs['profile'].user_agent
    heartbeat_options = {}
    if kwargs.get('initial_fetch') is not None:
        heartbeat_options['initial_fetch'] = kwargs['initial_fetch']
    with room_heartbeat(username, str(room['roomId']), kwargs['runtime_cookie'],
                        wss_user_agent, **heartbeat_options):
        if prepare_before_chat is not None:
            kwargs['csrf_token'] = prepare_before_chat()
        return _post_chat_with_heartbeat(**kwargs)


def _post_chat_with_heartbeat(
    *,
    session: requests.Session,
    message: str,
    cookies: dict[str, str],
    room: dict[str, Any],
    device_id: str,
    runtime_cookie: str,
    ms_token: str,
    csrf_token: str | None,
    profile: BrowserProfile,
    ticket_guard_state: dict[str, str] | None,
    ticket_guard_headers: dict[str, str] | None,
    signer: SigningSession,
    account_id: str,
    environment_plaintext: str | None = None,
    initial_fetch: object | None = None,
    wss_user_agent: str | None = None,
) -> RetryableMessageResult:
    """Build a fresh signed body and post it using an already prepared room/session."""
    def retry():
        retry_session = requests.Session(impersonate=profile.impersonate)
        try:
            return _post_prepared_chat(session=retry_session,message=message,cookies=cookies,room=room,
                device_id=device_id,runtime_cookie=runtime_cookie,ms_token=ms_token,csrf_token=csrf_token,
                profile=profile,ticket_guard_state=ticket_guard_state,ticket_guard_headers=ticket_guard_headers,
                signer=signer,account_id=account_id,
                environment_plaintext=environment_plaintext,
                initial_fetch=initial_fetch,wss_user_agent=wss_user_agent)
        finally:
            retry_session.close()
    url, body = build_chat_request(
        room_id=room["roomId"],
        message=message,
        cookies=cookies,
        device_id=device_id,
        referer=room["url"],
        profile=profile,
        region=room.get("region"),
        environment_plaintext=environment_plaintext,
        history_len=1,
    )
    try:
        response = post_chat_request(
            session, url, body, runtime_cookie, ms_token, csrf_token, room["url"],
            profile=profile, ticket_guard_state=ticket_guard_state,
            ticket_guard_headers=ticket_guard_headers,
            signer=signer, account_id=account_id, room_id=str(room["roomId"]),
        )
    except Exception as exc:
        if transient_failure(exc):
            raise RetryableMessageError(retry) from None
        raise
    logger.info(
        "直播接口已响应 | room_id={} | http_status={}",
        room["roomId"],
        response.status_code if type(response.status_code) is int else None,
    )
    try:
        payload = response.json()
    except Exception:
        if _LOGIN_REQUIRED_PATTERN.search(str(getattr(response, "text", "") or "")):
            values = {
                "ok": False,
                "roomExists": True,
                "loginExpired": True,
                "statusCode": None,
                "message": "账号登录状态已失效",
                "roomId": room["roomId"],
                "httpStatus": response.status_code,
                "accountUsername": room.get("accountUsername"),
                "accountNickname": room.get("accountNickname"),
            }
            result = RetryableMessageResult(values,retry)
            log_send_result(result, http_status=response.status_code)
            return result
        if response.status_code in (408,425,429,500,502,503,504):
            result = RetryableMessageResult(dict(ok=False,temporaryError=True,
                classification='temporary_http_error',roomExists=True,roomId=room['roomId'],
                httpStatus=response.status_code),retry)
            log_send_result(result,http_status=response.status_code)
            return result
        raise RuntimeError("直播接口返回的不是 JSON") from None

    log_chat_response(payload)
    values = interpret_chat_response(payload)
    if (response.status_code in (408,425,429,500,502,503,504)
            and not values.get('loginExpired') and not values.get('accountSilenced')
            and not values.get('accountBanned')
            and not values.get('stopProcessing') and values.get('roomExists') is not False):
        values.update(ok=False,temporaryError=True,classification='temporary_http_error')
    values.update({
        "roomId": room["roomId"],
        "httpStatus": response.status_code,
        "accountUsername": room.get("accountUsername"),
        "accountNickname": room.get("accountNickname"),
        "response": payload,
    })
    result = RetryableMessageResult(values,retry)
    log_send_result(result, http_status=response.status_code)
    return result


def _send_live_message(username: str, cookie: str, message: str, *, ticket_guard_state: dict[str, str] | None = None, ticket_guard_headers: dict[str, str] | None = None, account_display_name: str | None = None, user_agent: str | None = None, known_room_id: str | None = None, known_fetch_endpoint: str | None = None, known_region: str | None = None, known_device_id: str | None = None, room_id_cookie: str | None = None) -> dict[str, Any]:
    with request_flow():
        return _send_live_message_in_flow(username, cookie, message,
            ticket_guard_state=ticket_guard_state, ticket_guard_headers=ticket_guard_headers,
            account_display_name=account_display_name, user_agent=user_agent,
            known_room_id=known_room_id, known_fetch_endpoint=known_fetch_endpoint,
            known_region=known_region, known_device_id=known_device_id, room_id_cookie=room_id_cookie)


def _send_live_message_in_flow(username: str, cookie: str, message: str, *, ticket_guard_state: dict[str, str] | None = None, ticket_guard_headers: dict[str, str] | None = None, account_display_name: str | None = None, user_agent: str | None = None, known_room_id: str | None = None, known_fetch_endpoint: str | None = None, known_region: str | None = None, known_device_id: str | None = None, room_id_cookie: str | None = None) -> dict[str, Any]:
    options = {
        "ticket_guard_state": ticket_guard_state,
        "ticket_guard_headers": ticket_guard_headers,
        "account_display_name": account_display_name,
        "user_agent": user_agent,
        "known_room_id": known_room_id,
        "known_fetch_endpoint": known_fetch_endpoint,
        "known_region": known_region,
        "known_device_id": known_device_id,
        "room_id_cookie": room_id_cookie,
    }
    return _send_live_message_attempt(username, cookie, message, **options)


def _send_live_message_attempt(username: str, cookie: str, message: str, *, ticket_guard_state: dict[str, str] | None = None, ticket_guard_headers: dict[str, str] | None = None, account_display_name: str | None = None, user_agent: str | None = None, known_room_id: str | None = None, known_fetch_endpoint: str | None = None, known_region: str | None = None, known_device_id: str | None = None, room_id_cookie: str | None = None, _session: requests.Session | None = None) -> dict[str, Any]:
    if not str(cookie or "").strip():
        raise ValueError("cookie 不能为空")
    if not str(message or "").strip():
        raise ValueError("message 不能为空")
    cookies = parse_cookie_string(cookie)

    profile = browser_profile_from_user_agent(user_agent)
    # The TLS ClientHello must describe the same browser major as the UA,
    # sec-ch-ua and WebMS SDK payload.  A random Safari/Firefox fingerprint
    # produces a syntactically successful but risk-filtered chat request.
    fingerprint = profile.impersonate
    if _session is None:
        session = requests.Session(impersonate=fingerprint)
        try:
            return _send_live_message_attempt(
                username, cookie, message,
                ticket_guard_state=ticket_guard_state,
                ticket_guard_headers=ticket_guard_headers,
                account_display_name=account_display_name,
                user_agent=user_agent,
                known_room_id=known_room_id,
                known_fetch_endpoint=known_fetch_endpoint,
                known_region=known_region,
                known_device_id=known_device_id,
                room_id_cookie=room_id_cookie,
                _session=session,
            )
        finally:
            session.close()
    session = _session
    try:
        account_id = account_id_from_cookies(_cookie_records_from_header(cookie))
    except ValueError:
        account_id = hashlib.sha256(str(cookie or "").encode("utf-8")).hexdigest()
    requested_username = str(username or "").strip().lstrip("@")
    requested_page_url = f"https://www.tiktok.com/@{quote(requested_username)}/live"
    environment_plaintext = build_ms_token_environment(
        user_agent=profile.user_agent,
        account_seed=environment_seed_for_account_id(account_id),
        ttwid=cookies.get("ttwid", ""),
        page_url=requested_page_url,
    )
    sdk_state = create_sdk_state(requested_page_url, environment_plaintext)
    signer = SigningSession(
        account_id, None, requested_page_url, profile, sdk_state,
    )
    issued_ms_token = get_fresh_ms_token(
        session, cookie=cookie, signer=signer,
        environment_plaintext=environment_plaintext,
    )
    saved_query_token = first_saved_ms_token(cookie)
    ms_token = saved_query_token or issued_ms_token
    logger.info(
        "msToken 通道已分离 | query_token_source={} | query_token_length={} "
        "| classification=ms_token_channels_ready",
        "saved_page" if saved_query_token else "report_fallback",
        len(ms_token),
    )
    logger.info("请求环境准备完成")
    # Keep the browser-captured scoped msToken cookies intact.  The fresh
    # report token is tracked separately as the signed-query token.
    runtime_cookie = cookie_with_ms_token_fallback(cookie, ms_token)
    room_page_started_ms = int(time.time() * 1000)
    resolved_room_id = str(known_room_id or "").strip()
    # The live page refreshes tt_chain_token and related cookies.  Keep this GET
    # in the same HTTP session as initial fetch and chat; reusing only an
    # observer's room_id leaves the sender session with a stale cookie chain.
    room = _run_pre_send_with_retry(
        "live_room",
        lambda: get_live_room(
            session, username, runtime_cookie, profile=profile,
            fallback_cookie=room_id_cookie,
        ),
    )
    if resolved_room_id and str(room.get("roomId") or "") != resolved_room_id:
        raise SafeOperationError("room_state_unconfirmed")
    if (not room.get("fetchEndpoint") and isinstance(known_fetch_endpoint, str)
            and known_fetch_endpoint.strip()):
        room["fetchEndpoint"] = known_fetch_endpoint.strip()
    if (not room.get("region") and isinstance(known_region, str)
            and re.fullmatch(r"[A-Za-z]{2}", known_region.strip())):
        room["region"] = known_region.strip().upper()
    if (not room.get("deviceId") and isinstance(known_device_id, str)
            and re.fullmatch(r"[0-9]{10,20}", known_device_id.strip())
            and 0 < int(known_device_id.strip()) < 2**64):
        room["deviceId"] = known_device_id.strip()
    cookie_jar = getattr(session, "cookies", None)
    page_updates = (
        cookie_jar.get_dict()
        if cookie_jar is not None and hasattr(cookie_jar, "get_dict")
        else {}
    )
    if page_updates:
        runtime_cookie = _merge_runtime_cookie(runtime_cookie, page_updates)
        cookies = parse_cookie_string(runtime_cookie)
    if not room.get("exists") or "isLive" in room and room["isLive"] is not True:
        result = {"ok": False, "roomExists": False, "roomId": None, "statusCode": None, "message": "直播间不存在", "accountUsername": room.get("accountUsername"), "accountNickname": room.get("accountNickname")}
        log_send_result(result, http_status=room.get("status"))
        return result

    # Some room resolvers (and listener-provided room metadata) only return the
    # room id and host identity.  All browser-shaped pre-send requests still
    # need the canonical live-page referer, so fall back to the page we just
    # requested instead of turning an otherwise valid room into a KeyError.
    room["url"] = str(room.get("url") or requested_page_url)

    current_account_name = (room.get("accountNickname")
                            or room.get("accountUsername") or account_display_name or "-")
    log_room_ready(room["roomId"], room.get("username") or username, current_account_name)
    device_id = room.get("deviceId") or create_device_id()
    signer.bind_room(str(room["roomId"]))
    fetch_session = prepare_account(
        room.get("username") or username,
        str(room["roomId"]),
        runtime_cookie,
        profile.user_agent,
    )
    fetch_options = {"device_id": device_id, "ms_token": ms_token}
    try:
        signing_environment = json.loads(environment_plaintext)
        fetch_screen_size = (
            int(signing_environment["screen"]["sizeWidth"]),
            int(signing_environment["screen"]["sizeHeight"]),
        )
    except (KeyError, TypeError, ValueError, json.JSONDecodeError):
        raise SafeOperationError("signing_state_unsupported") from None
    fetch_options["screen_size"] = fetch_screen_size
    endpoint = room.get("fetchEndpoint")
    if isinstance(endpoint, str) and endpoint.strip():
        fetch_options["endpoint"] = endpoint.strip()
    try:
        initial_fetch = _run_pre_send_with_retry(
            "initial_fetch",
            lambda: fetch_initial_response(
                session, fetch_session, signer=signer,
                account_id=account_id, **fetch_options,
            ),
            checkpoint=signer.checkpoint,
            restore=signer.restore,
        )
    except Exception:
        raise SafeOperationError("initial_fetch_unavailable") from None
    fetch_updates = (
        cookie_jar.get_dict()
        if cookie_jar is not None and hasattr(cookie_jar, "get_dict")
        else {}
    )
    if fetch_updates:
        runtime_cookie = _merge_runtime_cookie(runtime_cookie, fetch_updates)
        cookies = parse_cookie_string(runtime_cookie)
    def prepare_before_chat() -> str:
        epiphron_url = build_epiphron_request(
            cookies, device_id, room["url"], profile=profile,
            region=room.get("region"),
            environment_plaintext=environment_plaintext,
            history_len=1,
        )
        epiphron_user_id = _active_tiktok_user_id(cookies)
        epiphron_owner_id = str(room.get("ownerId") or "")
        epiphron_now_ms = int(time.time() * 1000)
        if epiphron_user_id and epiphron_owner_id:
            epiphron_body = build_epiphron_body(
                str(room["roomId"]), epiphron_owner_id, epiphron_user_id,
                str(device_id), now_ms=epiphron_now_ms,
                watch_duration_ms=max(
                    30000, epiphron_now_ms - room_page_started_ms,
                ),
            )
        else:
            logger.warning(
                "Epiphron 身份字段不完整，使用兼容请求体继续流程 "
                "| owner_id_present={} | user_id_present={} "
                "| classification=epiphron_identity_fallback",
                bool(epiphron_owner_id), bool(epiphron_user_id),
            )
            epiphron_body = b"{}"
        def post_epiphron_once() -> requests.Response:
            return _run_pre_send_with_retry(
                "epiphron",
                lambda: post_epiphron_feature(
                    session, epiphron_url, runtime_cookie, ms_token,
                    room["url"], profile=profile, signer=signer,
                    account_id=account_id, room_id=str(room["roomId"]),
                    body=epiphron_body,
                ),
                checkpoint=signer.checkpoint,
                restore=signer.restore,
            )

        post_epiphron_once()
        csrf_token = _run_pre_send_with_retry(
            "csrf_token",
            lambda: get_sec_sdk_csrf_token(
                session, runtime_cookie, referer=room["url"], profile=profile,
            ),
        )
        if not csrf_token:
            raise SafeOperationError("csrf_token_unavailable")
        return csrf_token

    return _post_prepared_chat(
        session=session,
        message=message,
        cookies=cookies,
        room=room,
        device_id=device_id,
        runtime_cookie=runtime_cookie,
        ms_token=ms_token,
        csrf_token=None,
        profile=profile,
        ticket_guard_state=ticket_guard_state,
        ticket_guard_headers=ticket_guard_headers,
        signer=signer,
        account_id=account_id,
        environment_plaintext=environment_plaintext,
        initial_fetch=initial_fetch,
        wss_user_agent=profile.user_agent,
        prepare_before_chat=prepare_before_chat,
    )

def send_live_message(username: str, cookie: str, message: str, *, ticket_guard_state: dict[str, str] | None = None, ticket_guard_headers: dict[str, str] | None = None, account_display_name: str | None = None, user_agent: str | None = None, known_room_id: str | None = None, known_fetch_endpoint: str | None = None, known_region: str | None = None, known_device_id: str | None = None, room_id_cookie: str | None = None) -> dict[str, Any]:
    log_send_start(username, message)
    try:
        return _send_live_message(
            username, cookie, message, ticket_guard_state=ticket_guard_state,
            ticket_guard_headers=ticket_guard_headers,
            account_display_name=account_display_name, user_agent=user_agent,
            known_room_id=known_room_id, known_fetch_endpoint=known_fetch_endpoint,
            known_region=known_region, known_device_id=known_device_id,
            room_id_cookie=room_id_cookie,
        )
    except Exception as exc:
        # Request exceptions and server messages can contain session credentials.
        classification = exc.classification if isinstance(exc, SafeOperationError) else "send_exception"
        logger.error("发送流程异常 | classification={}", classification)
        raise


