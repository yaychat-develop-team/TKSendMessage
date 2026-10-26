"""Build and package the self-contained macOS application."""

from __future__ import annotations

import os
import platform
import subprocess
import sys
from pathlib import Path
from typing import Iterable


def default_browser_candidates() -> list[Path]:
    applications = Path('/Applications')
    user_applications = Path.home() / 'Applications'
    names = (
        'Google Chrome.app',
        'Google Chrome for Testing.app',
        'Chromium.app',
    )
    return [base / name for base in (applications, user_applications) for name in names]


def find_browser(candidates: Iterable[Path] | None = None) -> Path | None:
    """Return the first candidate that is a usable macOS application bundle."""
    for candidate in candidates or default_browser_candidates():
        candidate = Path(candidate)
        if (candidate / 'Contents' / 'Info.plist').is_file():
            return candidate
    return None


def build_release(root: Path, browser: Path, python_executable: str) -> Path:
    environment = os.environ.copy()
    environment['PYTHON_BIN'] = python_executable
    subprocess.run(
        ['/bin/bash', str(root / 'build' / 'build_macos.sh'), str(browser)],
        cwd=root,
        env=environment,
        check=True,
    )

    application = root / 'dist-macos' / 'TikTokLiveTool.app'
    if not application.is_dir():
        raise RuntimeError(f'构建结束，但没有找到应用：{application}')

    archive = root / 'dist-macos' / 'TikTokLiveTool-mac.zip'
    archive.unlink(missing_ok=True)
    subprocess.run(
        [
            '/usr/bin/ditto', '-c', '-k', '--sequesterRsrc', '--keepParent',
            str(application), str(archive),
        ],
        cwd=root,
        check=True,
    )
    return archive


def main(arguments: list[str] | None = None) -> int:
    arguments = list(sys.argv[1:] if arguments is None else arguments)
    if platform.system() != 'Darwin':
        print('错误：这个构建入口只能在 macOS 上运行。')
        return 2

    explicit = Path(arguments[0]).expanduser() if arguments else None
    browser = find_browser([explicit]) if explicit else find_browser()
    if browser is None:
        print('错误：没有找到 Google Chrome、Chrome for Testing 或 Chromium。')
        print('请先安装其中一个浏览器，然后重新双击构建文件。')
        return 2

    root = Path(__file__).resolve().parent.parent
    print(f'使用浏览器：{browser}')
    try:
        archive = build_release(root, browser, sys.executable)
    except (OSError, subprocess.CalledProcessError, RuntimeError) as error:
        print(f'构建失败：{error}')
        return 1

    print('\n构建完成。可以直接把下面的文件发给最终用户：')
    print(archive)
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
