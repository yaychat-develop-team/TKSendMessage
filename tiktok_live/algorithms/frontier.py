"""Initialized legacy frontier empty-payload calculation, in Python.

This is NOT an implementation of SDK/browser initialization. Callers must
provide a verified frontier_state; absence fails closed. In particular the
offline reference's env/interaction bytes and RNG prelude are not defaults.
See docs/third-party/tiktokapis-wss.md for the remaining migration gate.
"""
from base64 import b64encode
from collections.abc import Callable, Mapping
import hashlib
import json
import math
import secrets


class FrontierError(ValueError):
    """Safe failure without cookie, URL, SDK or random-state disclosure."""


_BASE64 = b'ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/='
_FRONTIER_BASE64 = b'Dkdpgh4ZKsQB80/Mfvw36XI1R25+WUAlEi7NLboqYTOPuzmFjJnryx9HVGcaStCe='


def _rc4(key: int, payload: bytes) -> bytes:
    permutation = list(range(256))
    cursor = 0
    for offset in range(256):
        cursor = (cursor + permutation[offset] + key) & 255
        permutation[offset], permutation[cursor] = permutation[cursor], permutation[offset]
    left = right = 0
    output = bytearray()
    for value in payload:
        left = (left + 1) & 255
        right = (right + permutation[left]) & 255
        permutation[left], permutation[right] = permutation[right], permutation[left]
        output.append(value ^ permutation[(permutation[left] + permutation[right]) & 255])
    return bytes(output)


def _unsigned(state: Mapping, name: str, maximum: int) -> int:
    value = state.get(name)
    if type(value) is not int or not 0 <= value <= maximum:
        raise FrontierError('Invalid frontier initialization state')
    return value


def _bounded_metrics(root: Mapping) -> None:
    pending = [(root, 0)]
    visited = 0
    text_size = 0
    while pending:
        value, depth = pending.pop()
        visited += 1
        if visited > 4096 or depth > 16:
            raise FrontierError('Invalid frontier input')
        if isinstance(value, str):
            if len(value) > 65536:
                raise FrontierError('Invalid frontier input')
            text_size += len(value.encode('utf-8'))
            if text_size > 65536:
                raise FrontierError('Invalid frontier input')
        elif isinstance(value, Mapping):
            if len(value) > 4096:
                raise FrontierError('Invalid frontier input')
            for count, (key, item) in enumerate(value.items()):
                if count >= 4096 or not isinstance(key, str):
                    raise FrontierError('Invalid frontier input')
                pending.extend(((key, depth + 1), (item, depth + 1)))
        elif isinstance(value, (list, tuple)):
            if len(value) > 4096:
                raise FrontierError('Invalid frontier input')
            pending.extend((item, depth + 1) for item in value)
        elif type(value) is int:
            if value.bit_length() > 256:
                raise FrontierError('Invalid frontier input')
        elif type(value) is float:
            if not math.isfinite(value):
                raise FrontierError('Invalid frontier input')
        elif value is not None and type(value) is not bool:
            raise FrontierError('Invalid frontier input')


def frontier_sign(*, cookie: str, user_agent: str, referer: str,
                  metrics: Mapping[str, object], now_ms: int | None = None,
                  random_float: Callable[[], float] | None = None) -> str:
    """Calculate from explicit initialized state and a fresh random source.

    metrics['frontier_state'] contains initialized (bool), env_code (uint32),
    ub_code (byte), bogus_index (uint32). It is read, never mutated. Cookie,
    UA, referer and clock are context/validation inputs: the upstream empty
    payload calculation has no direct dependency on their bytes. SDK init
    may depend on them, and is deliberately NOT silently approximated here.
    """
    try:
        for value, maximum in ((cookie, 65536), (user_agent, 2048), (referer, 4096)):
            if not isinstance(value, str) or len(value) > maximum or len(value.encode('utf-8')) > maximum:
                raise FrontierError('Invalid frontier input')
        if not isinstance(metrics, Mapping):
            raise FrontierError('Invalid frontier input')
        _bounded_metrics(metrics)
        encoded = json.dumps(dict(metrics), ensure_ascii=False, allow_nan=False)
        if len(encoded) > 65536 or len(encoded.encode('utf-8')) > 65536:
            raise FrontierError('Invalid frontier input')
        if now_ms is not None and (type(now_ms) is not int or not 0 <= now_ms < 2**63):
            raise FrontierError('Invalid frontier input')
        state = metrics.get('frontier_state')
        if not isinstance(state, Mapping) or type(state.get('initialized')) is not bool:
            raise FrontierError('Verified frontier initialization state required')
        environment = _unsigned(state, 'env_code', 0xFFFFFFFF)
        interaction = _unsigned(state, 'ub_code', 255)
        index = _unsigned(state, 'bogus_index', 0xFFFFFFFF)
        source = random_float if random_float is not None else secrets.SystemRandom().random
        draws = []
        for _ in range(3):
            value = source()
            if type(value) not in (int, float) or not math.isfinite(value) or not 0 <= value < 1:
                raise FrontierError('Invalid frontier random source')
            draws.append(value)
        # Fixed empty-payload call: websocket mode=1; MD5(empty) then MD5 of
        # its binary digest, and MD5 of the decoded fallback zero stub.
        empty_digest = hashlib.md5(hashlib.md5(b'').digest()).digest()
        fallback_digest = hashlib.md5(bytes(16)).digest()
        flags = (1 << 6) | (int(state['initialized']) << 5) | ((int(draws[0] * 100) & 1) << 4)
        header = bytearray([
            (index + 1) & 63, (environment >> 8) & 255,
            environment & 255, interaction,
            empty_digest[14], empty_digest[15],
            fallback_digest[14], fallback_digest[15], int(draws[1] * 255),
        ])
        checksum = 0
        for value in header:
            checksum ^= value
        header.append(checksum)
        key = int(draws[2] * 255)
        wire = bytes((flags, key)) + _rc4(key, bytes(header))
        return b64encode(wire).translate(bytes.maketrans(_BASE64, _FRONTIER_BASE64)).decode('ascii')
    except FrontierError:
        raise
    except Exception:
        raise FrontierError('Frontier calculation failed') from None
