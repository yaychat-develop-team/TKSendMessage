"""Safely replace one saved account after manual review and editing."""

from copy import deepcopy
from pathlib import Path

from tiktok_live.account_store import (
    _account_identity, _numeric_tiktok_uids, cookie_header_from_playwright,
    load_account_store, save_account_store_atomic,
)


class AccountEditError(Exception):
    """A fixed, user-facing edit failure without account contents."""


def account_for_details(account: dict) -> dict:
    shown = deepcopy(account)
    shown['cookie'] = cookie_header_from_playwright(shown.pop('cookies'))
    return shown


def account_from_details(original: dict, edited: object) -> dict:
    if not isinstance(edited, dict) or not isinstance(edited.get('cookie'), str):
        raise AccountEditError('Cookie 必须是网页格式的字符串，未保存。')
    header = edited['cookie']
    if not header.strip() or '\r' in header or '\n' in header:
        raise AccountEditError('Cookie 字符串格式不正确，未保存。')
    available = {}
    for cookie in original['cookies']:
        available.setdefault(cookie['name'], []).append(cookie)
    cookies = []
    for part in header.split(';'):
        part = part.strip()
        if not part or '=' not in part:
            raise AccountEditError('Cookie 字符串格式不正确，未保存。')
        name, value = part.split('=', 1)
        name = name.strip()
        if not name or any(char.isspace() for char in name):
            raise AccountEditError('Cookie 字符串格式不正确，未保存。')
        prior = available.get(name, [])
        cookie = deepcopy(prior.pop(0)) if prior else {
            'name': name, 'domain': '.tiktok.com', 'path': '/',
        }
        cookie['value'] = value.strip()
        cookies.append(cookie)
    result = deepcopy(edited)
    result.pop('cookie')
    result['cookies'] = cookies
    return result


def _validate_edited_account(original: dict, edited: object) -> None:
    if not isinstance(edited, dict) or edited.get('id') != original.get('id'):
        raise AccountEditError('账号 ID 不可修改。')
    cookies = edited.get('cookies')
    if (not isinstance(cookies, list) or not cookies or any(
            not isinstance(cookie, dict)
            or not isinstance(cookie.get('name'), str) or not cookie['name']
            or not isinstance(cookie.get('value'), str)
            or not isinstance(cookie.get('domain'), str) or not cookie['domain']
            or ('path' in cookie and not isinstance(cookie['path'], str))
            for cookie in cookies)):
        raise AccountEditError('Cookie 列表格式不正确，未保存。')
    if not _account_identity(cookies):
        raise AccountEditError('缺少有效的 TikTok 登录 Cookie，未保存。')
    for field in ('username', 'nickname', 'user_agent'):
        if field in edited and not isinstance(edited[field], str):
            raise AccountEditError('用户名、昵称和浏览器标识必须是文字。')
    for field in ('ticket_guard', 'ticket_guard_headers'):
        if field in edited and not isinstance(edited[field], dict):
            raise AccountEditError('登录会话字段格式不正确，未保存。')
    old_uids = _numeric_tiktok_uids(original['cookies'])
    new_uids = _numeric_tiktok_uids(cookies)
    if old_uids and new_uids and old_uids.isdisjoint(new_uids):
        raise AccountEditError('Cookie 属于不同的 TikTok 账号，未保存。')


def save_edited_account(path: Path, original: dict, edited: object) -> None:
    """Preserve other accounts and reject stale dialog contents atomically."""
    _validate_edited_account(original, edited)
    path = Path(path)
    try:
        before = path.read_bytes()
        store = load_account_store(path)
        if path.read_bytes() != before:
            raise AccountEditError('账号数据已变化，请重新打开详情后再编辑。')
    except OSError:
        raise AccountEditError('账号文件无法读取，未保存。') from None
    for index, current in enumerate(store['accounts']):
        if current['id'] == original['id']:
            if current != original:
                raise AccountEditError('账号数据已变化，请重新打开详情后再编辑。')
            store['accounts'][index] = deepcopy(edited)
            break
    else:
        raise AccountEditError('账号已不存在，未保存。')

    def guard() -> None:
        try:
            if path.read_bytes() != before:
                raise AccountEditError('账号数据已变化，请重新打开详情后再编辑。')
        except OSError:
            raise AccountEditError('账号文件无法读取，未保存。') from None

    save_account_store_atomic(path, store, before_replace=guard)
