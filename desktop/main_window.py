"""Desktop widgets and state coordination; business work stays in Qt workers."""

import threading
import time
from collections import deque
from pathlib import Path

from loguru import logger
from PySide6.QtCore import QTimer, Qt, Signal, Slot
from PySide6.QtGui import QColor, QIcon, QPixmap, QTextCharFormat, QTextCursor
from PySide6.QtWidgets import (QFrame, QFormLayout, QGridLayout, QGroupBox,
    QHBoxLayout, QLabel, QLineEdit, QMainWindow, QPlainTextEdit, QPushButton,
    QScrollArea, QSplitter, QVBoxLayout, QWidget, QSpinBox, QDoubleSpinBox,
    QComboBox, QCheckBox, QMessageBox)

from .account_view import AccountTable, load_account_summaries
from .account_edit import AccountEditError, save_edited_account
from .dialogs import AccountDetailsDialog, SettingsDialog, DeliveryDetailsDialog
from .log_bridge import QtLogSink, daily_log_path, sanitize_log_text
from .paths import RuntimePaths
from .settings_store import load_desktop_settings, save_desktop_settings
from .workers import LoginWorker, SendWorker, create_worker_thread
from .positive_count import PositiveCountBox
from tiktok_live.account_store import load_account_store, remove_account, save_account_store_atomic
from tiktok_live.errors import SafeOperationError
from tiktok_live.targets import parse_targets

_RESULT_LABELS = {
    'processed': '已处理', 'success': '接口成功', 'delivered': '已观察到消息',
    'acceptedNotObserved': '已接受 / 未观察到', 'failed': '失败',
    'expired': '登录失效', 'silenced': '疑似禁言',
    'listenerUnavailable': '监听不可用', 'temporaryError': '临时错误',
}
_ERRORS = {
    'listener_unavailable': '直播间监听连接失败，已停止发送。请检查主播用户名、开播状态和网络。',
    'initial_fetch_unavailable': '首次直播数据请求失败，未连接直播心跳、未发送消息。请检查直播间、账号或网络后重试。',
    'login_cancelled': '登录已取消。',
    'login_timeout': '登录超时，请重新添加账号。',
    'chromium_unavailable': '浏览器启动失败，请检查内置 Chromium。',
    'drissionpage_missing': '缺少 DrissionPage，请修复应用依赖后重试。',
    'login_state_unavailable': '无法读取登录状态，请检查浏览器与网络后重试。',
    'login_page_unavailable': '登录页面无法打开，请检查网络后重试。',
    'login_identity_unavailable': '尚未识别到完整账号，请重新登录并等待页面跳转。',
    'login_cookie_incomplete': '登录环境尚未就绪，请重新登录并等待页面加载完成。',
    'browser_close_failed': '登录浏览器未能关闭，请手动关闭本次登录窗口后重试。',
    'account_store_corrupt': '账号库损坏，请先恢复账号文件。',
    'account_store_unreadable': '无法读取账号库，请检查文件权限。',
    'account_save_failed': '账号保存失败，请检查运行目录权限。',
    'invalid_input': '请填写目标用户名和消息内容。',
    'invalid_targets': '请填写主播用户名，多个直播间用 ; 分隔，不要填写链接或昵称。',
    'no_accounts_selected': '请先勾选要发送的账号。',
}
_LOG_COLORS = {
    'SUCCESS': QColor('#188038'),
    'WARNING': QColor('#b06000'),
    'ERROR': QColor('#c5221f'),
    'CRITICAL': QColor('#c5221f'),
}

_APP_STYLE = """
QMainWindow, QWidget#appRoot { background: #f5f6f8; color: #17171c; }
QFrame#headerCard, QGroupBox#contentCard, QGroupBox#accountCard,
QGroupBox#logCard, QGroupBox#statCard {
    background: #ffffff; border: 1px solid #e6e8ee; border-radius: 12px;
}
QGroupBox { font-weight: 600; margin-top: 10px; padding-top: 12px; }
QGroupBox::title { subcontrol-origin: margin; left: 14px; padding: 0 5px; }
QLabel#brandTitle { font-size: 20px; font-weight: 700; color: #111217; }
QGroupBox#contentCard, QGroupBox#accountCard, QGroupBox#statCard {
    margin-top: 8px; padding-top: 0px;
}
QGroupBox#statCard { font-size: 11px; }
QGroupBox#statCard::title { left: 6px; padding: 0 2px; }
QLabel#brandSubtitle, QLabel#sectionHint { color: #747784; }
QLineEdit, QPlainTextEdit, QTableWidget {
    background: #ffffff; border: 1px solid #dfe2e9; border-radius: 8px;
    selection-background-color: #25f4ee; selection-color: #111217;
}
QLineEdit { min-height: 34px; padding: 0 10px; }
QPlainTextEdit { padding: 8px; }
QPlainTextEdit#messageText { padding: 4px; }
QLineEdit:focus, QPlainTextEdit:focus, QTableWidget:focus { border: 1px solid #25aeb1; }
QHeaderView::section { background: #f7f8fa; border: 0; border-bottom: 1px solid #e6e8ee; padding: 8px; }
QPushButton { min-height: 34px; border: 1px solid #d8dbe3; border-radius: 8px; padding: 0 16px; background: #ffffff; font-weight: 600; }
QPushButton:hover { background: #f2f3f6; }
QPushButton:disabled { color: #a7aab3; background: #f3f4f6; }
QPushButton#startButton, QPushButton#addAccountButton { background: #17171c; color: #ffffff; border-color: #17171c; }
QPushButton#startButton:hover, QPushButton#addAccountButton:hover { background: #2b2c33; }
QPushButton#deleteAccountButton { color: #fe2c55; }
QPushButton#stopButton { color: #b42318; }
QPlainTextEdit#logText { background: #111217; color: #dfe2e9; border: 0; font-family: Consolas, "Microsoft YaHei UI"; }
QPlainTextEdit#logText QScrollBar:vertical { background: #252832; width: 16px; margin: 0; }
QPlainTextEdit#logText QScrollBar::handle:vertical { background: #8892a6; min-height: 28px; border-radius: 6px; margin: 2px; }
QPlainTextEdit#logText QScrollBar::handle:vertical:hover { background: #b3bfd5; }
QPlainTextEdit#logText QScrollBar::add-line:vertical, QPlainTextEdit#logText QScrollBar::sub-line:vertical { height: 0; }
QPlainTextEdit#logText QScrollBar::add-page:vertical, QPlainTextEdit#logText QScrollBar::sub-page:vertical { background: none; }
QLabel#statusLabel { color: #525560; padding-left: 8px; }
QSplitter::handle:vertical { background: #dfe2e9; height: 8px; }
"""


def _icon_path() -> Path:
    return Path(__file__).resolve().parent / 'assets' / 'icon.png'


class MainWindow(QMainWindow):
    shutdown_finished = Signal()

    def __init__(self, paths: RuntimePaths):
        super().__init__()
        self.paths = paths
        self._threads = {}
        self._phase = None
        self._cancel_event = threading.Event()
        self._login_cleanup_event = threading.Event()
        self._login_saved = False
        self._last_error = None
        self._closing = False
        self._shutdown_notified = False
        self.shutdown_timeout_ms = 2000
        self._settings = load_desktop_settings(self.paths.settings_file)
        self._dialogs = set()
        self._log_records = deque(maxlen=2000)
        self._rendering_logs = False
        self._build_ui()
        self.account_table.delivery_details_requested.connect(self.show_delivery_details)
        self._log_sink = QtLogSink(self)
        self._log_sink.record_message.connect(
            self._append_log, Qt.ConnectionType.QueuedConnection
        )
        self._log_handler = logger.add(self._log_sink, format='{time:HH:mm:ss} | {message}')
        self.refresh_accounts()
        self._update_controls()

    @property
    def is_busy(self) -> bool:
        return self._phase is not None or bool(self._threads)

    def _build_ui(self):
        self.setWindowTitle('TKChatWave')
        self.setWindowIcon(QIcon(str(_icon_path())))
        self.resize(1180, 820)
        self.setMinimumSize(800, 640)
        root = QWidget(self)
        root.setObjectName('appRoot')
        self.setCentralWidget(root)
        outer_layout = QVBoxLayout(root)
        outer_layout.setContentsMargins(16, 10, 16, 10)
        outer_layout.setSpacing(8)
        self.setStyleSheet(_APP_STYLE)
        splitter = QSplitter(Qt.Orientation.Vertical)
        splitter.setObjectName('contentLogSplitter')
        splitter.setChildrenCollapsible(False)
        self._content_splitter = splitter
        upper_scroll = QScrollArea()
        upper_scroll.setObjectName('upperContentScroll')
        upper_scroll.setWidgetResizable(True)
        upper_scroll.setFrameShape(QFrame.Shape.NoFrame)
        upper_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        upper_scroll.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        upper_content = QWidget()
        layout = QVBoxLayout(upper_content)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(4)
        upper_scroll.setWidget(upper_content)
        self._upper_scroll = upper_scroll
        self._upper_content = upper_content

        header = QFrame()
        header.setObjectName('headerCard')
        header_layout = QHBoxLayout(header)
        header_layout.setContentsMargins(14, 4, 14, 4)
        self.brand_icon = QLabel()
        self.brand_icon.setObjectName('brandIcon')
        self.brand_icon.setFixedSize(36, 36)
        brand_pixmap = QPixmap(str(_icon_path())).scaled(
            144, 144, Qt.AspectRatioMode.KeepAspectRatio,
            Qt.TransformationMode.SmoothTransformation,
        )
        brand_pixmap.setDevicePixelRatio(4)
        self.brand_icon.setPixmap(brand_pixmap)
        title_stack = QVBoxLayout()
        title_stack.setSpacing(2)
        title = QLabel('TKChatWave')
        title.setObjectName('brandTitle')
        subtitle = QLabel('管理本地账号并向目标直播间发送消息')
        subtitle.setObjectName('brandSubtitle')
        title_stack.addWidget(title)
        title_stack.addWidget(subtitle)
        header_layout.addWidget(self.brand_icon)
        header_layout.addLayout(title_stack)
        header_layout.addStretch(1)
        self.settings_button = QPushButton('运行设置')
        self.settings_button.setObjectName('settingsButton')
        self.settings_button.clicked.connect(self.show_settings)
        header_layout.addWidget(self.settings_button)
        layout.addWidget(header)

        content = QGroupBox('发送设置')
        content.setObjectName('contentCard')
        form = QFormLayout()
        form.setContentsMargins(12, 6, 12, 6)
        form.setVerticalSpacing(6)
        self.username_input = QLineEdit()
        self.username_input.setObjectName('targetUsername')
        self.username_input.setPlaceholderText('例如 creator 或 @creator；多个直播间用 ; 分隔')
        self.username_input.setAccessibleName('目标直播间用户名')
        self.message_input = QPlainTextEdit()
        self.message_input.setObjectName('messageText')
        self.message_input.setPlaceholderText('输入要发送的消息')
        self.message_input.setAccessibleName('消息内容')
        self.message_input.setFixedHeight(50)
        form.addRow('目标用户名', self.username_input)
        form.addRow('消息内容', self.message_input)
        repeat_controls = QHBoxLayout()
        repeat_controls.setSpacing(12)
        self.message_count = PositiveCountBox()
        self.message_count.setObjectName('messageCount')
        self.message_count.setValue(1)
        self.message_count.setSuffix(' 次')
        self.message_count.setAccessibleName('每账号每直播间发送次数')
        self.message_interval = QDoubleSpinBox()
        self.message_interval.setObjectName('messageIntervalSeconds')
        self.message_interval.setRange(0.1, 3600)
        self.message_interval.setDecimals(1)
        self.message_interval.setValue(1.0)
        self.message_interval.setSuffix(' 秒')
        self.message_interval.setAccessibleName('每账号每直播间相邻消息发送间隔')
        repeat_controls.addWidget(QLabel('发送次数'))
        repeat_controls.addWidget(self.message_count)
        repeat_controls.addSpacing(12)
        repeat_controls.addWidget(QLabel('发送间隔'))
        repeat_controls.addWidget(self.message_interval)
        repeat_controls.addStretch(1)
        form.addRow('', repeat_controls)
        content.setLayout(form)
        layout.addWidget(content)
        self.username_input.textChanged.connect(self._update_controls)
        self.message_input.textChanged.connect(self._update_controls)

        accounts = QGroupBox('本地账号')
        accounts.setObjectName('accountCard')
        account_layout = QVBoxLayout(accounts)
        account_layout.setContentsMargins(14, 6, 14, 6)
        account_layout.setSpacing(6)
        account_actions = QHBoxLayout()
        account_actions.setSpacing(6)
        self.account_count = QLabel('尚无账号，请先点击“新增 / 更新账号”。')
        self.add_button = QPushButton('新增 / 更新账号')
        self.add_button.setObjectName('addAccountButton')
        self.add_button.clicked.connect(self.add_account)
        self.delete_button = QPushButton('删除账号')
        self.delete_button.setObjectName('deleteAccountButton')
        self.delete_button.setToolTip('删除绿色勾选的账号（支持多选，删除前需确认）')
        self.delete_button.clicked.connect(self.delete_selected_account)
        self.view_button = QPushButton('查看详情')
        self.view_button.setObjectName('viewAccountButton')
        self.view_button.clicked.connect(self.show_selected_account)
        self.expand_accounts_button = QPushButton('展开账号区')
        self.expand_accounts_button.setObjectName('expandAccountsButton')
        self.expand_accounts_button.setCheckable(True)
        self.expand_accounts_button.toggled.connect(self._set_account_area_expanded)
        account_actions.addWidget(self.expand_accounts_button)
        account_actions.addWidget(self.add_button)
        account_actions.addWidget(self.view_button)
        account_actions.addWidget(self.delete_button)
        self.account_table = AccountTable()
        self.account_table.setFixedHeight(70)
        self.account_table.itemSelectionChanged.connect(self._update_controls)
        self.account_table.itemChanged.connect(self._update_controls)
        self.account_table.itemDoubleClicked.connect(lambda *_: self.show_selected_account())
        self.checked_count = QLabel('发送账号：已选 0 个')
        self.checked_count.setObjectName('checkedAccountCount')
        self.check_all_button = QPushButton('全选')
        self.check_all_button.setObjectName('checkAllAccountsButton')
        self.check_all_button.clicked.connect(lambda: self.account_table.set_all_checked(True))
        self.uncheck_all_button = QPushButton('清空选择')
        self.uncheck_all_button.setObjectName('uncheckAllAccountsButton')
        self.uncheck_all_button.clicked.connect(lambda: self.account_table.set_all_checked(False))
        account_actions.addWidget(self.check_all_button)
        account_actions.addWidget(self.uncheck_all_button)
        account_meta = QHBoxLayout()
        account_meta.setSpacing(12)
        account_meta.addWidget(self.account_count)
        account_meta.addWidget(self.checked_count)
        account_meta.addStretch(1)
        self._account_toolbar = QGridLayout()
        self._account_toolbar.setContentsMargins(0, 0, 0, 0)
        self._account_toolbar.setSpacing(6)
        self._account_meta = account_meta
        self._account_actions = account_actions
        self._account_toolbar.addLayout(account_meta, 0, 0)
        self._account_toolbar.addLayout(account_actions, 0, 1)
        account_layout.addLayout(self._account_toolbar)
        account_layout.addWidget(self.account_table)
        layout.addWidget(accounts)

        results = QGridLayout()
        results.setVerticalSpacing(4)
        results.setHorizontalSpacing(6)
        self._results_grid = results
        self._result_cards = []
        self._wide_rows = None
        self.result_values = {}
        for index, (key, result_title) in enumerate(_RESULT_LABELS.items()):
            card = QGroupBox(result_title)
            card.setObjectName('statCard')
            value = QLabel('0')
            value.setObjectName('result_' + key)
            value.setAlignment(Qt.AlignmentFlag.AlignCenter)
            accent = '#fe2c55' if key in {'failed', 'expired', 'silenced'} else '#25aeb1'
            value.setStyleSheet(f'font-size: 18px; font-weight: 700; color: {accent};')
            card_layout = QVBoxLayout(card)
            card_layout.setContentsMargins(8, 8, 8, 2)
            card_layout.addWidget(value)
            results.addWidget(card, index // 5, index % 5)
            self._result_cards.append(card)
            self.result_values[key] = value
        layout.addLayout(results)

        log_card = QGroupBox()
        log_card.setObjectName('logCard')
        log_card.setStyleSheet('QGroupBox#logCard { margin-top: 0; padding-top: 0; }')
        self._log_card = log_card
        log_layout = QVBoxLayout(log_card)
        log_tools = QHBoxLayout()
        log_tools.addWidget(QLabel('运行日志'))
        self.log_level = QComboBox()
        self.log_level.setObjectName('logLevelFilter')
        for title, level in [('全部级别', ''), ('成功', 'SUCCESS'), ('警告', 'WARNING'), ('错误', 'ERROR')]:
            self.log_level.addItem(title, level)
        self.log_search = QLineEdit()
        self.log_search.setObjectName('logSearch')
        self.log_search.setPlaceholderText('搜索账号、直播间或错误分类…')
        self.log_search.setClearButtonEnabled(True)
        self.log_follow = QCheckBox('跟随最新')
        self.log_follow.setChecked(True)
        self.log_follow.setObjectName('logFollowLatest')
        log_tools.addWidget(self.log_level)
        log_tools.addWidget(self.log_search, 1)
        log_tools.addWidget(self.log_follow)
        log_layout.addLayout(log_tools)
        self.clear_console_button = QPushButton('清空控制台')
        self.clear_console_button.setObjectName('clearConsoleButton')
        self.clear_console_button.setToolTip('仅清除当前窗口显示的日志，已保存的每日日志不受影响')
        self.log_text = QPlainTextEdit()
        self.clear_console_button.clicked.connect(self._clear_console)
        self.log_text.setObjectName('logText')
        self.log_text.setReadOnly(True)
        self.log_text.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOn)
        self.log_text.verticalScrollBar().setAccessibleName('上下滚动查看日志')
        self.log_text.setMaximumBlockCount(2000)
        self.log_text.setAccessibleName('运行日志')
        log_layout.addWidget(self.log_text, 1)
        self.log_level.currentIndexChanged.connect(self._filter_logs)
        self.log_search.textChanged.connect(self._filter_logs)
        self.log_follow.toggled.connect(self._follow_logs)
        self.log_text.verticalScrollBar().valueChanged.connect(self._log_scrolled)
        splitter.addWidget(upper_scroll)
        splitter.addWidget(log_card)
        splitter.setStretchFactor(0, 0)
        splitter.setStretchFactor(1, 1)
        splitter.setSizes([450, 350])
        outer_layout.addWidget(splitter, 1)

        actions = QHBoxLayout()
        self.start_button = QPushButton('开始发送')
        self.start_button.setObjectName('startButton')
        self.start_button.clicked.connect(self.start_send)
        self.stop_button = QPushButton('停止')
        self.stop_button.setObjectName('stopButton')
        self.stop_button.clicked.connect(self.request_stop)
        self.status_label = QLabel('准备就绪')
        self.status_label.setObjectName('statusLabel')
        self.status_label.setWordWrap(True)
        self.status_label.setTextFormat(Qt.TextFormat.PlainText)
        actions.addWidget(self.start_button)
        actions.addWidget(self.stop_button)
        actions.addWidget(self.status_label, 1)
        actions.addWidget(self.clear_console_button)
        outer_layout.addLayout(actions)
        self._fit_upper_content()
        QTimer.singleShot(0, self._fit_upper_content)

    def _fit_upper_content(self):
        # On a normal desktop the entire form is fixed and visible. If the
        # window is physically shorter than the form, leave the log usable and
        # permit overflow rather than painting controls on top of each other.
        wide = self.width() >= 1180
        if wide != self._wide_rows:
            self._wide_rows = wide
            self._account_toolbar.removeItem(self._account_meta)
            self._account_toolbar.removeItem(self._account_actions)
            self._account_toolbar.addLayout(self._account_meta, 0, 0, 1, 1 if wide else 2)
            self._account_toolbar.addLayout(self._account_actions, 0 if wide else 1,
                                           1 if wide else 0, 1, 1 if wide else 2)
            columns = 9 if wide else 5
            for index, card in enumerate(self._result_cards):
                self._results_grid.removeWidget(card)
                self._results_grid.addWidget(card, index // columns, index % columns)
            self._upper_content.layout().activate()
        self.centralWidget().layout().activate()
        full_height = self._upper_content.sizeHint().height() + 4
        # Reserve real log space, including the log card's title and margins.
        # Otherwise the upper minimum can consume almost the entire splitter.
        log_reserve = max(200, self.height() // 3 + 64)
        available = max(160, self._content_splitter.height()
                        - self._content_splitter.handleWidth() - log_reserve)
        height = min(full_height, available)
        self._log_card.setMinimumHeight(log_reserve)
        self._upper_scroll.setMinimumHeight(height)
        self._content_splitter.setSizes([height, max(0,
            self._content_splitter.height() - height - self._content_splitter.handleWidth())])

    def resizeEvent(self, event):
        super().resizeEvent(event)
        if hasattr(self, '_upper_scroll'):
            self._fit_upper_content()

    @Slot(bool)
    def _set_account_area_expanded(self, expanded: bool):
        self._size_account_table()
        self.expand_accounts_button.setText('收起账号区' if expanded else '展开账号区')
        QTimer.singleShot(0, self._fit_upper_content)

    def _size_account_table(self):
        if self.expand_accounts_button.isChecked():
            height = 310
        else:
            rows = max(2, min(self.account_table.rowCount(), 4))
            height = min(166, self.account_table.horizontalHeader().height()
                         + rows * self.account_table.verticalHeader().defaultSectionSize()
                         + 2 * self.account_table.frameWidth() + 2)
        self.account_table.setFixedHeight(height)

    @Slot()
    def _update_controls(self):
        try:
            parse_targets(self.username_input.text())
            valid = bool(self.message_input.toPlainText().strip())
        except SafeOperationError:
            valid = False
        busy = self.is_busy or self._closing
        checked_count = len(self.account_table.checked_account_ids())
        self.checked_count.setText(f'发送账号：已选 {checked_count} 个')
        self.start_button.setEnabled(valid and checked_count > 0 and not busy)
        self.add_button.setEnabled(not busy)
        self.delete_button.setEnabled(
            not busy and checked_count > 0
        )
        self.view_button.setEnabled(
            not self._closing and self.account_table.selected_account_id() is not None
        )
        self.check_all_button.setEnabled(not busy and self.account_table.rowCount() > 0)
        self.uncheck_all_button.setEnabled(not busy and checked_count > 0)
        self.account_table.setEnabled(not self._closing)
        self.account_table.set_selection_locked(busy)
        self.settings_button.setEnabled(not self._closing)
        for dialog in self._dialogs:
            if isinstance(dialog, (AccountDetailsDialog, SettingsDialog)):
                dialog.set_read_only(busy)
        self.stop_button.setEnabled(busy and not self._cancel_event.is_set())
        self.username_input.setEnabled(not busy)
        self.message_input.setEnabled(not busy)
        self.message_count.setEnabled(not busy)
        self.message_interval.setEnabled(not busy)

    @Slot()
    def refresh_accounts(self):
        try:
            summaries = load_account_summaries(self.paths.account_store)
            self.account_table.set_accounts(summaries)
            self.account_count.setText(f'已保存 {len(summaries)} 个账号' if summaries
                                       else '尚无账号，请先点击“新增 / 更新账号”。')
        except Exception as exc:
            self.account_table.set_accounts([])
            self.account_count.setText('账号库读取失败')
            self.status_label.setText(_ERRORS.get(getattr(exc, 'classification', ''),
                                                 '账号库读取失败，请检查运行目录。'))
        self._size_account_table()
        QTimer.singleShot(0, self._fit_upper_content)
        self._update_controls()

    def _begin(self, phase):
        self._phase = phase
        self._last_error = None
        self._update_controls()

    def _launch(self, worker):
        thread = create_worker_thread(worker, self)
        self._threads[thread] = worker
        thread.finished.connect(self._thread_finished, Qt.ConnectionType.QueuedConnection)
        thread.start()

    @Slot()
    def start_send(self):
        if self._closing or self.is_busy or not self.start_button.isEnabled():
            return
        self._cancel_event = threading.Event()
        self._show_counts({})
        self._send()

    def _send(self):
        self.account_table.begin_delivery(self.account_table.checked_account_ids(),
            len(parse_targets(self.username_input.text())) * self.message_count.value())
        self._refresh_delivery_dialogs()
        self._begin('sending')
        self.status_label.setText('正在发送…')
        worker = SendWorker(
            self.username_input.text(), self.message_input.toPlainText(),
            self.paths, self._cancel_event, settings=self._settings,
            selected_account_ids=tuple(self.account_table.checked_account_ids()),
            message_count=self.message_count.value(),
            message_interval_seconds=self.message_interval.value(),
        )
        worker.progress.connect(self._send_progress, Qt.ConnectionType.QueuedConnection)
        worker.failed.connect(self._worker_failed, Qt.ConnectionType.QueuedConnection)
        worker.finished.connect(self._send_finished, Qt.ConnectionType.QueuedConnection)
        self._launch(worker)

    @Slot()
    def add_account(self):
        if self._closing or self.is_busy:
            return
        self._cancel_event = threading.Event()
        self._login()

    @Slot()
    def delete_selected_account(self):
        if self._closing or self.is_busy:
            return
        account_ids = tuple(self.account_table.checked_account_ids())
        if not account_ids:
            self.status_label.setText('请先勾选要删除的账号。')
            return
        usernames = [self.account_table.item(row, 1).text()
                     for row in range(self.account_table.rowCount())
                     if self.account_table.item(row, 0).checkState() == Qt.CheckState.Checked]
        prompt = QMessageBox(self)
        prompt.setWindowTitle('确认删除账号')
        prompt.setIcon(QMessageBox.Icon.Warning)
        prompt.setTextFormat(Qt.TextFormat.PlainText)
        preview = '\n'.join(usernames[:10])
        if len(usernames) > 10:
            preview += f'\n……另有 {len(usernames) - 10} 个账号（完整名单见详情）'
            prompt.setDetailedText('\n'.join(usernames))
        prompt.setText(f'确定删除绿色勾选的 {len(account_ids)} 个账号吗？\n{preview}\n\n仅删除本机登录信息，不会注销 TikTok 账号。再次使用需要重新登录。')
        prompt.setStandardButtons(QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.Cancel)
        prompt.button(QMessageBox.StandardButton.Yes).setText('删除账号')
        prompt.button(QMessageBox.StandardButton.Cancel).setText('取消')
        prompt.setDefaultButton(QMessageBox.StandardButton.Cancel)
        prompt.setEscapeButton(QMessageBox.StandardButton.Cancel)
        if prompt.exec() != QMessageBox.StandardButton.Yes:
            prompt.deleteLater()
            return
        prompt.deleteLater()
        if self._closing or self.is_busy:
            return
        try:
            store = load_account_store(self.paths.account_store)
            removed = [account_id for account_id in account_ids if remove_account(store, account_id)]
            if not removed:
                self.status_label.setText('所选账号已不存在。')
                self.refresh_accounts()
                return
            save_account_store_atomic(self.paths.account_store, store)
            for account_id in removed:
                logger.success(
                    '账号已删除 | account_id={} | classification=account_deleted',
                    account_id[:12],
                )
            self.status_label.setText(f'已删除 {len(removed)} 个勾选账号。')
            self.refresh_accounts()
        except Exception as exc:
            self.status_label.setText(_ERRORS.get(
                getattr(exc, 'classification', ''), '删除账号失败，请检查账号文件。'
            ))

    def _show_dialog(self, dialog):
        dialog.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose, True)
        self._dialogs.add(dialog)
        dialog.destroyed.connect(lambda *_: self._dialogs.discard(dialog))
        dialog.show()
        dialog.raise_()
        dialog.activateWindow()

    @Slot(str)
    def show_delivery_details(self, account_id):
        if self._closing:
            return
        for dialog in self._dialogs:
            if isinstance(dialog, DeliveryDetailsDialog) and dialog.account_id == account_id:
                dialog.raise_()
                dialog.activateWindow()
                return
        row = self.account_table._account_ids.index(account_id)
        username = self.account_table.item(row, 1).text()
        self._show_dialog(DeliveryDetailsDialog(account_id, username,
            self.account_table.delivery_details(account_id), self))

    def _refresh_delivery_dialogs(self):
        for dialog in self._dialogs:
            if isinstance(dialog, DeliveryDetailsDialog):
                dialog.update_details(self.account_table.delivery_details(dialog.account_id))

    @Slot()
    def show_selected_account(self):
        if self._closing:
            return
        account_id = self.account_table.selected_account_id()
        if not account_id:
            self.status_label.setText('请先选择要查看的账号。')
            return
        try:
            store = load_account_store(self.paths.account_store)
            account = next(
                (item for item in store['accounts'] if item.get('id') == account_id), None
            )
            if account is None:
                self.status_label.setText('所选账号已不存在。')
                self.refresh_accounts()
                return
            dialog = AccountDetailsDialog(account, self._save_account_details, self)
            dialog.set_read_only(self.is_busy)
            self._show_dialog(dialog)
        except Exception as exc:
            self.status_label.setText(_ERRORS.get(
                getattr(exc, 'classification', ''), '读取账号详情失败。'
            ))

    def _save_account_details(self, original: dict, edited: object) -> None:
        if self._closing or self.is_busy:
            raise AccountEditError('程序正在运行，请结束当前操作后再保存。')
        save_edited_account(self.paths.account_store, original, edited)
        self.refresh_accounts()
        self.status_label.setText('账号信息已保存。')
        logger.success('账号信息已保存 | classification=account_details_saved')

    @Slot()
    def show_settings(self):
        if self._closing:
            return
        dialog = SettingsDialog(self._settings, self)
        dialog.set_read_only(self.is_busy)
        dialog.saved.connect(self._save_settings)
        self._show_dialog(dialog)

    @Slot(object)
    def _save_settings(self, values):
        if self._closing or self.is_busy:
            return
        try:
            save_desktop_settings(self.paths.settings_file, values)
            self._settings = values
            self.status_label.setText('运行设置已保存。')
            logger.success('运行设置已保存 | classification=settings_saved')
        except Exception:
            self.status_label.setText('运行设置保存失败，请检查运行目录权限。')

    def _login(self):
        self._begin('login')
        self._login_saved = False
        self.status_label.setText('正在打开账号登录…')
        worker = LoginWorker(self.paths.account_store,
                             timeout_seconds=self._settings.login_timeout_seconds,
                             cancel_event=self._cancel_event,
                             profile_root=self.paths.profile_root)
        self._login_cleanup_event = worker.browser_cleanup_started
        worker.browser_ready.connect(self._browser_ready, Qt.ConnectionType.QueuedConnection)
        worker.browser_closing.connect(self._browser_closing, Qt.ConnectionType.QueuedConnection)
        worker.account_saved.connect(self._account_saved, Qt.ConnectionType.QueuedConnection)
        worker.failed.connect(self._worker_failed, Qt.ConnectionType.QueuedConnection)
        worker.finished.connect(self._login_finished, Qt.ConnectionType.QueuedConnection)
        self._launch(worker)

    @Slot(dict)
    def _send_progress(self, result):
        if self._closing:
            return
        self.account_table.update_delivery(result)
        self._refresh_delivery_dialogs()
        if result.get('phase') == 'login_required':
            self.status_label.setText('尚无账号，请先点击“新增 / 更新账号”完成登录。')
        else:
            self._show_counts(result)

    @Slot(dict)
    def _send_finished(self, result):
        logger.info('本轮运行结束，日志保存路径：{}', daily_log_path(self.paths.log_dir))
        if self._closing:
            return
        self.account_table.update_delivery(result, finished=True)
        self._refresh_delivery_dialogs()
        self._show_counts(result)
        if result.get('status') == 'login_required':
            self.status_label.setText('尚无账号，请先点击“新增 / 更新账号”完成登录。')
        elif self._last_error and self._last_error != 'runtime_error':
            pass
        else:
            rooms = result.get('rooms', [])
            failed = (result.get('status') == 'failed' or result.get('failed_tasks', 0)
                      or any(room.get('status') == 'failed' for room in rooms)
                      or self._last_error == 'runtime_error')
            if failed:
                summary = '本轮发送失败，已完成的结果保留。'
            elif result.get('status') == 'cancelled' or result.get('cancelled'):
                summary = '已停止，已完成的结果保留。'
            else:
                summary = '本轮发送完成。'
            summary += (f"已观察到消息 {result.get('delivered', 0)}；"
                        f"已接受 / 未观察到 {result.get('acceptedNotObserved', 0)}；"
                        f"监听不可用 {result.get('listenerUnavailable', 0)}。")
            room_labels = {'completed': '完成', 'failed': '失败', 'cancelled': '已停止',
                           'queued': '未启动', 'running': '未结束'}
            room_summaries = []
            for room in rooms:
                failed_accounts = sum(account.get('status') == 'failed'
                                      for account in room.get('accounts', []))
                room_summary = (f"{room.get('target', '')}："
                    f"{room_labels.get(room.get('status'), '未完成')}"
                    f"（账号失败 {failed_accounts}；已观察到消息 {room.get('delivered', 0)}；"
                    f"已接受 / 未观察到 {room.get('acceptedNotObserved', 0)}；"
                    f"监听不可用 {room.get('listenerUnavailable', 0)}）")
                room_summaries.append(room_summary)
                logger.info('直播间结果 | {}', room_summary)
            if room_summaries:
                summary += ' 房间：' + '；'.join(room_summaries[:3])
                if len(room_summaries) > 3:
                    summary += f'；其余 {len(room_summaries) - 3} 个房间见运行日志。'
            self.status_label.setText(summary)
        self.refresh_accounts()

    @Slot(dict)
    def _account_saved(self, summary):
        if self._closing:
            return
        self._login_saved = True
        self.status_label.setText('账号已更新。' if summary.get('duplicate') else '账号已保存。')

    @Slot()
    def _login_finished(self):
        status = ('saved' if self._login_saved else
                  'cancelled' if self._cancel_event.is_set() else 'failed')
        logger.info('登录操作结束 | status={} | classification=login_finished | 日志保存路径：{}',
                    status, daily_log_path(self.paths.log_dir))
        if self._closing:
            return
        self.refresh_accounts()
        if not self._last_error and self._cancel_event.is_set():
            self.status_label.setText('登录已取消。')

    @Slot(str)
    def _worker_failed(self, classification):
        if self._closing:
            return
        self._last_error = classification
        self.status_label.setText(_ERRORS.get(classification, '操作失败，请查看运行日志并重试。'))

    @Slot()
    def _thread_finished(self):
        self._finish_thread(self.sender())

    def _finish_thread(self, thread):
        if thread not in self._threads:
            return
        # finished is emitted before native thread-local cleanup completes.
        # Retain both wrappers until wait confirms the native thread has exited.
        if not thread.wait(0):
            QTimer.singleShot(10, lambda: self._finish_thread(thread))
            return
        self._retire_thread(thread)
        if self._threads:
            return
        self._phase = None
        if self._closing:
            self.close()
        self._update_controls()

    @Slot()
    def request_stop(self):
        if not self.is_busy:
            return
        self._cancel_event.set()
        self.status_label.setText('正在停止…当前操作完成后停止。')
        self._update_controls()

    def _show_counts(self, result):
        for key, value in self.result_values.items():
            count = result.get(key, 0)
            value.setText(str(max(0, count) if type(count) is int else 0))

    @Slot(str, str)
    def _append_log(self, text, level='INFO'):
        if self._closing:
            return
        safe_text = sanitize_log_text(text)[:16000]
        level = str(level).upper()
        self._rendering_logs = True
        try:
            for line in safe_text.splitlines() or ['']:
                if not line.strip():
                    continue
                if len(self._log_records) == self._log_records.maxlen:
                    old_line, old_level = self._log_records[0]
                    if self._log_matches(old_line, old_level):
                        cursor = QTextCursor(self.log_text.document())
                        cursor.movePosition(QTextCursor.MoveOperation.Start)
                        cursor.select(QTextCursor.SelectionType.BlockUnderCursor)
                        cursor.removeSelectedText()
                        cursor.deleteChar()
                self._log_records.append((line, level))
                if self._log_matches(line, level):
                    self._write_log_line(line, level)
        finally:
            self._rendering_logs = False

    def _log_matches(self, text, level):
        selected = self.log_level.currentData()
        return (not selected or level == selected or selected == 'ERROR' and level == 'CRITICAL') and (
            self.log_search.text().casefold() in text.casefold())

    def _filter_logs(self, *_):
        position = self.log_text.verticalScrollBar().value()
        self._rendering_logs = True
        try:
            self.log_text.clear()
            for text, level in self._log_records:
                if self._log_matches(text, level):
                    self._write_log_line(text, level)
            if not self.log_follow.isChecked():
                self.log_text.verticalScrollBar().setValue(position)
        finally:
            self._rendering_logs = False

    def _clear_console(self):
        self._log_records.clear()
        self.log_text.clear()

    def _follow_logs(self, checked):
        if checked:
            scrollbar = self.log_text.verticalScrollBar()
            scrollbar.setValue(scrollbar.maximum())

    def _log_scrolled(self, value):
        if not self._rendering_logs and value < self.log_text.verticalScrollBar().maximum():
            self.log_follow.setChecked(False)

    def _write_log_line(self, safe_text, level):
        scrollbar = self.log_text.verticalScrollBar()
        previous_position = scrollbar.value()
        follow_tail = self.log_follow.isChecked()
        cursor = self.log_text.textCursor()
        cursor.movePosition(QTextCursor.MoveOperation.End)
        if not self.log_text.document().isEmpty():
            cursor.insertText('\n')
        text_format = QTextCharFormat()
        color = _LOG_COLORS.get(str(level).upper())
        if color is not None:
            text_format.setForeground(color)
        cursor.insertText(safe_text, text_format)
        if follow_tail:
            self.log_text.setTextCursor(cursor)
            self.log_text.ensureCursorVisible()
            scrollbar.setValue(scrollbar.maximum())
        else:
            scrollbar.setValue(previous_position)

    @Slot()
    def _browser_closing(self):
        pass

    @Slot(int)
    def _browser_ready(self, process_id):
        if self._cancel_event.is_set() or self._login_cleanup_event.is_set():
            return
        self.status_label.setText('请在外部浏览器完成账号登录。')

    def _retire_thread(self, thread):
        # Called only after a successful native wait. The worker's deferred
        # deletion is handled in its own thread by create_worker_thread.
        thread.finished.disconnect(self._thread_finished)
        self._threads.pop(thread, None)
        thread.deleteLater()

    def shutdown(self, timeout_ms=None) -> bool:
        """Request cooperative stop and wait at most one shared time budget.

        False means close must be deferred: this window still owns the running
        thread and worker. Never terminate a thread or destroy its GUI owner.
        """
        self._closing = True
        self._cancel_event.set()
        if self._log_handler is not None:
            logger.remove(self._log_handler)
            self._log_handler = None
        self.status_label.setText('正在退出…等待当前操作安全结束。')
        self._update_controls()
        budget = self.shutdown_timeout_ms if timeout_ms is None else timeout_ms
        deadline = time.monotonic() + max(0, budget) / 1000
        for thread in list(self._threads):
            remaining_ms = max(0, int((deadline - time.monotonic()) * 1000))
            if thread.wait(remaining_ms):
                self._retire_thread(thread)
        if not self._threads:
            self._phase = None
            if not self._shutdown_notified:
                # Explicit completion must not depend on lastWindowClosed:
                # startup may fail while hidden or another window may be open.
                # Mark first to prevent recursive quit/shutdown notification.
                self._shutdown_notified = True
                self.shutdown_finished.emit()
        return not self.is_busy

    def closeEvent(self, event):
        if self.shutdown():
            event.accept()
        else:
            event.ignore()
