import unittest
from pathlib import Path


class MacBuildConfigTests(unittest.TestCase):
    def test_macos_spec_embeds_chromium_and_builds_app_bundle(self):
        root = Path(__file__).resolve().parent.parent
        spec = (root / 'build/tiktok_desktop_macos.spec').read_text(encoding='utf-8-sig')
        script = (root / 'build/build_macos.sh').read_text(encoding='utf-8-sig')
        self.assertIn("sys.platform != 'darwin'", spec)
        self.assertIn("root / 'browser' / 'chromium-mac' / 'Chromium.app'", spec)
        self.assertIn('BUNDLE(', spec)
        self.assertIn('dist-macos/TikTokLiveTool.app', script)
        self.assertIn('Application Support/TikTokLiveTool/runtime', script)


if __name__ == '__main__':
    unittest.main()

