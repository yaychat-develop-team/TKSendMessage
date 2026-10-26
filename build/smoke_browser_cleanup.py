"""Exercise real bundled Chrome shutdown without signing in or sending messages."""
import json
import os
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from tiktok_live import browser_auth as auth


def main():
    root = Path(__file__).resolve().parents[1]
    os.environ['TIKTOK_BROWSER_EXECUTABLE'] = str(root / 'browser/chromium/chrome.exe')
    result = {}
    with tempfile.TemporaryDirectory(prefix='browser-cleanup-smoke-') as directory:
        for mode in ('normal', 'driver_failure'):
            port = auth._free_local_port()
            profile = Path(directory) / mode
            browser = auth.launch_available_browser(headless=True, user_data_path=profile,
                                                    local_port=port, timeout_seconds=15)
            try:
                owned = auth._owned_browser_processes(port, profile)
                assert owned, 'Could not verify ownership of launched Chromium'
                target = browser
                if mode == 'driver_failure':
                    def broken_quit(**kwargs):
                        raise RuntimeError('controlled driver failure')
                    target = SimpleNamespace(address=browser.address,
                                             user_data_path=browser.user_data_path,
                                             states=browser.states, quit=broken_quit)
                closed = auth._close_browser(target, local_port=port, profile=profile)
                result[mode] = bool(closed and not auth._debug_port_is_open(port)
                                    and not any(p.is_running() for p in owned))
            finally:
                auth._close_browser(browser, local_port=port, profile=profile)
    print(json.dumps(result))
    return 0 if all(result.values()) else 1


if __name__ == '__main__':
    raise SystemExit(main())
