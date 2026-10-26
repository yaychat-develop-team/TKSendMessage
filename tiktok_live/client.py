"""TikTok live-room discovery and message request client."""

import json
import math
from http.cookiejar import Cookie
import hashlib
import re
import secrets
import time
from typing import Any, Callable
from urllib.parse import quote, unquote, urlencode, urlsplit

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
from .algorithms.ms_token import build_ms_token_report_json, generate_ms_seed_token
from .algorithms.ticket_guard import build_headers_from_cookie_state
from .errors import SafeOperationError
from .room_state import extract_bound_room_state, room_state_flags
from .heartbeat import room_heartbeat
from .wss.initial_fetch import fetch_initial_response
from .wss.sessions import prepare_account
from .retry_policy import RetryableMessageError, transient_failure
from .request_fingerprint import request_flow, current_fingerprint
from .config import (
    CHAT_URL, ENTER_URL, CHROME_BROWSER_PROFILES, DEFAULT_BROWSER_PROFILE, MS_TOKEN_URL,
    ROOM_ID_COOKIE, BrowserProfile, browser_profile_from_user_agent,
)
from .signing import (
    browser_headers,
    build_ms_token_environment,
    cookie_with_ms_token,
    parse_cookie_string,
    sign_url,
)

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


def _run_pre_send_with_retry(stage: str, operation: Callable[[], Any]) -> Any:
    """Run a safe pre-send operation and retry up to three times."""
    for retry_index, delay_seconds in enumerate(
        _PRE_SEND_RETRY_DELAYS_SECONDS, start=1
    ):
        try:
            return operation()
        except Exception:
            logger.warning(
                "发送前请求失败，准备重试 | stage={} | retry={}/{} | delay_seconds={}",
                stage,
                retry_index,
                len(_PRE_SEND_RETRY_DELAYS_SECONDS),
                delay_seconds,
            )
            time.sleep(delay_seconds)
    return operation()


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
    profile: BrowserProfile | None = None, timeout_seconds: float = 30,
) -> str:
    profile = _request_profile(profile)
    cookie_values = parse_cookie_string(cookie)
    try:
        account_id = account_id_from_cookies(_cookie_records_from_header(cookie))
    except ValueError:
        account_id = hashlib.sha256(str(cookie or "").encode("utf-8")).hexdigest()
    environment_seed = environment_seed_for_account_id(account_id)
    seed = generate_ms_seed_token()
    body = build_ms_token_report_json(
        user_agent=profile.user_agent,
        env_plaintext=build_ms_token_environment(
            user_agent=profile.user_agent,
            account_seed=environment_seed,
            ttwid=cookie_values.get("ttwid", ""),
        ),
    )
    report_url = sign_url(
        f"{MS_TOKEN_URL}?msToken={seed}", seed, body, user_agent=profile.user_agent
    )
    response = session.post(
        report_url,
        data=body,
        headers=browser_headers({
            "content-type": "text/plain;charset=UTF-8",
            "cookie": f"msToken={seed}",
            "origin": "https://www.tiktok.com",
            "referer": "https://www.tiktok.com/",
        }, user_agent=profile.user_agent, sec_ch_ua=profile.sec_ch_ua),
        timeout=timeout_seconds,
    )
    token = response.headers.get("x-ms-token") or response.cookies.get("msToken")
    if not token:
        match = re.search(r"(?:^|[,;]\s*)msToken=([^;,]+)", response.headers.get("set-cookie", ""))
        token = match.group(1) if match else None
    if not token:
        raise RuntimeError("/web/report 没有返回新的 msToken")
    return str(token)


def get_sec_sdk_csrf_token(
    session: requests.Session, cookie: str, *, profile: BrowserProfile | None = None
) -> str | None:
    profile = _request_profile(profile)
    response = session.head(
        CHAT_URL,
        headers=browser_headers({
            "accept": "*/*",
            "cookie": cookie,
            "origin": "https://www.tiktok.com",
            "referer": "https://www.tiktok.com/",
            "x-secsdk-csrf-request": "1",
            "x-secsdk-csrf-version": "1.2.22",
        }, user_agent=profile.user_agent, sec_ch_ua=profile.sec_ch_ua),
        timeout=30,
    )
    raw = response.headers.get("x-ware-csrf-token", "").strip()
    if not raw:
        return None
    parts = [part.strip() for part in raw.split(",")]
    if len(parts) >= 5 and parts[0] == "1":
        return f"{parts[1]},{parts[-1]}"
    return raw.split(",", 1)[-1].strip()


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
        resolved_username, nickname = extract_live_identity(html)
        status, is_live = None, None
    else:
        room_id = state["roomId"]
        resolved_username, nickname = state["username"], state["nickname"]
        status, is_live = state["liveStatus"], state["isLive"]
    account_username, account_nickname = extract_current_account_identity(html)
    return {
        "exists": bool(room_id),
        "roomId": room_id,
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
) -> tuple[str, bytes]:
    profile = _request_profile(profile)
    timestamp = int(now_ms if now_ms is not None else time.time() * 1000)
    region = cookies.get("store-country-code", "SG").upper()
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
        "history_len": "1",
        "input_type": "0",
        "is_fullscreen": "false",
        "is_page_visible": "true",
        "os": "windows",
        "priority_region": region,
        "referer": referer,
        "region": region,
        "room_id": str(room_id),
        "root_referer": "https://www.tiktok.com/",
        "screen_height": "1067",
        "screen_width": "1707",
        "tz_name": "Asia/Shanghai",
        "user_is_login": "true",
        "webcast_language": "zh-Hans",
    }
    if cookies.get("s_v_web_id"):
        params["verifyFp"] = cookies["s_v_web_id"]
    return f"{CHAT_URL}?{urlencode(params, doseq=True)}", body


def enter_live_room(
    session: requests.Session,
    room: dict[str, Any],
    cookies: dict[str, str],
    device_id: str,
    runtime_cookie: str,
    ms_token: str,
    profile: BrowserProfile,
) -> None:
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
        "history_len": "2",
        "is_fullscreen": "false",
        "is_page_visible": "true",
        "os": "windows",
        "priority_region": region,
        "referer": "",
        "region": region,
        "root_referer": "",
        "screen_height": "1067",
        "screen_width": "1707",
        "tz_name": "Asia/Shanghai",
        "user_is_login": "true",
    }
    if cookies.get("s_v_web_id"):
        params["verifyFp"] = cookies["s_v_web_id"]
    params["webcast_language"] = "zh-Hans"
    # Keep the exact form order used by the standalone, known-good API
    # example.  The signature hashes these bytes, so request construction and
    # signing must share this one encoded value.
    body = urlencode({"room_id": room["roomId"], "enter_source": "others-others"}, doseq=True).encode("utf-8")
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
    response = session.post(
        sign_url(url, ms_token, body, user_agent=profile.user_agent),
        data=body,
        headers=headers,
        cookies=parse_cookie_string(cookie_with_ms_token(runtime_cookie, ms_token)),
        timeout=30,
    )
    try:
        payload = response.json()
        accepted = response.status_code == 200 and int(payload.get("status_code", -1)) == 0
    except (TypeError, ValueError, AttributeError):
        accepted = False
    if not accepted:
        logger.warning("HTTP 进入直播间未成功 | room_id={} | http_status={}",
                       room["roomId"], response.status_code)
        raise SafeOperationError("room_enter_unavailable")
    logger.info("HTTP 进入直播间成功 | room_id={} | http_status={}",
                room["roomId"], response.status_code)


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
) -> requests.Response:
    profile = _request_profile(profile)
    headers = browser_headers({
        "accept": "*/*",
        "accept-language": "zh-CN,zh;q=0.9,en-US;q=0.8,en-GB;q=0.7,en;q=0.6",
        "cache-control": "no-cache",
        "content-type": "application/json; charset=UTF-8",
        "cookie": cookie_with_ms_token(cookie, ms_token),
        "origin": "https://www.tiktok.com",
        "pragma": "no-cache",
        "priority": "u=1, i",
        "referer": referer,
        "sec-fetch-dest": "empty",
        "sec-fetch-mode": "cors",
        "sec-fetch-site": "same-site",
    }, user_agent=profile.user_agent, sec_ch_ua=profile.sec_ch_ua)
    # Cached client-data binds an old timestamp. Re-sign every POST, including
    # retries; never fall back to replaying stored request headers.
    if ticket_guard_state:
        try:
            headers.update(build_headers_from_cookie_state(
                cookie, ticket_guard_state, urlsplit(url).path,
            ))
        except Exception:
            raise SafeOperationError("ticket_guard_unavailable") from None
    else:
        raise SafeOperationError("ticket_guard_unavailable")
    cookies = parse_cookie_string(cookie)
    if cookies.get("tt_csrf_token"):
        headers["tt-csrf-token"] = cookies["tt_csrf_token"]
    if csrf_token:
        headers["x-secsdk-csrf-token"] = csrf_token
    return session.post(
        sign_url(url, ms_token, body, user_agent=profile.user_agent),
        data=body,
        headers=headers,
        timeout=30,
    )


class RetryableMessageResult(dict[str, Any]):
    """Mapping result with a private callback that repeats only the chat POST."""

    def __init__(self, values: dict[str, Any], retry_callback: Callable[[], "RetryableMessageResult"]):
        super().__init__(values)
        self._retry_callback = retry_callback

    def retry_message(self) -> "RetryableMessageResult":
        return self._retry_callback()


def _post_prepared_chat(**kwargs):
    room = kwargs['room']
    username = room.get('username') or room['url'].split('/@', 1)[1].split('/', 1)[0]
    wss_user_agent = kwargs.get('wss_user_agent') or kwargs['profile'].user_agent
    heartbeat_options = {}
    if kwargs.get('initial_fetch') is not None:
        heartbeat_options['initial_fetch'] = kwargs['initial_fetch']
    with room_heartbeat(username, str(room['roomId']), kwargs['runtime_cookie'],
                        wss_user_agent, **heartbeat_options):
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
    initial_fetch: object | None = None,
    wss_user_agent: str | None = None,
) -> RetryableMessageResult:
    """Build a fresh signed body and post it using an already prepared room/session."""
    def retry():
        return _post_prepared_chat(session=session,message=message,cookies=cookies,room=room,
            device_id=device_id,runtime_cookie=runtime_cookie,ms_token=ms_token,csrf_token=csrf_token,
            profile=profile,ticket_guard_state=ticket_guard_state,ticket_guard_headers=ticket_guard_headers,
            initial_fetch=initial_fetch,wss_user_agent=wss_user_agent)
    url, body = build_chat_request(
        room_id=room["roomId"],
        message=message,
        cookies=cookies,
        device_id=device_id,
        referer=room["url"],
        profile=profile,
    )
    try:
        response = post_chat_request(
            session, url, body, runtime_cookie, ms_token, csrf_token, room["url"],
            profile=profile, ticket_guard_state=ticket_guard_state,
            ticket_guard_headers=ticket_guard_headers,
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
    if not str(cookie or "").strip():
        raise ValueError("cookie 不能为空")
    if not str(message or "").strip():
        raise ValueError("message 不能为空")
    cookies = parse_cookie_string(cookie)

    profile = browser_profile_from_user_agent(user_agent)
    fingerprint = current_fingerprint()
    session = requests.Session(impersonate=fingerprint)
    ms_token = _run_pre_send_with_retry(
        "ms_token",
        lambda: get_fresh_ms_token(session, cookie=cookie, profile=profile),
    )
    logger.info("请求环境准备完成")
    runtime_cookie = cookie_with_ms_token(cookie, ms_token)
    resolved_room_id = str(known_room_id or "").strip()
    if resolved_room_id:
        clean_username = str(username or "").strip().lstrip("@")
        room = {
            "exists": True, "roomId": resolved_room_id,
            "username": clean_username, "nickname": "-",
            "accountUsername": None, "accountNickname": account_display_name,
            "deviceId": None,
            "url": f"https://www.tiktok.com/@{quote(clean_username)}/live",
            "status": None,
        }
        if isinstance(known_fetch_endpoint, str) and known_fetch_endpoint.strip():
            room["fetchEndpoint"] = known_fetch_endpoint.strip()
        if isinstance(known_region, str) and re.fullmatch(r"[A-Za-z]{2}", known_region.strip()):
            room["region"] = known_region.strip().upper()
        if (isinstance(known_device_id, str)
                and re.fullmatch(r"[0-9]{10,20}", known_device_id.strip())
                and 0 < int(known_device_id.strip()) < 2**64):
            room["deviceId"] = known_device_id.strip()
        logger.info("复用弹幕监听 room_id，跳过公开直播页请求 | room_id={}", resolved_room_id)
    else:
        room = _run_pre_send_with_retry(
            "live_room",
            lambda: get_live_room(
                session, username, runtime_cookie, profile=profile,
                fallback_cookie=room_id_cookie,
            ),
        )
    if not room.get("exists") or "isLive" in room and room["isLive"] is not True:
        result = {"ok": False, "roomExists": False, "roomId": None, "statusCode": None, "message": "直播间不存在", "accountUsername": room.get("accountUsername"), "accountNickname": room.get("accountNickname")}
        log_send_result(result, http_status=room.get("status"))
        return result

    current_account_name = (room.get("accountNickname")
                            or room.get("accountUsername") or account_display_name or "-")
    log_room_ready(room["roomId"], room.get("username") or username, current_account_name)
    device_id = room.get("deviceId") or create_device_id()
    # `/webcast/im/fetch/` below carries `client_enter=1`; its protobuf
    # response is the authoritative audience/WSS bootstrap.  A separate
    # `/webcast/room/enter/` POST is not required and is deliberately omitted:
    # live API verification showed that fetch + WSS + chat is observable even
    # when no enter POST is sent, while the extra POST is independently liable
    # to risk-control 403 responses.
    fetch_session = prepare_account(
        room.get("username") or username,
        str(room["roomId"]),
        runtime_cookie,
        profile.user_agent,
    )
    fetch_http_session = requests.Session(impersonate=fingerprint)
    try:
        fetch_options = {"device_id": device_id, "ms_token": ms_token}
        endpoint = room.get("fetchEndpoint")
        if isinstance(endpoint, str) and endpoint.strip():
            fetch_options["endpoint"] = endpoint.strip()
        try:
            initial_fetch = _run_pre_send_with_retry(
                "initial_fetch",
                lambda: fetch_initial_response(
                    fetch_http_session, fetch_session, **fetch_options,
                ),
            )
        except Exception:
            raise SafeOperationError("initial_fetch_unavailable") from None
    finally:
        fetch_http_session.close()
    csrf_token = _run_pre_send_with_retry(
        "csrf_token",
        lambda: get_sec_sdk_csrf_token(session, runtime_cookie, profile=profile),
    )
    if not csrf_token:
        raise SafeOperationError("csrf_token_unavailable")
    return _post_prepared_chat(
        session=session,
        message=message,
        cookies=cookies,
        room=room,
        device_id=device_id,
        runtime_cookie=runtime_cookie,
        ms_token=ms_token,
        csrf_token=csrf_token,
        profile=profile,
        ticket_guard_state=ticket_guard_state,
        ticket_guard_headers=ticket_guard_headers,
        initial_fetch=initial_fetch,
        wss_user_agent=profile.user_agent,
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


