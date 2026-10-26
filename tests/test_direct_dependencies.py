import subprocess
import sys
import unittest
from pathlib import Path


class DirectDependencyTests(unittest.TestCase):
    def test_import_and_smoke_work_with_old_libraries_denied(self):
        script = '''
import sys
class Deny:
    def find_spec(self, fullname, *a):
        if fullname.split('.')[0] in {'TikTokLive', 'TikTokLiveProto', 'iv8', 'execjs', 'py_mini_racer'}:
            raise ImportError('forbidden dependency')
sys.meta_path.insert(0, Deny())
import listen_msg
from tiktok_live import heartbeat, runner, browser_auth
from tiktok_live.wss.direct_signer import self_test
assert self_test()
assert not any(n.split('.')[0] in {'TikTokLive','TikTokLiveProto'} for n in sys.modules)
print('direct dependencies verified')
'''
        result = subprocess.run([sys.executable, '-X', 'utf8', '-c', script],
            capture_output=True, text=True, timeout=30)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_specs_exclude_old_runtime_and_include_new_transport(self):
        root = Path(__file__).resolve().parents[1]
        requirements = (root / 'requirements.txt').read_text(encoding='utf-8-sig')
        self.assertNotIn('\nTikTokLive', requirements)
        self.assertIn('websocket-client==1.7.0', requirements)
        for name in ('tiktok_desktop.spec', 'tiktok_desktop_onefile.spec', 'tiktok_desktop_macos.spec'):
            spec = (root / 'build' / name).read_text(encoding='utf-8-sig')
            self.assertNotIn("collect_submodules('TikTokLive')", spec)
            self.assertNotIn("collect_data_files('TikTokLive')", spec)
            self.assertIn("collect_submodules('websocket')", spec)
            self.assertIn("'TikTokLiveProto'", spec)
