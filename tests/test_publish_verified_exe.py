import hashlib
import importlib.util
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch


class PublishTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.dist = self.root / 'dist'
        self.dist.mkdir()
        self.candidate = self.root / 'build/work/candidate/TKChatWave.exe'
        self.candidate.parent.mkdir(parents=True)
        self.candidate.write_bytes(b'new verified executable')
        self.digest = hashlib.sha256(self.candidate.read_bytes()).hexdigest()
        self.checks = dict.fromkeys(('unit_suite','source_scan','loopback_protocol','frozen_smoke','archive_inventory'), True)
        path = Path(__file__).resolve().parents[1] / 'build/publish_verified_exe.py'
        self.assertTrue(path.is_file(), 'publisher missing')
        spec = importlib.util.spec_from_file_location('publisher', path)
        self.module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.module)
        self.names = ['TKChatWave.exe','TKChatWave-loginfix.exe','TKChatWave-facebook-login-fix.exe','TKChatWave-facebook-login-fix-v2.exe']
        for name in self.names + ['unknown.exe']:
            (self.dist / name).write_bytes(b'old')
        (self.dist / 'runtime').mkdir()
        (self.dist / 'runtime/keep.exe').write_bytes(b'private untouched')

    def publish(self, **kwargs):
        return self.module.publish_verified_exe(self.candidate, self.root,
            expected_sha256=kwargs.get('digest', self.digest), checks=kwargs.get('checks', self.checks))

    def test_unverified_corrupt_or_reparse_candidate_deletes_nothing(self):
        for options in ({'digest':'0'*64}, {'checks':{}}, {'checks':{**self.checks,'unit_suite':False}}):
            with self.assertRaises(ValueError):
                self.publish(**options)
            self.assertTrue(all((self.dist / n).read_bytes() == b'old' for n in self.names))
        with patch.object(self.module, '_plain_path', side_effect=ValueError('reparse')):
            with self.assertRaises(ValueError):
                self.publish()

    def test_locked_destination_keeps_candidate_and_old_versions(self):
        with patch.object(self.module.os, 'replace', side_effect=PermissionError('locked')):
            with self.assertRaises(PermissionError):
                self.publish()
        self.assertTrue(self.candidate.is_file())
        self.assertTrue(all((self.dist / n).read_bytes() == b'old' for n in self.names))

    def test_publish_removes_only_known_top_level_app_versions(self):
        result = self.publish()
        self.assertEqual((self.dist / self.names[0]).read_bytes(), b'new verified executable')
        self.assertEqual(set(result['removed']), set(self.names[1:]))
        self.assertEqual((self.dist/'unknown.exe').read_bytes(), b'old')
        self.assertEqual((self.dist/'runtime/keep.exe').read_bytes(), b'private untouched')
        self.assertTrue(self.candidate.is_file())
