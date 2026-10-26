"""Behavioral release checks; fixtures never contain real account data."""
import importlib.util
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
VERIFIER = ROOT / 'build' / 'verify_release.py'


class ReleaseManifestTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.release = Path(self.temp.name) / 'release'
        self.release.mkdir()
        if not VERIFIER.is_file():
            self.fail('release verifier is not implemented')
        spec = importlib.util.spec_from_file_location('release_verifier', VERIFIER)
        self.verifier = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.verifier)

    def write(self, name, content='fixture'):
        path = self.release / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding='utf-8')
        return path

    def complete_release(self):
        for name in (
            'TKChatWave.exe', '_internal/python310.dll', '_internal/_ctypes.pyd', '_internal/ffi.dll',
            '_internal/_sqlite3.pyd', '_internal/sqlite3.dll', '_internal/libxml2.dll',
            '_internal/libxslt.dll', '_internal/libexslt.dll',
            '_internal/PySide6/QtCore.pyd', '_internal/PySide6/QtGui.pyd',
            '_internal/PySide6/QtWidgets.pyd', '_internal/PySide6/plugins/platforms/qwindows.dll',
            '_internal/PySide6/Qt6Core.dll', '_internal/PySide6/Qt6Gui.dll',
            '_internal/PySide6/Qt6Widgets.dll', '_internal/PySide6/pyside6.abi3.dll',
            '_internal/shiboken6/Shiboken.pyd', '_internal/shiboken6/shiboken6.abi3.dll',
            '_internal/PySide6/plugins/platforms/qoffscreen.dll',
            '_internal/DrissionPage/_configs/configs.ini',
            '_internal/curl_cffi/_wrapper.pyd', '_internal/tiktok_live/algorithms/browser_env.json',
            'browser/chromium/chrome.exe', 'browser/chromium/chrome.dll',
            'browser/chromium/icudtl.dat', 'browser/chromium/resources.pak',
            'browser/chromium/locales/en-US.pak',
        ):
            self.write(name)

    def test_scanner_rejects_captured_ticket_guard_literals_and_encoded_payloads(self):
        import base64
        payload = {'ts_sign': 'synthetic-sign', 'req_sign': 'synthetic-request-sign',
                   'req_content': 'ticket,path,timestamp', 'timestamp': 1234567890}
        encoded = base64.b64encode(json.dumps(payload).encode()).decode()
        fixtures = {
            'constant.py': '_TICKET_GUARD_CLIENT_DATA = ' + repr(encoded),
            'renamed.py': 'request_material = ' + repr(encoded),
            'decoded.json': json.dumps(payload),
            'header.py': 'headers = {"tt-ticket-guard-client-data": "synthetic-data"}',
            'state.py': 'state = {"encrypt_ticket": "synthetic-ticket"}',
        }
        for name, content in fixtures.items():
            with self.subTest(name=name):
                path = self.write(name, content)
                findings = self.verifier.scan_file(path)
                self.assertTrue(findings, 'captured Ticket Guard material passed the scanner')
                self.assertNotIn('synthetic', str(findings))

    def test_scanner_accepts_ticket_guard_names_and_runtime_generation(self):
        path = self.write('dynamic.py', 'fields = ["ts_sign", "req_sign", "encrypt_ticket"]\n'
                          'ts_sign = state.get("ts_sign")\n'
                          'headers = {"tt-ticket-guard-client-data": encode(payload)}\n')
        self.assertEqual(self.verifier.scan_file(path), [])
        self.assertEqual(self.verifier.scan_file(self.write('schema.json', json.dumps({
            'properties': {'ts_sign': {'type': 'string'}, 'req_sign': {'type': 'string'}},
            'fields': ['tt-ticket-guard-client-data', 'encrypt_ticket']}))), [])

    def test_manifest_is_relative_hashed_and_contains_browser(self):
        self.complete_release()
        manifest = self.verifier.generate_manifest(self.release)
        self.assertEqual(manifest['format'], 1)
        browser = next(item for item in manifest['files'] if item['path'] == 'browser/chromium/chrome.exe')
        self.assertEqual(browser['size'], 7)
        self.assertEqual(len(browser['sha256']), 64)
        self.assertTrue(all(not Path(item['path']).is_absolute() for item in manifest['files']))
        self.assertEqual(self.verifier.verify_release(self.release), [])

    def test_verifier_rejects_missing_ctypes_and_its_conda_ffi_dependency(self):
        self.complete_release()
        for relative in ('_internal/_ctypes.pyd', '_internal/ffi.dll', '_internal/_sqlite3.pyd',
                         '_internal/sqlite3.dll', '_internal/libxml2.dll',
                         '_internal/libxslt.dll', '_internal/libexslt.dll'):
            with self.subTest(relative=relative):
                path = self.release / relative
                path.unlink()
                self.assertTrue(any(relative in problem for problem in self.verifier.verify_release(self.release)))
                self.write(relative)

    def test_failed_console_preflight_prevents_windowed_smoke_launch(self):
        from types import SimpleNamespace
        from unittest.mock import patch
        spec = importlib.util.spec_from_file_location('smoke_release', ROOT / 'build/smoke_release.py')
        smoke = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(smoke)
        self.assertTrue(hasattr(smoke, 'run_startup_smoke'), 'safe console preflight is missing')
        diagnostic = Path(self.temp.name) / 'diagnostic.exe'
        diagnostic.write_bytes(b'controlled console executable boundary')
        launched = []
        def fail_console(command, **kwargs):
            launched.append(Path(command[0]).name)
            return SimpleNamespace(returncode=1, stderr='controlled missing ffi.dll', stdout='')
        with patch.object(smoke, 'run_smoke_process', side_effect=fail_console):
            with self.assertRaises(SystemExit):
                smoke.run_startup_smoke(self.release, diagnostic, {})
        self.assertEqual(launched, ['TKChatWave-diagnostic.exe'])

    def test_smoke_process_decodes_utf8_and_invalid_diagnostic_bytes(self):
        spec = importlib.util.spec_from_file_location('smoke_release', ROOT / 'build/smoke_release.py')
        smoke = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(smoke)
        self.assertTrue(hasattr(smoke, 'run_smoke_process'), 'Unicode-safe process runner is missing')
        result = smoke.run_smoke_process([sys.executable, '-c',
            "import sys;sys.stdout.buffer.write(bytes([228,184,173,230,150,135]));sys.stderr.buffer.write(bytes([255]))"],
            timeout=10)
        self.assertEqual(result.returncode, 0)
        self.assertEqual(result.stdout, '\u4e2d\u6587')
        self.assertEqual(result.stderr, '\ufffd')

    def test_cli_removes_conflicting_system_icu_but_preserves_application_dlls(self):
        self.complete_release()
        self.write('_internal/icuuc.dll', 'fixture-incompatible-icu')
        self.write('_internal/kept.dll', 'fixture-app-dll')
        result = subprocess.run([sys.executable, str(VERIFIER), str(self.release),
            '--remove-conflicting-icu', '--write-manifest'], capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertFalse((self.release / '_internal/icuuc.dll').exists())
        self.assertTrue((self.release / '_internal/kept.dll').is_file())

    def test_verifier_rejects_shadowing_windows_system_icu(self):
        self.complete_release()
        self.write('_internal/icuuc.dll', 'fixture-incompatible-icu')
        self.assertTrue(self.verifier.verify_release(self.release))

    def test_missing_native_dependencies_and_browser_fail(self):
        self.complete_release()
        (self.release / 'browser/chromium/chrome.dll').unlink()
        (self.release / '_internal/PySide6/plugins/platforms/qwindows.dll').unlink()
        problems = '\n'.join(self.verifier.verify_release(self.release))
        self.assertIn('chrome.dll', problems)
        self.assertIn('qwindows.dll', problems)

    def test_verifier_requires_essential_qt_and_shiboken_dlls_in_packaged_locations(self):
        self.complete_release()
        for relative in ('_internal/PySide6/Qt6Core.dll', '_internal/PySide6/Qt6Gui.dll',
                         '_internal/PySide6/Qt6Widgets.dll', '_internal/PySide6/pyside6.abi3.dll',
                         '_internal/shiboken6/Shiboken.pyd', '_internal/shiboken6/shiboken6.abi3.dll'):
            with self.subTest(relative=relative):
                path = self.release / relative
                if path.exists():
                    path.unlink()
                # A duplicate at the EXE root does not satisfy Qt's packaged path.
                self.write(Path(relative).name)
                self.assertTrue(any(relative in problem for problem in self.verifier.verify_release(self.release)))
                self.write(relative)

    def test_scanner_rejects_runtime_data_names(self):
        for name in ('tiktok_accounts.json', 'failed_accounts.json', 'silenced_accounts.json',
                     'runtime/accounts/file.txt', 'profiles/Default/Preferences',
                     '.browser-profile/Default/Cookies', 'reports/run.json', 'logs/desktop.log',
                     'Cache/data', 'Profile 2/Bookmarks', 'Crashpad/metadata',
                     'Local Storage/store.bin', 'User Data/Default/History', '__pycache__/app.pyc',
                     'browser/chromium/Default/Local State'):
            with self.subTest(name=name):
                path = self.write(name)
                self.assertTrue(self.verifier.scan_sensitive_files(self.release))
                path.unlink()
                # Remove empty forbidden directories to isolate every case.
                for parent in list(path.parents):
                    if parent == self.release:
                        break
                    if parent.exists() and not any(parent.iterdir()):
                        parent.rmdir()

    def test_scanner_rejects_values_without_echoing_them(self):
        secret = 'fixture-secret-value-123456789'
        for content in (
            'Cookie: sessionid=' + secret,
            json.dumps({'cookies': [{'name': 'sessionid', 'value': secret}]}),
            json.dumps({'msToken': secret}),
            json.dumps({'csrf_token': secret}),
            json.dumps({'tt_csrf_token': secret}),
            json.dumps({'privateKey': secret}),
            json.dumps({'x-tt-ticket-guard-client-data': secret}),
            json.dumps({'X-Gnarly': secret}),
            json.dumps({'private_key': secret}),
            '-----BEGIN PRIVATE KEY-----\n' + secret,
            'ROOM_ID_COOKIE = "sessionid=' + secret + '"',
        ):
            with self.subTest(kind=content.split(':')[0][:30]):
                self.write('payload.txt', content)
                problems = self.verifier.scan_sensitive_files(self.release)
                self.assertTrue(problems)
                self.assertNotIn(secret, '\n'.join(problems))

    def test_scanner_rejects_literal_secrets_in_python_code(self):
        for content in ('ROOM_ID_COOKIE = "fixture-private-cookie"',
                        'headers = {"msToken": "fixture-private-token"}',
                        'request(ttwid="fixture-private-id")'):
            with self.subTest(content=content.split('=')[0]):
                self.write('credential.py', content)
                self.assertTrue(self.verifier.scan_sensitive_files(self.release))

    def test_scanner_rejects_recursive_cookie_field_mappings(self):
        for data in ({"sessionid": "synthetic-review-secret"},
                     {"cookies": {"sid_tt": "synthetic-review-secret"}},
                     {"rows": [{"auth": {"uid_tt_ss": "synthetic-review-secret"}}]},
                     {"value": "synthetic-review-secret", "name": "sid_tt"}):
            with self.subTest(data_shape=list(data)):
                self.write('mapped-cookie.json', json.dumps(data))
                problems = self.verifier.scan_sensitive_files(self.release)
                self.assertTrue(problems)
                self.assertNotIn('synthetic-review-secret', '\n'.join(problems))

    def test_scanner_rejects_annotated_keyed_and_attribute_python_credentials(self):
        for content in ('msToken: str = "synthetic-review-secret"',
                        'headers["msToken"] = "synthetic-review-secret"',
                        'state.sid_tt = "synthetic-review-secret"',
                        'state.sessionid: str = "synthetic-review-secret"',
                        'headers["uid_tt"]: str = "synthetic-review-secret"'):
            with self.subTest(target=content.split('=')[0]):
                self.write('credential.py', content)
                self.assertTrue(self.verifier.scan_sensitive_files(self.release))

    def test_scanner_detects_bom_marked_utf16_credentials_and_accepts_plain_text(self):
        path = self.write('encoded.txt')
        for encoding in ('utf-16', 'utf-16-be'):
            with self.subTest(encoding=encoding):
                payload = 'msToken = "synthetic-review-secret"'.encode(encoding)
                if encoding == 'utf-16-be':
                    payload = b'\xfe\xff' + payload
                path.write_bytes(payload)
                self.assertTrue(self.verifier.scan_sensitive_files(self.release))
        path.write_text('ordinary browser resource', encoding='utf-16')
        self.assertEqual(self.verifier.scan_sensitive_files(self.release), [])

    def test_scanner_rejects_undecodable_text_without_echoing_bytes(self):
        path = self.write('invalid.txt')
        path.write_bytes(b'\x80synthetic-review-secret')
        problems = self.verifier.scan_sensitive_files(self.release)
        self.assertTrue(problems)
        self.assertNotIn('synthetic-review-secret', '\n'.join(problems))

    def test_scanner_allows_code_field_names_and_empty_defaults(self):
        self.write('settings.py', 'ROOM_ID_COOKIE = ""\n')
        self.write('module.py', 'name = "sessionid"\nheaders["Cookie"] = cookie\n'
                   'token = data.get("msToken")\nprivate_key = state["private_key"]\n'
                   'request(ttwid=cookie_values.get("ttwid", ""), csrf_token=csrf_token)\n')
        self.write('references.py', 'sessionid: str\nmsToken: str = ""\n'
                   'headers["msToken"] = token\nstate.sid_tt = auth.cookies.get("sid_tt")\n')
        self.write('schema.json', json.dumps({"cookieFields": ["sessionid", "sid_tt"],
                   "properties": {"sessionid": {"type": "string"}}, "cookies": {"sid_tt": ""}}))
        self.write('cookie_parser.py', 'import re\nre.search(r"msToken=([^;,]+)", header)\n')
        self.write('browser_env.json', '{"userAgent": "Mozilla/5.0", "platform": "Win32"}')
        self.assertEqual(self.verifier.scan_sensitive_files(self.release), [])

    def test_scanner_rejects_links_that_can_import_external_data(self):
        external = Path(self.temp.name) / 'outside.json'
        external.write_text('{}', encoding='utf-8')
        try:
            (self.release / 'linked.json').symlink_to(external)
        except OSError:
            self.skipTest('creating symlinks requires Windows developer mode')
        self.assertTrue(self.verifier.scan_sensitive_files(self.release))

    def test_packaged_fingerprint_has_no_fixed_session_identifiers(self):
        data = json.loads((ROOT / 'tiktok_live/algorithms/browser_env.json').read_text(encoding='utf-8-sig'))
        self.assertFalse(data.get('customInit', {}).get('ttwid'))
        self.assertFalse(data.get('tokenList'))

    def test_onefile_spec_embeds_browser_and_application_icon(self):
        spec = ROOT / 'build/tiktok_desktop_onefile.spec'
        self.assertTrue(spec.is_file(), 'single-file build spec is not implemented')
        text = spec.read_text(encoding='utf-8')
        self.assertIn("'browser/chromium'", text)
        self.assertIn("icon=str(source / 'desktop' / 'assets' / 'icon.ico')", text)
        self.assertNotIn('COLLECT(', text)

    def test_build_rejects_missing_and_contaminated_browser_before_staging(self):
        script = ROOT / 'build/build.ps1'
        self.assertTrue(script.is_file(), 'build script is not implemented')
        for has_executable in (False, True):
            with self.subTest(has_executable=has_executable):
                browser = Path(self.temp.name) / ('contaminated' if has_executable else 'missing')
                browser.mkdir()
                if has_executable:
                    (browser / 'chrome.exe').write_bytes(b'fixture')
                    (browser / 'tiktok_accounts.json').write_text('{}', encoding='utf-8')
                result = subprocess.run(['powershell', '-NoProfile', '-ExecutionPolicy', 'Bypass',
                    '-File', str(script), '-ChromiumPath', str(browser),
                    '-PythonExecutable', sys.executable], capture_output=True, text=True)
                self.assertNotEqual(result.returncode, 0)
                self.assertIn('chrome.exe' if not has_executable else 'tiktok_accounts.json',
                              result.stdout + result.stderr)

    def test_build_never_removes_its_browser_input_when_reusing_staging_path(self):
        project = Path(self.temp.name) / 'project'
        build = project / 'build'
        build.mkdir(parents=True)
        import shutil
        shutil.copy(ROOT / 'build/build.ps1', build / 'build.ps1')
        shutil.copy(VERIFIER, build / 'verify_release.py')
        browser = project / 'browser/chromium'
        browser.mkdir(parents=True)
        executable = browser / 'chrome.exe'
        executable.write_bytes(b'fixture-browser')
        result = subprocess.run(['powershell', '-NoProfile', '-ExecutionPolicy', 'Bypass',
            '-File', str(build / 'build.ps1'), '-ChromiumPath', str(browser),
            '-PythonExecutable', sys.executable], capture_output=True, text=True)
        self.assertNotEqual(result.returncode, 0)
        self.assertTrue(executable.exists(), 'build deleted its source browser')
        self.assertEqual(executable.read_bytes(), b'fixture-browser')
        self.assertIn('generated staging', result.stdout + result.stderr)

    def make_disposable_build_project(self, name):
        import shutil
        project = Path(self.temp.name) / name
        build = project / 'build'
        build.mkdir(parents=True)
        shutil.copy(ROOT / 'build/build.ps1', build / 'build.ps1')
        shutil.copy(VERIFIER, build / 'verify_release.py')
        sentinel = project / 'build/work/source_inputs/keep.txt'
        sentinel.parent.mkdir(parents=True)
        sentinel.write_bytes(b'untouched-stage')
        return project, sentinel

    def run_disposable_build(self, project, browser):
        return subprocess.run(['powershell', '-NoProfile', '-ExecutionPolicy', 'Bypass',
            '-File', str(project / 'build/build.ps1'), '-ChromiumPath', str(browser),
            '-PythonExecutable', sys.executable], capture_output=True, text=True)

    def test_build_rejects_every_generated_path_overlap_before_any_cleanup(self):
        for index, relative in enumerate(('build/work/source_inputs/vendor', 'build/work',
                'build/work/pyinstaller/vendor', 'dist', 'dist/TKChatWave/browser/chromium')):
            with self.subTest(relative=relative):
                project, sentinel = self.make_disposable_build_project('overlap-' + str(index))
                browser = project / relative
                browser.mkdir(parents=True, exist_ok=True)
                executable = browser / 'chrome.exe'
                executable.write_bytes(b'untouched-browser')
                result = self.run_disposable_build(project, browser)
                self.assertNotEqual(result.returncode, 0)
                self.assertTrue(executable.exists(), 'build deleted supplied Chromium input')
                self.assertEqual(executable.read_bytes(), b'untouched-browser')
                self.assertTrue(sentinel.exists(), 'build mutated staging before rejection')
                self.assertEqual(sentinel.read_bytes(), b'untouched-stage')
                self.assertIn('generated staging', result.stdout + result.stderr)

    def test_build_rejects_reparse_aliases_before_any_cleanup(self):
        for mode in ('input', 'browser', 'cache'):
            generated_alias = mode != 'input'
            with self.subTest(mode=mode):
                project, sentinel = self.make_disposable_build_project('alias-' + mode)
                target = (Path(self.temp.name) / ('external-browser-' + mode) if generated_alias
                          else project / 'build/work/source_inputs/vendor')
                target.mkdir(parents=True)
                executable = target / 'chrome.exe'
                executable.write_bytes(b'untouched-browser')
                alias = (project / ('build/work/pyinstaller-config/pyinstaller' if mode == 'cache'
                                    else 'browser/chromium') if generated_alias
                         else Path(self.temp.name) / 'input-browser-alias')
                alias.parent.mkdir(parents=True, exist_ok=True)
                script = "New-Item -ItemType Junction -Path '" + str(alias).replace("'", "''") + "' -Target '" + str(target).replace("'", "''") + "' | Out-Null"
                result = subprocess.run(['powershell', '-NoProfile', '-Command', script], capture_output=True)
                self.assertEqual(result.returncode, 0, 'test junction creation failed')
                result = self.run_disposable_build(project, target if generated_alias else alias)
                self.assertNotEqual(result.returncode, 0)
                self.assertTrue(executable.exists(), 'reparse alias caused Chromium input removal')
                self.assertEqual(executable.read_bytes(), b'untouched-browser')
                self.assertTrue(sentinel.exists(), 'reparse alias allowed staging mutation')
                self.assertEqual(sentinel.read_bytes(), b'untouched-stage')
                self.assertIn('generated staging', result.stdout + result.stderr)

    def test_build_rejects_junction_on_source_staging_directory(self):
        import shutil
        project, sentinel = self.make_disposable_build_project('source-staging-alias')
        alias = sentinel.parent
        shutil.rmtree(alias)
        target = Path(self.temp.name) / 'outside-source-staging'
        target.mkdir()
        (target / 'keep.txt').write_bytes(b'untouched-stage')
        browser = target / 'vendor'
        browser.mkdir()
        executable = browser / 'chrome.exe'
        executable.write_bytes(b'untouched-browser')
        script = "New-Item -ItemType Junction -Path '" + str(alias).replace("'", "''") + "' -Target '" + str(target).replace("'", "''") + "' | Out-Null"
        result = subprocess.run(['powershell', '-NoProfile', '-Command', script], capture_output=True)
        self.assertEqual(result.returncode, 0, 'test junction creation failed')
        result = self.run_disposable_build(project, browser)
        self.assertNotEqual(result.returncode, 0)
        self.assertTrue(getattr(alias.lstat(), 'st_file_attributes', 0) & 0x400,
                        'build replaced source staging junction before validation')
        self.assertEqual(sentinel.read_bytes(), b'untouched-stage')
        self.assertEqual(executable.read_bytes(), b'untouched-browser')
        self.assertIn('generated staging', result.stdout + result.stderr)

    def test_cli_writes_manifest_and_exits_nonzero_after_contamination(self):
        self.complete_release()
        command = [sys.executable, str(VERIFIER), str(self.release), '--write-manifest']
        result = subprocess.run(command, capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
        self.assertTrue((self.release / 'release-manifest.json').is_file())
        self.write('tiktok_accounts.json', '{}')
        result = subprocess.run(command, capture_output=True, text=True)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('tiktok_accounts.json', result.stdout + result.stderr)


if __name__ == '__main__':
    unittest.main()
