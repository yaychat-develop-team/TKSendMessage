import tempfile
import unittest
import importlib.util
import zipfile
from pathlib import Path


class MacOneClickBuildTests(unittest.TestCase):
    temporary_root = Path(__file__).resolve().parent.parent / '.codex_tmp'

    @staticmethod
    def load_module():
        module_path = Path(__file__).resolve().parent.parent / 'build/macos_one_click.py'
        if not module_path.is_file():
            raise AssertionError('macOS one-click build helper is missing')
        spec = importlib.util.spec_from_file_location('macos_one_click', module_path)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module

    def test_find_browser_selects_first_valid_application_bundle(self):
        find_browser = self.load_module().find_browser
        self.temporary_root.mkdir(exist_ok=True)
        with tempfile.TemporaryDirectory(dir=self.temporary_root) as temporary_directory:
            root = Path(temporary_directory)
            invalid = root / 'Missing Chromium.app'
            valid = root / 'Google Chrome.app'
            (valid / 'Contents').mkdir(parents=True)
            (valid / 'Contents' / 'Info.plist').write_text('plist', encoding='utf-8')

            selected = find_browser([invalid, valid])

            self.assertEqual(selected, valid)

    def test_find_browser_reports_no_usable_bundle(self):
        find_browser = self.load_module().find_browser
        self.temporary_root.mkdir(exist_ok=True)
        with tempfile.TemporaryDirectory(dir=self.temporary_root) as temporary_directory:
            missing = Path(temporary_directory) / 'Missing.app'

            selected = find_browser([missing])

            self.assertIsNone(selected)

    def test_source_packager_preserves_executable_scripts_and_excludes_build_data(self):
        module_path = Path(__file__).resolve().parent.parent / 'build/package_macos_source.py'
        if not module_path.is_file():
            self.fail('macOS source packager is missing')
        spec = importlib.util.spec_from_file_location('package_macos_source', module_path)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)

        self.temporary_root.mkdir(exist_ok=True)
        with tempfile.TemporaryDirectory(dir=self.temporary_root) as temporary_directory:
            root = Path(temporary_directory) / 'project'
            root.mkdir()
            (root / 'Build-macOS.command').write_text('#!/bin/bash\n', encoding='utf-8')
            (root / 'app.py').write_text('print("ok")\n', encoding='utf-8')
            (root / 'build/work').mkdir(parents=True)
            (root / 'build/work/private.log').write_text('private', encoding='utf-8')
            archive = Path(temporary_directory) / 'source.zip'

            module.create_source_archive(root, archive)

            with zipfile.ZipFile(archive) as packaged:
                self.assertIsNone(packaged.testzip())
                self.assertNotIn('project/build/work/private.log', packaged.namelist())
                command = packaged.getinfo('project/Build-macOS.command')
                self.assertTrue((command.external_attr >> 16) & 0o111)


if __name__ == '__main__':
    unittest.main()
