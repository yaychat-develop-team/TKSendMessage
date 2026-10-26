# PyInstaller onedir; inputs are the validated source/browser staging trees.
from pathlib import Path
import sys
import os
import _ctypes
from PyInstaller.depend.bindepend import get_imports
from PyInstaller.utils.hooks import collect_data_files, collect_submodules, copy_metadata

# Direct invocation of a Conda Python does not activate its DLL search path.
# Let PyInstaller resolve the complete native dependency closure from that
# selected runtime, including lxml/SQLite dependencies, before any analysis.
runtime_bin = Path(sys.base_prefix) / 'Library' / 'bin'
if runtime_bin.is_dir():
    os.environ['PATH'] = str(runtime_bin) + os.pathsep + os.environ.get('PATH', '')

root = Path(SPECPATH).parent
source = root / 'build' / 'work' / 'source_inputs'
browser = root / 'browser' / 'chromium'
if not (source / 'app.py').is_file() or not (browser / 'chrome.exe').is_file():
    raise SystemExit('Run build/build.ps1 first to validate and stage inputs')
sys.path.insert(0, str(source))

datas = collect_data_files('DrissionPage')
datas += [(str(source / 'tiktok_live' / 'algorithms' / 'browser_env.json'), 'tiktok_live/algorithms')]
datas += [(str(source / 'desktop' / 'assets' / 'icon.png'), 'desktop/assets')]
# Browser bytes are data, not Python DLL dependencies. Preserve the vendor tree.
datas += [(str(browser), 'browser/chromium')]
hidden = collect_submodules('DrissionPage') + collect_submodules('websocket')
hidden += collect_submodules('tiktok_live') + ['listen_msg', 'settings', 'curl_cffi']

# Resolve _ctypes' native libffi from the selected Python runtime, not PATH.
# Conda does not put Library/bin on PATH when Python is invoked directly.
ctypes_path = Path(_ctypes.__file__)
runtime_search = [str(ctypes_path.parent), str(Path(sys.base_prefix) / 'Library' / 'bin')]
ctypes_imports = get_imports(str(ctypes_path), search_paths=runtime_search)
native = [(str(ctypes_path), '.')]
for dependency, resolved in ctypes_imports:
    if 'ffi' in dependency.lower():
        if not resolved:
            raise SystemExit('Cannot resolve the selected Python _ctypes libffi dependency')
        native.append((resolved, '.'))

analysis = Analysis(
    [str(source / 'app.py')], pathex=[str(source)],
    binaries=native, datas=datas, hiddenimports=hidden,
    hookspath=[], runtime_hooks=[],
    excludes=['TikTokLive', 'TikTokLiveProto', 'execjs', 'iv8', 'py_mini_racer',
              'PySide6.QtWebEngineCore', 'PySide6.QtWebEngineWidgets',
              'PySide6.QtWebEngineQuick', 'PyQt5', 'PyQt6', 'tkinter',
              'pytest', 'IPython', 'matplotlib', 'pandas', 'scipy',
              'torch', 'tensorflow', 'notebook'],
    noarchive=False,
)
# Qt 6.12 uses the Windows system ICU ABI. Ignore unversioned ICU DLLs
# resolved from unrelated PATH tools (for example Poppler's versioned ABI).
analysis.binaries = [item for item in analysis.binaries
                     if Path(item[0]).name.lower() not in {'icuuc.dll', 'icuin.dll', 'icudt.dll'}]
pyz = PYZ(analysis.pure)
exe = EXE(pyz, analysis.scripts, [], exclude_binaries=True,
          name='TKChatWave', debug=False, strip=False, upx=False,
          console=False, disable_windowed_traceback=False,
          contents_directory='_internal',
          icon=str(source / 'desktop' / 'assets' / 'icon.ico'))
# Use the identical application archive in a console bootloader before opening
# the windowed executable. Keep this probe in build/work, outside the release.
diagnostic = EXE(pyz, analysis.scripts, [], exclude_binaries=True,
                 name='TKChatWave-diagnostic', debug=False, strip=False, upx=False,
                 console=True, contents_directory='_internal')
release = COLLECT(exe, analysis.binaries, analysis.datas,
                  strip=False, upx=False, name='TKChatWave')
# RuntimePaths prefers EXE-adjacent Chromium; move the collected vendor tree
# outside _internal while retaining it as a declared spec resource.
import shutil
shutil.move(str(Path(release.name) / '_internal' / 'browser'),
            str(Path(release.name) / 'browser'))
