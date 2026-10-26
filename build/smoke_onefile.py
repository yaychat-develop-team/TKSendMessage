"""Validate a candidate EXE in a disposable directory without user accounts."""
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile


with tempfile.TemporaryDirectory(prefix='tiktok-csrf-smoke-') as directory:
    root = Path(directory)
    exe = root / 'TKChatWave.exe'
    shutil.copy2(sys.argv[1], exe)
    env = dict(os.environ, QT_QPA_PLATFORM='offscreen')
    for key in ('PYTHONHOME', 'PYTHONPATH', 'QT_PLUGIN_PATH',
                'TIKTOK_BROWSER_EXECUTABLE', 'CHROME_BIN', 'CHROMIUM_PATH'):
        env.pop(key, None)
    result = subprocess.run(
        [str(exe), '--smoke-test'], cwd=root, env=env, timeout=120,
        capture_output=True, creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0),
    )
    if result.returncode:
        raise SystemExit(f'Candidate startup failed: exit={result.returncode}')
    report = json.loads((root / 'runtime/smoke-result.json').read_text(encoding='utf-8'))
    required = ('window_visible', 'bundled_browser', 'empty_accounts', 'dependencies_loaded')
    if not all(report.get(key) is True for key in required):
        raise SystemExit('Candidate startup diagnostic failed')
    print(json.dumps({key: report[key] for key in required}))
