"""Paths and immutable browser/request profile constants."""

from dataclasses import dataclass
import re
from pathlib import Path

from settings import (
    ACCOUNT_REQUEST_INTERVAL_SECONDS as ACCOUNT_REQUEST_INTERVAL_SECONDS,
    ADD_NEW_ACCOUNT as ADD_NEW_ACCOUNT,
    LOGIN_TIMEOUT_SECONDS as LOGIN_TIMEOUT_SECONDS,
    MANUAL_INPUT as MANUAL_INPUT,
    MESSAGE_OBSERVATION_ENABLED as MESSAGE_OBSERVATION_ENABLED,
    MESSAGE_OBSERVATION_TIMEOUT_SECONDS as MESSAGE_OBSERVATION_TIMEOUT_SECONDS,
    MESSAGE_NOT_OBSERVED_RETRIES as MESSAGE_NOT_OBSERVED_RETRIES,
    MESSAGE_NOT_OBSERVED_RETRY_DELAY_SECONDS as MESSAGE_NOT_OBSERVED_RETRY_DELAY_SECONDS,
    MESSAGE_OBSERVER_CONNECT_TIMEOUT_SECONDS as MESSAGE_OBSERVER_CONNECT_TIMEOUT_SECONDS,
    MESSAGE_OBSERVER_CONNECT_RETRIES as MESSAGE_OBSERVER_CONNECT_RETRIES,
    MESSAGE_OBSERVER_RETRY_DELAY_SECONDS as MESSAGE_OBSERVER_RETRY_DELAY_SECONDS,
    SHOW_LIVE_COMMENTS_IN_CONSOLE as SHOW_LIVE_COMMENTS_IN_CONSOLE,
    ROOM_ID_COOKIE as ROOM_ID_COOKIE,
)

PROJECT_ROOT = Path(__file__).resolve().parent.parent
ACCOUNT_STORE_PATH = PROJECT_ROOT / "tiktok_accounts.json"
BROWSER_ENV_PATH = Path(__file__).resolve().parent / "algorithms" / "browser_env.json"
BROWSER_PROFILE_ROOT = PROJECT_ROOT / "port"
REPORT_ROOT = PROJECT_ROOT


@dataclass(frozen=True)
class BusinessPaths:
    """Explicit desktop paths with the historical command-line defaults."""

    account_store: Path = ACCOUNT_STORE_PATH
    profile_root: Path = BROWSER_PROFILE_ROOT
    report_dir: Path = REPORT_ROOT

@dataclass(frozen=True)
class BrowserProfile:
    user_agent: str
    sec_ch_ua: str
    impersonate: str

    @property
    def browser_version(self) -> str:
        return self.user_agent.removeprefix("Mozilla/")


def _chrome_profile(major: int, impersonate_major: int | None = None) -> BrowserProfile:
    return BrowserProfile(
        user_agent=(
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            f"Chrome/{major}.0.0.0 Safari/537.36"
        ),
        sec_ch_ua=(
            f'"Chromium";v="{major}", "Not:A-Brand";v="99"'
        ),
        impersonate=f"chrome{impersonate_major or major}",
    )


CHROME_BROWSER_PROFILES = tuple(
    _chrome_profile(major, impersonate_major)
    for major, impersonate_major in (
        (120, None), (123, None), (124, None), (131, None),
        (136, None), (145, None), (146, 145),
    )
)
DEFAULT_BROWSER_PROFILE = CHROME_BROWSER_PROFILES[-1]


def strict_browser_profile_from_user_agent(user_agent: str) -> BrowserProfile:
    """Select a transport whose Chrome major matches the signed UA exactly."""
    value = str(user_agent or "").strip()
    match = re.search(r"\bChrome/(\d+)\.", value)
    if not value.startswith("Mozilla/") or not match:
        raise ValueError("unsupported_browser_profile")
    major = int(match.group(1))
    for profile in CHROME_BROWSER_PROFILES:
        if profile.impersonate == f"chrome{major}":
            return BrowserProfile(
                user_agent=value,
                sec_ch_ua=profile.sec_ch_ua,
                impersonate=profile.impersonate,
            )
    raise ValueError("unsupported_browser_profile")


def browser_profile_from_user_agent(user_agent: str | None) -> BrowserProfile:
    """Build a stable request profile from the UA captured during account login."""
    value = str(user_agent or "").strip() or DEFAULT_BROWSER_PROFILE.user_agent
    match = re.search(r"Chrome/(\d+)", value)
    if not match:
        return BrowserProfile(
            user_agent=value,
            sec_ch_ua=DEFAULT_BROWSER_PROFILE.sec_ch_ua,
            impersonate=DEFAULT_BROWSER_PROFILE.impersonate,
        )
    major = int(match.group(1))
    transport = min(
        CHROME_BROWSER_PROFILES,
        key=lambda item: abs(
            int(re.search(r"Chrome/(\d+)", item.user_agent).group(1)) - major
        ),
    )
    return BrowserProfile(
        user_agent=value,
        sec_ch_ua=(
            f'"Chromium";v="{major}", "Not:A-Brand";v="99"'
        ),
        impersonate=transport.impersonate,
    )


USER_AGENT = DEFAULT_BROWSER_PROFILE.user_agent
SEC_CH_UA = DEFAULT_BROWSER_PROFILE.sec_ch_ua
IMPERSONATE = DEFAULT_BROWSER_PROFILE.impersonate
MS_TOKEN_URL = "https://mssdk-sg.tiktok.com/web/report"
CHAT_URL = "https://webcast.tiktok.com/webcast/room/chat/"
ENTER_URL = "https://webcast.tiktok.com/webcast/room/enter/"
RISK_KEYS = {"msToken", "X-Dynosaur", "X-Bogus", "X-Gnarly"}


