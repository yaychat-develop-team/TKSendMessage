#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

if [[ "$(uname -s)" != "Darwin" ]]; then
  echo "错误：macOS 应用只能在 macOS 上构建。"
  exit 2
fi
if [[ $# -ne 1 || ! -d "$1/Contents" ]]; then
  echo "用法：./build/build_macos.sh /Applications/Chromium.app"
  echo "也可传入 Chrome for Testing 的 .app 路径。"
  exit 2
fi

PYTHON_BIN="${PYTHON_BIN:-python3}"
STAGE="$ROOT/build/work/source_inputs"
BROWSER_STAGE="$ROOT/browser/chromium-mac/Chromium.app"

"$PYTHON_BIN" -m pip install -r requirements-build.txt
mkdir -p "$STAGE/desktop" "$STAGE/tiktok_live" "$(dirname "$BROWSER_STAGE")"
rsync -a --delete --exclude '__pycache__' desktop/ "$STAGE/desktop/"
rsync -a --delete --exclude '__pycache__' tiktok_live/ "$STAGE/tiktok_live/"
cp app.py listen_msg.py send_live_message.py settings.py "$STAGE/"
rm -rf "$BROWSER_STAGE"
ditto "$1" "$BROWSER_STAGE"

"$PYTHON_BIN" -m unittest -q tests.test_desktop_settings tests.test_desktop_paths tests.test_desktop_workers tests.test_drission_browser_auth tests.test_main_window tests.test_regression tests.test_saved_request_identity tests.test_structure
"$PYTHON_BIN" -m compileall -q app.py desktop tiktok_live build tests
"$PYTHON_BIN" -m PyInstaller --noconfirm --clean --distpath dist-macos --workpath build/work/pyinstaller-macos build/tiktok_desktop_macos.spec

echo "完成：$ROOT/dist-macos/TikTokLiveTool.app"
echo "账号和 Cookie 将保存在 ~/Library/Application Support/TikTokLiveTool/runtime/"
