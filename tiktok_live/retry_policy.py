"""Bounded retries for transient outcomes only, never terminal account failures."""
from curl_cffi.requests.exceptions import ConnectionError as CurlConnectionError, Timeout as CurlTimeout


class RetryableMessageError(RuntimeError):
    """Safe error retaining the prepared request, never raw network text."""
    def __init__(self, callback):
        super().__init__('消息接口暂时不可用')
        self._retry_callback = callback

    def retry_message(self):
        return self._retry_callback()


def terminal_failure(value):
    return bool(isinstance(value,dict) and (value.get('loginExpired')
        or value.get('accountSilenced') or value.get('accountBanned') or value.get('stopProcessing')
        or value.get('roomExists') is False))


def transient_failure(value):
    if isinstance(value, Exception):
        return isinstance(value, (RetryableMessageError,TimeoutError, ConnectionError, CurlConnectionError, CurlTimeout))
    if not isinstance(value, dict):
        return False
    if terminal_failure(value):
        return False
    return (value.get('temporaryError') is True or
            value.get('httpStatus') in (408, 425, 429, 500, 502, 503, 504))
