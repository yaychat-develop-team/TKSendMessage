"""Bounded protobuf/gzip codec; adapted from standalone_live_comments.py."""
import gzip
import io
import zlib
from .direct_models import ChatEvent, DecodedFrame

MAX_FRAME_BYTES = 2 * 1024 * 1024
MAX_DECODED_BYTES = 4 * 1024 * 1024

def _as_frame(data):
    events = tuple(ChatEvent(e['message_id'], e['user_id'], e['username'], e['nickname'], e['text'])
                   for e in data['events'])
    return DecodedFrame(data['payload_type'], data['log_id'], data['need_ack'],
                        data['internal_ext'], events, data.get('room_id'))

def _fields(raw):
    """Read bounded protobuf wire fields; no generated proto module needed."""
    if not isinstance(raw, (bytes, bytearray)) or len(raw) > MAX_DECODED_BYTES:
        raise ValueError('Invalid protobuf size/type')
    result, offset, count = {}, 0, 0

    def number():
        nonlocal offset
        value = 0
        for index in range(10):
            if offset >= len(raw):
                raise ValueError('Truncated protobuf varint')
            byte = raw[offset]
            offset += 1
            if index == 9 and byte > 1:
                raise ValueError('Protobuf integer exceeds uint64')
            value |= (byte & 127) << (index * 7)
            if byte < 128:
                return value
        raise ValueError('Overlong protobuf varint')

    while offset < len(raw):
        count += 1
        if count > 20000:
            raise ValueError('Too many protobuf fields')
        tag = number()
        field, wire = tag >> 3, tag & 7
        if not 0 < field < (1 << 29):
            raise ValueError('Invalid protobuf field')
        if wire == 0:
            value = number()
        elif wire in (1, 2, 5):
            size = number() if wire == 2 else (8 if wire == 1 else 4)
            end = offset + size
            if end > len(raw):
                raise ValueError('Truncated protobuf payload')
            value, offset = raw[offset:end], end
        else:
            raise ValueError('Unsupported protobuf wire type')
        result.setdefault(field, []).append(value)
    return result

def _first(tree, key, default):
    value = tree.get(key, [default])[0]
    if not isinstance(value, type(default)):
        raise ValueError('Unexpected protobuf field type')
    return value

def _text(tree, key):
    return _first(tree, key, b'').decode('utf-8')

def decode_push_frame(raw, *, max_decoded_bytes=MAX_DECODED_BYTES):
    """Return chat events plus exact server ACK metadata, without socket I/O."""
    if not isinstance(raw, bytes) or len(raw) > MAX_FRAME_BYTES:
        raise ValueError('Invalid WSS frame size/type')
    if not 1 <= max_decoded_bytes <= MAX_DECODED_BYTES:
        raise ValueError('Invalid decode limit')
    frame = _fields(raw)
    kind = _text(frame, 7)
    result = dict(payload_type=kind, log_id=_first(frame, 2, 0),
                  need_ack=False, internal_ext='', events=[])
    if kind not in ('msg', 'im_enter_room_resp'):
        if kind in ('hb', 'im_enter_room'):
            # Outbound frames contain a protobuf room ID, while server replies
            # can use an opaque payload. Only extract the ID when present.
            try:
                room = _first(_fields(_first(frame, 8, b'')), 1, 0)
            except ValueError:
                room = 0
            if 0 < room < 2**64:
                result['room_id'] = str(room)
        return _as_frame(result)
    payload = _first(frame, 8, b'')
    headers = {}
    for raw_header in frame.get(5, []):
        header = _fields(raw_header)
        headers[_text(header, 1)] = _text(header, 2)
    compression = headers.get('compress_type', 'none')
    if compression not in ('', 'none', 'gzip'):
        raise ValueError('Unsupported WSS compression')
    if compression == 'gzip' or payload.startswith(b'\x1f\x8b'):
        try:
            with gzip.GzipFile(fileobj=io.BytesIO(payload)) as stream:
                payload = stream.read(max_decoded_bytes + 1)
        except (OSError, EOFError, zlib.error) as exc:
            raise ValueError('Invalid compressed WSS frame') from None
    if len(payload) > max_decoded_bytes:
        raise ValueError('Decoded WSS payload exceeds limit')
    response = _fields(payload)
    result.update(need_ack=bool(_first(response, 9, 0)),
                  internal_ext=_text(response, 5))
    for raw_envelope in response.get(1, []):
        envelope = _fields(raw_envelope)
        if _text(envelope, 1) != 'WebcastChatMessage':
            continue
        body = _fields(_first(envelope, 2, b''))
        user = _fields(_first(body, 2, b''))
        message_id = _first(envelope, 3, 0)
        user_id = _first(user, 1, 0)
        result['events'].append(dict(type='chat',
            user_id=str(user_id) if user_id else '', username=_text(user, 38),
            nickname=_text(user, 3), text=_text(body, 3),
            message_id=str(message_id) if message_id else ''))
    return _as_frame(result)

def _uint(value):
    if type(value) is not int or not 0 <= value < (1 << 64):
        raise ValueError('Expected uint64')
    result = bytearray()
    while value > 127:
        result.append((value & 127) | 128)
        value >>= 7
    result.append(value)
    return bytes(result)

def _wire(key, value):
    if isinstance(value, int):
        return _uint(key << 3) + _uint(value)
    if isinstance(value, str):
        value = value.encode('utf-8')
    if not isinstance(value, bytes) or len(value) > MAX_FRAME_BYTES:
        raise ValueError('Invalid outbound payload')
    return _uint((key << 3) | 2) + _uint(len(value)) + value

def _encode(kind, payload, log_id=None):
    return (b'' if log_id is None else _wire(2, log_id)) + _wire(6, 'pb') + _wire(7, kind) + _wire(8, payload)

def encode_heartbeat(room_id):
    return _encode('hb', _wire(1, room_id))

def encode_enter_room(room_id, live_id=12, cursor=''):
    payload = (_wire(1, room_id) + _wire(4, live_id) + _wire(5, 'audience')
               + _wire(6, cursor) + _wire(7, 0) + _wire(9, '0') + _wire(10, 0))
    return _encode('im_enter_room', payload)

def encode_ack(log_id, internal_ext):
    return _encode('ack', internal_ext.encode('utf-8'), log_id)
