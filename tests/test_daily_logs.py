import os
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace

os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
from PySide6.QtWidgets import QApplication
from desktop import log_bridge
from desktop.main_window import MainWindow
from desktop.paths import RuntimePaths


class DailyLogTests(unittest.TestCase):
    def test_context_metadata_is_safe_and_unknown_extra_is_not_logged(self):
        from loguru import logger
        with tempfile.TemporaryDirectory() as directory:
            handle = logger.add(log_bridge.DailyLogSink(directory), format='{message}')
            try:
                with logger.contextualize(target_room='@room1\n', sender_account='',
                        sender_id='abcdef123456', raw_response='sessionid=private'):
                    logger.info('safe body')
            finally:
                logger.remove(handle)
            text = next(Path(directory).glob('*.log')).read_text(encoding='utf-8')
        self.assertIn('room=room1 | account_username=abcdef123456 | account_id=abcdef123456 | safe body', text)
        self.assertEqual(len(text.splitlines()), 1)
        self.assertNotIn('private', text)

    def test_append_restart_midnight_and_redaction(self):
        self.assertTrue(hasattr(log_bridge, 'DailyLogSink'))
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for day, text in [(18, 'first'), (18, 'second'), (19, 'sessionid=secret')]:
                sink = log_bridge.DailyLogSink(root)
                sink(SimpleNamespace(record={'time': datetime(2026, 10, day),
                    'level': SimpleNamespace(name='INFO'), 'message': text}))
            self.assertEqual(sorted(p.name for p in root.iterdir()),
                             ['2026-10-18.log', '2026-10-19.log'])
            text = (root / '2026-10-18.log').read_text(encoding='utf-8')
            self.assertIn('first', text)
            self.assertIn('second', text)
            self.assertNotIn('secret', (root / '2026-10-19.log').read_text(encoding='utf-8'))


class LogWindowTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.window = MainWindow(RuntimePaths.from_application(Path(self.tmp.name)))
        self.window.show()
        self.app.processEvents()

    def tearDown(self):
        self.window.close()
        self.app.processEvents()
        self.tmp.cleanup()

    def test_scrollbar_visible_and_reading_position_preserved(self):
        bar = self.window.log_text.verticalScrollBar()
        self.assertTrue(bar.isVisible())
        for i in range(100):
            self.window._append_log('line ' + str(i))
        self.app.processEvents()
        self.assertEqual(bar.value(), bar.maximum())
        bar.setValue(10)
        self.window._append_log('new line')
        self.assertEqual(bar.value(), 10)
        self.assertFalse(self.window.log_follow.isChecked())
        self.window.log_follow.setChecked(True)
        self.window._append_log('latest')
        self.assertEqual(bar.value(), bar.maximum())

    def test_finish_reports_path_for_success_failure_and_stop(self):
        for result in ({'status': 'completed'}, {'status': 'failed'}, {'cancelled': True}):
            self.window.log_text.clear()
            self.window._send_finished(result)
            self.app.processEvents()
            expected = self.window.paths.log_dir / (datetime.now().strftime('%Y-%m-%d') + '.log')
            self.assertIn(str(expected), self.window.log_text.toPlainText())
