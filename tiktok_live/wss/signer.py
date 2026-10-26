"""Fail-closed adapter for the initialized frontier algorithm.

No production bootstrap currently establishes the required frontier state.
Do not install fixture state as a fallback to make this adapter succeed.
"""
from collections.abc import Callable

from ..algorithms.frontier import FrontierError, frontier_sign
from .models import SessionContext


def sign_frontier(session: SessionContext, *,
                  should_cancel: Callable[[], bool] | None = None) -> str:
    try:
        if should_cancel is not None and should_cancel():
            raise FrontierError('Frontier signing cancelled')
        if session.role not in ('delivery_observer', 'account_heartbeat'):
            raise FrontierError('Invalid signing session')
        if session.role == 'account_heartbeat' and not session.cookie_header:
            raise FrontierError('Authenticated signing session required')
        result = frontier_sign(cookie=session.cookie_header,
                               user_agent=session.user_agent,
                               referer=session.referer,
                               metrics=session.metrics)
        if should_cancel is not None and should_cancel():
            raise FrontierError('Frontier signing cancelled')
        return result
    except FrontierError:
        raise
    except Exception:
        raise FrontierError('Frontier signing failed') from None
