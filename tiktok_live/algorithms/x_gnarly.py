"""
X-Gnarly 相关算法模块。
"""

from __future__ import annotations

import base64
import hashlib
import random
import secrets
import struct
import time
from typing import Any
from urllib.parse import urlencode, urlsplit, urlunsplit

__all__ = [
    "get_x_gnarly",
    "build_x_gnarly_payload",
    "generate_x_gnarly",
]

GNARLY_ALPHABET = "u09tbS3UvgDEe6r-ZVMXzLpsAohTn7mdINQlW412GqBjfYiyk8JORCF5/xKHwacP"
SCM_VERSION = "1.0.0.382"
SDK_VERSION = "5.3.0"
RISK_KEYS = {"msToken", "X-Dynosaur", "X-Bogus", "X-Gnarly"}
OUTPUT_SIGN_KEYS = {"X-Bogus", "X-Gnarly"}

CHACHA_CONST = [1196819126, 600974999, 3863347763, 1451689750]
RANDOM_STATE_PREFIX = [
    2517678443,
    2718276124,
    3212677781,
    2633865432,
    217618912,
    2931180889,
    1498001188,
    2157053261,
    211147047,
    185100057,
    2903579748,
    3732962506,
]

_GNARLY_DEFAULT_ORDER = [12, 16, 5, 6, 10, 7, 1, 3, 2, 4, 15, 0, 8, 14, 9, 13, 11]
_GNARLY_SESSION_CONTEXT = {
    "field8": 1881297510,
    "field12": 72,
    "field13": 12,
    "order": _GNARLY_DEFAULT_ORDER,
}

_GNARLY_REPORT_CONTEXT = {
    "field8": 1881297510,
    "field12": 70,
    "field13": 11,
    "order": _GNARLY_DEFAULT_ORDER,
}

_GNARLY_WEBCAST_ROOM_ENTER_CONTEXT = {
    "field2": 14,
    "field8": 2072048325,
    "field12": 2,
    "field13": 1,
    "order": [1, 11, 6, 2, 8, 16, 7, 3, 13, 12, 0, 4, 5, 14, 9, 15, 10],
}

def _u32(value: int) -> int:
    return value & 0xFFFFFFFF


def _rotl32(value: int, bits: int) -> int:
    value &= 0xFFFFFFFF
    return ((value << bits) | (value >> (32 - bits))) & 0xFFFFFFFF


def _chacha_quarter(state: list[int], a: int, b: int, c: int, d: int) -> None:
    state[a] = _u32(state[a] + state[b])
    state[d] = _rotl32(state[d] ^ state[a], 16)
    state[c] = _u32(state[c] + state[d])
    state[b] = _rotl32(state[b] ^ state[c], 12)
    state[a] = _u32(state[a] + state[b])
    state[d] = _rotl32(state[d] ^ state[a], 8)
    state[c] = _u32(state[c] + state[d])
    state[b] = _rotl32(state[b] ^ state[c], 7)


def _chacha_block(state: list[int], rounds: int) -> list[int]:
    work = [x & 0xFFFFFFFF for x in state]
    step = 0
    while step < rounds:
        _chacha_quarter(work, 0, 4, 8, 12)
        _chacha_quarter(work, 1, 5, 9, 13)
        _chacha_quarter(work, 2, 6, 10, 14)
        _chacha_quarter(work, 3, 7, 11, 15)
        step += 1
        if step >= rounds:
            break
        _chacha_quarter(work, 0, 5, 10, 15)
        _chacha_quarter(work, 1, 6, 11, 12)
        _chacha_quarter(work, 2, 7, 12, 13)
        _chacha_quarter(work, 3, 4, 13, 14)
        step += 1
    return [_u32(work[i] + state[i]) for i in range(16)]


def _increment_counter(state: list[int]) -> list[int]:
    state[12] = _u32(state[12] + 1)
    return state


def _random_word_source(now_ms: int | None = None) -> tuple[list[int], bytes, int]:
    millis = int(time.time() * 1000) if now_ms is None else now_ms
    state = RANDOM_STATE_PREFIX + [
        millis & 0xFFFFFFFF,
        secrets.randbits(32),
        secrets.randbits(32),
        secrets.randbits(32),
    ]
    index = 0
    key_words: list[int] = []
    rand_bytes = bytearray()
    rounds = 0

    for _ in range(12):
        block = _chacha_block(state, 8)
        low = block[index]
        high = (0xFFFFF800 & block[index + 8]) >> 11
        random_float = (low + 4294967296 * high) / 9007199254740992
        word = int(random_float * 4294967296) & 0xFFFFFFFF
        key_words.append(word)
        rand_bytes.extend(word.to_bytes(4, "little"))
        rounds = (rounds + (word & 0x0F)) & 0x0F
        if index == 7:
            state = _increment_counter(state)
            index = 0
        else:
            index += 1
    return key_words, bytes(rand_bytes), rounds + 5


def _chacha_xor_gnarly(data: bytes, key_words: list[int], rounds: int = 15) -> bytes:
    state = CHACHA_CONST + [word & 0xFFFFFFFF for word in key_words]
    out = bytearray(data)
    offset = 0
    while offset < len(out):
        block = _chacha_block(state, rounds)
        stream = b"".join(struct.pack("<I", word) for word in block)
        size = min(64, len(out) - offset)
        for index in range(size):
            out[offset + index] ^= stream[index]
        state[12] = _u32(state[12] + 1)
        offset += size
    return bytes(out)


def _custom_b64(data: bytes, alphabet: str = GNARLY_ALPHABET) -> str:
    value = base64.b64encode(data).decode("ascii").rstrip("=")
    return value.translate(str.maketrans("ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/", alphabet))


def _custom_b64_decode(value: str, alphabet: str = GNARLY_ALPHABET) -> bytes:
    text = value.translate(str.maketrans(alphabet, "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/"))
    text += "=" * ((4 - len(text) % 4) % 4)
    return base64.b64decode(text)


def _extract_query_value_raw(plaintext: str, key: str) -> str:
    parts = urlsplit(plaintext)
    query = parts.query if parts.scheme and parts.netloc else plaintext.lstrip("?")
    for part in query.split("&"):
        if _query_key(part) == key:
            return part.split("=", 1)[1] if "=" in part else ""
    return ""


def _md5_hex(value: str | bytes) -> str:
    data = value if isinstance(value, bytes) else value.encode("utf-8")
    return hashlib.md5(data).hexdigest()


def _query_key(part: str) -> str:
    return part.split("=", 1)[0]


def _query_has_key(query: str, key: str) -> bool:
    return any(_query_key(part) == key for part in query.split("&") if part)


def _append_query_raw(query: str, key: str, value: str) -> str:
    return query + ("&" if query else "") + f"{key}={value}"


def _clean_query_raw(query: str, blocked_keys: set[str]) -> str:
    if not query:
        return ""
    return "&".join(part for part in query.split("&") if part and _query_key(part) not in blocked_keys)


def _sign_plaintext(value: str, keep_ms_token: bool = False) -> tuple[str, str]:
    blocked_keys = OUTPUT_SIGN_KEYS if keep_ms_token else RISK_KEYS
    parts = urlsplit(value)
    if parts.scheme and parts.netloc:
        clean_query = _clean_query_raw(parts.query, blocked_keys)
        unsigned_url = urlunsplit((parts.scheme, parts.netloc, parts.path, clean_query, parts.fragment))
        return unsigned_url, clean_query

    query = value[1:] if value.startswith("?") else value
    clean_query = _clean_query_raw(query, blocked_keys)
    return clean_query, clean_query


def _post_plaintext(value: Any = "") -> str | bytes | None:
    if value is None:
        return None
    if isinstance(value, bytes):
        return value
    if isinstance(value, str):
        return value
    if isinstance(value, dict):
        return urlencode(value, doseq=True)
    if isinstance(value, (list, tuple)):
        return urlencode(value, doseq=True)
    return str(value)


def _query_with_ms_token(plaintext: str, ms_token: str) -> str:
    _, query = _sign_plaintext(plaintext, keep_ms_token=True)
    if ms_token and not _query_has_key(query, "msToken"):
        query = _append_query_raw(query, "msToken", ms_token)
    return query


def _gnarly_field(tag: int, value: int | str) -> bytes:
    if isinstance(value, str):
        data = value.encode("utf-8")
    elif 0 <= value <= 0xFFFF:
        data = struct.pack(">H", value)
    else:
        data = struct.pack(">I", value & 0xFFFFFFFF)
    return bytes([tag, 0, len(data)]) + data


def _gnarly_string_word(value: str) -> int:
    """S(50145)：字符串只取前 4 个字符，按大端拼成一个 u32。"""
    word = 0
    for char in value[:4]:
        word = ((word << 8) | (ord(char) & 0xFF)) & 0xFFFFFFFF
    return word


def _gnarly_xor_values(values: list[int | str], include_strings: bool) -> int:
    """
    S(50145)：X-Gnarly 内部的字段折叠函数。

    seed=1 时只折叠数字；seed=2 时数字和字符串一起折叠。
    首位是字符串时不会参与初始值，这个行为和 VM 一致。
    """
    if not values:
        return 0

    first = values[0]
    result = int(first) & 0xFFFFFFFF if isinstance(first, int) else 0
    for value in values[1:]:
        if isinstance(value, int):
            result ^= value & 0xFFFFFFFF
        elif include_strings and isinstance(value, str):
            result ^= _gnarly_string_word(value)
    return result & 0xFFFFFFFF


def _gnarly_field8(seed: int | None = None) -> int:
    """VM 全局槽 978。浏览器里是页面级 session 值。"""
    if seed is None:
        return int(abs((1000 * (int(time.time() * 1000) + random.random() + random.random())) % 2147483648))
    return seed & 0xFFFFFFFF


def _gnarly_field14(ts: int, field8: int) -> int:
    """S(46627)：0x10000 | ts 高低 16 位与 field8 高低 16 位异或。"""
    return (
        0x10000
        | ((ts & 0xFFFF) ^ ((ts >> 16) & 0xFFFF) ^ (field8 & 0xFFFF) ^ ((field8 >> 16) & 0xFFFF))
    ) & 0xFFFFFFFF


def _gnarly_field15(ts: int, perf_data: int, field8: int) -> int:
    """field15：高 16 位来自 perf_data，低 16 位来自 timestamp，二者都和 field8 混合。"""
    high = ((perf_data & 0xFFFF) ^ ((field8 >> 16) & 0xFFFF)) & 0xFFFF
    low = ((ts & 0xFFFF) ^ (field8 & 0xFFFF)) & 0xFFFF
    return ((high << 16) | low) & 0xFFFFFFFF


def build_x_gnarly_payload(
    query: str,
    user_agent: str,
    ts: int | None = None,
    body: str | bytes | None = None,
    field8_seed: int | None = None,
    cfg: dict[str, Any] | None = None,
    field_order: list[int] | None = None,
) -> bytes:
    """
    webmssdk 1.0.0.382 的 X-Gnarly 明文 TLV。

    字段顺序必须和 VM 一致，服务端会校验顺序和值。
    """
    ts = int(time.time()) if ts is None else ts
    perf_data = int((cfg or {}).get("perf_data", 596860301))
    sdk_version = str((cfg or {}).get("sdk_version", SDK_VERSION))
    scm_version = str((cfg or {}).get("scm_version", SCM_VERSION))
    field8 = _gnarly_field8(field8_seed)
    field2 = int((cfg or {}).get("field2", 8))
    field12 = int((cfg or {}).get("field12", 72))
    field13 = int((cfg or {}).get("field13", 12))
    field14 = _gnarly_field14(ts, field8)
    field15 = _gnarly_field15(ts, perf_data, field8)
    body_hash = _md5_hex(body or b"")

    values: list[int | str] = [
        0,
        1,
        field2,
        _md5_hex(query),
        body_hash,
        _md5_hex(user_agent),
        ts,
        perf_data,
        field8,
        sdk_version,
        scm_version,
        1,
        field12,
        field13,
        field14,
        field15,
    ]
    field16 = _gnarly_xor_values(values, include_strings=True)
    values.append(field16)
    field0 = _gnarly_xor_values(values, include_strings=False)

    field_map: dict[int, int | str] = {
        0: field0,
        1: 1,
        2: field2,
        3: _md5_hex(query),
        4: body_hash,
        5: _md5_hex(user_agent),
        6: ts,
        7: perf_data,
        8: field8,
        9: sdk_version,
        10: scm_version,
        11: 1,
        12: field12,
        13: field13,
        14: field14,
        15: field15,
        16: field16,
    }

    order = field_order or _GNARLY_DEFAULT_ORDER
    fields = [(tag, field_map[tag]) for tag in order]
    return bytes([len(fields)]) + b"".join(_gnarly_field(tag, value) for tag, value in fields)


def _parse_x_gnarly_payload(data: bytes) -> dict[str, Any]:
    pos = 1
    order: list[int] = []
    fields: dict[int, int | str] = {}
    if not data or data[0] != 17:
        raise ValueError("X-Gnarly payload field count mismatch")

    for _ in range(data[0]):
        if pos + 3 > len(data):
            raise ValueError("X-Gnarly payload truncated")
        tag, zero, size = data[pos], data[pos + 1], data[pos + 2]
        pos += 3
        raw = data[pos : pos + size]
        pos += size
        if zero != 0 or not raw:
            raise ValueError("X-Gnarly payload field malformed")
        if size == 2 or size == 4:
            value: int | str = int.from_bytes(raw, "big")
        else:
            value = raw.decode("utf-8")
        order.append(tag)
        fields[tag] = value

    if pos != len(data):
        raise ValueError("X-Gnarly payload has trailing bytes")
    return {"order": order, "fields": fields}


def _decode_x_gnarly_context(x_gnarly: str) -> dict[str, Any] | None:
    """从已有 X-Gnarly 里解出页面 session 参数：field8、字段顺序和采集计数。"""
    if not x_gnarly:
        return None
    try:
        raw = _custom_b64_decode(x_gnarly, GNARLY_ALPHABET)
    except Exception:
        return None
    if len(raw) < 1 + 48:
        return None

    mixed = raw[1:]
    for rounds in range(1, 33):
        for slot in range(0, len(mixed) - 48 + 1):
            key_string = mixed[slot : slot + 48]
            cipher = mixed[:slot] + mixed[slot + 48 :]
            # Small integer fields use two bytes; valid session payloads are
            # not always exactly the size of the original captured fixture.
            if not 180 <= len(cipher) <= 256:
                continue
            key_words = list(struct.unpack("<12I", key_string))
            payload = _chacha_xor_gnarly(cipher, key_words, rounds=rounds)
            try:
                parsed = _parse_x_gnarly_payload(payload)
            except Exception:
                continue
            fields = parsed["fields"]
            if set(fields) == set(range(17)) and len(parsed["order"]) == 17:
                return {
                    "field2": int(fields[2]),
                    "field8": int(fields[8]),
                    "perf_data": int(fields[7]),
                    "sdk_version": str(fields[9]),
                    "scm_version": str(fields[10]),
                    "field12": int(fields[12]),
                    "field13": int(fields[13]),
                    "order": parsed["order"],
                }
    return None


def _select_x_gnarly_context(plaintext: str) -> dict[str, Any]:
    """按接口选择默认 session；有旧 X-Gnarly 时仍优先复用旧值。"""
    parts = urlsplit(plaintext)
    if "mssdk" in parts.netloc and parts.path.rstrip("/") == "/web/report":
        return _GNARLY_REPORT_CONTEXT
    if "webcast" in parts.netloc and parts.path.rstrip("/") == "/webcast/room/enter":
        return _GNARLY_WEBCAST_ROOM_ENTER_CONTEXT
    return _GNARLY_SESSION_CONTEXT


def generate_x_gnarly(
    url: str,
    query: str,
    user_agent: str,
    ms_token: str,
    x_bogus: str,
    ts: int | None = None,
    body: str | bytes | None = None,
    field8_seed: int | None = None,
    cfg: dict[str, Any] | None = None,
    field_order: list[int] | None = None,
) -> str:
    """用规范化后的 URL/query、UA、msToken 和可选 body 生成 X-Gnarly。"""
    payload = build_x_gnarly_payload(query, user_agent, ts, body, field8_seed=field8_seed, cfg=cfg, field_order=field_order)

    key_words, key_string, rounds = _random_word_source()

    encrypted = _chacha_xor_gnarly(payload, key_words, rounds=rounds)

    slot = (sum(key_string) + sum(encrypted)) % (len(encrypted) + 1)
    mixed = encrypted[:slot] + key_string + encrypted[slot:]

    raw = bytes([(1 << 6) | 8 | 3]) + mixed
    return _custom_b64(raw, GNARLY_ALPHABET)


def get_x_gnarly(
    user_agent: str,
    plaintext: str,
    post_params: Any = "",
    ms_token: str = "",
    x_bogus: str = "",
    ts: int | None = None,
    context: dict[str, Any] | None = None,
) -> str:
    """传入 UA、请求明文 URL/query 和 POST 明文参数，返回 X-Gnarly。GET 时 post_params 传空值。"""
    ts = int(time.time()) if ts is None else ts
    body = _post_plaintext(post_params)
    token = ms_token or _extract_query_value_raw(plaintext, "msToken")
    if not token:
        raise ValueError("ms_token 不能为空")
    unsigned_url, _ = _sign_plaintext(plaintext)
    query = _query_with_ms_token(plaintext, token)
    bogus = x_bogus or "1"
    context = context or _decode_x_gnarly_context(
        _extract_query_value_raw(plaintext, "X-Gnarly")
    )
    cfg = None
    field8_seed = None
    field_order = None
    if not context:
        context = _select_x_gnarly_context(plaintext)
    if context:
        field8_seed = int(context["field8"])
        cfg = {
            "perf_data": int(context.get("perf_data", 596860301)),
            "sdk_version": str(context.get("sdk_version", SDK_VERSION)),
            "scm_version": str(context.get("scm_version", SCM_VERSION)),
            "field2": int(context.get("field2", 8)),
            "field12": int(context["field12"]),
            "field13": int(context["field13"]),
        }
        field_order = list(context["order"])
    x_gnarly = generate_x_gnarly(
        unsigned_url,
        query,
        user_agent,
        token,
        bogus,
        ts=ts,
        body=body,
        field8_seed=field8_seed,
        cfg=cfg,
        field_order=field_order,
    )
    return x_gnarly
