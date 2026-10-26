"""Versioned local account store and identity matching."""

import hashlib
import json
import os
import re
import tempfile
from copy import deepcopy
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable
from urllib.parse import unquote

from .errors import AccountFileError as _AccountFileError
from .errors import AccountStoreError as _AccountStoreError

def _cookie_records_from_header(cookie_header: str) -> list[dict[str, Any]]:
    records = []
    for part in str(cookie_header or "").split(";"):
        item = part.strip()
        if not item or "=" not in item:
            continue
        name, value = item.split("=", 1)
        name = name.strip()
        if name:
            records.append({
                "name": name, "value": value.strip(),
                "domain": ".tiktok.com", "path": "/",
            })
    return records


def _decode_persisted_store(store: Any) -> Any:
    if not isinstance(store, dict) or not isinstance(store.get("accounts"), list):
        return store
    for account in store["accounts"]:
        if not isinstance(account, dict) or "cookie" not in account:
            continue
        cookie_header = account.get("cookie")
        if not isinstance(cookie_header, str):
            continue
        account["cookies"] = _cookie_records_from_header(cookie_header)
        account.pop("cookie", None)
    return store


def _encode_persisted_store(store: dict[str, Any]) -> dict[str, Any]:
    persisted = deepcopy(store)
    for account in persisted["accounts"]:
        account["cookie"] = cookie_header_from_playwright(account.pop("cookies"))
    return persisted


def _validate_account_store(store: Any) -> None:
    if (not isinstance(store, dict) or type(store.get("version")) is not int
            or store["version"] != 1 or not isinstance(store.get("accounts"), list)):
        raise _AccountStoreError("account_store_corrupt")
    seen = set()
    for account in store["accounts"]:
        if (not isinstance(account, dict) or not isinstance(account.get("id"), str)
                or not account["id"] or account["id"] in seen
                or not isinstance(account.get("cookies"), list)
                or any(not isinstance(cookie, dict) for cookie in account["cookies"])):
            raise _AccountStoreError("account_store_corrupt")
        seen.add(account["id"])


def load_account_store(path: Path) -> dict[str, Any]:
    try:
        raw = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return {"version": 1, "accounts": []}
    except UnicodeError:
        raise _AccountStoreError("account_store_corrupt") from None
    except OSError:
        raise _AccountFileError("account_store_unreadable") from None
    if not raw.strip():
        return {"version": 1, "accounts": []}
    try:
        store = json.loads(raw)
    except (ValueError, UnicodeError):
        raise _AccountStoreError("account_store_corrupt") from None
    store = _decode_persisted_store(store)
    _validate_account_store(store)
    return store


def save_account_store_atomic(
    path: Path, store: dict[str, Any], *, before_replace: Callable[[], None] | None = None,
) -> None:
    """Stage and sync the store, then optionally guard its atomic commit.

    A guard that raises aborts replacement; the staging file is cleaned up and
    the previous account file remains intact. Ordinary callers need no guard.
    """
    _validate_account_store(store)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=path.parent,
                                         prefix=f".{path.name}.", suffix=".tmp",
                                         delete=False) as handle:
            temporary = Path(handle.name)
            json.dump(_encode_persisted_store(store), handle, ensure_ascii=False, indent=2)
            handle.flush()
            os.fsync(handle.fileno())
        if before_replace is not None:
            before_replace()
        os.replace(temporary, path)
    except (OSError, ValueError, TypeError):
        raise _AccountFileError("account_save_failed") from None
    finally:
        if temporary is not None:
            try:
                temporary.unlink(missing_ok=True)
            except OSError:
                pass


def _is_tiktok_cookie(cookie: dict[str, Any]) -> bool:
    domain = str(cookie.get("domain", "")).lower().lstrip(".")
    return domain == "tiktok.com" or domain.endswith(".tiktok.com")


def _numeric_tiktok_uids(cookies: list[dict[str, Any]]) -> set[str]:
    """Extract numeric account IDs carried by TikTok multi_sids."""
    result = set()
    for cookie in cookies:
        value = cookie.get("value")
        if (not _is_tiktok_cookie(cookie) or cookie.get("name") != "multi_sids"
                or not isinstance(value, str) or not value):
            continue
        decoded = unquote(value)
        result.update(re.findall(r"(?:^|,)\s*(\d{5,30}):", decoded))
    return result


def account_id_from_cookies(cookies: list[dict[str, Any]]) -> str:
    numeric_uids = _numeric_tiktok_uids(cookies)
    if len(numeric_uids) == 1:
        value = next(iter(numeric_uids))
        return hashlib.sha256(value.encode("utf-8")).hexdigest()
    for name in ("uid_tt", "sessionid", "sessionid_ss", "sid_tt"):
        for cookie in cookies:
            value = cookie.get("value")
            if cookie.get("name") == name and isinstance(value, str) and value:
                return hashlib.sha256(value.encode("utf-8")).hexdigest()
    raise ValueError("No stable TikTok account identifier")


def environment_seed_for_account_id(account_id: str) -> str:
    """Return a stable, non-secret seed for one saved account environment."""
    return hashlib.sha256(f"tiktok-browser-env:{account_id}".encode("utf-8")).hexdigest()[:16]


def _ensure_environment_seeds(store: dict[str, Any]) -> bool:
    changed = False
    for account in store["accounts"]:
        seed = account.get("environment_seed")
        if not isinstance(seed, str) or not re.fullmatch(r"[a-f0-9]{16}", seed):
            account["environment_seed"] = environment_seed_for_account_id(account["id"])
            changed = True
    return changed


def _account_identity(cookies: list[dict[str, Any]]) -> set[tuple[str, str]]:
    # A numeric TikTok UID is stable across logins. Session tokens confirm only
    # a current overlap and remain a compatibility fallback for legacy records.
    identity = {("tiktok_uid", value) for value in _numeric_tiktok_uids(cookies)}
    identity.update({(cookie["name"], cookie["value"]) for cookie in cookies
                     if _is_tiktok_cookie(cookie)
                     and cookie.get("name") in {
                         "uid_tt", "sessionid", "sessionid_ss", "sid_tt"
                     }
                     and isinstance(cookie.get("value"), str) and cookie["value"]})
    return identity


def _identity_is_ambiguous(identity: set[tuple[str, str]]) -> bool:
    numeric_uids = {value for name, value in identity if name == "tiktok_uid"}
    if len(numeric_uids) > 1:
        return True
    if numeric_uids:
        return False
    uid_tokens = {value for name, value in identity if name == "uid_tt"}
    return len(uid_tokens) > 1


def _identities_match(left: set[tuple[str, str]], right: set[tuple[str, str]]) -> bool:
    left_numeric = {value for name, value in left if name == "tiktok_uid"}
    right_numeric = {value for name, value in right if name == "tiktok_uid"}
    if left_numeric and right_numeric:
        return bool(left_numeric & right_numeric)
    left_uid = {value for name, value in left if name == "uid_tt"}
    right_uid = {value for name, value in right if name == "uid_tt"}
    # Legacy records without numeric UID may still match an overlapping session,
    # but conflicting uid_tt values are never joined.
    if len(left_uid | right_uid) > 1:
        return False
    return bool(left & right)

def _record_time(account: dict[str, Any], key: str) -> float:
    try:
        value = datetime.fromisoformat(account[key])
        if value.tzinfo is None:
            value = value.replace(tzinfo=timezone.utc)
        return value.timestamp()
    except (KeyError, TypeError, ValueError, OverflowError):
        return float("-inf")


def _deduplicate_accounts(store: dict[str, Any]) -> bool:
    """Merge confirmed current identities, preserving a historical record ID."""
    groups = []
    for account in store["accounts"]:
        identity = _account_identity(account["cookies"])
        matches = [group for group in groups if _identities_match(group[1], identity)]
        combined = identity | set().union(*(group[1] for group in matches))
        if matches and not _identity_is_ambiguous(combined):
            group = matches[0]
            group[0].append(account)
            group[1].update(identity)
            for other in matches[1:]:
                group[0].extend(other[0])
                group[1].update(other[1])
                groups.remove(other)
        else:
            groups.append([[account], identity])
    changed = len(groups) != len(store["accounts"])
    merged = []
    for records, identity in groups:
        retained = records[0]
        if len(records) > 1:
            latest = max(records, key=lambda record: _record_time(record, "updated_at"))
            created = [record for record in records if _record_time(record, "created_at") != float("-inf")]
            if created:
                retained["created_at"] = min(created, key=lambda record: _record_time(record, "created_at"))["created_at"]
            retained.update(cookies=deepcopy(latest["cookies"]))
            if "updated_at" in latest:
                retained["updated_at"] = latest["updated_at"]
        merged.append(retained)
    store["accounts"] = merged
    return changed


def upsert_account(store: dict[str, Any], cookies: list[dict[str, Any]], now: datetime) -> dict[str, Any]:
    _validate_account_store(store)
    account_id = account_id_from_cookies(cookies)
    timestamp = now.isoformat()
    identity = _account_identity(cookies)
    matches = [item for item in store["accounts"]
               if _identities_match(_account_identity(item["cookies"]), identity)]
    combined = identity | set().union(*(_account_identity(item["cookies"]) for item in matches))
    if _identity_is_ambiguous(combined):
        raise ValueError("Ambiguous account identity")
    account = matches[0] if matches else None
    if account is None:
        # Older versions intentionally retained a record's historical ID when
        # its cookies were refreshed. That can leave an ID derived from a
        # previous identity attached to different current cookies and later
        # collide with a legitimately new account. Repair only provably stale
        # IDs before deciding whether the new identity is a duplicate.
        collisions = [item for item in store["accounts"] if item["id"] == account_id]
        for collision in collisions:
            replacement_id = account_id_from_cookies(collision["cookies"])
            if replacement_id != account_id and not any(
                item is not collision and item["id"] == replacement_id
                for item in store["accounts"]
            ):
                collision["id"] = replacement_id
        if any(item["id"] == account_id for item in store["accounts"]):
            # A legacy ID alone is insufficient evidence to overwrite a record.
            raise ValueError("Unconfirmed account identity")
        account = {"id": account_id, "created_at": timestamp}
        store["accounts"].append(account)
    if matches:
        created = [item for item in matches if _record_time(item, "created_at") != float("-inf")]
        if created:
            account["created_at"] = min(created, key=lambda item: _record_time(item, "created_at"))["created_at"]
        store["accounts"] = [item for item in store["accounts"]
                             if item is account or all(item is not match for match in matches)]
    account.update(cookies=deepcopy(cookies), updated_at=timestamp)
    if not isinstance(account.get("environment_seed"), str):
        account["environment_seed"] = environment_seed_for_account_id(account["id"])
    return account


def remove_account(store: dict[str, Any], account_id: str) -> bool:
    _validate_account_store(store)
    for index, account in enumerate(store["accounts"]):
        if account["id"] == account_id:
            del store["accounts"][index]
            return True
    return False


def cookie_header_from_playwright(cookies: list[dict[str, Any]]) -> str:
    return "; ".join(f"{cookie['name']}={cookie['value']}" for cookie in cookies
                     if _is_tiktok_cookie(cookie) and isinstance(cookie.get("name"), str)
                     and cookie["name"] and isinstance(cookie.get("value"), str))

