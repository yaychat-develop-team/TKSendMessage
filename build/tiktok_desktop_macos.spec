# Build on macOS only. Produces dist-macos/TikTokLiveTool.app with Chromium embedded.
from pathlib import Path
import sys
from PyInstaller.utils.hooks import collect_data_files, collect_submodules, copy_metadata

if sys.platform != 'darwin':
    raise SystemExit('This spec must be built on macOS')

root = Path(SPECPATH).parent
source = root / 'build' / 'work' / 'source_inputs'
browser = root / 'browser' / 'chromium-mac' / 'Chromium.app'
if not (source / 'app.py').is_file() or not (browser / 'Contents' / 'Info.plist').is_file():
    raise SystemExit('Stage validated application inputs and Chromium.app before building')
sys.path.insert(0, str(source))

datas = collect_data_files('DrissionPage')
datas += [(str(source / 'tiktok_live' / 'algorithms' / 'browser_env.json'), 'tiktok_live/algorithms')]
datas += [(str(source / 'desktop' / 'assets' / 'icon.png'), 'desktop/assets')]
datas += [(str(browser), 'browser/chromium/Chromium.app')]
hidden = collect_submodules('DrissionPage') + collect_submodules('websocket')
hidden += collect_submodules('tiktok_live') + ['listen_msg', 'settings', 'curl_cffi']

analysis = Analysis(
    [str(source / 'app.py')], pathex=[str(source)],
    binaries=[], datas=datas, hiddenimports=hidden,
    hookspath=[], runtime_hooks=[],
    excludes=['TikTokLive', 'TikTokLiveProto', 'execjs', 'iv8', 'py_mini_racer',
              'PySide6.QtWebEngineCore', 'PySide6.QtWebEngineWidgets',
              'PySide6.QtWebEngineQuick', 'PyQt5', 'PyQt6', 'tkinter',
              'pytest', 'IPython', 'matplotlib', 'pandas', 'scipy',
              'torch', 'tensorflow', 'notebook'],
    noarchive=False,
)
pyz = PYZ(analysis.pure)
exe = EXE(
    pyz, analysis.scripts, analysis.binaries, analysis.datas, [],
    name='TikTokLiveTool', debug=False, strip=False, upx=False,
    console=False, disable_windowed_traceback=False,
    target_arch=None, codesign_identity=None, entitlements_file=None,
)
app = BUNDLE(
    exe,
    name='TikTokLiveTool.app',
    icon=str(source / 'desktop' / 'assets' / 'icon.png'),
    bundle_identifier='com.tiktoklivetool.desktop',
    info_plist={
        'CFBundleDisplayName': 'TikTok 直播消息工具',
        'NSHighResolutionCapable': True,
    },
)
