"""Fail-closed release inventory and sensitive-data checks (stdlib only).

Names and actual stored values are checked; a module mentioning a credential
field is not a credential. Findings contain paths/classifications, never values.
"""
import argparse
import base64
import binascii
import ast
import hashlib
import json
import os
import re
from pathlib import Path

FORBIDDEN_PARTS = {
    'runtime', 'profiles', 'reports', 'logs', 'cache', 'code cache', 'gpucache',
    '.browser-profile', 'user data', 'default', '__pycache__', '.pytest_cache',
    'port', 'tiktok_accounts.json', 'tiktok_accounts.json.tmp',
    'failed_accounts.json', 'silenced_accounts.json', 'cookies', 'history',
    'login data', 'local state', 'preferences', 'network persistent state',
    'crashpad', 'crash reports', 'local storage', 'session storage', 'indexeddb',
    'service worker', 'sessions', 'shadercache', 'dawncache', 'grshadercache',
}
TEXT_SUFFIXES = {'.py', '.json', '.txt', '.ini', '.cfg', '.yaml', '.yml', '.toml', '.js', '.pem', '.key'}
SECRET_FIELDS = r'(?:sessionid(?:_ss)?|sid_tt|uid_tt(?:_ss)?|msToken|(?:tt[_-])?csrf(?:[_-]?token)?|private[_-]?key|ticket_guard_private_key|private_key_pem|ec_privateKey|encrypt_ticket|ts_sign|req_sign|_?ticket[_-]guard[_-]client[_-](?:data|csr)|(?:x-)?tt-ticket-guard-client-(?:data|csr)|X-Gnarly|X-Bogus|_signature|ttwid|ROOM_ID_COOKIE)'
SECRET_VALUE = re.compile(
    rf'[\"\']?{SECRET_FIELDS}[\"\']?\s*[:=]\s*[\"\']([^\"\'\r\n]+)[\"\']', re.I)
COOKIE_VALUE = re.compile(r'\b(?:sessionid(?:_ss)?|sid_tt|uid_tt(?:_ss)?|ttwid|msToken|csrf_token)\s*=\s*([A-Za-z0-9_.+/%=-]+)', re.I)
COOKIE_OBJECT = re.compile(r'[\"\']name[\"\']\s*:\s*[\"\'](?:sessionid(?:_ss)?|sid_tt|uid_tt(?:_ss)?|ttwid|msToken|csrf_token)[\"\']\s*,\s*[\"\']value[\"\']\s*:\s*[\"\']([^\"\']+)', re.I)
SYSTEM_ICU = ('icuuc.dll', 'icuin.dll', 'icudt.dll')
PLACEHOLDERS = {'', 'none', 'null', 'undefined', '[redacted]', '<redacted>'}


def _contains_private_literal(value) -> bool:
    fields = re.compile(SECRET_FIELDS, re.I)
    def private(child):
        return (isinstance(child, (str, int, float, bool))
                and str(child or '').strip().lower() not in PLACEHOLDERS)
    if isinstance(value, dict):
        if fields.fullmatch(str(value.get('name', ''))) and private(value.get('value')):
            return True
        return any((fields.fullmatch(str(key)) and private(child))
                   or _contains_private_literal(child) for key, child in value.items())
    return isinstance(value, list) and any(_contains_private_literal(child) for child in value)


def _encoded_private_literal(text: str) -> bool:
    # Check payload content even if a captured base64 JSON blob was renamed.
    # Field names, schema descriptions and dynamically sourced values stay safe.
    for match in re.finditer(r'[A-Za-z0-9_+/-]{32,}={0,2}', text):
        encoded = match.group()
        if len(encoded) > 1024 * 1024:
            continue
        try:
            decoded = base64.b64decode(encoded + '=' * (-len(encoded) % 4), altchars=b'-_', validate=True)
            if decoded.startswith((b'{', b'[')) and _contains_private_literal(json.loads(decoded)):
                return True
        except (ValueError, UnicodeError, binascii.Error):
            continue
    return False


def scan_file(path: Path, label: str | None = None) -> list[str]:
    label = label or path.name
    if path.is_symlink() or (getattr(path.lstat(), 'st_file_attributes', 0) & 0x400):
        return [f'{label}: external link/reparse point']
    if path.suffix.lower() not in TEXT_SUFFIXES:
        return []
    try:
        raw = path.read_bytes()
        encoding = ('utf-32' if raw.startswith((b'\xff\xfe\x00\x00', b'\x00\x00\xfe\xff'))
                    else 'utf-16' if raw.startswith((b'\xff\xfe', b'\xfe\xff'))
                    else 'utf-8-sig')
        text = raw.decode(encoding)
    except OSError:
        return [f'{label}: unreadable input']
    except UnicodeDecodeError:
        return [f'{label}: undecodable text input']
    texts = [text]
    if path.suffix.lower() == '.py':
        try:
            nodes = list(ast.walk(ast.parse(text)))
        except SyntaxError:
            return [f'{label}: invalid Python input']
        fields = re.compile(SECRET_FIELDS, re.I)
        for node in nodes:
            pairs = []
            if isinstance(node, (ast.Assign, ast.AnnAssign)):
                targets = node.targets if isinstance(node, ast.Assign) else [node.target]
                for target in targets:
                    if isinstance(target, ast.Name):
                        pairs.append((target.id, node.value))
                    elif isinstance(target, ast.Attribute):
                        pairs.append((target.attr, node.value))
                    elif (isinstance(target, ast.Subscript) and isinstance(target.slice, ast.Constant)
                          and isinstance(target.slice.value, str)):
                        pairs.append((target.slice.value, node.value))
            elif isinstance(node, ast.Dict):
                pairs = [(key.value, value) for key, value in zip(node.keys, node.values)
                         if isinstance(key, ast.Constant) and isinstance(key.value, str)]
            elif isinstance(node, ast.keyword) and node.arg:
                pairs = [(node.arg, node.value)]
            if any(fields.fullmatch(key) and isinstance(value, ast.Constant)
                   and str(value.value or '').strip().lower() not in PLACEHOLDERS for key, value in pairs):
                return [f'{label}: literal credential in Python input']
        texts = [node.value for node in nodes if isinstance(node, ast.Constant) and isinstance(node.value, str)]
    if any(_encoded_private_literal(value) for value in texts):
        return [f'{label}: encoded credential-bearing content']
    matches = [match for value in texts for pattern in (SECRET_VALUE, COOKIE_VALUE, COOKIE_OBJECT)
               for match in pattern.finditer(value)]
    if (any('-----BEGIN PRIVATE KEY-----' in value or '-----BEGIN EC PRIVATE KEY-----' in value
            or '-----BEGIN RSA PRIVATE KEY-----' in value for value in texts)
            or any(match.group(1).lower() not in PLACEHOLDERS for match in matches)):
        return [f'{label}: credential-bearing content']
    # Parse cookie objects without assuming their JSON property order.
    if path.suffix.lower() == '.json':
        try:
            data = json.loads(text)
        except ValueError:
            return []
        if _contains_private_literal(data):
            return [f'{label}: stored browser cookie']
    return []


def scan_sensitive_files(root: Path) -> list[str]:
    root = Path(root)
    if not root.is_dir():
        return ['release directory missing']
    problems = []
    for path in sorted(root.rglob('*')):
        relative = path.relative_to(root).as_posix()
        if any(part.lower() in FORBIDDEN_PARTS or re.fullmatch(r'profile[ _-]?\d+', part, re.I)
               for part in path.relative_to(root).parts) or path.suffix.lower() == '.log':
            problems.append(f'{relative}: runtime/private-data path')
        elif path.is_symlink() or (getattr(path.lstat(), 'st_file_attributes', 0) & 0x400):
            problems.append(f'{relative}: external link/reparse point')
        elif path.is_file():
            problems.extend(scan_file(path, relative))
    return problems


def verify_release(root: Path) -> list[str]:
    root = Path(root)
    problems = scan_sensitive_files(root)
    required = (
        'TKChatWave.exe', '_internal/_ctypes.pyd', '_internal/ffi.dll',
        '_internal/_sqlite3.pyd', '_internal/sqlite3.dll', '_internal/libxml2.dll',
        '_internal/libxslt.dll', '_internal/libexslt.dll',
        '_internal/PySide6/QtCore.pyd',
        '_internal/PySide6/QtGui.pyd', '_internal/PySide6/QtWidgets.pyd',
        '_internal/PySide6/Qt6Core.dll', '_internal/PySide6/Qt6Gui.dll',
        '_internal/PySide6/Qt6Widgets.dll', '_internal/PySide6/pyside6.abi3.dll',
        '_internal/shiboken6/Shiboken.pyd', '_internal/shiboken6/shiboken6.abi3.dll',
        '_internal/PySide6/plugins/platforms/qwindows.dll',
        '_internal/PySide6/plugins/platforms/qoffscreen.dll',
        '_internal/DrissionPage/_configs/configs.ini',
        '_internal/tiktok_live/algorithms/browser_env.json',
        'browser/chromium/chrome.exe', 'browser/chromium/chrome.dll',
        'browser/chromium/icudtl.dat', 'browser/chromium/resources.pak',
        'browser/chromium/locales/en-US.pak',
    )
    for relative in required:
        file = root / relative
        if not file.is_file() or not file.stat().st_size:
            problems.append(f'{relative}: required runtime dependency missing/empty')
    for name in SYSTEM_ICU:
        if (root / '_internal' / name).exists():
            problems.append(f'_internal/{name}: shadows Windows system ICU')
    for pattern in ('python3*.dll', 'curl_cffi/*wrapper*.pyd'):
        if not any(p.is_file() and p.stat().st_size for p in (root / '_internal').glob(pattern)):
            problems.append(f'_internal/{pattern}: required runtime dependency missing/empty')
    return problems


def validate_build_paths(project_root: Path, browser_input: Path) -> list[str]:
    """Reject destructive/output overlap in both lexical and physical paths."""
    project_lexical = Path(os.path.abspath(project_root))
    input_lexical = Path(os.path.abspath(browser_input))
    try:
        project_physical = project_lexical.resolve(strict=True)
        input_physical = input_lexical.resolve(strict=True)
        for relative in ('build/work', 'build/work/source_inputs', 'build/work/pyinstaller',
                         'build/work/pyinstaller-config', 'build/work/pyinstaller-config/pyinstaller',
                         'browser/chromium', 'dist',
                         'dist/TKChatWave'):
            target_lexical = project_lexical / relative
            target_physical = target_lexical.resolve()
            if (target_physical == project_physical
                    or not target_physical.is_relative_to(project_physical)):
                return [f'{relative}: generated staging/output path escapes project through a reparse alias']
            for supplied, generated in ((input_lexical, target_lexical),
                                         (input_physical, target_physical)):
                if (supplied == generated or supplied.is_relative_to(generated)
                        or generated.is_relative_to(supplied)):
                    return [f'{relative}: Chromium input overlaps generated staging/output path']
    except (OSError, RuntimeError):
        return ['generated staging/input path could not be safely resolved']
    return []


def generate_manifest(root: Path) -> dict:
    root = Path(root)
    entries = []
    for path in sorted(root.rglob('*')):
        if not path.is_file() or path.name == 'release-manifest.json':
            continue
        digest = hashlib.sha256()
        with path.open('rb') as stream:
            for chunk in iter(lambda: stream.read(1024 * 1024), b''):
                digest.update(chunk)
        entries.append({'path': path.relative_to(root).as_posix(),
                        'size': path.stat().st_size, 'sha256': digest.hexdigest()})
    return {'format': 1, 'files': entries}


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('release', type=Path)
    parser.add_argument('--write-manifest', action='store_true')
    parser.add_argument('--scan-only', action='store_true')
    parser.add_argument('--build-project-root', type=Path,
                        help='validate Chromium input against every generated build directory before mutation')
    parser.add_argument('--remove-conflicting-icu', action='store_true',
                        help='remove unversioned third-party ICU DLLs shadowing Windows ICU')
    args = parser.parse_args(argv)
    if args.build_project_root is not None:
        path_problems = validate_build_paths(args.build_project_root, args.release)
        if path_problems:
            print('Build path validation failed:\n' + '\n'.join(path_problems))
            return 1
    problems = scan_sensitive_files(args.release) if args.scan_only else verify_release(args.release)
    if args.remove_conflicting_icu and not args.scan_only:
        conflicts = {f'_internal/{name}: shadows Windows system ICU' for name in SYSTEM_ICU}
        if not any(problem not in conflicts for problem in problems):
            for name in SYSTEM_ICU:
                candidate = args.release / '_internal' / name
                if candidate.is_file():
                    candidate.unlink()
            problems = verify_release(args.release)
    if problems:
        print('Release verification failed:')
        print('\n'.join(problems))
        return 1
    if args.write_manifest:
        (args.release / 'release-manifest.json').write_text(
            json.dumps(generate_manifest(args.release), ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    print('Release verification passed')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
