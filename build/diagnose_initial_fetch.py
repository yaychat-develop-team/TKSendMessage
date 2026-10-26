"""Read-only, redacted diagnostic for the account initial webcast fetch."""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import sys
from urllib.parse import urlsplit

from curl_cffi import requests

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from tiktok_live.account_store import cookie_header_from_playwright, load_account_store
from tiktok_live.client import create_device_id, enter_live_room, get_fresh_ms_token, get_live_room
from tiktok_live.config import browser_profile_from_user_agent
from tiktok_live.signing import cookie_with_ms_token, parse_cookie_string
from tiktok_live.wss import initial_fetch as initial_fetch_module
from tiktok_live.wss.sessions import prepare_account


def _probe(
    account: dict,
    host: str,
    *,
    use_saved_fetch_ua: bool = False,
    perform_enter: bool = False,
    guest_fetch: bool = False,
    anonymous_token: bool = False,
) -> dict:
    cookie = cookie_header_from_playwright(account["cookies"])
    profile = browser_profile_from_user_agent(account.get("user_agent"))
    page_session = requests.Session(impersonate=profile.impersonate)
    try:
        ms_token = get_fresh_ms_token(
            page_session,
            cookie="" if anonymous_token else cookie,
            profile=profile,
        )
        runtime_cookie = cookie_with_ms_token(cookie, ms_token)
        room = get_live_room(
            page_session,
            host,
            runtime_cookie,
            profile=profile,
            timeout_seconds=30,
        )
        device_id = room.get("deviceId") or create_device_id()
        if perform_enter:
            enter_live_room(
                page_session,
                room,
                parse_cookie_string(cookie),
                device_id,
                runtime_cookie,
                ms_token,
                profile,
            )
    finally:
        page_session.close()

    endpoint = room.get("fetchEndpoint") or "https://webcast.tiktok.com/webcast/im/fetch/"
    fetch_user_agent = profile.user_agent if use_saved_fetch_ua else initial_fetch_module.FETCH_USER_AGENT
    original_fetch_user_agent = initial_fetch_module.FETCH_USER_AGENT
    initial_fetch_module.FETCH_USER_AGENT = fetch_user_agent
    session = prepare_account(
        room.get("username") or host,
        str(room["roomId"]),
        runtime_cookie,
        fetch_user_agent,
    )
    try:
        url, headers = initial_fetch_module.build_initial_fetch_request(
            session,
            device_id=device_id,
            ms_token=ms_token,
            endpoint=endpoint,
        )
    finally:
        initial_fetch_module.FETCH_USER_AGENT = original_fetch_user_agent
    cookies = parse_cookie_string(session.cookie_header)
    cookies["msToken"] = ms_token
    if guest_fetch:
        cookies = {"msToken": ms_token}
    fetch_session = requests.Session(impersonate=profile.impersonate)
    try:
        response = fetch_session.get(
            url,
            headers=headers,
            cookies=cookies,
            timeout=20,
            allow_redirects=False,
        )
    finally:
        fetch_session.close()
    body = bytes(response.content or b"")
    if len(body) > 2 * 1024 * 1024:
        raise ValueError("response_too_large")

    result = {
        "account": account.get("username"),
        "account_id": str(account.get("id") or "")[:12],
        "room_id": str(room.get("roomId") or ""),
        "page_source": room.get("querySource"),
        "page_region": room.get("region"),
        "page_device_id_present": bool(room.get("deviceId")),
        "fetch_host": urlsplit(endpoint).hostname,
        "transport": profile.impersonate,
        "saved_ua_major": profile.user_agent.split("Chrome/", 1)[-1].split(".", 1)[0],
        "signed_ua_major": fetch_user_agent.split("Chrome/", 1)[-1].split(".", 1)[0],
        "http_status": response.status_code,
        "content_type": str(response.headers.get("content-type", "")).split(";", 1)[0].lower(),
        "response_bytes": len(body),
        "response_sha256_prefix": hashlib.sha256(body).hexdigest()[:12] if body else None,
        "redirected": bool(getattr(response, "history", None)),
        "enter_performed": perform_enter,
        "guest_fetch": guest_fetch,
        "anonymous_token": anonymous_token,
    }
    if result["content_type"] == "application/json" and body:
        try:
            payload = json.loads(body)
            if isinstance(payload, dict):
                result["json_fields"] = sorted(str(key) for key in payload)[:32]
                for key in ("status_code", "status_msg", "message"):
                    value = payload.get(key)
                    if isinstance(value, (str, int, float, bool)) or value is None:
                        result["json_" + key] = value
        except (UnicodeError, ValueError):
            result["json_decoded"] = False
    if response.status_code == 200 and result["content_type"] == "application/x-protobuf" and body:
        try:
            decoded = initial_fetch_module.decode_initial_fetch(bytes(body))
            result.update({
                "protobuf_decoded": True,
                "push_host": urlsplit(decoded.push_server).hostname,
                "route_count": len(decoded.route_params),
                "cursor_present": bool(decoded.cursor),
            })
        except Exception as exc:
            result.update({"protobuf_decoded": False, "decode_error": type(exc).__name__})
    return result


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("host")
    parser.add_argument("accounts", nargs="+")
    parser.add_argument("--saved-fetch-ua", action="store_true")
    parser.add_argument("--enter", action="store_true")
    parser.add_argument("--guest-fetch", action="store_true")
    parser.add_argument("--anonymous-token", action="store_true")
    parser.add_argument(
        "--store",
        default="dist/runtime/accounts/tiktok_accounts.json",
    )
    args = parser.parse_args()
    stored = load_account_store(Path(args.store))["accounts"]
    by_name = {str(item.get("username")): item for item in stored}
    for username in args.accounts:
        account = by_name.get(username)
        if account is None:
            print(json.dumps({"account": username, "error": "account_not_found"}))
            continue
        try:
            result = _probe(
                account,
                args.host,
                use_saved_fetch_ua=args.saved_fetch_ua,
                perform_enter=args.enter,
                guest_fetch=args.guest_fetch,
                anonymous_token=args.anonymous_token,
            )
        except Exception as exc:
            result = {
                "account": username,
                "account_id": str(account.get("id") or "")[:12],
                "error_type": type(exc).__name__,
            }
        print(json.dumps(result, ensure_ascii=False, sort_keys=True), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
