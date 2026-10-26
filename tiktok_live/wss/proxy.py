"""Explicit anonymous-observer proxy input; never changes process proxy state."""
import ipaddress
import re
from urllib.parse import quote, unquote, urlsplit


def normalize_observer_proxy(value: str) -> str:
    """Return a SOCKS5 URL with remote DNS, or empty for the legacy route.

    Error messages deliberately contain no input (it can include a password).
    """
    error = 'SOCKS5 代理格式无效：请填写 主机:端口[:用户名:密码] 或 socks5://用户名:密码@主机:端口；用户名和密码各限 127 字节'
    try:
        if not isinstance(value, str) or len(value) > 4096:
            raise ValueError()
        if any(ord(c) < 32 or ord(c) == 127 for c in value):
            raise ValueError()
        value = value.strip()
        if not value:
            return ''
        if not re.match(r'^[A-Za-z][A-Za-z0-9+.-]*://', value):
            # IPv6 with authentication uses the unambiguous URL form.
            if value.startswith('['):
                value = 'socks5h://' + value
            else:
                parts = value.split(':', 3)
                if len(parts) not in (2, 4):
                    raise ValueError()
                host, port = parts[:2]
                auth = ''
                if len(parts) == 4:
                    if not parts[2] or not parts[3]:
                        raise ValueError()
                    auth = quote(parts[2], safe='') + ':' + quote(parts[3], safe='') + '@'
                value = 'socks5h://' + auth + host + ':' + port
        parsed = urlsplit(value)
        host, port = parsed.hostname, parsed.port
        if (parsed.scheme not in ('socks5', 'socks5h') or not host or port is None
                or not 1 <= port <= 65535 or parsed.path not in ('', '/')
                or parsed.query or parsed.fragment or '?' in value or '#' in value):
            raise ValueError()
        if ':' in host:
            ipaddress.IPv6Address(host)
            host = '[' + host + ']'
        elif not re.fullmatch(r'[A-Za-z0-9](?:[A-Za-z0-9.-]*[A-Za-z0-9])?', host):
            raise ValueError()
        auth = ''
        if parsed.username is not None or parsed.password is not None:
            raw_user, raw_password = parsed.username, parsed.password
            if not raw_user or not raw_password:
                raise ValueError()
            credentials = []
            for raw in (raw_user, raw_password):
                if re.search(r'%(?![0-9a-fA-F]{2})', raw):
                    raise ValueError()
                decoded = unquote(raw, errors='strict')
                # PySocks 1.x encodes >=128 as a multi-byte length, not an octet.
                if (not 1 <= len(decoded.encode('utf-8')) <= 127
                        or any(ord(c) < 32 or ord(c) == 127 for c in decoded)):
                    raise ValueError()
                credentials.append(quote(decoded, safe=''))
            auth = credentials[0] + ':' + credentials[1] + '@'
        return f'socks5h://{auth}{host}:{port}'
    except (ValueError, TypeError, UnicodeError):
        raise ValueError(error) from None
