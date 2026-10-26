"""TikTok login capture and saved-session validation through DrissionPage."""

import json
import os
import re
import socket
import threading
import time
from copy import deepcopy
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Literal
from urllib.parse import urlsplit

from loguru import logger
import psutil

from .account_store import (
    _is_tiktok_cookie, cookie_header_from_playwright, load_account_store,
    save_account_store_atomic, upsert_account,
)
from .algorithms.ticket_guard import build_headers_from_cookie_state
from .config import BROWSER_PROFILE_ROOT, CHAT_URL, DEFAULT_BROWSER_PROFILE
from .client import extract_current_account_identity
from .errors import BrowserError as _BrowserError
from .errors import LoginTimeoutError as _LoginTimeoutError
from .models import AccountValidation

_LOGIN_URL = "https://www.tiktok.com/login"
_LOGIN_PREFIXES = ("/login", "/signup", "/passport", "/auth")
_CRITICAL_COOKIE_NAMES = {"sessionid", "sessionid_ss", "sid_tt"}
_LOGIN_STATE_READ_FAILURE_LIMIT = 10
_BROWSER_OPERATION_TIMEOUT_SECONDS = 10
_BROWSER_QUIT_TIMEOUT_SECONDS = 2
_LOGIN_SUCCESS_CLOSE_DELAY_SECONDS = 10
_CHALLENGE_PATTERN = re.compile(
    r"SlardarWAF|_wafchallengeid|Please wait|captcha|verify to continue|"
    r"security verification|验证码|安全验证",
    re.IGNORECASE,
)
_LOGIN_SELECTOR = (
    'css:[data-e2e="login-modal"], [data-e2e="login-container"], '
    'a[href*="/login/phone-or-email"], a[href*="/login/qrcode"]'
)


def _load_drissionpage() -> tuple[Any, Any]:
    from DrissionPage import Chromium, ChromiumOptions
    return Chromium, ChromiumOptions


def _free_local_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def build_chromium_options(
    options_cls: Any,
    *,
    headless: bool,
    user_data_path: Path | None,
    timeout_seconds: int,
    local_port: int | None = None,
) -> Any:
    """Build an isolated DrissionPage Chrome configuration.

    Login uses a persistent dedicated profile so Google/Facebook sessions can
    survive between runs. Validation uses an ephemeral profile. The browser's
    native user agent is retained to avoid a stale UA/version mismatch.
    """
    options = options_cls(read_file=False)
    custom_executable = os.environ.get("TIKTOK_BROWSER_EXECUTABLE", "").strip()
    if custom_executable:
        options.set_browser_path(custom_executable)
    if user_data_path is None:
        options.auto_port()
    else:
        user_data_path = user_data_path.resolve()
        user_data_path.mkdir(parents=True, exist_ok=True)
        options.set_local_port(local_port if local_port is not None else _free_local_port())
        options.set_user_data_path(str(user_data_path))
    options.headless(headless)
    if not headless:
        options.set_argument("--start-maximized")
        options.set_argument("--incognito")
    options.set_argument("--lang", "zh-CN")
    options.set_argument("--disable-blink-features", "AutomationControlled")
    operation_timeout = min(
        _BROWSER_OPERATION_TIMEOUT_SECONDS, max(1, timeout_seconds)
    )
    options.set_timeouts(base=operation_timeout, page_load=operation_timeout)
    return options


def _owned_browser_processes(local_port: int | None, profile: Path | None,
                             process_id: int | None = None) -> list[Any]:
    """Require BOTH the dedicated profile and port before touching an OS process."""
    if local_port is None or profile is None:
        return []
    expected = os.path.normcase(os.path.abspath(profile))
    owned = {}
    if process_id is not None:
        try:
            candidates = [psutil.Process(int(process_id))]
        except (psutil.Error, ValueError, TypeError):
            return []
    else:
        # Query command lines only after filtering browser names, not every OS process.
        candidates = psutil.process_iter(['pid', 'name'])
    for process in candidates:
        try:
            info = getattr(process, 'info', None)
            if info is None:
                info = process.as_dict(attrs=['pid', 'name'])
            if str(info.get('name') or '').lower() not in (
                    'chrome.exe', 'chromium.exe', 'chrome', 'chromium'):
                continue
            args = info.get('cmdline')
            if args is None:
                args = process.cmdline()
            flags = {}
            for index, argument in enumerate(args):
                if argument.startswith('--'):
                    key, separator, value = argument.partition('=')
                    flags[key] = value if separator else (args[index + 1] if index + 1 < len(args) else '')
            path = flags.get('--user-data-dir', '').strip('"')
            if (flags.get('--remote-debugging-port') != str(local_port) or not path
                    or os.path.normcase(os.path.abspath(path)) != expected
                    or '--type' in flags):
                continue
            owned[process.pid] = process
            for child in process.children(recursive=True):
                owned[child.pid] = child
        except (psutil.Error, OSError, ValueError):
            continue
    return list(owned.values())


def _close_browser(browser: Any, *, local_port: int | None = None,
                   profile: Path | None = None) -> bool:
    """Bound driver shutdown, then reap only this login's verified process tree."""
    if browser is not None:
        if profile is None:
            profile = getattr(browser, 'user_data_path', None)
        if local_port is None:
            address = str(getattr(browser, 'address', ''))
            if address.startswith('127.0.0.1:') and address.rsplit(':', 1)[-1].isdigit():
                local_port = int(address.rsplit(':', 1)[-1])
    owned = _owned_browser_processes(local_port, profile,
                                      getattr(browser, 'process_id', None))
    logger.info('正在关闭登录浏览器 | classification=browser_closing')
    done = threading.Event()

    def quit_driver():
        try:
            try:
                browser.quit(timeout=_BROWSER_QUIT_TIMEOUT_SECONDS, force=True)
            except TypeError:
                browser.quit()
        except Exception as exc:
            logger.warning('浏览器驱动关闭失败 | error_type={} | classification=browser_quit_failed',
                           type(exc).__name__)
        finally:
            done.set()

    if browser is not None:
        threading.Thread(target=quit_driver, name='login-browser-cleanup', daemon=True).start()
        if not done.wait(_BROWSER_QUIT_TIMEOUT_SECONDS + 1):
            logger.warning('浏览器驱动关闭超时 | classification=browser_quit_timeout')
    survivors = []
    for process in owned:
        try:
            if process.is_running():
                survivors.append(process)
        except psutil.NoSuchProcess:
            pass
        except psutil.Error:
            survivors.append(process)
    if survivors:
        logger.warning('常规关闭后浏览器仍在运行，清理本次登录进程 | process_count={} '
                       '| classification=browser_cleanup_fallback', len(survivors))
        for process in survivors:
            try:
                process.kill()  # psutil checks process identity, protecting against PID reuse.
            except psutil.NoSuchProcess:
                pass
            except psutil.Error as exc:
                logger.warning('登录进程清理失败 | error_type={} | classification=browser_kill_failed',
                               type(exc).__name__)
        _, survivors = psutil.wait_procs(survivors, timeout=_BROWSER_QUIT_TIMEOUT_SECONDS)
    closed = not survivors if owned else (browser is None or not _browser_is_alive(browser))
    if closed:
        logger.info('登录浏览器已关闭 | classification=browser_closed')
    else:
        logger.error('无法确认登录浏览器已关闭，请手动关闭本次登录窗口 '
                     '| classification=browser_close_failed')
    return closed


def launch_available_browser(
    *, headless: bool, user_data_path: Path | None = None, timeout_seconds: int = 30,
    local_port: int | None = None,
) -> Any:
    profile = Path(user_data_path).resolve() if user_data_path is not None else None
    try:
        chromium_cls, options_cls = _load_drissionpage()
    except ImportError:
        raise _BrowserError("drissionpage_missing") from None
    try:
        options = build_chromium_options(
            options_cls,
            headless=headless,
            user_data_path=user_data_path,
            timeout_seconds=timeout_seconds,
            local_port=local_port,
        )
        return chromium_cls(options)
    except _BrowserError:
        raise
    except Exception as exc:
        logger.error('浏览器启动失败 | error_type={} | classification=chromium_unavailable',
                     type(exc).__name__)
        _close_browser(None, local_port=local_port, profile=profile)
        raise _BrowserError("chromium_unavailable") from None


def _normalise_cookies(cookies: Any) -> list[dict[str, Any]]:
    result = []
    for item in cookies or []:
        try:
            cookie = dict(item)
        except (TypeError, ValueError):
            continue
        if isinstance(cookie.get("name"), str) and isinstance(cookie.get("value"), str):
            result.append(cookie)
    return result


def _browser_cookies(browser: Any, tab: Any = None) -> list[dict[str, Any]]:
    if tab is not None:
        # DrissionPage's cookies(all_domains=True) uses Storage.getCookies;
        # even on a page target it misses Chrome's --incognito cookie store.
        # Network.getAllCookies is bound to the page's actual network context.
        return _normalise_cookies(tab.run_cdp('Network.getAllCookies')['cookies'])
    return _normalise_cookies(browser.cookies(all_info=True))


def read_browser_user_agent(browser: Any) -> str | None:
    """Read the native UA from the authenticated TikTok tab."""
    tabs = _tabs(browser)
    ordered = [
        tab for tab in reversed(tabs)
        if _is_official_tiktok_url(str(getattr(tab, "url", "")))
    ]
    ordered.extend(tab for tab in reversed(tabs) if tab not in ordered)
    for tab in ordered:
        try:
            value = str(tab.run_js("return navigator.userAgent;") or "").strip()
        except Exception:
            continue
        if value:
            return value
    return None


_TICKET_GUARD_STORAGE_SCRIPT = r"""
return (() => {
    const result = {fetch: null, crypt: null};
    const stores = [window.localStorage, window.sessionStorage];
    for (const storage of stores) {
        for (let index = 0; index < storage.length; index += 1) {
            const key = storage.key(index) || "";
            if (key.includes("s_sdk_sign_data_key") && key.endsWith("/tt_fetch")) {
                result.fetch = storage.getItem(key);
            } else if (key === "security-sdk/s_sdk_crypt_sdk") {
                result.crypt = storage.getItem(key);
            }
        }
    }
    return result.fetch ? result : null;
})();
"""


def ticket_guard_state_from_tab(tab: Any) -> dict[str, str] | None:
    """Read same-session Ticket Guard ticket state without exposing it to logs."""
    def decode_storage(value: Any) -> dict[str, Any] | None:
        if not value:
            return None
        payload = json.loads(value) if isinstance(value, str) else value
        nested = payload.get("data", payload) if isinstance(payload, dict) else None
        if isinstance(nested, str):
            nested = json.loads(nested)
        return nested if isinstance(nested, dict) else None

    try:
        value = tab.run_js(_TICKET_GUARD_STORAGE_SCRIPT)
        if not value:
            return None
        if isinstance(value, dict) and ("fetch" in value or "crypt" in value):
            fetch_state = decode_storage(value.get("fetch"))
            crypt_state = decode_storage(value.get("crypt"))
        else:
            # Compatibility with an older single-value capture shape.
            fetch_state = decode_storage(value)
            crypt_state = None
        if not fetch_state:
            return None
        ts_sign = str(fetch_state.get("ts_sign", "")).strip()
        encrypt_ticket = str(fetch_state.get("encrypt_ticket", "")).strip()
        if not ts_sign or not encrypt_ticket:
            return None
        state = {"ts_sign": ts_sign, "encrypt_ticket": encrypt_ticket}
        private_key = str((crypt_state or {}).get("ec_privateKey", "")).strip()
        if private_key:
            state["private_key_pem"] = private_key
        return state
    except (AttributeError, TypeError, ValueError, json.JSONDecodeError):
        return None


def wait_for_ticket_guard_state(
    browser: Any, timeout_seconds: float = 10.0, poll_seconds: float = 0.25,
    *, cancel_event: threading.Event | None = None, local_port: int | None = None,
) -> dict[str, str] | None:
    deadline = time.monotonic() + max(0.0, float(timeout_seconds))
    while True:
        _check_login_active(browser, cancel_event, local_port)
        for tab in _tabs(browser):
            if not _is_official_tiktok_url(str(getattr(tab, "url", ""))):
                continue
            try:
                state = ticket_guard_state_from_tab(tab)
            except _BrowserError:
                raise
            except Exception:
                # DrissionPage may disconnect while run_js is in flight, before
                # the normal post-read check can observe the closed browser.
                _check_login_active(browser, cancel_event, local_port)
                raise _BrowserError("login_state_unavailable") from None
            _check_login_active(browser, cancel_event, local_port)
            if state:
                return state
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return None
        _wait_for_login_poll(min(max(0.01, poll_seconds), remaining), cancel_event)


def _critical_login_cookies(cookies: list[dict[str, Any]]) -> tuple:
    return tuple(sorted(
        (cookie["name"], cookie["value"])
        for cookie in cookies
        if _is_tiktok_cookie(cookie)
        and cookie.get("name") in _CRITICAL_COOKIE_NAMES
        and isinstance(cookie.get("value"), str)
        and cookie["value"]
        and (cookie.get("expires", -1) == -1 or cookie.get("expires", 0) > time.time())
    ))


def has_critical_login_cookie(cookies: list[dict[str, Any]]) -> bool:
    return bool(_critical_login_cookies(cookies))


def _complete_login_cookie_state(cookies: list[dict[str, Any]]) -> tuple:
    critical = _critical_login_cookies(cookies)
    fingerprints = tuple(sorted(
        (cookie["name"], cookie["value"])
        for cookie in cookies
        if _is_tiktok_cookie(cookie)
        and cookie.get("name") == "s_v_web_id"
        and isinstance(cookie.get("value"), str)
        and cookie["value"]
        and (cookie.get("expires", -1) == -1 or cookie.get("expires", 0) > time.time())
    ))
    return critical + fingerprints if critical and fingerprints else ()


def has_complete_login_cookie(cookies: list[dict[str, Any]]) -> bool:
    return bool(_complete_login_cookie_state(cookies))


def critical_cookies_locally_expired(cookies: list[dict[str, Any]], now_epoch: float) -> bool:
    critical = [
        cookie for cookie in cookies
        if _is_tiktok_cookie(cookie)
        and cookie.get("name") in _CRITICAL_COOKIE_NAMES
        and isinstance(cookie.get("value"), str)
        and cookie["value"]
    ]
    return bool(critical) and all(
        type(cookie.get("expires")) in (int, float)
        and cookie["expires"] >= 0
        and cookie["expires"] <= now_epoch
        for cookie in critical
    )


def _is_official_tiktok_url(url: str) -> bool:
    location = urlsplit(str(url or ""))
    return (
        location.scheme == "https"
        and (location.hostname == "tiktok.com" or str(location.hostname).endswith(".tiktok.com"))
    )


def _is_login_url(url: str) -> bool:
    location = urlsplit(str(url or ""))
    path = location.path.rstrip("/").lower()
    return _is_official_tiktok_url(url) and any(
        path == prefix or path.startswith(prefix + "/") for prefix in _LOGIN_PREFIXES
    )


def classify_login_page(
    url: str, has_login_ui: bool, cookies: list[dict[str, Any]]
) -> Literal["valid", "expired"]:
    if _is_login_url(url) and has_login_ui:
        return "expired"
    if _is_official_tiktok_url(url) and not _is_login_url(url) and not has_login_ui:
        if has_complete_login_cookie(cookies):
            return "valid"
        if has_critical_login_cookie(cookies):
            return "expired"
    raise ValueError("无法确认官方页面登录状态")


def _debug_port_is_open(local_port: int, timeout_seconds: float = 0.2) -> bool:
    """Check the real Chrome process endpoint instead of trusting stale driver state."""
    try:
        with socket.create_connection(
            ("127.0.0.1", int(local_port)), timeout=max(0.01, timeout_seconds)
        ):
            return True
    except (OSError, TypeError, ValueError):
        return False


def _browser_is_alive(browser: Any) -> bool:
    try:
        return bool(browser.states.is_alive)
    except Exception:
        return False


def _raise_if_login_cancelled(cancel_event: threading.Event | None) -> None:
    if cancel_event is not None and cancel_event.is_set():
        raise _BrowserError("login_cancelled")


def _check_login_active(
    browser: Any, cancel_event: threading.Event | None, local_port: int | None = None,
) -> None:
    _raise_if_login_cancelled(cancel_event)
    if local_port is not None and not _debug_port_is_open(local_port):
        raise _BrowserError("login_cancelled")
    if not _browser_is_alive(browser):
        raise _BrowserError("login_cancelled")


def _wait_for_login_poll(seconds: float, cancel_event: threading.Event | None) -> None:
    if cancel_event is None:
        time.sleep(seconds)
    elif cancel_event.wait(seconds):
        raise _BrowserError("login_cancelled")


def _tabs(browser: Any) -> list[Any]:
    try:
        return list(browser.get_tabs())
    except Exception:
        return []


def clear_tiktok_cookies(browser: Any) -> None:
    """Clear only TikTok cookies while preserving Google/Facebook OAuth state."""
    tab = browser.latest_tab
    for cookie in _browser_cookies(browser):
        if not _is_tiktok_cookie(cookie):
            continue
        args = {"name": cookie["name"], "domain": cookie.get("domain", ".tiktok.com")}
        if cookie.get("path"):
            args["path"] = cookie["path"]
        tab.run_cdp("Network.deleteCookies", **args)


def wait_for_tiktok_login(
    browser: Any,
    timeout_seconds: int,
    poll_seconds: float = 1.0,
    local_port: int | None = None,
    *, cancel_event: threading.Event | None = None,
) -> list[dict[str, Any]]:
    if poll_seconds <= 0:
        raise ValueError("poll_seconds 必须大于 0")
    deadline = time.monotonic() + max(0, timeout_seconds)
    previous = {}
    last_diagnostic = None
    last_diagnostic_time = float('-inf')
    consecutive_read_failures = 0
    saw_authenticated_without_fingerprint = False
    _check_login_active(browser, cancel_event)
    while time.monotonic() < deadline:
        _check_login_active(browser, cancel_event)
        if local_port is not None and not _debug_port_is_open(local_port):
            raise _BrowserError("login_cancelled")
        try:
            tabs = _tabs(browser)
            # OAuth providers may create TikTok session cookies before their
            # redirect has completed. Closing Chrome at that point interrupts
            # the login and may persist a transitional/invalid cookie set.
            completed_tabs = [tab for tab in tabs
                              if _is_official_tiktok_url(str(getattr(tab, 'url', '')))
                              and not _is_login_url(str(getattr(tab, 'url', '')))]
            current = {}
            reason = 'waiting_tiktok_redirect'
            failed_reads = 0
            for tab in completed_tabs:
                try:
                    cookies = _browser_cookies(browser, tab)
                except Exception:
                    failed_reads += 1
                    continue
                _check_login_active(browser, cancel_event)
                complete = _complete_login_cookie_state(cookies)
                key = getattr(tab, 'tab_id', id(tab))
                if complete:
                    if previous.get(key) == complete:
                        if time.monotonic() < deadline:
                            return cookies
                    current[key] = complete
                    reason = 'confirming_stable_session'
                elif _critical_login_cookies(cookies):
                    saw_authenticated_without_fingerprint = True
                    if reason != 'confirming_stable_session':
                        reason = 'fingerprint_cookie_missing'
                elif reason == 'waiting_tiktok_redirect':
                    reason = 'session_cookie_missing'
            if completed_tabs and failed_reads == len(completed_tabs):
                raise _BrowserError('login_state_unavailable')
        except Exception:
            consecutive_read_failures += 1
            if consecutive_read_failures >= _LOGIN_STATE_READ_FAILURE_LIMIT:
                raise _BrowserError("login_state_unavailable") from None
            remaining = deadline - time.monotonic()
            if remaining > 0:
                _wait_for_login_poll(min(poll_seconds, remaining), cancel_event)
            continue
        consecutive_read_failures = 0
        _check_login_active(browser, cancel_event)
        if time.monotonic() >= deadline:
            break
        previous = current
        diagnostic = (reason, len(completed_tabs))
        now = time.monotonic()
        if diagnostic != last_diagnostic or now - last_diagnostic_time >= 15:
            logger.info(
                '等待 TikTok 登录确认 | reason={} | tiktok_page_count={} | '
                'classification=login_waiting', reason, len(completed_tabs),
            )
            last_diagnostic = diagnostic
            last_diagnostic_time = now
        remaining = deadline - time.monotonic()
        if remaining > 0:
            _wait_for_login_poll(min(poll_seconds, remaining), cancel_event)
    _check_login_active(browser, cancel_event)
    if saw_authenticated_without_fingerprint:
        raise _BrowserError("login_cookie_incomplete")
    raise _LoginTimeoutError("login_timeout")


def _page_text(tab: Any) -> str:
    try:
        body = tab.ele("tag:body", timeout=0)
        return str(getattr(body, "text", "") or "")
    except Exception:
        return ""


def _page_source(tab: Any) -> str:
    """Read the authenticated document without logging its contents."""
    try:
        return str(getattr(tab, "html", "") or "")
    except Exception:
        return ""


def _has_login_ui(tab: Any) -> bool:
    try:
        return bool(tab.ele(_LOGIN_SELECTOR, timeout=0))
    except Exception:
        return False


def validate_saved_account(account: dict[str, Any], timeout_seconds: int = 30) -> AccountValidation:
    saved_cookies = deepcopy(account.get("cookies", []))
    if critical_cookies_locally_expired(saved_cookies, time.time()):
        return AccountValidation("expired", saved_cookies, "关键登录 Cookie 已本地过期")
    browser = None
    try:
        browser = launch_available_browser(headless=True, timeout_seconds=timeout_seconds)
        browser.set.cookies(saved_cookies)
        tab = browser.latest_tab
        loaded = tab.get(
            _LOGIN_URL,
            timeout=min(_BROWSER_OPERATION_TIMEOUT_SECONDS, max(1, timeout_seconds)),
        )
        if loaded is False:
            return AccountValidation("temporary_error", saved_cookies, "官方页面暂时无法访问")
        body = _page_text(tab)
        # Do not scan the full HTML source here. TikTok's normal application
        # bundle contains dormant strings such as "captcha", which caused an
        # authenticated /foryou page to be mistaken for an active challenge.
        if _CHALLENGE_PATTERN.search(body):
            return AccountValidation("temporary_error", saved_cookies, "官方页面需要安全验证")
        refreshed = _browser_cookies(browser)
        status = classify_login_page(str(getattr(tab, "url", "")), _has_login_ui(tab), refreshed)
        return AccountValidation(
            status,
            refreshed if status == "valid" else saved_cookies,
            "官方页面确认已登录" if status == "valid" else "官方页面确认未登录",
        )
    except _BrowserError as exc:
        return AccountValidation("temporary_error", saved_cookies, exc.classification)
    except Exception:
        return AccountValidation("temporary_error", saved_cookies, "会话验证暂时失败，请稍后重试")
    finally:
        if browser is not None:
            _close_browser(browser)


def login_new_account(
    store_path: Path, timeout_seconds: int = 300, *,
    on_browser_ready: Callable[[int], None] | None = None,
    on_browser_closing: Callable[[], None] | None = None,
    cancel_event: threading.Event | None = None,
    profile_root: Path | None = None,
) -> dict[str, Any]:
    """Capture a complete login, announcing the browser before navigation.

    Cancellation is cooperative at browser-operation boundaries; cleanup is
    attempted for every launched browser before returning or raising a safe error.
    on_browser_closing announces intentional cleanup before quit removes the HWND;
    it does not mark login successful or disable explicit cancellation.
    """
    _raise_if_login_cancelled(cancel_event)
    profile_root = BROWSER_PROFILE_ROOT if profile_root is None else profile_root
    store = load_account_store(store_path)
    account_count_before = len(store["accounts"])
    local_port = _free_local_port()
    logger.info('正在启动独立登录浏览器 | classification=login_browser_starting')
    browser = launch_available_browser(
        headless=False,
        user_data_path=profile_root / str(local_port),
        timeout_seconds=timeout_seconds,
        local_port=local_port,
    )
    try:
        logger.info('登录浏览器已启动 | classification=login_browser_ready')
        _check_login_active(browser, cancel_event)
        if on_browser_ready is not None:
            try:
                on_browser_ready(browser.process_id)
            except _BrowserError:
                raise
            except Exception:
                raise _BrowserError("login_cancelled") from None
        _check_login_active(browser, cancel_event)
        try:
            clear_tiktok_cookies(browser)
            _check_login_active(browser, cancel_event)
            tab = browser.latest_tab
            loaded = tab.get(
                _LOGIN_URL,
                timeout=min(_BROWSER_OPERATION_TIMEOUT_SECONDS, max(1, timeout_seconds)),
            )
            _check_login_active(browser, cancel_event)
            if loaded is False:
                raise _BrowserError("login_page_unavailable")
        except _BrowserError:
            raise
        except Exception:
            _check_login_active(browser, cancel_event)
            raise _BrowserError("login_page_unavailable") from None
        _check_login_active(browser, cancel_event)
        cookies = wait_for_tiktok_login(
            browser, timeout_seconds, local_port=local_port, cancel_event=cancel_event,
        )
        logger.info('已确认完整登录状态，正在采集账号会话 | classification=login_detected')
        _check_login_active(browser, cancel_event)
        account_username, account_nickname = extract_current_account_identity(
            _page_source(browser.latest_tab)
        )
        login_user_agent = read_browser_user_agent(browser)
        ticket_guard_state = wait_for_ticket_guard_state(
            browser, timeout_seconds=min(10, max(1, timeout_seconds)),
            cancel_event=cancel_event, local_port=local_port,
        )
        _check_login_active(browser, cancel_event, local_port)
        logger.info('登录会话采集完成，{} 秒后关闭浏览器 | classification=login_capture_complete',
                    _LOGIN_SUCCESS_CLOSE_DELAY_SECONDS)
        _wait_for_login_poll(_LOGIN_SUCCESS_CLOSE_DELAY_SECONDS, cancel_event)
        _check_login_active(browser, cancel_event, local_port)
    finally:
        try:
            if on_browser_closing is not None:
                try:
                    on_browser_closing()
                except _BrowserError:
                    raise
                except Exception:
                    raise _BrowserError("login_cancelled") from None
        finally:
            closed = _close_browser(browser, local_port=local_port,
                                    profile=profile_root / str(local_port))
    if not closed:
        raise _BrowserError('browser_close_failed')
    _raise_if_login_cancelled(cancel_event)
    try:
        account = upsert_account(store, cookies, datetime.now(timezone.utc))
    except ValueError:
        raise _BrowserError("login_identity_unavailable") from None
    account["user_agent"] = login_user_agent or DEFAULT_BROWSER_PROFILE.user_agent
    clean_username = re.sub(
        r"[^A-Za-z0-9._-]", "", str(account_username or "").strip().lstrip("@")
    )[:64]
    clean_nickname = re.sub(r"\s+", " ", str(account_nickname or "")).strip()[:200]
    if clean_username:
        account["username"] = clean_username
    if clean_nickname:
        account["nickname"] = clean_nickname
    if ticket_guard_state:
        account["ticket_guard"] = ticket_guard_state
        try:
            account["ticket_guard_headers"] = build_headers_from_cookie_state(
                cookie_header_from_playwright(cookies),
                ticket_guard_state,
                urlsplit(CHAT_URL).path,
            )
        except Exception:
            account.pop("ticket_guard_headers", None)
            logger.warning(
                "Ticket Guard 请求头生成失败，未写入缓存 | "
                "classification=ticket_guard_headers_unavailable"
            )
    else:
        account.pop("ticket_guard", None)
        account.pop("ticket_guard_headers", None)
        logger.warning(
            "未读取到动态 Ticket Guard 会话，消息发送将停止；请重新登录 | "
            "classification=ticket_guard_state_missing"
        )
    _raise_if_login_cancelled(cancel_event)
    if cancel_event is None:
        save_account_store_atomic(store_path, store)
    else:
        save_account_store_atomic(
            store_path, store, before_replace=lambda: _raise_if_login_cancelled(cancel_event),
        )
    short_id = account["id"][:12]
    if len(store["accounts"]) > account_count_before:
        logger.success(
            "新增用户成功 | account_id={} | classification=account_added",
            short_id,
        )
    else:
        logger.info(
            "用户重复，未写入新账户，已覆盖原 Cookie | duplicate_account_id={} | classification=account_duplicate",
            short_id,
        )
    return account
