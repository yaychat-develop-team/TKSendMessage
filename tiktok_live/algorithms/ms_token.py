"""
msToken 相关算法模块。
"""

from __future__ import annotations

import base64
import json
import secrets
import time
from typing import Any
from urllib.parse import urlencode

__all__ = [
    "generate_ms_seed_token",
    "generate_ms_token",
    "encrypt_ms_token_report",
    "build_ms_token_report_json",
    "build_ms_token_report_form",
]

DEFAULT_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/150.0.0.0 Safari/537.36"
)

MSSDK_REPORT_ALPHABET = "Dkdpgh4ZKsQB80/Mfvw36XI1R25+WUAlEi7NLboqYTOPuzmFjJnryx9HVGcaStCe="
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


def _bytes_to_words_le(data: bytes) -> list[int]:
    out: list[int] = []
    for offset in range(0, len(data), 4):
        value = 0
        for index, byte in enumerate(data[offset : offset + 4]):
            value |= byte << (8 * index)
        out.append(value)
    return out


def _words_to_bytes_le(words: list[int], size: int) -> bytes:
    out = bytearray()
    for word in words:
        out.extend((word & 0xFFFFFFFF).to_bytes(4, "little"))
    return bytes(out[:size])


def _chacha_xor_with_state(data: bytes, state: list[int], rounds: int) -> bytes:
    words = _bytes_to_words_le(data)
    local_state = [x & 0xFFFFFFFF for x in state]
    offset = 0

    while offset + 16 < len(words):
        stream = _chacha_block(local_state, rounds)
        for index in range(16):
            words[offset + index] = _u32(words[offset + index] ^ stream[index])
        local_state = _increment_counter(local_state)
        offset += 16

    stream = _chacha_block(local_state, rounds)
    for index in range(len(words) - offset):
        words[offset + index] = _u32(words[offset + index] ^ stream[index])
    return _words_to_bytes_le(words, len(data))


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


def _lzw_compress(text: str) -> bytes:
    out = bytearray()
    bit_index = 0
    current = 0

    def write_bits(value: int, width: int) -> None:
        nonlocal bit_index, current
        for _ in range(width):
            if value & 1:
                current |= 1 << bit_index
            value >>= 1
            bit_index += 1
            if bit_index == 8:
                out.append(current)
                current = 0
                bit_index = 0

    table = {chr(i): i for i in range(256)}
    width = 8
    next_code = 255
    pos = 0

    while pos < len(text):
        chunk = text[pos]
        while pos + 1 < len(text) and chunk + text[pos + 1] in table:
            pos += 1
            chunk += text[pos]
        write_bits(table[chunk], width)
        if pos + 1 == len(text):
            break
        next_code += 1
        if (next_code & (next_code - 1)) == 0:
            width += 1
        table[chunk + text[pos + 1]] = next_code
        pos += 1

    if bit_index:
        out.append(current)
    return bytes(out)


def _mssdk_b64(data: bytes) -> str:
    value = base64.b64encode(data).decode("ascii")
    return value.translate(
        str.maketrans("ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/", MSSDK_REPORT_ALPHABET[:64])
    )


def _env_plaintext(value: Any) -> str:
    if value is None:
        raise ValueError("env_plaintext 不能为空，请在 example.py 里传入环境明文长文本")
    if isinstance(value, bytes):
        return value.decode("utf-8")
    if isinstance(value, str):
        return value
    raise TypeError("env_plaintext 必须是 str 或 bytes，不要传 dict/list")


def generate_ms_seed_token() -> str:
    """生成 webmssdk 首次写入 cookie 的 seed：94 字节 -> urlsafe base64，保留 ==。"""
    return base64.urlsafe_b64encode(secrets.token_bytes(94)).decode("ascii")


def generate_ms_token(length: int = 128) -> str:
    """兼容旧调用；默认返回 SDK seed 形态，其他长度走普通字符兜底。"""
    if length == 128:
        return generate_ms_seed_token()
    MSTOKEN_ALPHABET = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_"
    return "".join(secrets.choice(MSTOKEN_ALPHABET) for _ in range(length))


def encrypt_ms_token_report(fingerprint_json: str, now_ms: int | None = None) -> str:
    """把环境明文压缩并加密成 /web/report 里的 strData。"""
    compressed = _lzw_compress(fingerprint_json)
    key_words, rand_bytes, rounds = _random_word_source(now_ms)
    cipher = _chacha_xor_with_state(compressed, CHACHA_CONST + key_words, rounds)
    return _mssdk_b64(_insert_random(cipher, rand_bytes, b"L"))


def _ms_token_report_payload(
    user_agent: str = DEFAULT_UA,
    now_ms: int | None = None,
    env_plaintext: Any = None,
) -> dict[str, int | str]:
    millis = int(time.time() * 1000) if now_ms is None else now_ms
    fingerprint = _env_plaintext(env_plaintext)
    return {
        "magic": 538969122,
        "version": 1,
        "dataType": 8,
        "strData": encrypt_ms_token_report(fingerprint, now_ms=millis),
        "tspFromClient": millis,
    }


def build_ms_token_report_json(
    user_agent: str = DEFAULT_UA,
    now_ms: int | None = None,
    env_plaintext: Any = None,
) -> bytes:
    """纯 Python 计算新版 webmssdk /web/report JSON/text 上报体。"""
    return json.dumps(
        _ms_token_report_payload(user_agent=user_agent, now_ms=now_ms, env_plaintext=env_plaintext),
        ensure_ascii=False,
        separators=(",", ":"),
    ).encode("utf-8")


def build_ms_token_report_form(
    user_agent: str = DEFAULT_UA,
    now_ms: int | None = None,
    env_plaintext: Any = None,
) -> bytes:
    """旧版 /web/report form 上报体，保留作对照验证。"""
    payload = _ms_token_report_payload(user_agent=user_agent, now_ms=now_ms, env_plaintext=env_plaintext)
    return urlencode({key: str(value) for key, value in payload.items()}, doseq=True).encode("utf-8")
