"""Packaged resources and writable application data, without GUI dependencies."""

import os
import plistlib
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path


def resolve_bundled_browser(app_dir: Path) -> Path | None:
    """Prefer the external onedir browser, then PyInstaller's resource bundle."""
    roots = [Path(app_dir).resolve()]
    if getattr(sys, 'frozen', False) and hasattr(sys, '_MEIPASS'):
        roots.append(Path(sys._MEIPASS).resolve())
    for root in roots:
        browser_root = root / 'browser' / 'chromium'
        windows_executable = browser_root / 'chrome.exe'
        if windows_executable.is_file():
            return windows_executable
        mac_bundle = browser_root / 'Chromium.app'
        info_plist = mac_bundle / 'Contents' / 'Info.plist'
        if info_plist.is_file():
            try:
                bundle_info = plistlib.loads(info_plist.read_bytes())
                executable_name = str(bundle_info.get('CFBundleExecutable') or '').strip()
            except (OSError, ValueError, TypeError):
                executable_name = ''
            if executable_name:
                mac_executable = mac_bundle / 'Contents' / 'MacOS' / executable_name
                if mac_executable.is_file():
                    return mac_executable
    return None


@dataclass(frozen=True)
class RuntimePaths:
    app_dir: Path
    runtime_dir: Path
    account_store: Path
    profile_root: Path
    report_dir: Path
    log_dir: Path
    settings_file: Path
    browser_executable: Path | None

    @classmethod
    def from_application(cls, app_dir: Path | None = None) -> 'RuntimePaths':
        """Create runtime folders beside the source project or frozen executable.

        Bundled Chromium wins over any developer browser override. When no
        bundle exists the environment is preserved for normal CLI discovery.
        """
        if app_dir is None:
            if getattr(sys, 'frozen', False) and sys.platform == 'darwin':
                app_dir = Path.home() / 'Library' / 'Application Support' / 'TikTokLiveTool'
            else:
                app_dir = (Path(sys.executable).parent if getattr(sys, 'frozen', False)
                           else Path(__file__).resolve().parent.parent)
        app_dir = Path(app_dir).resolve()
        runtime_dir = app_dir / 'runtime'
        account_dir = runtime_dir / 'accounts'
        profile_root = runtime_dir / 'profiles'
        report_dir = runtime_dir / 'reports'
        log_dir = runtime_dir / 'logs'
        for directory in (account_dir, profile_root, report_dir, log_dir):
            directory.mkdir(parents=True, exist_ok=True)
            # Detect read-only existing directories before presenting a usable UI.
            with tempfile.NamedTemporaryFile(prefix='.write-check-', dir=directory):
                pass
        with tempfile.NamedTemporaryFile(prefix='.write-check-', dir=runtime_dir):
            pass
        browser_executable = resolve_bundled_browser(app_dir)
        if browser_executable is not None:
            os.environ['TIKTOK_BROWSER_EXECUTABLE'] = str(browser_executable)
        return cls(app_dir, runtime_dir, account_dir / 'tiktok_accounts.json',
                   profile_root, report_dir, log_dir,
                   runtime_dir / 'desktop-settings.json', browser_executable)



