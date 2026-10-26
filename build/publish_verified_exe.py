"""Publish one verified candidate; never glob or recursively delete executables."""
import hashlib
import os
from pathlib import Path
import shutil
import tempfile

CHECKS = {'unit_suite', 'source_scan', 'loopback_protocol', 'frozen_smoke', 'archive_inventory'}
OLD_NAMES = ('TKChatWave-loginfix.exe', 'TKChatWave-facebook-login-fix.exe',
             'TKChatWave-facebook-login-fix-v2.exe')


def _plain_path(path):
    path = Path(path).absolute()
    for part in (path, *path.parents):
        if part.exists() and (part.is_symlink() or getattr(part.lstat(), 'st_file_attributes', 0) & 0x400):
            raise ValueError('Release path contains link/reparse point')
    return path.resolve()


def publish_verified_exe(candidate, project_root, *, expected_sha256, checks):
    if any(checks.get(name) is not True for name in CHECKS):
        raise ValueError('Release gates have not passed')
    root = _plain_path(project_root)
    candidate = _plain_path(candidate)
    work = _plain_path(root / 'build/work')
    dist = _plain_path(root / 'dist')
    if work not in candidate.parents or not candidate.is_file() or not dist.is_dir():
        raise ValueError('Candidate outside allowed generated directory')
    paths = [_plain_path(dist / name) for name in ('TKChatWave.exe', *OLD_NAMES)]
    if any(path.parent != dist or (path.exists() and not path.is_file()) for path in paths):
        raise ValueError('Invalid release destination')
    digest = hashlib.sha256(candidate.read_bytes()).hexdigest()
    if digest.lower() != str(expected_sha256).lower():
        raise ValueError('Candidate hash mismatch')
    stage = None
    try:
        with tempfile.NamedTemporaryFile(prefix='.verified-', suffix='.tmp', dir=dist, delete=False) as stream:
            stage = Path(stream.name)
            with candidate.open('rb') as source:
                shutil.copyfileobj(source, stream)
            stream.flush()
            os.fsync(stream.fileno())
        if hashlib.sha256(stage.read_bytes()).hexdigest() != digest:
            raise ValueError('Candidate changed during copy')
        os.replace(stage, paths[0])  # no old variant is removed if this fails
        stage = None
    finally:
        if stage is not None and stage.exists():
            stage.unlink()
    removed, remaining = [], []
    for path in paths[1:]:
        if path.exists():
            try:
                path.unlink()
                removed.append(path.name)
            except OSError:
                remaining.append(path.name)
    remaining += [path.name for path in dist.glob('*.exe') if path.name != 'TKChatWave.exe'
                  and path.name not in remaining]
    return dict(published=str(paths[0]), sha256=digest, removed=removed, remaining=remaining)
