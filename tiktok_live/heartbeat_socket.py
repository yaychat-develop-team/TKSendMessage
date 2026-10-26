"""Bounded evidence belonging to exactly one direct WSS connection."""
from collections import Counter
from threading import Lock
import time
from .wss.protocol import decode_push_frame
from .wss.direct_models import validate_heartbeat_interval, heartbeat_warmup_seconds


class FrameEvidence:
    def __init__(self, *, heartbeat_interval_seconds=10.0):
        self.heartbeat_interval_seconds = validate_heartbeat_interval(heartbeat_interval_seconds)
        self._lock = Lock()
        self._counts = {'sent': Counter(), 'received': Counter()}
        self._beats = {}
        self._first_beat = {}
        self._entered = set()
        self._errors = 0

    def record(self, message, direction, *, now=None):
        with self._lock:
            try:
                frame = decode_push_frame(message)
                kind = frame.payload_type
                self._counts[direction][kind if kind in (
                    'hb', 'ack', 'msg', 'im_enter_room', 'im_enter_room_resp') else 'other'] += 1
                if direction == 'sent' and kind == 'hb':
                    stamps = self._beats.setdefault(frame.room_id, [])
                    stamps.append(time.monotonic() if now is None else now)
                    self._first_beat.setdefault(frame.room_id,stamps[-1])
                    del stamps[:-10]
                if direction == 'sent' and kind == 'im_enter_room':
                    self._entered.add(frame.room_id)
            except Exception:
                self._errors += 1

    def _ready(self, room, connected):
        beats = self._beats.get(str(room), [])
        return bool(connected and str(room) in self._entered and len(beats) >= 3
                    and beats[-1] - self._first_beat.get(str(room),beats[0]) >= 2 * self.heartbeat_interval_seconds
                    and self._counts['received']['im_enter_room_resp'] > 0)

    def ready(self, room, *, elapsed, connected):
        with self._lock:
            return elapsed >= heartbeat_warmup_seconds(self.heartbeat_interval_seconds) and self._ready(room, connected)

    def ready_to_send(self, room, *, connected):
        with self._lock:
            return self._ready(room, connected)

    def snapshot(self):
        with self._lock:
            return {**{key: dict(value) for key, value in self._counts.items()},
                    'entered_rooms': sorted(self._entered),
                    'heartbeat_gaps_s': {room: [round(b-a, 2) for a, b in zip(stamps, stamps[1:])]
                                         for room, stamps in self._beats.items()},
                    'parse_errors': self._errors}
