import os
import sys
import tempfile
import unittest
from dataclasses import FrozenInstanceError
from pathlib import Path
from unittest.mock import patch

from desktop import paths as subject
from tiktok_live.config import BusinessPaths, PROJECT_ROOT


class RuntimePathsTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name).resolve()

    def test_source_mode_uses_source_root_not_working_directory(self):
        with patch.object(subject, '__file__', str(self.root / 'desktop' / 'paths.py')), \
                patch.object(sys, 'frozen', False, create=True):
            paths = subject.RuntimePaths.from_application()
        self.assertEqual(paths.app_dir, self.root)
        self.assertEqual(paths.account_store, self.root / 'runtime/accounts/tiktok_accounts.json')
        self.assertTrue(paths.profile_root.is_dir())

    def test_frozen_mode_writes_beside_executable_not_extraction_root(self):
        extraction = self.root / '_internal'
        with patch.object(sys, 'frozen', True, create=True), \
                patch.object(sys, 'executable', str(self.root / 'tool.exe')), \
                patch.object(sys, '_MEIPASS', str(extraction), create=True):
            paths = subject.RuntimePaths.from_application()
        self.assertEqual(paths.app_dir, self.root)
        self.assertEqual(paths.runtime_dir, self.root / 'runtime')
        self.assertFalse((extraction / 'runtime').exists())

    def test_macos_frozen_mode_uses_user_application_support(self):
        with patch.object(sys, 'platform', 'darwin'), \
                patch.object(sys, 'frozen', True, create=True), \
                patch.object(Path, 'home', return_value=self.root), \
                patch.object(sys, 'executable', '/Applications/TikTokLiveTool.app/Contents/MacOS/TikTokLiveTool'):
            paths = subject.RuntimePaths.from_application()
        expected = self.root / 'Library/Application Support/TikTokLiveTool'
        self.assertEqual(paths.app_dir, expected)
        self.assertEqual(paths.runtime_dir, expected / 'runtime')
    def test_explicit_directory_creates_runtime_folders_and_is_immutable(self):
        app_dir = self.root / 'new/app'
        paths = subject.RuntimePaths.from_application(app_dir)
        for relative in ('accounts', 'profiles', 'reports', 'logs'):
            self.assertTrue((app_dir / 'runtime' / relative).is_dir())
        self.assertEqual(paths.profile_root, app_dir / 'runtime/profiles')
        self.assertEqual(paths.report_dir, app_dir / 'runtime/reports')
        self.assertEqual(paths.log_dir, app_dir / 'runtime/logs')
        self.assertFalse(paths.account_store.exists())
        with self.assertRaises(FrozenInstanceError):
            paths.app_dir = self.root
        self.assertEqual(subject.RuntimePaths.from_application(app_dir), paths)

    def test_bundle_overrides_environment_and_precedes_frozen_resource_bundle(self):
        bundle = self.root / 'browser/chromium/chrome.exe'
        resource = self.root / '_internal/browser/chromium/chrome.exe'
        for executable in (bundle, resource):
            executable.parent.mkdir(parents=True)
            executable.touch()
        with patch.dict(os.environ, {'TIKTOK_BROWSER_EXECUTABLE': 'custom.exe'}), \
                patch.object(sys, 'frozen', True, create=True), \
                patch.object(sys, '_MEIPASS', str(self.root / '_internal'), create=True):
            paths = subject.RuntimePaths.from_application(self.root)
            self.assertEqual(paths.browser_executable, bundle)
            self.assertEqual(os.environ['TIKTOK_BROWSER_EXECUTABLE'], str(bundle))

    def test_frozen_resources_supply_browser_when_external_bundle_missing(self):
        executable = self.root / '_internal/browser/chromium/chrome.exe'
        executable.parent.mkdir(parents=True)
        executable.touch()
        with patch.object(sys, 'frozen', True, create=True), \
                patch.object(sys, '_MEIPASS', str(self.root / '_internal'), create=True):
            self.assertEqual(subject.resolve_bundled_browser(self.root), executable)

    def test_macos_bundle_resolves_executable_from_info_plist(self):
        bundle = self.root / 'browser/chromium/Chromium.app'
        executable = bundle / 'Contents/MacOS/Chrome for Testing'
        executable.parent.mkdir(parents=True)
        executable.touch()
        plist = bundle / 'Contents/Info.plist'
        plist.write_bytes(
            b'<?xml version="1.0" encoding="UTF-8"?>'
            b'<plist version="1.0"><dict><key>CFBundleExecutable</key>'
            b'<string>Chrome for Testing</string></dict></plist>'
        )
        self.assertEqual(subject.resolve_bundled_browser(self.root), executable)
    def test_missing_browser_preserves_environment_and_rejects_directory(self):
        (self.root / 'browser/chromium/chrome.exe').mkdir(parents=True)
        with patch.dict(os.environ, {'TIKTOK_BROWSER_EXECUTABLE': 'custom.exe'}), \
                patch.object(sys, 'frozen', False, create=True):
            paths = subject.RuntimePaths.from_application(self.root)
            self.assertIsNone(paths.browser_executable)
            self.assertEqual(os.environ['TIKTOK_BROWSER_EXECUTABLE'], 'custom.exe')
        with patch.dict(os.environ, {}, clear=True), \
                patch.object(sys, 'frozen', False, create=True):
            subject.RuntimePaths.from_application(self.root)
            self.assertNotIn('TIKTOK_BROWSER_EXECUTABLE', os.environ)

    def test_business_paths_keep_cli_defaults_and_accept_desktop_paths(self):
        defaults = BusinessPaths()
        self.assertEqual(defaults.account_store, PROJECT_ROOT / 'tiktok_accounts.json')
        self.assertEqual(defaults.profile_root, PROJECT_ROOT / 'port')
        self.assertEqual(defaults.report_dir, PROJECT_ROOT)
        desktop = subject.RuntimePaths.from_application(self.root)
        configured = BusinessPaths(account_store=desktop.account_store,
                                   profile_root=desktop.profile_root,
                                   report_dir=desktop.report_dir)
        self.assertEqual(configured.account_store, self.root / 'runtime/accounts/tiktok_accounts.json')
        self.assertEqual(configured.profile_root, self.root / 'runtime/profiles')
        self.assertEqual(configured.report_dir, self.root / 'runtime/reports')



class ExplicitProfilePathTests(unittest.TestCase):
    def test_login_uses_explicit_profile_root_and_preserves_default(self):
        from tests.test_drission_browser_auth import FakeBrowser, DrissionBrowserAuthTests
        import tiktok_live.browser_auth as auth
        from tiktok_live.config import BROWSER_PROFILE_ROOT

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for kwargs, expected in (({}, BROWSER_PROFILE_ROOT / '12345'),
                                     ({'profile_root': root / 'profiles'}, root / 'profiles/12345')):
                browser = FakeBrowser()
                with patch.object(auth, '_free_local_port', return_value=12345), \
                        patch.object(auth, '_debug_port_is_open', return_value=True), \
                        patch.object(auth, 'launch_available_browser', return_value=browser) as launch, \
                        patch.object(auth, 'wait_for_tiktok_login', return_value=DrissionBrowserAuthTests.cookies), \
                        patch.object(auth, 'wait_for_ticket_guard_state', return_value=None):
                    auth.login_new_account(root / 'accounts.json', **kwargs)
                self.assertEqual(launch.call_args.kwargs['user_data_path'], expected)
                self.assertEqual(browser.quit_calls, 1)


if __name__ == '__main__':
    unittest.main()


