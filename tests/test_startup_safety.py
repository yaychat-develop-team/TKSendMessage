import importlib
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
from PySide6.QtWidgets import QApplication


class StartupSafetyTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.qt = QApplication.instance() or QApplication([])

    def test_data_permission_failure_is_reported_without_exception_contents(self):
        module = importlib.import_module('app')
        from desktop.single_instance import SingleInstance
        with tempfile.TemporaryDirectory() as directory:
            reports = []
            with patch.object(module, 'SingleInstance', side_effect=lambda: SingleInstance(Path(directory))), \
                    patch.object(module.RuntimePaths, 'from_application', side_effect=PermissionError('Cookie: synthetic-secret')), \
                    patch.object(module, 'show_startup_error', side_effect=reports.append):
                self.assertEqual(module.main(['startup-test']), 1)
            self.assertEqual(reports, ['startup_data_permission'])
            again = SingleInstance(Path(directory))
            try:
                self.assertTrue(again.acquire(), 'failed startup retained instance lock')
            finally:
                again.close()

    def test_duplicate_never_initializes_data_or_business_window(self):
        module = importlib.import_module('app')
        from desktop.single_instance import SingleInstance
        with tempfile.TemporaryDirectory() as directory:
            owner = SingleInstance(Path(directory))
            try:
                self.assertTrue(owner.acquire())
                with patch.object(module, 'SingleInstance', side_effect=lambda: SingleInstance(Path(directory))), \
                        patch.object(module.QMessageBox, 'information', return_value=None), \
                        patch.object(module.RuntimePaths, 'from_application', side_effect=AssertionError('duplicate touched data')), \
                        patch.object(module, 'MainWindow', side_effect=AssertionError('duplicate made business window')):
                    self.assertEqual(module.main(['startup-test']), 0)
            finally:
                owner.close()

    def test_error_dialog_shows_actionable_plain_text_without_credentials(self):
        from PySide6.QtWidgets import QMessageBox
        from desktop.startup_errors import show_startup_error
        seen = []
        def inspect(dialog):
            seen.append((dialog.windowTitle(), dialog.text()))
            return QMessageBox.StandardButton.Ok
        with patch.object(QMessageBox, 'exec', inspect):
            show_startup_error('startup_data_permission')
            show_startup_error('unknown')
        self.assertIn('写入', seen[0][1])
        self.assertIn('移动', seen[0][1])
        self.assertNotIn('unknown', seen[1][1])

    def test_existing_readonly_runtime_is_detected_before_window_creation(self):
        from desktop.paths import RuntimePaths
        with tempfile.TemporaryDirectory() as directory:
            paths = RuntimePaths.from_application(Path(directory))
            paths.account_store.write_text('synthetic-preserve', encoding='utf-8')
            with patch('desktop.paths.tempfile.NamedTemporaryFile', side_effect=PermissionError('denied')):
                with self.assertRaises(PermissionError):
                    RuntimePaths.from_application(Path(directory))
            self.assertEqual(paths.account_store.read_text(encoding='utf-8'), 'synthetic-preserve')

    def test_unwritable_instance_directory_is_not_misreported_as_exe_directory(self):
        from desktop.single_instance import SingleInstance, InstanceError
        with tempfile.TemporaryDirectory() as directory:
            with patch('desktop.single_instance.Path.mkdir', side_effect=PermissionError('denied')):
                with self.assertRaises(InstanceError):
                    SingleInstance(Path(directory))
