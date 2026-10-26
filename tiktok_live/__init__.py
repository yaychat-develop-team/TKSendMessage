"""Public TikTok live messaging API."""

from importlib import import_module

_EXPORTS = {
    'load_account_store': 'account_store', 'save_account_store_atomic': 'account_store',
    'login_new_account': 'browser_auth', 'validate_saved_account': 'browser_auth',
    'send_live_message': 'client', 'AccountValidation': 'models',
    'run_saved_accounts': 'runner',
}


def __getattr__(name):
    if name not in _EXPORTS:
        raise AttributeError(name)
    value = getattr(import_module('.' + _EXPORTS[name], __name__), name)
    globals()[name] = value
    return value

__all__ = [
    "AccountValidation",
    "load_account_store",
    "save_account_store_atomic",
    "login_new_account",
    "validate_saved_account",
    "send_live_message",
    "run_saved_accounts",
]
