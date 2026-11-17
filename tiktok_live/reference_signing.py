"""Request signing backed by the byte-for-byte successful reference algorithms."""

from urllib.parse import urlencode, urlsplit, urlunsplit

from .algorithms import reference_x_dynosaur, reference_x_gnarly
from .algorithms.reference_x_dynosaur import get_x_dynosaur
from .algorithms.reference_x_gnarly import get_x_gnarly
from .config import RISK_KEYS, USER_AGENT


def _query_key(part: str) -> str:
    return part.split("=", 1)[0]


def _clean_query(query: str) -> str:
    return "&".join(
        part for part in query.split("&")
        if part and _query_key(part) not in RISK_KEYS
    )


def _append_query(query: str, key: str, value: str) -> str:
    return query + ("&" if query else "") + f"{key}={value}"


def _body_bytes(body):
    if body is None or isinstance(body, bytes):
        return body
    if isinstance(body, str):
        return body.encode("utf-8")
    if isinstance(body, (dict, list, tuple)):
        return urlencode(body, doseq=True).encode("utf-8")
    return str(body).encode("utf-8")


def _reference_counter_contexts(
    path: str, dynosaur_context, gnarly_context,
) -> tuple[dict | None, dict | None]:
    """Keep the accepted reference shape and vary only lifecycle counters."""
    if path == "/webcast/room/enter/":
        return None, None
    if path == "/web/report":
        dyn = dict(reference_x_dynosaur._DYNOSAUR_REPORT_CONTEXT)
        gnarly = dict(reference_x_gnarly._GNARLY_REPORT_CONTEXT)
    else:
        dyn = dict(reference_x_dynosaur._DYNOSAUR_SESSION_CONTEXT)
        gnarly = dict(reference_x_gnarly._GNARLY_SESSION_CONTEXT)
    if dynosaur_context:
        for key in ("field37", "field47"):
            if key in dynosaur_context:
                dyn[key] = int(dynosaur_context[key])
    if gnarly_context:
        for key in ("field12", "field13"):
            if key in gnarly_context:
                gnarly[key] = int(gnarly_context[key])
    return dyn, gnarly


def sign_url(
    url: str,
    ms_token: str,
    body=None,
    *,
    user_agent: str = USER_AGENT,
    dynosaur_context=None,
    gnarly_context=None,
    emit_ms_token: str | None = None,
) -> str:
    """Assemble the exact successful Dynosaur/msToken/Bogus/Gnarly chain."""
    if not ms_token:
        raise ValueError("ms_token 不能为空")
    parts = urlsplit(url)
    clean_query = _clean_query(parts.query)
    unsigned_url = urlunsplit(
        (parts.scheme, parts.netloc, parts.path, clean_query, parts.fragment)
    )
    body = _body_bytes(body)
    dynosaur_context, gnarly_context = _reference_counter_contexts(
        parts.path, dynosaur_context, gnarly_context,
    )

    import time
    timestamp = int(time.time())
    x_dynosaur = get_x_dynosaur(
        user_agent, unsigned_url, post_params=body, ts=timestamp,
        context=dynosaur_context,
    )
    query = _append_query(clean_query, "X-Dynosaur", x_dynosaur)
    emitted_token = ms_token if emit_ms_token is None else emit_ms_token
    query = _append_query(query, "msToken", emitted_token)
    token_url = urlunsplit(
        (parts.scheme, parts.netloc, parts.path, query, parts.fragment)
    )
    x_gnarly = get_x_gnarly(
        user_agent,
        token_url,
        post_params=body,
        ms_token=ms_token,
        x_bogus="1",
        ts=timestamp,
        context=gnarly_context,
    )
    query = _append_query(query, "X-Bogus", "1")
    query = _append_query(query, "X-Gnarly", x_gnarly)
    return urlunsplit(
        (parts.scheme, parts.netloc, parts.path, query, parts.fragment)
    )
