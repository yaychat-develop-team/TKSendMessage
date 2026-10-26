"""Direct audience profile extracted from the approved standalone script.
Not a full browser SDK initializer. Source hash is recorded in third-party notes.
"""
import base64
import hashlib
import math
import re
import secrets
from .direct_models import validate_heartbeat_interval

PROFILE_NAME = 'standalone_audience_direct_v1'


def self_test():
    """Independent vector evaluated from the original pure source function."""
    return frontier_sign('', context={'sdk_version': '1.0.0.417', 'envcode': 129,
        'ubcode': 0, 'index': 1, 'random_values': [0.1, 0.2, 0.3]}) == 'fgr7sfNukxlz4KgD'
WS_PATH = '/webcast/im/ws_proxy/ws_reuse_supplement/'

def frontier_sign(payload: str, *, context: dict) -> str:
    """SDK 1.0.0.417 anonymous, uninitialized audience WSS signing branch.

    This is NOT the HTTP X-Bogus signer. Explicit context is required because
    environment codes must not be guessed from this development-time fixture.
    index is the SDK counter AFTER increment; only its low six bits enter the
    frame. random_values holds the three successive Math.random() outputs:
    header selector, payload nonce, RC4 key. No clock is consumed in this branch.
    Caller owns state and fresh entropy; this pure function never mutates them.
    Anonymous bootstrap and production-browser environment derivation remain
    unimplemented. Reference vectors prove this branch, not server acceptance.
    """
    keys = {'sdk_version', 'envcode', 'ubcode', 'index', 'random_values'}
    if type(context) is not dict or set(context) != keys:
        raise ValueError('Explicit frontier context fields required; no extra fields')
    if context['sdk_version'] != '1.0.0.417':
        raise ValueError('Unsupported frontier SDK version')
    for name, minimum, maximum in (
            ('envcode', 0, 65535), ('ubcode', 0, 255), ('index', 1, 2**53 - 1)):
        value = context[name]
        if type(value) is not int or not minimum <= value <= maximum:
            raise ValueError('Invalid frontier integer context')
    values = context['random_values']
    if type(values) not in (list, tuple) or len(values) != 3:
        raise ValueError('Three frontier random values required')
    if any(type(value) not in (int, float) or not 0 <= value < 1 for value in values):
        raise ValueError('Frontier random values must be finite numbers in [0,1)')
    if type(payload) is not str:
        raise ValueError('Frontier payload must be text')
    try:
        encoded = payload.encode('utf-8')
    except UnicodeEncodeError:
        raise ValueError('Unpaired Unicode surrogate in frontier payload') from None
    if len(encoded) > 65536:
        raise ValueError('Frontier payload exceeds 64 KiB')

    # The SDK uses a literal zero stub for an empty payload, NOT md5('').
    stub_bytes = hashlib.md5(encoded).digest() if payload else bytes(16)
    query_tail = hashlib.md5(hashlib.md5(b'').digest()).digest()[-2:]
    payload_tail = hashlib.md5(stub_bytes).digest()[-2:]
    envcode = context['envcode']
    raw = bytearray((context['index'] & 63, envcode >> 8, envcode & 255,
                     context['ubcode'], *query_tail, *payload_tail,
                     math.floor(255 * values[1]) & 255))
    checksum = 0
    for byte in raw:
        checksum ^= byte
    raw.append(checksum)

    key = math.floor(255 * values[2]) & 255
    box = list(range(256))
    j = 0
    for i in range(256):
        j = (j + box[i] + key) & 255
        box[i], box[j] = box[j], box[i]
    i = j = 0
    for offset in range(len(raw)):
        i = (i + 1) & 255
        j = (j + box[i]) & 255
        box[i], box[j] = box[j], box[i]
        raw[offset] ^= box[(box[i] + box[j]) & 255]
    header = 64 | ((math.floor(100 * values[0]) & 1) << 4)
    alphabet = 'Dkdpgh4ZKsQB80/Mfvw36XI1R25+WUAlEi7NLboqYTOPuzmFjJnryx9HVGcaStCe='
    standard = 'ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/='
    packed = bytes((header, key)) + raw
    return base64.b64encode(packed).decode('ascii').translate(str.maketrans(standard, alphabet))

def build_direct_url(room_id, *, user_agent, random_values=None,heartbeat_interval_seconds=10.0,
                     endpoint='wss://webcast-ws.eu.tiktok.com' + WS_PATH):
    """SDK-evidenced direct mode; empty cursor is sent in the enter frame.

    This named compatibility profile (uninitialized audience, no interaction,
    envcode 1 | DOM-probe bit 128) was checked against the original SDK and a
    real anonymous connection. It is not a captured login/browser state and
    is not an implementation of every possible SDK fingerprint branch.
    """
    room_id = str(room_id)
    heartbeat_interval_seconds=validate_heartbeat_interval(heartbeat_interval_seconds)
    if not re.fullmatch(r'[0-9]{1,20}', room_id) or not 0 < int(room_id) < 2**64:
        raise ValueError('Invalid current room ID')
    if random_values is None:
        source = secrets.SystemRandom()
        random_values = [source.random() for _ in range(3)]
    marker = frontier_sign('', context={'sdk_version': '1.0.0.417',
        'envcode': 1 | 128, 'ubcode': 0, 'index': 1, 'random_values': random_values})
    pairs = [('version_code', '180800'), ('device_platform', 'web'),
        ('cookie_enabled', 'true'), ('screen_width', '1920'), ('screen_height', '1080'),
        ('browser_language', 'en-US'), ('browser_platform', 'Win32'),
        ('browser_name', 'Mozilla'), ('browser_version', user_agent.split('Mozilla/', 1)[-1]),
        ('browser_online', 'true'), ('tz_name', 'Asia/Shanghai'), ('app_name', 'tiktok_web'),
        ('sup_ws_ds_opt', '1'), ('update_version_code', '2.0.0'), ('compress', 'gzip'),
        ('webcast_language', 'en'), ('ws_direct', '1'), ('aid', '1988'), ('live_id', '12'),
        ('version_code', '270000'), ('app_language', 'en'), ('client_enter', '1'),
        ('room_id', room_id), ('identity', 'audience'), ('history_comment_count', '6'),
        ('last_rtt', '0'), ('heartbeat_duration',str(round(heartbeat_interval_seconds*1000))), ('resp_content_type', 'protobuf'),
        ('did_rule', '3'), ('X-Bogus', marker)]
    # Preserve duplicate keys and the SDK's raw query normalization, including
    # literal plus/slash characters in its signature alphabet.
    if not isinstance(endpoint,str) or '?' in endpoint or '#' in endpoint:
        raise ValueError('Invalid direct WSS endpoint')
    url = (endpoint + '?' + '&'.join(key + '=' + value for key, value in pairs)).replace(' ', '%20')
    from .sessions import validate_destination
    validate_destination(url,room_id)
    return url
