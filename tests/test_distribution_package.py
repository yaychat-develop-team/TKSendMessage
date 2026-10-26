import hashlib
import json
import tempfile
import unittest
import zipfile
from pathlib import Path


class DistributionTests(unittest.TestCase):
    def test_only_allowlisted_files_are_distributed_and_existing_data_is_untouched(self):
        from build.create_distribution import create_distribution
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / 'dist'
            (source / 'runtime/accounts').mkdir(parents=True)
            private = source / 'runtime/accounts/private.json'
            private.write_text('synthetic-private', encoding='utf-8')
            exe = source / 'TKChatWave.exe'
            exe.write_bytes(b'MZsynthetic executable')
            (source / 'old.exe').write_bytes(b'old')
            digest = hashlib.sha256(exe.read_bytes()).hexdigest()
            target = root / 'release'
            report = create_distribution(exe, target, version='2026.10.23.1', expected_sha256=digest)
            self.assertEqual({p.name for p in target.iterdir()},
                             {'TKChatWave.exe', '使用说明.txt', '版本信息.json'})
            with zipfile.ZipFile(report['zip']) as archive:
                self.assertEqual(set(archive.namelist()),
                                 {'TKChatWave.exe', '使用说明.txt', '版本信息.json'})
                self.assertEqual(archive.read('TKChatWave.exe'), exe.read_bytes())
                self.assertNotIn(b'synthetic-private', archive.read('使用说明.txt'))
            metadata = json.loads((target / '版本信息.json').read_text(encoding='utf-8'))
            self.assertEqual(metadata['sha256'], digest)
            self.assertEqual(private.read_text(encoding='utf-8'), 'synthetic-private')
            with self.assertRaises(FileExistsError):
                create_distribution(exe, target, version='2026.10.23.1', expected_sha256=digest)

    def test_hash_mismatch_never_creates_a_publishable_package(self):
        from build.create_distribution import create_distribution
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            exe = root / 'TKChatWave.exe'
            exe.write_bytes(b'MZsynthetic executable')
            with self.assertRaises(ValueError):
                create_distribution(exe, root / 'release', version='2026.10.23.1', expected_sha256='0' * 64)
            self.assertFalse((root / 'release').exists())
            self.assertFalse((root / 'release.zip').exists())
