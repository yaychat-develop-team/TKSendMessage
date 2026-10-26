"""Create a small macOS build-source ZIP with Unix script permissions."""

from __future__ import annotations

import argparse
import time
import zipfile
from pathlib import Path, PurePosixPath


EXCLUDED_DIRECTORIES = {
    '.git',
    '.superpowers',
    '.codex_tmp',
    '__pycache__',
    'browser',
    'dist',
    'dist-macos',
    'runtime',
}
EXCLUDED_SUFFIXES = {'.pyc', '.pyo', '.zip'}


def _is_excluded(relative_path: Path) -> bool:
    parts = relative_path.parts
    if any(part in EXCLUDED_DIRECTORIES for part in parts):
        return True
    if len(parts) >= 2 and parts[0] == 'build' and parts[1] == 'work':
        return True
    return relative_path.suffix.lower() in EXCLUDED_SUFFIXES


def create_source_archive(root: Path, output: Path) -> Path:
    root = Path(root).resolve()
    output = Path(output).resolve()
    temporary_output = output.with_suffix(output.suffix + '.tmp')
    output.parent.mkdir(parents=True, exist_ok=True)
    temporary_output.unlink(missing_ok=True)

    try:
        with zipfile.ZipFile(
            temporary_output,
            mode='w',
            compression=zipfile.ZIP_DEFLATED,
            compresslevel=9,
        ) as archive:
            for path in sorted(root.rglob('*')):
                if not path.is_file() or path.is_symlink():
                    continue
                relative_path = path.relative_to(root)
                if _is_excluded(relative_path) or path.resolve() == output:
                    continue

                timestamp = time.localtime(path.stat().st_mtime)[:6]
                if timestamp[0] < 1980:
                    timestamp = (1980, 1, 1, 0, 0, 0)
                archive_name = str(PurePosixPath(root.name, *relative_path.parts))
                info = zipfile.ZipInfo(archive_name, date_time=timestamp)
                info.create_system = 3
                mode = 0o100755 if path.suffix in {'.command', '.sh'} else 0o100644
                info.external_attr = mode << 16
                info.compress_type = zipfile.ZIP_DEFLATED
                archive.writestr(info, path.read_bytes(), compresslevel=9)

        temporary_output.replace(output)
    except BaseException:
        temporary_output.unlink(missing_ok=True)
        raise
    return output


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument('output', type=Path)
    arguments = parser.parse_args()
    project_root = Path(__file__).resolve().parent.parent
    archive = create_source_archive(project_root, arguments.output)
    print(archive)
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
