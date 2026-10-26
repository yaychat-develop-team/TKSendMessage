"""Fresh TikTok Ticket Guard header construction.

Core cryptographic flow adapted from:
https://github.com/cv-cat/TiktokApis/blob/master/signing/ticket_guard.py
"""

from __future__ import annotations

import base64
import json
import time
from urllib.parse import unquote

from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC


_AES_SECRET = b"tt-ticket-guard-iv"
_AES_SALT = b"secure-salt"
_AES_ITERATIONS = 1000


def _b64decode(value: str) -> bytes:
    text = str(value).strip()
    text += "=" * (-len(text) % 4)
    return base64.b64decode(text.encode("ascii"), validate=False)


def decrypt_encrypt_ticket(encrypt_ticket: str) -> str:
    """Decrypt the browser ``encrypt_ticket`` storage value to its ticket."""
    ciphertext = _b64decode(encrypt_ticket)
    if len(ciphertext) <= 12 + 16:
        raise ValueError("encrypt_ticket 长度不足，无法解密")
    key = PBKDF2HMAC(
        algorithm=hashes.SHA256(), length=16, salt=_AES_SALT,
        iterations=_AES_ITERATIONS,
    ).derive(_AES_SECRET)
    plaintext = AESGCM(key).decrypt(ciphertext[:12], ciphertext[12:], None)
    ticket = plaintext.decode("utf-8")
    if not ticket:
        raise ValueError("encrypt_ticket 解密为空")
    return ticket


def public_key_base64(private_key_pem: str) -> str:
    """Return Chrome's 65-byte uncompressed P-256 public key as base64."""
    private_key = serialization.load_pem_private_key(
        str(private_key_pem).encode("utf-8"), password=None,
    )
    if not isinstance(private_key, ec.EllipticCurvePrivateKey):
        raise TypeError("ticket guard private key 不是 EC 私钥")
    public_bytes = private_key.public_key().public_bytes(
        serialization.Encoding.X962,
        serialization.PublicFormat.UncompressedPoint,
    )
    return base64.b64encode(public_bytes).decode("ascii")


def sign_ticket_path(private_key_pem: str, ticket: str, path: str,
                     timestamp: int) -> str:
    """Sign the SDK's ``ticket=<...>&path=<...>&timestamp=<...>`` string."""
    private_key = serialization.load_pem_private_key(
        str(private_key_pem).encode("utf-8"), password=None,
    )
    if not isinstance(private_key, ec.EllipticCurvePrivateKey):
        raise TypeError("ticket guard private key 不是 EC 私钥")
    sign_data = f"ticket={ticket}&path={path}&timestamp={int(timestamp)}"
    signature = private_key.sign(
        sign_data.encode("utf-8"), ec.ECDSA(hashes.SHA256())
    )
    return base64.b64encode(signature).decode("ascii")


def encode_client_data(*, ts_sign: str, req_sign: str, timestamp: int,
                       req_content: str = "ticket,path,timestamp") -> str:
    payload = {
        "ts_sign": str(ts_sign),
        "req_content": str(req_content),
        "req_sign": str(req_sign),
        "timestamp": int(timestamp),
    }
    raw = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
    return base64.b64encode(raw.encode("utf-8")).decode("ascii")


def decode_client_data(value: str) -> dict[str, object]:
    raw = _b64decode(value).decode("utf-8")
    payload = json.loads(raw)
    if not isinstance(payload, dict):
        raise ValueError("client-data 不是 JSON 对象")
    for key in ("ts_sign", "req_content", "req_sign", "timestamp"):
        if key not in payload:
            raise ValueError(f"client-data 缺少 {key}")
    return payload


def build_headers(*, private_key_pem: str, encrypt_ticket: str,
                  ts_sign: str, path: str, timestamp: int | None = None,
                  version: str = "2", iteration_version: str = "0") -> dict[str, str]:
    """Build the five browser Ticket Guard headers from fresh SDK state."""
    now = int(timestamp if timestamp is not None else time.time())
    ticket = decrypt_encrypt_ticket(encrypt_ticket)
    req_sign = sign_ticket_path(private_key_pem, ticket, path, now)
    return {
        "tt-ticket-guard-public-key": public_key_base64(private_key_pem),
        "tt-ticket-guard-web-version": "1",
        "tt-ticket-guard-version": str(version),
        "tt-ticket-guard-iteration-version": str(iteration_version),
        "tt-ticket-guard-client-data": encode_client_data(
            ts_sign=ts_sign, req_sign=req_sign, timestamp=now,
        ),
    }


def _cookie_values(cookie: str) -> dict[str, str]:
    values: dict[str, str] = {}
    for part in str(cookie or "").split(";"):
        item = part.strip()
        if not item or "=" not in item:
            continue
        key, value = item.split("=", 1)
        if key.strip():
            values[key.strip()] = value.strip()
    return values


def cookie_key_material(cookie: str) -> dict[str, str]:
    """Extract the same-session EC key and versions from the browser Cookie."""
    encoded = _cookie_values(cookie).get("tt_ticket_guard_client_data", "")
    if not encoded:
        raise ValueError("Cookie 中缺少 tt_ticket_guard_client_data，请重新登录")
    try:
        outer = json.loads(_b64decode(unquote(encoded)).decode("utf-8"))
        origin = outer.get("tt-ticket-guard-origin-crypt")
        if origin is None:
            origin = outer["tt-ticket-guard-origin-crypto"]
        if isinstance(origin, str):
            origin = json.loads(origin)
        private_key = origin["ec_privateKey"]
    except (KeyError, TypeError, ValueError, UnicodeError, json.JSONDecodeError):
        raise ValueError("Cookie 中的 Ticket Guard 密钥无效，请重新登录") from None
    if not isinstance(private_key, str) or not private_key.strip():
        raise ValueError("Cookie 中的 Ticket Guard 私钥为空，请重新登录")
    return {
        "private_key_pem": private_key,
        "public_key": str(outer.get("tt-ticket-guard-public-key", "")),
        "version": str(outer.get("tt-ticket-guard-version", "2")),
        "iteration_version": str(
            outer.get("tt-ticket-guard-iteration-version", "0")
        ),
    }


def build_headers_from_cookie_state(
    cookie: str, state: dict[str, str], path: str
) -> dict[str, str]:
    """Build fresh headers from Cookie key material and matching SDK storage."""
    if not isinstance(state, dict):
        raise ValueError("Ticket Guard 会话状态缺失，请重新登录")
    encrypt_ticket = str(state.get("encrypt_ticket", "")).strip()
    ts_sign = str(state.get("ts_sign", "")).strip()
    if not encrypt_ticket or not ts_sign:
        raise ValueError("Ticket Guard 会话状态不完整，请重新登录")
    storage_private_key = str(state.get("private_key_pem", "")).strip()
    try:
        material = cookie_key_material(cookie)
    except ValueError:
        if not storage_private_key:
            raise
        material = {
            "private_key_pem": storage_private_key,
            "public_key": "",
            "version": "2",
            "iteration_version": "0",
        }
    if storage_private_key:
        storage_public_key = public_key_base64(storage_private_key)
        expected_public_key = material["public_key"]
        if expected_public_key and storage_public_key != expected_public_key:
            raise ValueError("Ticket Guard 会话与 Cookie 不匹配，请重新登录")
        material["private_key_pem"] = storage_private_key
    headers = build_headers(
        private_key_pem=material["private_key_pem"],
        encrypt_ticket=encrypt_ticket,
        ts_sign=ts_sign,
        path=path,
        version=material["version"],
        iteration_version=material["iteration_version"],
    )
    expected_public_key = material["public_key"]
    if expected_public_key and headers["tt-ticket-guard-public-key"] != expected_public_key:
        raise ValueError("Ticket Guard 会话与 Cookie 不匹配，请重新登录")
    return headers


__all__ = [
    "build_headers", "build_headers_from_cookie_state", "cookie_key_material",
    "decode_client_data", "decrypt_encrypt_ticket", "encode_client_data",
    "public_key_base64", "sign_ticket_path",
]
