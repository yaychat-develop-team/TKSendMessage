import importlib
import json
import os
import tempfile
import threading
import time
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
from PySide6.QtCore import QPoint, QRect, Qt
from PySide6.QtGui import QColor, QFont, QFontDatabase
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QLineEdit, QPlainTextEdit, QLabel, QPushButton, QScrollArea, QSplitter, QTableWidget, QWidget, QSpinBox, QDoubleSpinBox, QAbstractSpinBox
from loguru import logger
from desktop.paths import RuntimePaths
from tiktok_live.account_store import load_account_store, save_account_store_atomic, upsert_account
from tiktok_live.errors import BrowserError
from tests.test_desktop_login_lifecycle import LoginBrowser




class MainWindowTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.module = importlib.import_module('desktop.main_window')
        self.tmp = tempfile.TemporaryDirectory()
        self.paths = RuntimePaths.from_application(Path(self.tmp.name))
        self.window = self.module.MainWindow(self.paths)
        self.window.show()
        self.gates = []
        self.app.processEvents()

    def tearDown(self):
        for gate in self.gates:
            gate.set()
        self.window.request_stop()
        self.pump_until(lambda: not self.window.is_busy)
        self.window.close()
        self.app.processEvents()
        self.tmp.cleanup()

    def widget(self, kind, name):
        widget = self.window.findChild(kind, name)
        self.assertIsNotNone(widget, name)
        return widget

    def inputs(self):
        self.widget(QLineEdit, 'targetUsername').setText(' @host ')
        self.widget(QPlainTextEdit, 'messageText').setPlainText(' hello ')

    def pump_until(self, condition, timeout=3):
        deadline = time.monotonic() + timeout
        while not condition() and time.monotonic() < deadline:
            self.app.processEvents()
            time.sleep(.005)
        self.app.processEvents()
        self.assertTrue(condition(), 'Qt operation did not finish')

    def save_account(self, value='one', username='viewer'):
        store = load_account_store(self.paths.account_store)
        account = upsert_account(store, [{'name': 'uid_tt', 'value': 'synthetic-' + value,
                                         'domain': '.tiktok.com'}], datetime.now(timezone.utc))
        account['username'] = username
        save_account_store_atomic(self.paths.account_store, store)
        return account

    def test_busy_allows_readonly_details_and_settings_and_restores_editing(self):
        from desktop.dialogs import AccountDetailsDialog, SettingsDialog
        from PySide6.QtWidgets import QDialogButtonBox
        self.save_account()
        self.window.refresh_accounts()
        self.window.account_table.selectRow(0)
        self.window.show_settings()
        settings = self.window.findChild(SettingsDialog)
        self.window._begin('sending')
        try:
            self.assertTrue(self.window.account_table.isEnabled())
            self.assertTrue(self.window.view_button.isEnabled())
            self.assertTrue(self.window.settings_button.isEnabled())
            self.assertFalse(self.window.account_table.item(0, 0).flags() & Qt.ItemFlag.ItemIsUserCheckable)
            self.window.show_selected_account()
            details = self.window.findChild(AccountDetailsDialog)
            self.assertIsNotNone(details)
            self.assertTrue(details.details.isReadOnly())
            self.assertTrue(settings.room_cookie.isReadOnly())
            for dialog in (details, settings):
                self.assertFalse(dialog.findChild(QDialogButtonBox).button(QDialogButtonBox.StandardButton.Save).isEnabled())
            original = self.window._settings
            self.window._save_settings(original)
            self.assertFalse(self.paths.settings_file.exists())
        finally:
            self.window._phase = None
            self.window._update_controls()
        self.assertFalse(details.details.isReadOnly())
        self.assertTrue(settings.login_timeout.isEnabled())

    def test_delivery_column_aggregates_observation_not_http_success_and_survives_refresh(self):
        account = self.save_account()
        self.window.refresh_accounts()
        table = self.window.account_table
        self.assertEqual(table.columnCount(), 5)
        table.begin_delivery([account['id']], 3)
        def snapshot(delivered, status='running'):
            return {'rooms': [{'target': 'host', 'accounts': [{
                'id': account['id'][:12], 'status': status, 'delivered': delivered,
                'success': 3, 'acceptedNotObserved': 3 - delivered}]}]}
        self.window._send_progress(snapshot(0))
        self.assertIn('观察中', table.item(0, 4).text())
        self.window._send_progress(snapshot(0, 'completed'))
        self.assertEqual(table.item(0, 4).foreground().color().name(), '#b54708')
        self.window._send_progress(snapshot(2, 'completed'))
        self.assertIn('2/3', table.item(0, 4).text())
        self.assertEqual(table.item(0, 4).foreground().color().name(), '#b54708')
        self.window._send_progress(snapshot(3, 'completed'))
        self.assertEqual(table.item(0, 4).foreground().color().name(), '#15803d')
        self.assertFalse(table.item(0, 4).icon().isNull())
        self.window.refresh_accounts()
        self.assertIn('3/3', table.item(0, 4).text())
        table.begin_delivery([], 1)
        self.assertIn('未测试', table.item(0, 4).text())

    def test_only_target_and_message_are_editable_and_blank_input_blocks_start(self):
        editable = [w.objectName() for w in self.window.findChildren(QLineEdit)
                    if not w.isReadOnly() and not isinstance(w.parent(), QAbstractSpinBox)]
        editable += [w.objectName() for w in self.window.findChildren(QPlainTextEdit) if not w.isReadOnly()]
        self.assertCountEqual(editable, ['targetUsername', 'messageText', 'logSearch'])
        start = self.widget(QPushButton, 'startButton')
        self.assertFalse(start.isEnabled())
        self.widget(QLineEdit, 'targetUsername').setText('host')
        self.widget(QPlainTextEdit, 'messageText').setPlainText('   ')
        self.assertFalse(start.isEnabled())
        self.inputs()
        self.assertFalse(start.isEnabled())
        self.widget(QLineEdit, 'targetUsername').setText(' @ ')
        self.assertFalse(start.isEnabled())

    def test_delivery_multiple_rooms_is_per_account_and_cancel_keeps_partial_results(self):
        one = self.save_account('one')
        two = self.save_account('two')
        self.window.refresh_accounts()
        table = self.window.account_table
        table.begin_delivery([one['id'], two['id']], 2)
        result = {'rooms': [
            {'target': 'first', 'accounts': [
                {'id': one['id'][:12], 'delivered': 1, 'status': 'completed'},
                {'id': two['id'][:12], 'delivered': 0, 'status': 'failed',
                 'error': {'classification': 'initial_fetch_unavailable'}}]},
            {'target': 'second', 'accounts': [
                {'id': one['id'][:12], 'delivered': 0, 'status': 'running'},
                {'id': two['id'][:12], 'delivered': 0, 'status': 'queued'}]}]}
        self.window._send_progress(result)
        self.assertIn('1/2', table.item(0, 4).text())
        self.assertIn('观察中', table.item(0, 4).text())
        # Repeated cumulative snapshots must never double-count.
        self.window._send_progress(result)
        self.window._send_finished({**result, 'status': 'cancelled', 'cancelled': True})
        self.assertIn('1/2', table.item(0, 4).text())
        self.assertEqual(table.item(0, 4).foreground().color().name(), '#b54708')
        self.assertEqual(table.item(1, 4).foreground().color().name(), '#667085')
        self.assertIn('initial_fetch_unavailable', table.item(1, 4).toolTip())

    def test_open_delivery_details_tracks_all_rooms_without_cross_account_or_reason_leaks(self):
        one = self.save_account('one')
        two = self.save_account('two')
        self.window.refresh_accounts()
        table = self.window.account_table
        table.begin_delivery([one['id'], two['id']], 6)
        rooms = [
            {'target': 'first', 'accounts': [
                {'id': one['id'][:12], 'status': 'failed', 'sent': 1,
                 'failure_reasons': ['room_comment_restricted']},
                {'id': two['id'][:12], 'status': 'failed',
                 'failure_reasons': ['initial_fetch_unavailable']}]},
            {'target': 'second', 'accounts': [
                {'id': one['id'][:12], 'status': 'running', 'sent': 2, 'delivered': 1}]},
            {'target': 'third', 'accounts': [
                {'id': one['id'][:12], 'status': 'queued'}]}]
        self.window._send_progress({'rooms': rooms})
        table.cellClicked.emit(0, 4)
        from desktop.dialogs import DeliveryDetailsDialog
        dialog = self.window.findChild(DeliveryDetailsDialog)
        self.assertIsNotNone(dialog)
        text = dialog.details.toPlainText()
        sections = text.split('直播间：')[1:]
        self.assertEqual([part.splitlines()[0] for part in sections], ['first', 'second', 'third'])
        self.assertIn('room_comment_restricted', sections[0])
        self.assertNotIn('initial_fetch_unavailable', text)
        self.assertNotIn('room_comment_restricted', sections[1])
        self.assertNotIn('room_comment_restricted', sections[2])
        self.assertIn('执行中', sections[1])
        self.assertIn('等待执行', sections[2])
        # A cumulative snapshot replaces counts, preserving previous room results.
        rooms[1]['accounts'][0].update(status='completed', delivered=2)
        rooms[2]['accounts'][0].update(status='running', sent=1, delivered=1)
        for _ in range(2):
            self.window._send_progress({'rooms': rooms})
        updated = dialog.details.toPlainText().split('直播间：')[1:]
        self.assertEqual(updated[0], sections[0])
        self.assertIn('已展示：2', updated[1])
        self.assertIn('执行中', updated[2])
        self.assertEqual(table._delivery[one['id']]['delivered'], 3)
        self.assertEqual(table._delivery[one['id']]['sent'], 4)
        self.assertEqual(table._delivery[one['id']]['total'], 6)

    def test_room_execution_setting_labels_limit_persists_and_is_readonly_while_running(self):
        from PySide6.QtWidgets import QComboBox
        from desktop.dialogs import SettingsDialog
        from desktop.settings_store import DesktopSettings, load_desktop_settings, save_desktop_settings
        dialog = SettingsDialog(DesktopSettings(), self.window)
        self.addCleanup(dialog.close)
        control = dialog.findChild(QComboBox, 'roomExecutionMode')
        self.assertIsNotNone(control)
        self.assertEqual(control.currentData(), 'serial')
        self.assertIn('最多同时执行 3 个直播间', control.itemText(control.findData('parallel')))
        self.assertFalse(any('最多同时执行 3 个直播间' in label.text() and not label.isHidden()
                            for label in dialog.findChildren(QLabel)))
        control.setCurrentIndex(control.findData('parallel'))
        dialog.saved.connect(lambda values: save_desktop_settings(self.paths.settings_file, values))
        dialog.set_read_only(True)
        self.assertFalse(control.isEnabled())
        dialog._save()
        self.assertFalse(self.paths.settings_file.exists())
        dialog.set_read_only(False)
        self.assertTrue(control.isEnabled())
        dialog._save()
        self.assertEqual(load_desktop_settings(self.paths.settings_file).room_execution_mode, 'parallel')

    def test_delivery_matches_legacy_hex_account_ids(self):
        from desktop.account_view import AccountSummary
        table = self.window.account_table
        key = 'abcdef0123456789abcdef0123456789'
        table.set_accounts([AccountSummary('legacy', key[:12], key)])
        table.begin_delivery([key], 1)
        self.window._send_progress({'rooms': [{'target': 'host', 'accounts': [
            {'id': key[:12], 'delivered': 1, 'status': 'completed'}]}]})
        self.assertEqual(table.item(0, 4).foreground().color().name(), '#15803d')

    def test_delivery_icons_supply_native_pixels_at_fractional_and_high_dpi(self):
        from PySide6.QtCore import QSize
        account = self.save_account()
        self.window.refresh_accounts()
        table = self.window.account_table
        table.begin_delivery([account['id']], 1)
        for delivered, status in ((0, 'running'), (0, 'completed'), (1, 'completed')):
            table.update_delivery({'rooms': [{'accounts': [{
                'id': account['id'][:12], 'delivered': delivered, 'status': status}]}]})
            icon = table.item(0, 4).icon()
            for ratio, pixels in ((1, 16), (1.25, 20), (1.5, 24), (2, 32), (3, 48), (4, 64)):
                with self.subTest(ratio=ratio, status=status, delivered=delivered):
                    pixmap = icon.pixmap(QSize(16, 16), ratio)
                    self.assertEqual(pixmap.width(), pixels)
                    self.assertAlmostEqual(pixmap.devicePixelRatio(), ratio)

    def test_delivery_distinguishes_failure_uncertainty_and_cancel_and_opens_details(self):
        account = self.save_account()
        self.window.refresh_accounts()
        table = self.window.account_table
        for values, color in (
            ({'status': 'failed', 'failed': 1, 'failure_reasons': ['initial_fetch_unavailable']}, '#b42318'),
            ({'status': 'completed', 'success': 1, 'acceptedNotObserved': 1}, '#b54708'),
            ({'status': 'failed', 'error': {'classification': 'listener_unavailable'}}, '#b54708'),
            ({'status': 'cancelled', 'cancelled': True}, '#667085'),
        ):
            table.begin_delivery([account['id']], 1)
            self.window._send_progress({'rooms': [{'target': 'host', 'accounts': [
                {'id': account['id'][:12], **values}]}]})
            self.assertEqual(table.item(0, 4).foreground().color().name(), color)
        table.begin_delivery([account['id']], 1)
        self.window._send_progress({'rooms': [{'target': 'host', 'accounts': [
            {'id': account['id'][:12], 'status': 'failed', 'failed': 1,
             'failure_reasons': ['initial_fetch_unavailable']}]}]})
        table.cellClicked.emit(0, 4)
        from desktop.dialogs import DeliveryDetailsDialog
        dialog = self.window.findChild(DeliveryDetailsDialog)
        self.assertIsNotNone(dialog)
        self.assertTrue(dialog.details.isReadOnly())
        text = dialog.details.toPlainText()
        self.assertIn('host', text)
        self.assertIn('initial_fetch_unavailable', text)
        self.assertIn('首次直播数据', text)
        self.assertNotIn('synthetic-', text)

    def test_log_filters_search_and_clear_do_not_restore_removed_records(self):
        from PySide6.QtWidgets import QComboBox
        self.window._append_log('host-a passed', 'SUCCESS')
        self.window._append_log('host-b uncertain', 'WARNING')
        self.window._append_log('host-a failed', 'ERROR')
        level = self.widget(QComboBox, 'logLevelFilter')
        search = self.widget(QLineEdit, 'logSearch')
        level.setCurrentIndex(level.findData('ERROR'))
        self.assertEqual(self.window.log_text.toPlainText(), 'host-a failed')
        search.setText('host-b')
        self.assertEqual(self.window.log_text.toPlainText(), '')
        level.setCurrentIndex(0)
        self.assertEqual(self.window.log_text.toPlainText(), 'host-b uncertain')
        self.window._append_log('host-b new', 'INFO')
        self.assertIn('host-b new', self.window.log_text.toPlainText())
        self.window.clear_console_button.click()
        search.clear()
        self.assertEqual(self.window.log_text.toPlainText(), '')

    def test_log_filter_retention_is_bounded_and_secrets_are_not_cached(self):
        from PySide6.QtWidgets import QComboBox
        level = self.widget(QComboBox, 'logLevelFilter')
        level.setCurrentIndex(level.findData('ERROR'))
        self.window._append_log('cookie=synthetic-secret', 'INFO')
        for index in range(2010):
            self.window._append_log(f'line-{index}', 'INFO')
        level.setCurrentIndex(0)
        text = self.window.log_text.toPlainText()
        self.assertNotIn('synthetic-secret', repr(self.window._log_records))
        self.assertNotIn('line-0\n', text)
        self.assertIn('line-2009', text)
        self.assertLessEqual(len(self.window._log_records), 2000)

    def test_brand_icon_preserves_high_dpi_pixels_without_changing_layout_size(self):
        import subprocess
        import sys
        for ratio in (1, 1.25, 1.5, 2):
            result = subprocess.run([sys.executable, 'tests/support/dpi_probe.py'],
                env={**os.environ, 'QT_QPA_PLATFORM': 'offscreen',
                     'QT_SCALE_FACTOR': str(ratio)}, capture_output=True, text=True,
                encoding='utf-8', timeout=30)
            self.assertEqual(result.returncode, 0, result.stderr)
            data = json.loads(result.stdout)
            self.assertEqual(data['brand_pixels'], round(36 * ratio))
            self.assertEqual(data['mark_pixels'], round(16 * ratio))
            self.assertEqual(data['dpr'], ratio)
            self.assertTrue(data['text_fits'])

    def test_log_follow_can_pause_resume_and_visible_cache_evicts_old_lines(self):
        self.window.show()
        self.app.processEvents()
        for index in range(2010):
            self.window._append_log(f'record-{index}', 'INFO')
        self.assertEqual(self.window.log_text.toPlainText().splitlines(),
                         [f'record-{index}' for index in range(10, 2010)])
        bar = self.window.log_text.verticalScrollBar()
        bar.setValue(5)
        self.assertFalse(self.window.log_follow.isChecked())
        self.window._append_log('latest', 'ERROR')
        self.assertLess(bar.value(), bar.maximum())
        self.window.log_follow.setChecked(True)
        self.assertEqual(bar.value(), bar.maximum())

    def test_blank_logs_do_not_desynchronize_visible_retention(self):
        from collections import deque
        self.window._clear_console()
        self.window._log_records = deque(maxlen=3)
        for line in ('', 'first', 'second', 'third', 'fourth'):
            self.window._append_log(line, 'INFO')
        self.assertEqual(self.window.log_text.toPlainText(), 'second\nthird\nfourth')

    def test_message_repeat_controls_are_forwarded_and_locked_during_send(self):
        self.save_account()
        self.window.refresh_accounts()
        self.window.account_table.set_all_checked(True)
        self.inputs()
        count = self.widget(QAbstractSpinBox, 'messageCount')
        interval = self.widget(QDoubleSpinBox, 'messageIntervalSeconds')
        self.assertEqual(count.value(), 1)
        self.assertEqual(interval.value(), 1.0)
        count.setValue(3)
        interval.setValue(0.3)
        entered = threading.Event()
        gate = threading.Event()
        self.gates.append(gate)
        captured = {}
        def run(*args, **kwargs):
            captured.update(kwargs)
            entered.set()
            gate.wait(3)
            return {'success': 3}
        with patch('desktop.workers.run_parallel_targets', side_effect=run):
            self.window.start_send()
            self.pump_until(entered.is_set)
            self.assertFalse(count.isEnabled())
            self.assertFalse(interval.isEnabled())
            for name in ('targetUsername', 'messageText', 'addAccountButton',
                         'deleteAccountButton',
                         'checkAllAccountsButton', 'uncheckAllAccountsButton'):
                self.assertFalse(self.widget(QWidget, name).isEnabled(), name)
            self.assertTrue(self.window.account_table.isEnabled())
            self.assertTrue(self.window.settings_button.isEnabled())
            self.assertTrue(self.widget(QPushButton, 'stopButton').isEnabled())
            gate.set()
            self.pump_until(lambda: not self.window.is_busy)
        self.assertEqual(captured['message_count'], 3)
        self.assertEqual(captured['message_interval_seconds'], 0.3)
        self.assertTrue(count.isEnabled())

    def test_message_uses_full_row_and_parameters_align_below_at_both_window_sizes(self):
        for width, height in ((1100, 850), (800, 700)):
            with self.subTest(size=(width, height)):
                self.window.resize(width, height)
                self.app.processEvents()
                self.assertEqual((self.window.width(), self.window.height()), (width, height))
                def bounds(widget):
                    return QRect(widget.mapTo(self.window, QPoint(0, 0)), widget.size())
                target = bounds(self.widget(QLineEdit, 'targetUsername'))
                message = bounds(self.widget(QPlainTextEdit, 'messageText'))
                count = bounds(self.widget(QAbstractSpinBox, 'messageCount'))
                interval = bounds(self.widget(QDoubleSpinBox, 'messageIntervalSeconds'))
                self.assertEqual(message.left(), target.left())
                self.assertEqual(message.right(), target.right())
                self.assertGreater(count.top(), message.bottom() + 5)
                self.assertGreater(interval.top(), message.bottom() + 5)
                self.assertLessEqual(abs(count.center().y() - interval.center().y()), 2)
                self.assertLess(count.right(), interval.left())
                upper = self.widget(QScrollArea, 'upperContentScroll')
                self.assertLessEqual(upper.widget().width(), upper.viewport().width())
                for text in ('目标用户名', '消息内容', '发送次数', '发送间隔'):
                    label = next(label for label in self.window.findChildren(QLabel)
                                 if label.text() == text)
                    self.assertGreaterEqual(label.width(), label.fontMetrics().horizontalAdvance(text))
                    self.assertLessEqual(bounds(label).right(), self.window.width() - 20)
                self.assertIn(';', self.widget(QLineEdit, 'targetUsername').placeholderText())
                self.assertIn('每账号每直播间', self.widget(QAbstractSpinBox, 'messageCount').accessibleName())

    def test_two_targets_and_duplicate_room_use_real_coordinator_once_per_account_room(self):
        one = self.save_account('one')
        two = self.save_account('two')
        self.window.refresh_accounts()
        self.window.account_table.set_all_checked(True)
        self.inputs()
        self.window.username_input.setText(' A;@B; a；B ')
        self.window.message_count.setValue(3)
        calls = []
        lock = threading.Lock()
        def send(target, message, **kwargs):
            with lock:
                calls.append((target, message, kwargs['selected_account_ids'], kwargs['message_count']))
            return {'success': 3, 'delivered': 3}
        with patch('tiktok_live.parallel_runner.run_saved_accounts', side_effect=send):
            self.window.start_send()
            self.pump_until(lambda: not self.window.is_busy)
        self.assertEqual(sorted(target for target, *_ in calls), ['A', 'A', 'B', 'B'])
        self.assertCountEqual([(target, ids) for target, _, ids, _ in calls],
            [('A', (one['id'],)), ('A', (two['id'],)),
             ('B', (one['id'],)), ('B', (two['id'],))])
        self.assertTrue(all(message == 'hello' and len(ids) == 1 and count == 3
                            for _, message, ids, count in calls))
        self.assertEqual(self.window.result_values['processed'].text(), '12')
        self.assertEqual(self.window.result_values['delivered'].text(), '12')
        self.assertIn('A', self.window.status_label.text())
        self.assertIn('B', self.window.status_label.text())

    def test_malformed_later_target_disables_start_and_never_launches_worker(self):
        self.save_account()
        self.window.refresh_accounts()
        self.window.account_table.set_all_checked(True)
        self.inputs()
        for target in ('A;https://invalid.example', 'A;@', ';；', 'A;../bad', 'A;two words'):
            with self.subTest(target=target):
                self.window.username_input.setText(target)
                with patch.object(self.window, '_launch') as launch:
                    self.assertFalse(self.window.start_button.isEnabled())
                    self.window.start_send()
                    launch.assert_not_called()
                    self.assertFalse(self.window.is_busy)

    def test_room_failure_summary_survives_runtime_error_and_preserves_cumulative_progress(self):
        self.save_account()
        self.window.refresh_accounts()
        self.window.account_table.set_all_checked(True)
        self.inputs()
        self.window.username_input.setText('A;B')
        first = threading.Event()
        release = threading.Event()
        finish = threading.Event()
        self.gates.append(release)
        self.gates.append(finish)
        def send(targets, message, **kwargs):
            kwargs['on_progress']({'success': 1, 'delivered': 1})
            first.set()
            release.wait(3)
            kwargs['on_progress']({'success': 2, 'delivered': 1, 'acceptedNotObserved': 1})
            finish.wait(3)
            return {'success': 2, 'delivered': 1, 'acceptedNotObserved': 1,
                    'listenerUnavailable': 1, 'failed_tasks': 1, 'status': 'failed',
                    'rooms': [
                        {'target': 'A', 'status': 'completed', 'success': 1, 'delivered': 1,
                         'accounts': [{'id': 'abc123', 'username': 'one', 'status': 'completed',
                                       'success': 1, 'delivered': 1}]},
                        {'target': 'B', 'status': 'failed', 'success': 1, 'acceptedNotObserved': 1,
                         'listenerUnavailable': 1,
                         'accounts': [{'id': 'def456', 'username': 'two', 'status': 'failed',
                                       'success': 1, 'acceptedNotObserved': 1, 'listenerUnavailable': 1,
                                       'error': {'type': 'RuntimeError', 'classification': 'runtime_error'}}]}]}
        with patch('desktop.workers.run_parallel_targets', side_effect=send):
            self.window.start_send()
            self.pump_until(first.is_set)
            self.pump_until(lambda: self.window.result_values['processed'].text() == '1')
            release.set()
            self.pump_until(lambda: self.window.result_values['processed'].text() == '2')
            self.assertEqual(self.window.result_values['acceptedNotObserved'].text(), '1')
            self.assertTrue(self.window.is_busy)
            finish.set()
            self.pump_until(lambda: not self.window.is_busy)
        self.assertEqual(self.window.result_values['processed'].text(), '2')
        self.assertEqual(self.window.result_values['failed'].text(), '0')
        status = self.window.status_label.text()
        for expected in ('失败', 'A', 'B', '已观察到', '未观察到', '监听不可用', '账号失败 1'):
            self.assertIn(expected, status)
        self.assertNotIn('本轮发送完成', status)

    def test_completion_distinguishes_accepted_without_observation_and_listener_unavailable(self):
        self.window._send_finished({'status': 'completed', 'success': 2,
            'delivered': 0, 'acceptedNotObserved': 1, 'listenerUnavailable': 1,
            'rooms': [{'target': 'A', 'status': 'completed', 'success': 2,
                       'delivered': 0, 'acceptedNotObserved': 1, 'listenerUnavailable': 1,
                       'accounts': []}]})
        status = self.window.status_label.text()
        for expected in ('完成', 'A', '已观察到消息 0', '未观察到 1', '监听不可用 1'):
            self.assertIn(expected, status)

    def test_failed_room_takes_priority_over_cancelled_and_cancelled_rooms_keep_counts(self):
        for status, expected in (('failed', '失败'), ('cancelled', '停止')):
            with self.subTest(status=status):
                self.window._send_finished({'status': status, 'cancelled': True,
                    'success': 1, 'delivered': 1,
                    'rooms': [{'target': 'A', 'status': status, 'success': 1,
                               'delivered': 1, 'accounts': []}]})
                self.assertIn(expected, self.window.status_label.text())
                self.assertIn('A', self.window.status_label.text())
                self.assertEqual(self.window.result_values['success'].text(), '1')

    def test_branded_header_and_window_icon_are_present(self):
        icon_label = self.widget(QLabel, 'brandIcon')
        self.assertIsNotNone(icon_label.pixmap())
        self.assertFalse(icon_label.pixmap().isNull())
        self.assertFalse(self.window.windowIcon().isNull())
        self.assertIn('#fe2c55', self.window.styleSheet().lower())

    def test_login_browser_stays_external_and_workspace_remains_visible(self):
        self.assertIsNone(self.window.findChild(QWidget, 'browserPanel'))

        self.window._browser_ready(321)
        self.app.processEvents()

        self.assertTrue(self.widget(QLineEdit, 'targetUsername').isVisible())
        self.assertTrue(self.widget(QPlainTextEdit, 'logText').isVisible())
        self.assertIn('外部浏览器', self.widget(QLabel, 'statusLabel').text())

    def test_double_start_is_blocked_and_worker_failure_recovers_controls(self):
        self.save_account()
        self.window.refresh_accounts()
        self.widget(QPushButton, 'checkAllAccountsButton').click()
        self.inputs()
        gate = threading.Event()
        entered = threading.Event()
        self.gates.append(gate)
        calls = []
        def send(username, message, **kwargs):
            calls.append((username, message))
            entered.set()
            gate.wait(3)
            raise RuntimeError('Cookie: do-not-display')
        with patch('desktop.workers.run_parallel_targets', side_effect=send):
            self.window.start_send()
            self.pump_until(entered.is_set)
            self.window.start_send()
            self.assertFalse(self.widget(QPushButton, 'startButton').isEnabled())
            self.assertFalse(self.widget(QPushButton, 'addAccountButton').isEnabled())
            self.assertFalse(self.widget(QLineEdit, 'targetUsername').isEnabled())
            gate.set()
            self.pump_until(lambda: not self.window.is_busy)
        self.assertEqual(calls, [(('host',), 'hello')])
        self.assertTrue(self.widget(QPushButton, 'startButton').isEnabled())
        self.assertTrue(self.widget(QPushButton, 'addAccountButton').isEnabled())
        self.assertIn('失败', self.widget(QLabel, 'statusLabel').text())
        self.assertNotIn('do-not-display', self.widget(QPlainTextEdit, 'logText').toPlainText())

    def test_initial_fetch_failure_has_actionable_desktop_message(self):
        self.window._worker_failed('initial_fetch_unavailable')

        status = self.widget(QLabel, 'statusLabel').text()
        self.assertIn('首次直播数据', status)
        self.assertIn('未发送', status)

    def test_saved_account_rows_show_only_safe_summary(self):
        account = self.save_account(username='@viewer')
        self.window.refresh_accounts()
        table = self.widget(QTableWidget, 'accountTable')
        self.assertEqual(table.columnCount(), 5)
        self.assertEqual(table.rowCount(), 1)
        self.assertEqual(table.item(0, 0).checkState(), Qt.CheckState.Unchecked)
        self.assertEqual(table.item(0, 1).text(), 'viewer')
        self.assertEqual(table.item(0, 2).text(), account['id'][:12])
        self.assertEqual(table.item(0, 3).text(), '已保存')
        self.assertFalse(table.item(0, 1).flags() & Qt.ItemFlag.ItemIsEditable)
        self.assertIsNone(table.item(0, 1).data(256))
        visible = ' '.join(table.item(0, i).text() for i in range(5))
        self.assertNotIn('synthetic', visible)

    def test_only_checked_accounts_are_passed_to_send_and_refresh_keeps_checks(self):
        one = self.save_account('one', 'one')
        two = self.save_account('two', 'two')
        self.window.refresh_accounts()
        self.inputs()
        table = self.widget(QTableWidget, 'accountTable')
        self.assertFalse(self.widget(QPushButton, 'startButton').isEnabled())
        table.item(1, 0).setCheckState(Qt.CheckState.Checked)
        self.app.processEvents()
        self.assertTrue(self.widget(QPushButton, 'startButton').isEnabled())
        self.assertEqual(self.window.account_table.checked_account_ids(), [two['id']])
        self.window.refresh_accounts()
        self.assertEqual(self.window.account_table.checked_account_ids(), [two['id']])
        received = []
        with patch('desktop.workers.run_parallel_targets', side_effect=lambda *a, **kw: received.append(kw['selected_account_ids']) or {'success': 1}):
            self.window.start_send()
            self.pump_until(lambda: not self.window.is_busy)
        self.assertEqual(received, [(two['id'],)])
        self.assertNotIn(one['id'], received[0])
        self.assertTrue(table.isEnabled())
        self.assertTrue(table.viewport().isEnabled())
        self.assertEqual(self.window.account_table.checked_account_ids(), [two['id']])

        checked_item = table.item(1, 0)
        cell = table.visualItemRect(checked_item)
        self.assertTrue(cell.isValid())
        self.assertTrue(table.viewport().rect().contains(cell.center()))
        QTest.mouseClick(table.viewport(), Qt.MouseButton.LeftButton, pos=cell.center())
        self.app.processEvents()
        self.assertEqual(self.window.account_table.checked_account_ids(), [])
        QTest.mouseClick(table.viewport(), Qt.MouseButton.LeftButton, pos=cell.center())
        self.app.processEvents()
        self.assertEqual(self.window.account_table.checked_account_ids(), [two['id']])

    def test_clicking_account_checkbox_shows_green_checkmark(self):
        account = self.save_account()
        self.window.refresh_accounts()
        table = self.widget(QTableWidget, 'accountTable')
        item = table.item(0, 0)
        cell = table.visualItemRect(item)
        QTest.mouseClick(table.viewport(), Qt.MouseButton.LeftButton, pos=cell.center())
        self.app.processEvents()
        self.assertEqual(table.checked_account_ids(), [account['id']])
        image = table.viewport().grab(cell).toImage()
        green = QColor('#1c9f5e')
        self.assertTrue(any(image.pixelColor(x, y) == green
                            for y in range(image.height()) for x in range(image.width())))

    def test_clear_console_keeps_saved_daily_log(self):
        from desktop.log_bridge import DailyLogSink, daily_log_path
        handler = logger.add(DailyLogSink(self.paths.log_dir), format='{message}')
        logger.info('console-clear-fixture')
        logger.remove(handler)
        self.app.processEvents()
        log = self.widget(QPlainTextEdit, 'logText')
        self.assertIn('console-clear-fixture', log.toPlainText())
        self.widget(QPushButton, 'clearConsoleButton').click()
        self.assertEqual(log.toPlainText(), '')
        self.assertIn('console-clear-fixture', daily_log_path(self.paths.log_dir).read_text(encoding='utf-8'))

    def test_select_all_and_clear_selection_control_send(self):
        self.save_account('one')
        self.save_account('two')
        self.window.refresh_accounts()
        self.inputs()
        self.widget(QPushButton, 'checkAllAccountsButton').click()
        self.assertEqual(len(self.window.account_table.checked_account_ids()), 2)
        self.assertTrue(self.widget(QPushButton, 'startButton').isEnabled())
        self.widget(QPushButton, 'uncheckAllAccountsButton').click()
        self.assertEqual(self.window.account_table.checked_account_ids(), [])
        self.assertFalse(self.widget(QPushButton, 'startButton').isEnabled())

    def test_selected_account_can_be_deleted_without_exposing_full_id(self):
        account = self.save_account(username='viewer')
        self.window.refresh_accounts()
        table = self.widget(QTableWidget, 'accountTable')
        delete = self.widget(QPushButton, 'deleteAccountButton')
        self.assertFalse(delete.isEnabled())

        table.selectRow(0)
        self.app.processEvents()
        self.assertTrue(delete.isEnabled())
        self.assertIsNone(table.item(0, 0).data(256))
        from PySide6.QtWidgets import QMessageBox
        def accept_confirmation():
            prompt = self.window.findChild(QMessageBox)
            self.assertIsNotNone(prompt)
            self.assertIn('viewer', prompt.text())
            self.assertNotIn(account['id'], prompt.text())
            self.assertEqual(prompt.defaultButton(), prompt.button(QMessageBox.StandardButton.Cancel))
            prompt.done(QMessageBox.StandardButton.Yes)
        from PySide6.QtCore import QTimer
        QTimer.singleShot(0, accept_confirmation)
        delete.click()
        self.app.processEvents()

        self.assertEqual(load_account_store(self.paths.account_store)['accounts'], [])
        self.assertEqual(table.rowCount(), 0)
        self.assertNotIn(account['id'], self.widget(QPlainTextEdit, 'logText').toPlainText())

    def test_cancelling_account_deletion_preserves_file_and_selection(self):
        from PySide6.QtCore import QTimer
        from PySide6.QtWidgets import QMessageBox
        account = self.save_account(username='viewer')
        self.window.refresh_accounts()
        self.window.account_table.selectRow(0)
        before = self.paths.account_store.read_bytes()
        def cancel():
            prompt = self.window.findChild(QMessageBox)
            self.assertIsNotNone(prompt)
            prompt.reject()
        QTimer.singleShot(0, cancel)
        self.window.delete_selected_account()
        self.assertEqual(self.paths.account_store.read_bytes(), before)
        self.assertEqual(self.window.account_table.selected_account_id(), account['id'])

    def test_selected_account_details_show_complete_saved_record(self):
        account = self.save_account(username='viewer')
        self.window.refresh_accounts()
        table = self.widget(QTableWidget, 'accountTable')
        table.selectRow(0)
        self.app.processEvents()

        self.widget(QPushButton, 'viewAccountButton').click()
        self.app.processEvents()

        details = self.window.findChild(QPlainTextEdit, 'accountDetailsText')
        self.assertIsNotNone(details)
        visible = details.toPlainText()
        self.assertIn(account['id'], visible)
        shown = json.loads(visible)
        self.assertEqual(shown['cookie'], 'uid_tt=synthetic-one')
        self.assertNotIn('cookies', shown)
        self.assertFalse(details.isReadOnly())

    def test_account_details_can_be_edited_and_saved_without_changing_identity(self):
        account = self.save_account(username='viewer')
        self.window.refresh_accounts()
        self.widget(QTableWidget, 'accountTable').selectRow(0)
        self.widget(QPushButton, 'viewAccountButton').click()
        details = self.widget(QPlainTextEdit, 'accountDetailsText')
        changed = json.loads(details.toPlainText())
        changed['username'] = 'edited_viewer'
        changed['cookie'] += '; test_cookie=changed-value'
        details.setPlainText(json.dumps(changed))
        self.widget(QPushButton, 'saveAccountDetailsButton').click()
        saved = load_account_store(self.paths.account_store)['accounts'][0]
        self.assertEqual(saved['id'], account['id'])
        self.assertEqual(saved['username'], 'edited_viewer')
        self.assertEqual(saved['cookies'][-1]['name'], 'test_cookie')
        self.assertEqual(saved['cookies'][-1]['value'], 'changed-value')
        self.assertEqual(self.widget(QTableWidget, 'accountTable').item(0, 1).text(), 'edited_viewer')

    def test_invalid_account_json_does_not_overwrite_saved_account(self):
        self.save_account(username='viewer')
        account = load_account_store(self.paths.account_store)['accounts'][0]
        self.window.refresh_accounts()
        self.widget(QTableWidget, 'accountTable').selectRow(0)
        self.widget(QPushButton, 'viewAccountButton').click()
        self.widget(QPlainTextEdit, 'accountDetailsText').setPlainText('{invalid')
        self.widget(QPushButton, 'saveAccountDetailsButton').click()
        self.assertEqual(load_account_store(self.paths.account_store)['accounts'][0], account)
        self.assertTrue(self.widget(QLabel, 'accountEditStatus').text())

    def test_invalid_cookie_string_does_not_overwrite_saved_account(self):
        self.save_account(username='viewer')
        account = load_account_store(self.paths.account_store)['accounts'][0]
        self.window.refresh_accounts()
        self.widget(QTableWidget, 'accountTable').selectRow(0)
        self.widget(QPushButton, 'viewAccountButton').click()
        details = self.widget(QPlainTextEdit, 'accountDetailsText')
        changed = json.loads(details.toPlainText())
        changed['cookie'] += '; malformed'
        details.setPlainText(json.dumps(changed))
        self.widget(QPushButton, 'saveAccountDetailsButton').click()
        self.assertEqual(load_account_store(self.paths.account_store)['accounts'][0], account)
        self.assertIn('Cookie 字符串格式', self.widget(QLabel, 'accountEditStatus').text())

    def test_account_row_is_fully_visible_and_account_area_can_expand(self):
        self.save_account(username='viewer')
        self.window.refresh_accounts()
        self.window.resize(1180, 820)
        self.app.processEvents()
        table = self.widget(QTableWidget, 'accountTable')
        row = table.visualItemRect(table.item(0, 1))
        self.assertLess(row.bottom(), table.viewport().height())
        self.assertGreaterEqual(table.viewport().height(), table.rowHeight(0))
        collapsed_height = table.height()
        expand = self.widget(QPushButton, 'expandAccountsButton')
        expand.click()
        self.app.processEvents()
        self.assertGreater(table.height(), collapsed_height + 100)
        self.assertLess(table.visualItemRect(table.item(0, 1)).bottom(),
                        table.viewport().height())
        expand.click()
        self.app.processEvents()
        self.assertEqual(table.height(), collapsed_height)

    def test_account_table_uses_only_needed_rows_then_scrolls_when_full(self):
        self.save_account(value='one')
        self.window.refresh_accounts()
        self.app.processEvents()
        table = self.widget(QTableWidget, 'accountTable')
        single_height = table.height()
        self.assertLess(single_height, 140)
        self.assertLess(table.visualItemRect(table.item(0, 1)).bottom(),
                        table.viewport().height())
        for index in range(2, 8):
            self.save_account(value=str(index))
        self.window.refresh_accounts()
        self.app.processEvents()
        self.assertGreater(table.height(), single_height)
        self.assertLessEqual(table.height(), 166)
        self.assertGreater(table.verticalScrollBar().maximum(), 0)

    def test_default_account_area_shows_four_rows_without_clipping(self):
        for index in range(4):
            self.save_account(value=str(index))
        self.window.refresh_accounts()
        self.app.processEvents()
        table = self.widget(QTableWidget, 'accountTable')
        self.assertLess(table.visualItemRect(table.item(3, 1)).bottom(),
                        table.viewport().height())

    def test_compact_message_leaves_large_log_after_account_refresh_and_resize(self):
        for index in range(2):
            self.save_account(value=str(index))
        self.window.refresh_accounts()
        for width, height in ((1180, 820), (1400, 1000), (1920, 1152), (1180, 820)):
            with self.subTest(size=(width, height)):
                self.window.resize(width, height)
                self.app.processEvents()
                message = self.widget(QPlainTextEdit, 'messageText')
                log = self.widget(QPlainTextEdit, 'logText')
                self.assertLessEqual(message.height(), 60)
                self.assertGreaterEqual(message.viewport().height(),
                                        message.fontMetrics().lineSpacing() * 2)
                self.assertGreaterEqual(log.height(), height // 3)
                self.assertGreaterEqual(self.window.account_table.viewport().height(),
                                        self.window.account_table.rowHeight(0) * 2)
                last_result = self.widget(QLabel, 'result_temporaryError')
                upper = self.widget(QScrollArea, 'upperContentScroll')
                self.assertLess(last_result.mapTo(upper.viewport(),
                    last_result.rect().bottomLeft()).y(), upper.viewport().height())

    def test_splitter_can_adjust_log_without_shrinking_below_readable_budget(self):
        self.window.resize(1400, 1000)
        self.app.processEvents()
        splitter = self.widget(QSplitter, 'contentLogSplitter')
        upper = self.widget(QScrollArea, 'upperContentScroll')
        before = upper.height()
        splitter.moveSplitter(before + 50, 1)
        self.app.processEvents()
        self.assertGreater(upper.height(), before + 20)
        splitter.moveSplitter(splitter.height() - 20, 1)
        self.app.processEvents()
        self.assertGreaterEqual(self.widget(QPlainTextEdit, 'logText').height(), 333)

    def test_native_font_keeps_stat_values_below_titles_and_inside_normal_window(self):
        font_path = Path('C:/Windows/Fonts/msyh.ttc')
        if not font_path.is_file():
            self.skipTest('Windows UI font unavailable')
        font_id = QFontDatabase.addApplicationFont(str(font_path))
        self.assertGreaterEqual(font_id, 0)
        self.addCleanup(QFontDatabase.removeApplicationFont, font_id)
        self.window.setFont(QFont('Microsoft YaHei', 9))
        for index in range(2):
            self.save_account(value=str(index))
        self.window.refresh_accounts()
        self.window.resize(1180, 820)
        for _ in range(4):
            self.app.processEvents()
        upper = self.widget(QScrollArea, 'upperContentScroll')
        self.assertEqual(upper.verticalScrollBar().maximum(), 0)
        for value in self.window.result_values.values():
            card = value.parentWidget()
            self.assertGreaterEqual(value.mapTo(card, QPoint()).y(),
                                    card.fontMetrics().height() + 2)
            self.assertLess(value.mapTo(upper.viewport(), value.rect().bottomLeft()).y(),
                            upper.viewport().height())
        self.assertGreaterEqual(self.window.log_text.height(), 273)

    def test_saved_account_count_aligns_with_actions_inside_card(self):
        self.save_account(username='viewer')
        self.window.refresh_accounts()
        self.app.processEvents()
        card = self.widget(QWidget, 'accountCard')
        count = self.window.account_count
        add = self.widget(QPushButton, 'addAccountButton')
        table = self.widget(QTableWidget, 'accountTable')
        count_center = count.mapTo(card, count.rect().center())
        button_center = add.mapTo(card, add.rect().center())
        self.assertLessEqual(abs(count_center.y() - button_center.y()), 6)
        self.assertGreaterEqual(count.mapTo(card, count.rect().topLeft()).x(), 14)
        self.assertGreaterEqual(table.mapTo(card, table.rect().topLeft()).y()
                                - add.mapTo(card, add.rect().bottomLeft()).y(), 6)

    def test_clear_console_shares_action_row_with_send_and_stop(self):
        self.window.resize(1400, 1000)
        self.app.processEvents()
        self.assertIsNotNone(self.widget(QSplitter, 'contentLogSplitter'))
        self.assertIsNotNone(self.widget(QScrollArea, 'upperContentScroll'))
        log = self.widget(QPlainTextEdit, 'logText')
        self.assertGreaterEqual(log.height(), 120)
        table = self.widget(QTableWidget, 'accountTable')
        self.assertEqual(table.verticalScrollBarPolicy(), Qt.ScrollBarPolicy.ScrollBarAlwaysOn)
        clear = self.widget(QPushButton, 'clearConsoleButton')
        start = self.widget(QPushButton, 'startButton')
        stop = self.widget(QPushButton, 'stopButton')
        centers = [button.mapTo(self.window, button.rect().center()).y()
                   for button in (start, stop, clear)]
        self.assertLessEqual(max(centers) - min(centers), 4)
        self.assertGreater(clear.mapTo(self.window, clear.rect().center()).x(),
                           stop.mapTo(self.window, stop.rect().center()).x())

    def test_large_window_keeps_result_cards_visible_with_account_table(self):
        self.window.resize(1920, 1152)
        self.app.processEvents()
        upper = self.widget(QScrollArea, 'upperContentScroll')
        last_result = self.widget(QLabel, 'result_temporaryError')
        bottom = last_result.mapTo(upper.viewport(), last_result.rect().bottomLeft()).y()
        self.assertLess(bottom, upper.viewport().height())
        self.assertGreaterEqual(self.widget(QPlainTextEdit, 'logText').height(), 220)

    def test_results_stay_visible_without_scrolling_upper_panel(self):
        self.window.resize(1920, 1152)
        self.app.processEvents()
        upper = self.widget(QScrollArea, 'upperContentScroll')
        last_result = self.widget(QLabel, 'result_temporaryError')
        for expanded in (False, True):
            self.widget(QPushButton, 'expandAccountsButton').setChecked(expanded)
            self.app.processEvents()
            bottom = last_result.mapTo(upper.viewport(), last_result.rect().bottomLeft()).y()
            self.assertLess(bottom, upper.viewport().height())
            self.assertEqual(upper.verticalScrollBar().maximum(), 0)

    def test_small_window_does_not_hide_log_behind_upper_panel(self):
        self.window.resize(800, 640)
        self.app.processEvents()
        upper = self.widget(QScrollArea, 'upperContentScroll')
        log = self.widget(QPlainTextEdit, 'logText')
        self.assertGreater(log.height(), 70)
        self.assertLess(log.mapTo(self.window, log.rect().bottomLeft()).y(),
                        self.window.height())
        self.assertGreater(upper.verticalScrollBar().maximum(), 0)

    def test_settings_button_exposes_script_configuration(self):
        self.widget(QPushButton, 'settingsButton').click()
        self.app.processEvents()
        for name in ('loginTimeoutSeconds', 'accountIntervalSeconds',
                     'observationEnabled', 'showLiveComments',
                     'observationTimeoutSeconds', 'messageAttempts',
                     'messageRetryDelaySeconds', 'listenerConnectTimeoutSeconds',
                     'listenerConnectRetries', 'listenerRetryDelaySeconds',
                     'roomIdCookie'):
            self.assertIsNotNone(self.window.findChild(QWidget, name), name)

    def test_log_levels_render_with_distinct_colors(self):
        logger.success('synthetic success')
        logger.warning('synthetic warning')
        logger.error('synthetic error')
        self.app.processEvents()

        document = self.widget(QPlainTextEdit, 'logText').document()
        colors = {}
        block = document.begin()
        while block.isValid():
            text = block.text()
            if text:
                iterator = block.begin()
                if not iterator.atEnd():
                    colors[text] = iterator.fragment().charFormat().foreground().color().name()
            block = block.next()
        def color_for(message):
            return next(color for text, color in colors.items() if text.endswith(message))
        self.assertEqual(color_for('synthetic success'), QColor('#188038').name())
        self.assertEqual(color_for('synthetic warning'), QColor('#b06000').name())
        self.assertEqual(color_for('synthetic error'), QColor('#c5221f').name())

    def test_observed_delivery_is_green_but_unobserved_acceptance_is_not(self):
        from tiktok_live.runner import log_message_display
        for status in ('observed', 'not_observed'):
            log_message_display(room='host', account={'id': 'a' * 64, 'username': 'alice'},
                index=1, total=1, message_id='123', status=status, classification='delivered')
        self.app.processEvents()
        colors = {}
        block = self.widget(QPlainTextEdit, 'logText').document().begin()
        while block.isValid():
            for status in ('observed', 'not_observed'):
                if 'display_status=' + status + ' |' in block.text():
                    colors[status] = block.begin().fragment().charFormat().foreground().color().name()
            block = block.next()
        self.assertEqual(colors['observed'], QColor('#188038').name())
        self.assertNotEqual(colors['not_observed'], QColor('#188038').name())

    def test_empty_store_send_prompts_for_add_account_without_login_or_send(self):
        self.inputs()
        with patch('desktop.workers.login_new_account',
                   side_effect=AssertionError('send must not open login')), patch(
                'desktop.workers.run_parallel_targets',
                side_effect=AssertionError('send must not run without an account')):
            self.window.start_send()
            self.assertFalse(self.window.is_busy)

        self.assertEqual(load_account_store(self.paths.account_store)['accounts'], [])
        self.assertFalse(self.widget(QPushButton, 'startButton').isEnabled())
        self.assertEqual(self.widget(QLabel, 'result_processed').text(), '0')


    def test_progress_result_cards_and_cooperative_stop_preserve_finished_counts(self):
        self.save_account()
        self.window.refresh_accounts()
        self.widget(QPushButton, 'checkAllAccountsButton').click()
        self.inputs()
        entered = threading.Event()
        def send(username, message, **kwargs):
            counts = {'success': 5, 'delivered': 2, 'acceptedNotObserved': 3,
                      'failed': 4, 'expired': 1, 'temporaryError': 2,
                      'silenced': 1, 'listenerUnavailable': 1}
            kwargs['on_progress'](counts)
            entered.set()
            while not kwargs['should_cancel']():
                time.sleep(.005)
            return {**counts, 'cancelled': True}
        with patch('desktop.workers.run_parallel_targets', side_effect=send):
            self.window.start_send()
            self.pump_until(entered.is_set)
            self.pump_until(lambda: self.widget(QLabel, 'result_processed').text() == '12')
            self.window.request_stop()
            self.pump_until(lambda: not self.window.is_busy)
        for key, value in {'processed': 12, 'success': 5, 'delivered': 2, 'acceptedNotObserved': 3,
                           'failed': 4, 'expired': 1, 'temporaryError': 2,
                           'silenced': 1, 'listenerUnavailable': 1}.items():
            self.assertEqual(self.widget(QLabel, 'result_' + key).text(), str(value))
        self.assertIn('停止', self.widget(QLabel, 'statusLabel').text())


    def real_login_browser(self, *, complete=True):
        cookies = [
            {'name': 'sessionid', 'value': 'synthetic-session', 'domain': '.tiktok.com', 'expires': -1},
            {'name': 's_v_web_id', 'value': 'verify_synthetic', 'domain': '.tiktok.com', 'expires': -1},
            {'name': 'multi_sids', 'value': '1234567890%3Asynthetic', 'domain': '.tiktok.com', 'expires': -1},
        ] if complete else []
        return LoginBrowser(cookies)

    def assert_normal_browser_cleanup_preserves_account(self):
        self.inputs()
        browser = self.real_login_browser()
        cleanup_entered = threading.Event()
        release_cleanup = threading.Event()
        self.gates.append(release_cleanup)
        def quit_browser():
            cleanup_entered.set()
            release_cleanup.wait(3)
        browser.on_quit = quit_browser
        sends = []
        def send(username, message, **kwargs):
            sends.append(len(load_account_store(kwargs['store_path'])['accounts']))
            return {'success': 1, 'delivered': 1}
        with patch('tiktok_live.browser_auth.launch_available_browser', return_value=browser), patch(
                'tiktok_live.browser_auth._debug_port_is_open', return_value=True), patch(
                'tiktok_live.browser_auth._LOGIN_SUCCESS_CLOSE_DELAY_SECONDS', 0), patch(
                'desktop.workers.run_parallel_targets', side_effect=send):
            self.window.add_account()
            self.pump_until(cleanup_entered.is_set)
            cleanup_cancelled = self.window._cancel_event.is_set()
            release_cleanup.set()
            self.pump_until(lambda: not self.window.is_busy)
        self.assertFalse(cleanup_cancelled, 'normal browser cleanup must not cancel captured login')
        self.assertFalse(self.window._cancel_event.is_set())
        self.assertEqual(len(load_account_store(self.paths.account_store)['accounts']), 1)
        self.assertEqual(sends, [])
        self.assertEqual(browser.quit_calls, 1)



    def test_real_successful_add_account_cleanup_saves_without_sending(self):
        self.assert_normal_browser_cleanup_preserves_account()

    def test_real_browser_closure_before_authentication_still_cancels_login(self):
        self.inputs()
        browser = self.real_login_browser(complete=False)
        with patch('tiktok_live.browser_auth.launch_available_browser', return_value=browser), patch(
                'tiktok_live.browser_auth._debug_port_is_open', return_value=True), patch(
                'desktop.workers.run_parallel_targets', side_effect=AssertionError('must not send')):
            self.window.add_account()
            self.pump_until(lambda: bool(browser.visited))
            browser.states.is_alive = False
            self.pump_until(lambda: not self.window.is_busy)
        self.assertEqual(load_account_store(self.paths.account_store)['accounts'], [])
        self.assertIn('取消', self.widget(QLabel, 'statusLabel').text())
        self.assertEqual(browser.quit_calls, 1)




    def test_missing_chromium_has_actionable_login_error(self):
        with patch('desktop.workers.login_new_account', side_effect=BrowserError('chromium_unavailable')):
            self.window.add_account()
            self.pump_until(lambda: not self.window.is_busy)
        self.assertIn('Chromium', self.widget(QLabel, 'statusLabel').text())
        self.assertTrue(self.widget(QPushButton, 'addAccountButton').isEnabled())

    def test_timeout_after_empty_store_does_not_send_and_restores_start(self):
        self.inputs()
        def login(path, **kwargs):
            raise BrowserError('login_timeout')
        with patch('desktop.workers.login_new_account', side_effect=login), patch(
                'desktop.workers.run_parallel_targets', side_effect=AssertionError('must not send')):
            self.window.add_account()
            self.pump_until(lambda: not self.window.is_busy)
        self.assertIn('超时', self.widget(QLabel, 'statusLabel').text())
        self.assertFalse(self.widget(QPushButton, 'startButton').isEnabled())
        self.assertEqual(self.widget(QLabel, 'result_processed').text(), '0')

    def test_log_panel_redacts_credentials_and_renders_logs_as_plain_text(self):
        logger.info('Cookie: synthetic-secret')
        logger.info('msToken=synthetic-secret')
        logger.info('<b>发送完成</b>')
        self.app.processEvents()
        output = self.widget(QPlainTextEdit, 'logText').toPlainText()
        self.assertNotIn('synthetic-secret', output)
        self.assertIn('[redacted sensitive log]', output)
        self.assertIn('<b>发送完成</b>', output)

    def test_corrupt_store_reports_safe_error_without_rendering_contents(self):
        self.paths.account_store.write_text('Cookie: synthetic-secret', encoding='utf-8')
        self.window.refresh_accounts()
        self.assertIn('账号', self.widget(QLabel, 'statusLabel').text())
        self.assertNotIn('synthetic-secret', self.widget(QPlainTextEdit, 'logText').toPlainText())
        self.assertEqual(self.widget(QTableWidget, 'accountTable').rowCount(), 0)


if __name__ == '__main__':
    unittest.main()
