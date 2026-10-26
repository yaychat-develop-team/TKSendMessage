"""Test a disposable release copy without installed Chrome or network login."""
import argparse
import json
import os
import shutil
import subprocess
import tempfile
from pathlib import Path


def run_smoke_process(command, *, cwd=None, env=None, timeout=45):
    """Chromium emits UTF-8 diagnostics independently of Windows' ANSI locale."""
    return subprocess.run(command, cwd=cwd, env=env, timeout=timeout,
                          capture_output=True, encoding='utf-8', errors='replace',
                          creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))


def run_startup_smoke(release: Path, diagnostic_exe: Path, env: dict):
    """Prove the same frozen archive starts in a console before one GUI launch."""
    if not diagnostic_exe.is_file():
        raise SystemExit('Console startup diagnostic missing; windowed smoke was not launched')
    probe = release / 'TKChatWave-diagnostic.exe'
    shutil.copy2(diagnostic_exe, probe)
    result = run_smoke_process([str(probe), '--smoke-test'], cwd=release.parent, env=env, timeout=45)
    if result.returncode:
        raise SystemExit(f'Frozen console startup preflight failed ({result.returncode}):\n{result.stderr}')
    probe.unlink()
    result = run_smoke_process([str(release / 'TKChatWave.exe'), '--smoke-test'],
                              cwd=release.parent, env=env, timeout=45)
    if result.returncode:
        raise SystemExit(f'Frozen application startup failed ({result.returncode})')


def run_onefile_smoke(candidate: Path) -> dict:
    """Never launch a candidate beside the user's real dist/runtime."""
    with tempfile.TemporaryDirectory(prefix='TKChatWave-onefile-') as directory:
        root = Path(directory)
        executable = root / 'TKChatWave.exe'
        shutil.copy2(candidate, executable)
        env = dict(os.environ, QT_QPA_PLATFORM='offscreen')
        for key in ('PYTHONHOME', 'PYTHONPATH', 'QT_PLUGIN_PATH',
                    'TIKTOK_BROWSER_EXECUTABLE', 'CHROME_BIN', 'CHROMIUM_PATH'):
            env.pop(key, None)
        env['PATH'] = os.path.join(env.get('SystemRoot', r'C:\Windows'), 'System32')
        result = run_smoke_process([str(executable), '--smoke-test'], cwd=root, env=env, timeout=120)
        if result.returncode:
            raise SystemExit(f'Frozen onefile startup failed ({result.returncode})')
        report = json.loads((root / 'runtime/smoke-result.json').read_text(encoding='utf-8'))
        keys = ('window_visible', 'bundled_browser', 'empty_accounts', 'dependencies_loaded')
        if not all(report.get(key) is True for key in keys):
            raise SystemExit('Frozen onefile diagnostic failed')
        if (root / 'runtime/accounts/tiktok_accounts.json').exists():
            raise SystemExit('Onefile smoke created an account store')
        return {key: report[key] for key in keys}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('release', type=Path)
    parser.add_argument('--onefile', action='store_true')
    parser.add_argument('--report', type=Path)
    parser.add_argument('--diagnostic-exe', type=Path, default=Path(__file__).resolve().parent /
                        'work/pyinstaller/tiktok_desktop/TKChatWave-diagnostic.exe')
    args = parser.parse_args(argv)
    if args.onefile:
        report = run_onefile_smoke(args.release)
        if args.report:
            args.report.write_text(json.dumps(report), encoding='utf-8')
        print(json.dumps(report))
        return 0
    with tempfile.TemporaryDirectory(prefix='TKChatWave-smoke-') as temp:
        temporary = Path(temp)
        release = temporary / 'release'
        shutil.copytree(args.release, release)
        env = dict(os.environ)
        for name in ('TIKTOK_BROWSER_EXECUTABLE', 'CHROME_BIN', 'CHROMIUM_PATH',
                     'PYTHONHOME', 'PYTHONPATH', 'QT_PLUGIN_PATH'):
            env.pop(name, None)
        env['QT_QPA_PLATFORM'] = 'offscreen'
        env['PATH'] = os.path.join(env.get('SystemRoot', r'C:\Windows'), 'System32')
        run_startup_smoke(release, args.diagnostic_exe, env)
        smoke = json.loads((release / 'runtime/smoke-result.json').read_text(encoding='utf-8'))
        if not all(smoke.get(key) is True for key in (
                'window_visible', 'bundled_browser', 'empty_accounts', 'dependencies_loaded')):
            raise SystemExit('Frozen application diagnostic checks failed')
        if (release / 'runtime/accounts/tiktok_accounts.json').exists():
            raise SystemExit('Smoke unexpectedly created an account store')
        print('Frozen startup smoke passed')
        # Use the same local CDP automation as login. Chrome's --dump-dom CLI
        # can keep background child pipes open on Windows after the DOM is ready.
        import socket
        from DrissionPage import Chromium, ChromiumOptions
        with socket.socket() as port_probe:
            port_probe.bind(('127.0.0.1', 0))
            port = port_probe.getsockname()[1]
        options = ChromiumOptions().set_browser_path(str(release / 'browser/chromium/chrome.exe'))
        options.set_user_data_path(str(temporary / 'chromium-profile'))
        options.set_local_port(port).headless(True).set_timeouts(base=10, page_load=10)
        for flag in ('--no-first-run', '--no-default-browser-check', '--disable-background-networking',
                     '--disable-component-update', '--disable-sync', '--disable-extensions'):
            options.set_argument(flag)
        browser = None
        try:
            browser = Chromium(options)
            page = browser.latest_tab
            page.get('about:blank')
            if '<html' not in page.html:
                raise SystemExit('Bundled Chromium blank-page smoke failed')
        finally:
            if browser is not None:
                browser.quit(timeout=10, force=True)
        print('Frozen startup and bundled Chromium smoke passed (no login/send)')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
