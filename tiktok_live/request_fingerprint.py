"""Random HTTP transport per flow; signed/header UA identity stays untouched."""
from contextlib import contextmanager
from contextvars import ContextVar
from secrets import choice as _choose

FINGERPRINTS = (
    'chrome120', 'chrome123', 'chrome124', 'chrome131', 'chrome133a', 'chrome136',
    'chrome142', 'chrome145', 'safari153', 'safari155', 'safari170', 'safari172_ios',
    'safari180', 'safari180_ios', 'safari184', 'safari184_ios', 'safari260',
    'safari2601', 'safari260_ios', 'firefox133', 'firefox135', 'firefox144', 'firefox147',
)
_active_fingerprint = ContextVar('request_flow_fingerprint', default=None)


def current_fingerprint():
    return _active_fingerprint.get() or _choose(FINGERPRINTS)


@contextmanager
def request_flow():
    if _active_fingerprint.get() is not None:
        yield
        return
    token = _active_fingerprint.set(_choose(FINGERPRINTS))
    try:
        yield
    finally:
        _active_fingerprint.reset(token)
