# PyInstaller single-file release. Chromium is extracted to _MEIPASS at launch.
from pathlib import Path
import sys
import os
import _ctypes
from PyInstaller.depend.bindepend import get_imports
from PyInstaller.utils.hooks import collect_data_files, collect_submodules, copy_metadata

runtime_bin = Path(sys.base_prefix) / 'Library' / 'bin'
if runtime_bin.is_dir():
    os.environ['PATH'] = str(runtime_bin) + os.pathsep + os.environ.get('PATH', '')

root = Path(SPECPATH).parent
source = Path(os.environ.get('TIKTOK_BUILD_SOURCE') or root / 'build' / 'work' / 'source_inputs')
browser = root / 'browser' / 'chromium'
if not (source / 'app.py').is_file() or not (browser / 'chrome.exe').is_file():
    raise SystemExit('Stage validated application inputs before building')
sys.path.insert(0, str(source))

datas = collect_data_files('DrissionPage')
datas += [(str(source / 'tiktok_live' / 'algorithms' / 'browser_env.json'), 'tiktok_live/algorithms')]
datas += [(str(source / 'desktop' / 'assets' / 'icon.png'), 'desktop/assets')]
datas += [(str(browser), 'browser/chromium')]
hidden = collect_submodules('DrissionPage') + collect_submodules('websocket')
hidden += collect_submodules('tiktok_live') + ['listen_msg', 'settings', 'curl_cffi']

ctypes_path = Path(_ctypes.__file__)
runtime_search = [str(ctypes_path.parent), str(Path(sys.base_prefix) / 'Library' / 'bin')]
native = [(str(ctypes_path), '.')]
for dependency, resolved in get_imports(str(ctypes_path), search_paths=runtime_search):
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
analysis.binaries = [item for item in analysis.binaries
                     if Path(item[0]).name.lower() not in {'icuuc.dll', 'icuin.dll', 'icudt.dll'}]
pyz = PYZ(analysis.pure)
exe = EXE(
    pyz, analysis.scripts, analysis.binaries, analysis.datas, [],
    name='TKChatWave', debug=False, strip=False, upx=False,
    console=False, disable_windowed_traceback=False,
    icon=str(source / 'desktop' / 'assets' / 'icon.ico'),
)
