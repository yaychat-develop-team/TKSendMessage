"""Bind live-page IDs and status to one host, never a recommended room."""
import json
import re


def room_state_flags(*values):
    statuses = [value for value in values if value is not None]
    if any(type(value) is not int for value in statuses):
        raise ValueError("Invalid room status")
    if 4 in statuses:
        return 4, False
    if statuses and all(value == 2 for value in statuses):
        return 2, True
    return None, None


def extract_bound_room_state(html, username):
    """None means legacy text; an empty result means no matching hydrated host."""
    texts = re.findall(
        r'<script\b[^>]*\bid\s*=\s*["\'](?:SIGI_STATE|__UNIVERSAL_DATA_FOR_REHYDRATION__)["\'][^>]*>(.*?)</script>',
        html, re.I | re.S)
    hydrated = bool(texts)
    if not texts:
        texts = [html]
    pending = []
    for text in texts:
        try:
            pending.append((json.loads(text), 0))
        except (TypeError, ValueError, RecursionError):
            continue
    found_container = False
    visited = 0
    while pending and visited < 20000:
        value, depth = pending.pop()
        visited += 1
        if depth > 30:
            continue
        if isinstance(value, list):
            pending.extend((child, depth + 1) for child in reversed(value) if isinstance(child, (dict, list)))
            continue
        if not isinstance(value, dict):
            continue
        for key in ("liveRoomUserInfo", "live_room_user_info"):
            container = value.get(key)
            if not isinstance(container, dict):
                continue
            found_container = True
            user = container.get("user")
            if not isinstance(user, dict):
                continue
            owner = user.get("uniqueId") or user.get("unique_id")
            if not isinstance(owner, str) or owner.casefold() != username.casefold():
                continue
            live = container.get("liveRoom") or {}
            if not isinstance(live, dict):
                raise ValueError("Invalid live-room state")
            room_id = (user.get("roomId") or user.get("room_id") or live.get("id")
                       or value.get("roomId") or value.get("room_id"))
            if (type(room_id) not in (str, int) or not re.fullmatch(r"[0-9]{1,20}", str(room_id))
                    or not 0 < int(room_id) < 2**64):
                room_id = None
            status, is_live = room_state_flags(user.get("status"), live.get("status"))
            return {"roomId": str(room_id) if room_id is not None else None,
                    "username": owner, "nickname": user.get("nickname") or user.get("nickName") or "-",
                    "liveStatus": status, "isLive": is_live}
        pending.extend((child, depth + 1) for child in reversed(list(value.values()))
                       if isinstance(child, (dict, list)))
    if hydrated or found_container:
        return {"roomId": None, "username": username, "nickname": "-", "liveStatus": None, "isLive": None}
    return None
