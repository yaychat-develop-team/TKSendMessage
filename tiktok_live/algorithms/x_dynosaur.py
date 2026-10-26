"""
X-Dynosaur 相关算法模块。
"""

from __future__ import annotations

import base64
import secrets
import struct
import time
from typing import Any
from urllib.parse import urlencode, urlsplit, urlunsplit

__all__ = [
    "get_x_dynosaur",
    "build_x_dynosaur_payload",
    "generate_x_dynosaur",
]

EX_VERSION = "1.0.0.2717"
SCM_VERSION = "1.0.0.417"
SDK_VERSION = "5.3.2"
GNARLY_ALPHABET = "u09tbS3UvgDEe6r-ZVMXzLpsAohTn7mdINQlW412GqBjfYiyk8JORCF5/xKHwacP"
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

_DYNOSAUR_SESSION_CONTEXT = {
    "field32_seed": 222,
    "field37": 12,
    "field40": 476443666,
    "field47": 72,
    "field50": EX_VERSION,
    "field51": "e654a84f2341b6252f44cc551a72c780",
    "field52": 1881297863,
    "field54": 8,
    "field56": bytes.fromhex("66a320f1"),
}

_DYNOSAUR_REPORT_CONTEXT = {
    **_DYNOSAUR_SESSION_CONTEXT,
    "field37": 11,
    "field47": 70,
}

_DYNOSAUR_WEBCAST_ROOM_ENTER_CONTEXT = {
    "field32_seed": 210,
    "field37": 1,
    "field40": 0,
    "field47": 2,
    "field50": bytes.fromhex("dbdedfe00001"),
    "field51": bytes.fromhex("dbdedfe00001"),
    "field52": 2072048768,
    "field54": 14,
    "field56": bytes.fromhex("811c9dc4"),
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


def _insert_random(cipher: bytes, rand_bytes: bytes, prefix: bytes) -> bytes:
    slot = (sum(rand_bytes) + sum(cipher)) % (len(cipher) + 1)
    return prefix + cipher[:slot] + rand_bytes + cipher[slot:]


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


def _custom_b64_padded(data: bytes, alphabet: str = GNARLY_ALPHABET) -> str:
    value = base64.b64encode(data).decode("ascii")
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


def _query_key(part: str) -> str:
    return part.split("=", 1)[0]


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


def _dynosaur_hash(value: str | bytes | None) -> bytes:
    """webmssdk_ex C=37：UTF-8 字节上的 32-bit hash，大端输出。"""
    if value is None:
        data = b""
    elif isinstance(value, bytes):
        data = value
    else:
        data = value.encode("utf-8")

    result = 0x811C9DC4
    for byte in data:
        result = ((result ^ byte) * 0x210033F3) & 0xFFFFFFFF
    return result.to_bytes(4, "big")


def _dynosaur_string(value: int | str) -> bytes:
    """webmssdk_ex C=43 的字符串字段编码。"""
    text = str(value)
    raw = text.encode("utf-8")
    size = max(len(raw), 4)
    out = bytearray(size + 2)
    for index in range(size):
        if index >= len(raw):
            out[index] = (221 + index) & 0xFF
            continue
        item = raw[index] ^ ((103 + index) & 0xFF)
        item = (item + 1 + (index & 0xAA)) & 0xFF
        item = ((item << 2) | (item >> 6)) & 0xFF
        item ^= 0xBB
        out[index] = (item + 1) & 0xFF
    out[-2:] = len(raw).to_bytes(2, "big")
    return bytes(out)


def _dynosaur_string_decode(value: bytes) -> str:
    if len(value) < 6:
        raise ValueError("X-Dynosaur string field is too short")
    length = int.from_bytes(value[-2:], "big")
    if length > len(value) - 2:
        raise ValueError("X-Dynosaur string field length mismatch")
    out = bytearray()
    for index, byte in enumerate(value[:length]):
        item = (byte - 1) & 0xFF
        item ^= 0xBB
        item = ((item >> 2) | (item << 6)) & 0xFF
        item = (item - 1 - (index & 0xAA)) & 0xFF
        out.append(item ^ ((103 + index) & 0xFF))
    return bytes(out).decode("utf-8")


def _dynosaur_checksum_string(value: int) -> bytes:
    """webmssdk_ex C=47：field32 的 0..255 短整数编码。"""
    if not 0 <= value <= 0xFF:
        raise ValueError("X-Dynosaur field32 checksum must fit in one byte")
    text = str(value).encode("ascii")
    masks = (0x3C, 0x3E, 0x24)
    out = bytearray(6)
    for index in range(4):
        if index >= len(text):
            out[index] = 221 + index
            continue
        rotated = ((text[index] << 1) | (text[index] >> 7)) & 0xFF
        out[index] = rotated ^ masks[index]
    out[-2:] = len(text).to_bytes(2, "big")
    return bytes(out)


def _dynosaur_checksum_decode(value: bytes) -> int:
    length = int.from_bytes(value[-2:], "big")
    masks = (0x3C, 0x3E, 0x24)
    raw = bytearray()
    for index, byte in enumerate(value[:length]):
        rotated = byte ^ masks[index]
        raw.append(((rotated >> 1) | (rotated << 7)) & 0xFF)
    return int(bytes(raw).decode("ascii"))


def _dynosaur_tlv(tag: int, value: bytes) -> bytes:
    if len(value) > 0xFF:
        raise ValueError("X-Dynosaur field is too large")
    return bytes((tag, 0, len(value))) + value


def _dynosaur_perf_field(timestamp: int, field52: int) -> int:
    return (
        0x10000
        | ((timestamp & 0xFFFF) ^ ((timestamp >> 16) & 0xFFFF) ^ (field52 & 0xFFFF) ^ ((field52 >> 16) & 0xFFFF))
    ) & 0xFFFFFFFF


def _select_x_dynosaur_context(plaintext: str) -> dict[str, Any]:
    parts = urlsplit(plaintext)
    if "mssdk" in parts.netloc and parts.path.rstrip("/") == "/web/report":
        return dict(_DYNOSAUR_REPORT_CONTEXT)
    if "webcast" in parts.netloc and parts.path.rstrip("/") == "/webcast/room/enter":
        return dict(_DYNOSAUR_WEBCAST_ROOM_ENTER_CONTEXT)
    return dict(_DYNOSAUR_SESSION_CONTEXT)


def build_x_dynosaur_payload(
    query: str,
    user_agent: str,
    ts: int | None = None,
    body: str | bytes | None = None,
    context: dict[str, Any] | None = None,
) -> bytes:
    """webmssdk_ex 1.0.0.2717 的 25 个顺序 TLV（tag 32..56）。"""
    timestamp = int(time.time()) if ts is None else int(ts)
    cfg = {**_DYNOSAUR_SESSION_CONTEXT, **(context or {})}
    field51 = cfg["field51"]
    field51_raw = field51 if isinstance(field51, bytes) else _dynosaur_string(str(field51))
    field50 = cfg.get("field50", EX_VERSION)
    field50_raw = field50 if isinstance(field50, bytes) else _dynosaur_string(str(field50))
    field56 = cfg["field56"]
    field56_raw = field56 if isinstance(field56, bytes) else _dynosaur_hash(str(field56))
    zero = _dynosaur_string(0)
    marker = bytes.fromhex("5ededfe00001")

    values: dict[int, bytes] = {
        32: marker,
        33: marker,
        34: marker,
        35: zero,
        36: _dynosaur_string(_dynosaur_perf_field(timestamp, int(cfg["field52"]))),
        37: _dynosaur_string(int(cfg["field37"])),
        38: _dynosaur_string(1),
        39: _dynosaur_string(timestamp),
        40: _dynosaur_string(int(cfg["field40"])),
        41: zero,
        42: _dynosaur_string(SDK_VERSION),
        43: _dynosaur_hash(body),
        44: _dynosaur_string(int(cfg.get("field44", 596860301))),
        45: zero,
        46: _dynosaur_hash(query),
        47: _dynosaur_string(int(cfg["field47"])),
        48: _dynosaur_hash(user_agent),
        49: _dynosaur_string(SCM_VERSION),
        50: field50_raw,
        51: field51_raw,
        52: _dynosaur_string(int(cfg["field52"])),
        53: _dynosaur_string(cfg.get("field53", 0)),
        54: _dynosaur_string(int(cfg["field54"])),
        55: zero,
        56: field56_raw,
    }

    checksum = int(cfg.get("field32_seed", 222)) & 0xFF
    for tag in range(33, 57):
        checksum ^= values[tag][1]
    values[32] = _dynosaur_checksum_string(checksum)
    return b"".join(_dynosaur_tlv(tag, values[tag]) for tag in range(32, 57))


def _parse_x_dynosaur_payload(data: bytes) -> dict[int, bytes]:
    fields: dict[int, bytes] = {}
    pos = 0
    for tag in range(32, 57):
        if pos + 3 > len(data):
            raise ValueError("X-Dynosaur payload truncated")
        actual_tag, zero, size = data[pos : pos + 3]
        pos += 3
        value = data[pos : pos + size]
        pos += size
        if actual_tag != tag or zero != 0 or len(value) != size:
            raise ValueError("X-Dynosaur payload malformed")
        fields[tag] = value
    if pos != len(data):
        raise ValueError("X-Dynosaur payload has trailing bytes")
    return fields


def _decode_x_dynosaur_context(x_dynosaur: str) -> dict[str, Any] | None:
    if not x_dynosaur:
        return None
    try:
        raw = _custom_b64_decode(x_dynosaur)
    except Exception:
        return None
    if len(raw) < 49 or raw[0] != 0x4B:
        return None

    mixed = raw[1:]
    for slot in range(len(mixed) - 47):
        key_string = mixed[slot : slot + 48]
        cipher = mixed[:slot] + mixed[slot + 48 :]
        if not 200 <= len(cipher) <= 512:
            continue
        key_words = list(struct.unpack("<12I", key_string))
        rounds = (sum(word & 0x0F for word in key_words) & 0x0F) + 5
        payload = _chacha_xor_gnarly(cipher, key_words, rounds=rounds)
        try:
            fields = _parse_x_dynosaur_payload(payload)
            field32_seed = _dynosaur_checksum_decode(fields[32])
            for tag in range(33, 57):
                field32_seed ^= fields[tag][1]
            return {
                "field32_seed": field32_seed,
                "field37": int(_dynosaur_string_decode(fields[37])),
                "field40": int(_dynosaur_string_decode(fields[40])),
                "field44": int(_dynosaur_string_decode(fields[44])),
                "field47": int(_dynosaur_string_decode(fields[47])),
                "field50": fields[50],
                "field51": fields[51],
                "field52": int(_dynosaur_string_decode(fields[52])),
                "field53": _dynosaur_string_decode(fields[53]),
                "field54": int(_dynosaur_string_decode(fields[54])),
                "field56": fields[56],
            }
        except (ValueError, UnicodeDecodeError):
            continue
    return None


def generate_x_dynosaur(payload: bytes) -> str:
    key_words, key_string, rounds = _random_word_source()
    encrypted = _chacha_xor_gnarly(payload, key_words, rounds=rounds)
    raw = _insert_random(encrypted, key_string, b"\x4b")
    return _custom_b64_padded(raw)


def get_x_dynosaur(
    user_agent: str,
    plaintext: str,
    post_params: Any = "",
    ts: int | None = None,
    context: dict[str, Any] | None = None,
) -> str:
    """生成 X-Dynosaur。POST 必须传入最终发送的 body 字符串或原始 bytes。"""
    body = _post_plaintext(post_params)
    _, query = _sign_plaintext(plaintext)
    existing = _decode_x_dynosaur_context(_extract_query_value_raw(plaintext, "X-Dynosaur"))
    cfg = context or existing or _select_x_dynosaur_context(plaintext)
    payload = build_x_dynosaur_payload(query, user_agent, ts=ts, body=body, context=cfg)
    return generate_x_dynosaur(payload)
