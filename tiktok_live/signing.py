"""Request signature assembly and browser-environment preparation."""

import json
import hashlib
import time
from urllib.parse import urlsplit, urlunsplit

from .algorithms.x_dynosaur import get_x_dynosaur
from .algorithms.x_gnarly import get_x_gnarly
from .config import BROWSER_ENV_PATH, RISK_KEYS, SEC_CH_UA, USER_AGENT

def parse_cookie_string(cookie: str) -> dict[str, str]:
    result: dict[str, str] = {}
    for part in str(cookie or "").split(";"):
        item = part.strip()
        if not item or "=" not in item:
            continue
        key, value = item.split("=", 1)
        if key.strip():
            result[key.strip()] = value.strip()
    return result


def cookie_with_ms_token(cookie: str, ms_token: str) -> str:
    source = [part.strip() for part in str(cookie).split(";") if part.strip()]
    positions = [index for index, part in enumerate(source) if part.startswith("msToken=")]
    keep_at = positions[-1] if positions else len(source)
    parts = []
    for index, part in enumerate(source):
        if part.startswith("msToken="):
            if index == keep_at:
                parts.append(f"msToken={ms_token}")
            continue
        parts.append(part)
    if not positions:
        parts.append(f"msToken={ms_token}")
    return "; ".join(parts)


def _query_key(part: str) -> str:
    return part.split("=", 1)[0]


def _clean_query(query: str) -> str:
    return "&".join(
        part for part in query.split("&") if part and _query_key(part) not in RISK_KEYS
    )


def _append_query(query: str, key: str, value: str) -> str:
    return query + ("&" if query else "") + f"{key}={value}"


def sign_url(
    url: str, ms_token: str, body: bytes | str | None = None, *,
    user_agent: str = USER_AGENT,
    dynosaur_context: dict | None = None,
    gnarly_context: dict | None = None,
) -> str:
    if not ms_token:
        raise ValueError("ms_token 不能为空")
    if isinstance(body, str):
        body = body.encode("utf-8")

    parts = urlsplit(url)
    clean_query = _clean_query(parts.query)
    unsigned_url = urlunsplit(
        (parts.scheme, parts.netloc, parts.path, clean_query, parts.fragment)
    )
    timestamp = int(time.time())
    x_dynosaur = get_x_dynosaur(
        user_agent, unsigned_url, post_params=body, ts=timestamp,
        context=dynosaur_context,
    )
    query = _append_query(clean_query, "X-Dynosaur", x_dynosaur)
    query = _append_query(query, "msToken", ms_token)
    token_url = urlunsplit(
        (parts.scheme, parts.netloc, parts.path, query, parts.fragment)
    )
    x_gnarly = get_x_gnarly(
        user_agent,
        token_url,
        post_params=body,
        ms_token=ms_token,
        x_bogus="1",
        ts=timestamp,
        context=gnarly_context,
    )
    query = _append_query(query, "X-Bogus", "1")
    query = _append_query(query, "X-Gnarly", x_gnarly)
    return urlunsplit(
        (parts.scheme, parts.netloc, parts.path, query, parts.fragment)
    )


_ACCOUNT_ENVIRONMENT_PROFILES = (
    {"screen": (1920, 1080, 1040), "viewport": (1536, 864), "cpu": 12, "memory": "16"},
    {"screen": (2560, 1440, 1392), "viewport": (1558, 982), "cpu": 20, "memory": "32"},
    {"screen": (1366, 768, 728), "viewport": (1349, 657), "cpu": 8, "memory": "8"},
    {"screen": (1920, 1200, 1160), "viewport": (1903, 1057), "cpu": 16, "memory": "16"},
)


def _environment_profile(account_seed: str) -> dict[str, object]:
    raw = str(account_seed or "0")
    try:
        index = int(raw, 16)
    except ValueError:
        index = int(hashlib.sha256(raw.encode("utf-8")).hexdigest(), 16)
    return _ACCOUNT_ENVIRONMENT_PROFILES[index % len(_ACCOUNT_ENVIRONMENT_PROFILES)]


def build_ms_token_environment(
    user_agent: str = USER_AGENT, *, account_seed: str = "", ttwid: str = ""
) -> str:
    env = json.loads(BROWSER_ENV_PATH.read_text(encoding="utf-8"))
    profile = _environment_profile(account_seed)
    screen_width, screen_height, avail_height = profile["screen"]
    inner_width, inner_height = profile["viewport"]
    cpu = profile["cpu"]
    memory = profile["memory"]
    now = int(time.time() * 1000)
    start = now - 3200
    env["navigator"]["userAgent"] = user_agent
    env["navigator"]["appVersion"] = user_agent.replace("Mozilla/", "", 1)
    env["navigator"]["hardwareConcurrency"] = cpu
    env["navigator"]["deviceMemory"] = memory
    env["navigator"]["maxTouchPoints"] = 0
    env["wID"]["timestamp"] = str(now)
    env["wID"]["sdkVersion"] = "5.3.0"
    env["wID"]["scmVersion"] = "1.0.0.382"
    env["wID"]["iframeInfo"]["iFrameNP"]["uA"] = user_agent
    env["wID"]["iframeInfo"]["iFrameNP"]["hC"] = cpu
    env["wID"]["iframeInfo"]["iFrameSP"].update({
        "aH": avail_height, "aW": screen_width,
        "h": screen_height, "w": screen_width, "pD": 24,
    })
    env["screen"].update({
        "innerWidth": inner_width, "innerHeight": inner_height,
        "outerWidth": min(screen_width, inner_width + 16),
        "outerHeight": min(screen_height, inner_height + 95),
        "screenX": 0, "screenY": 0,
        "availWidth": screen_width, "availHeight": avail_height,
        "sizeWidth": screen_width, "sizeHeight": screen_height,
        "clientWidth": inner_width, "clientHeight": inner_height,
        "colorDepth": 24, "pixelDepth": 24,
    })
    env["mediaQuery"].update({
        "maxHeight": inner_height, "maxWidth": inner_width,
        "dppx": 1, "dpi": 96, "anyPointer": "fine",
    })
    if str(ttwid or "").strip():
        env["customInit"]["ttwid"] = str(ttwid).strip()
    offsets = {
        "nS": 0, "fS": 1, "dLS": 1, "dLE": 1, "cS": 1, "cE": 1,
        "reqS": 24, "resS": 560, "dL": 565, "resE": 715,
        "dI": 725, "dC": 810, "dCEE": 810, "dCES": 810,
        "lES": 810, "lEE": 810,
    }
    for name, offset in offsets.items():
        env["wID"]["perf"][name] = str(start + offset)
    return json.dumps(env, ensure_ascii=False, separators=(",", ":"))


def browser_headers(
    headers: dict[str, str], *, user_agent: str = USER_AGENT, sec_ch_ua: str = SEC_CH_UA
) -> dict[str, str]:
    result = dict(headers)
    result.update({
        "user-agent": user_agent,
        "sec-ch-ua": sec_ch_ua,
        "sec-ch-ua-mobile": "?0",
        "sec-ch-ua-platform": '"Windows"',
    })
    return result


