"""Modeless account-details and runtime-settings dialogs."""

import json
from copy import deepcopy

from PySide6.QtCore import Signal
from PySide6.QtWidgets import (
    QCheckBox, QComboBox, QDialog, QDialogButtonBox, QDoubleSpinBox, QFormLayout,
    QGroupBox, QLabel, QLineEdit, QPlainTextEdit, QPushButton, QSpinBox,
    QTabWidget, QVBoxLayout, QWidget,
)

from .settings_store import DesktopSettings
from tiktok_live.wss.proxy import normalize_observer_proxy
from .account_edit import AccountEditError, account_for_details, account_from_details


class DeliveryDetailsDialog(QDialog):
    def __init__(self, account_id, username, text, parent=None):
        super().__init__(parent)
        self.account_id = account_id
        self.setWindowTitle(f'消息展示详情 — {username}')
        self.resize(720, 480)
        layout = QVBoxLayout(self)
        self.details = QPlainTextEdit()
        self.details.setReadOnly(True)
        self.details.setPlainText(text)
        layout.addWidget(self.details)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        buttons.rejected.connect(self.close)
        layout.addWidget(buttons)

    def update_details(self, text):
        if text == self.details.toPlainText():
            return
        position = self.details.verticalScrollBar().value()
        self.details.setPlainText(text)
        self.details.verticalScrollBar().setValue(position)


class AccountDetailsDialog(QDialog):
    def __init__(self, account: dict, save_callback, parent=None):
        super().__init__(parent)
        self._original = deepcopy(account)
        self._save_callback = save_callback
        self.setWindowTitle('账号保存信息')
        self.resize(760, 580)
        layout = QVBoxLayout(self)
        warning = QLabel('可编辑下方 JSON 并保存。cookie 字段采用网页格式 name=value; name2=value2。内容包含 Cookie 和签名密钥，请勿发送给他人；账号 ID 不可修改。')
        warning.setStyleSheet('color: #b42318; font-weight: 600;')
        warning.setWordWrap(True)
        layout.addWidget(warning)
        self.details = QPlainTextEdit()
        self.details.setObjectName('accountDetailsText')
        self.details.setPlainText(json.dumps(account_for_details(account), ensure_ascii=False, indent=2))
        layout.addWidget(self.details, 1)
        self.status = QLabel('')
        self.status.setObjectName('accountEditStatus')
        self.status.setWordWrap(True)
        layout.addWidget(self.status)
        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Save | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.button(QDialogButtonBox.StandardButton.Save).setObjectName('saveAccountDetailsButton')
        buttons.accepted.connect(self._save)
        buttons.rejected.connect(self.close)
        layout.addWidget(buttons)
        self._save_button = buttons.button(QDialogButtonBox.StandardButton.Save)
        self._read_only = False

    def set_read_only(self, value):
        self._read_only = bool(value)
        self.details.setReadOnly(value)
        self._save_button.setEnabled(not value)
        self.status.setText('任务运行中：仅可查看，结束后可编辑。' if value else '')

    def _save(self):
        if self._read_only:
            return
        try:
            edited = account_from_details(self._original,
                                          json.loads(self.details.toPlainText()))
        except (ValueError, UnicodeError):
            self.status.setText('JSON 格式不正确，未保存。')
            return
        except AccountEditError as exc:
            self.status.setText(str(exc))
            return
        try:
            self._save_callback(self._original, edited)
        except Exception as exc:
            self.status.setText(str(exc) if isinstance(exc, AccountEditError)
                                else '保存失败，请检查账号文件权限。')
            return
        self.close()


def _number(value: float, *, decimals=1, minimum=0.0, maximum=3600.0) -> QDoubleSpinBox:
    control = QDoubleSpinBox()
    control.setDecimals(decimals)
    control.setRange(minimum, maximum)
    control.setSingleStep(1)
    control.setValue(float(value))
    control.setSuffix(' 秒')
    return control


def _integer(value: int, *, minimum=0, maximum=3600, suffix='') -> QSpinBox:
    control = QSpinBox()
    control.setRange(minimum, maximum)
    control.setValue(int(value))
    control.setSuffix(suffix)
    return control


class SettingsDialog(QDialog):
    saved = Signal(object)

    def __init__(self, values: DesktopSettings, parent=None):
        super().__init__(parent)
        self.setWindowTitle('运行设置')
        self.resize(620, 540)
        layout = QVBoxLayout(self)
        tabs = QTabWidget()

        general = QWidget()
        general_form = QFormLayout(general)
        self.room_execution_mode = QComboBox()
        self.room_execution_mode.setObjectName('roomExecutionMode')
        self.room_execution_mode.addItem('串行：每账号独立按顺序切换直播间', 'serial')
        self.room_execution_mode.addItem('并行：最多同时执行 3 个直播间', 'parallel')
        self.room_execution_mode.setCurrentIndex(1 if values.room_execution_mode == 'parallel' else 0)
        general_form.addRow('直播间执行方式', self.room_execution_mode)
        self.login_timeout = _integer(values.login_timeout_seconds, minimum=10, suffix=' 秒')
        self.login_timeout.setObjectName('loginTimeoutSeconds')
        self.account_interval = _number(values.account_interval_seconds)
        self.account_interval.setObjectName('accountIntervalSeconds')
        general_form.addRow('登录最长等待', self.login_timeout)
        self.account_interval.setToolTip('0 秒同时启动所有已选账号；大于 0 仅错开启动，不等待前一账号完成。')
        general_form.addRow('账号启动错峰间隔', self.account_interval)
        tabs.addTab(general, '基础')

        observation = QWidget()
        observation_form = QFormLayout(observation)
        self.observation_enabled = QCheckBox('监听直播弹幕并确认消息是否展示')
        self.observation_enabled.setObjectName('observationEnabled')
        self.observation_enabled.setChecked(values.observation_enabled)
        self.show_comments = QCheckBox('在运行日志中显示监听到的弹幕')
        self.show_comments.setObjectName('showLiveComments')
        self.show_comments.setChecked(values.show_live_comments)
        self.observation_timeout = _number(values.observation_timeout_seconds, minimum=1)
        self.observation_timeout.setObjectName('observationTimeoutSeconds')
        self.message_attempts = _integer(values.message_attempts, minimum=1, maximum=20, suffix=' 次')
        self.message_attempts.setObjectName('messageAttempts')
        self.message_retry_delay = _number(values.message_retry_delay_seconds)
        self.message_retry_delay.setObjectName('messageRetryDelaySeconds')
        observation_form.addRow(self.observation_enabled)
        observation_form.addRow(self.show_comments)
        observation_form.addRow('消息展示观察时间', self.observation_timeout)
        self.message_attempts.setToolTip('每条计划消息的最多尝试次数，包含首次；临时失败或未观察到时重试，可能出现重复弹幕。')
        observation_form.addRow('每条消息总尝试次数', self.message_attempts)
        observation_form.addRow('临时失败/未展示重试间隔', self.message_retry_delay)
        tabs.addTab(observation, '消息确认')

        listener = QWidget()
        listener_form = QFormLayout(listener)
        self.listener_timeout = _number(values.listener_connect_timeout_seconds, minimum=1)
        self.listener_timeout.setObjectName('listenerConnectTimeoutSeconds')
        self.listener_retries = _integer(values.listener_connect_retries, maximum=20, suffix=' 次')
        self.listener_retries.setObjectName('listenerConnectRetries')
        self.listener_retry_delay = _number(values.listener_retry_delay_seconds)
        self.listener_retry_delay.setObjectName('listenerRetryDelaySeconds')
        self.heartbeat_interval = _number(values.heartbeat_interval_seconds,minimum=1,maximum=60)
        self.heartbeat_interval.setObjectName('heartbeatIntervalSeconds')
        self.heartbeat_interval.setToolTip('账号和访客 WSS 共用；较大间隔会延长发送前等待，仍需至少 30 秒且 3 次成功心跳。')
        listener_form.addRow(QLabel('同时作用于访客监听和账号心跳；账号每次新连接仍需至少 30 秒、3 次心跳预热。'))
        listener_form.addRow('单次 WSS 连接超时', self.listener_timeout)
        listener_form.addRow('首次失败后的重试次数', self.listener_retries)
        listener_form.addRow('监听连接重试间隔', self.listener_retry_delay)
        listener_form.addRow('心跳发送间隔',self.heartbeat_interval)
        self.observer_proxy = QLineEdit(values.observer_proxy)
        self.observer_proxy.setObjectName('observerProxy')
        self.observer_proxy.setEchoMode(QLineEdit.EchoMode.Password)
        self.observer_proxy.setPlaceholderText('留空沿用当前网络；主机:端口:用户名:密码')
        self.observer_proxy.setMaxLength(4096)
        self.observer_proxy.setToolTip('也支持 socks5://用户名:密码@主机:端口；用户名和密码各限 127 个 UTF-8 字节；仅匿名初始化、监听及重连使用。')
        listener_form.addRow('匿名监听 SOCKS5 代理', self.observer_proxy)
        reveal_proxy = QCheckBox('显示代理内容')
        reveal_proxy.toggled.connect(lambda shown: self.observer_proxy.setEchoMode(
            QLineEdit.EchoMode.Normal if shown else QLineEdit.EchoMode.Password))
        listener_form.addRow('', reveal_proxy)
        proxy_notice = QLabel('仅匿名监听使用，不影响账号 fetch、发消息和心跳。留空沿用当前网络；代理失败不切换直连。代理信息保存在本机设置文件中，请勿分享。')
        proxy_notice.setWordWrap(True)
        listener_form.addRow(proxy_notice)
        self.proxy_error = QLabel('')
        self.proxy_error.setObjectName('observerProxyError')
        self.proxy_error.setWordWrap(True)
        self.proxy_error.setStyleSheet('color: #b42318;')
        listener_form.addRow(self.proxy_error)
        tabs.addTab(listener, '监听连接')

        advanced = QWidget()
        advanced_form = QFormLayout(advanced)
        self.room_cookie = QLineEdit(values.room_id_cookie)
        self.room_cookie.setObjectName('roomIdCookie')
        self.room_cookie.setEchoMode(QLineEdit.EchoMode.Password)
        self.room_cookie.setPlaceholderText('通常留空，仅用于 roomId 获取失败时兜底')
        reveal = QCheckBox('显示 Cookie')
        reveal.toggled.connect(lambda shown: self.room_cookie.setEchoMode(
            QLineEdit.EchoMode.Normal if shown else QLineEdit.EchoMode.Password
        ))
        advanced_form.addRow('roomId 兜底 Cookie', self.room_cookie)
        advanced_form.addRow('', reveal)
        tabs.addTab(advanced, '高级兜底')

        layout.addWidget(tabs)
        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Save | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.accepted.connect(self._save)
        buttons.rejected.connect(self.close)
        layout.addWidget(buttons)
        self._save_button = buttons.button(QDialogButtonBox.StandardButton.Save)
        self._read_only = False
        self._readonly_notice = QLabel('任务运行中：仅可查看，结束后可编辑。')
        self._readonly_notice.hide()
        layout.addWidget(self._readonly_notice)

    def set_read_only(self, value):
        self._read_only = bool(value)
        self._save_button.setEnabled(not value)
        self._readonly_notice.setVisible(value)
        self.room_cookie.setReadOnly(value)
        self.observer_proxy.setReadOnly(value)
        for control in (self.room_execution_mode, self.login_timeout, self.account_interval,
                self.observation_enabled, self.show_comments, self.observation_timeout,
                self.message_attempts, self.message_retry_delay, self.listener_timeout,
                self.listener_retries, self.listener_retry_delay, self.heartbeat_interval):
            control.setEnabled(not value)

    def _save(self):
        if self._read_only:
            return
        try:
            normalize_observer_proxy(self.observer_proxy.text())
        except ValueError as exc:
            self.proxy_error.setText(str(exc))
            self.observer_proxy.setFocus()
            return
        self.proxy_error.clear()
        self.saved.emit(DesktopSettings(
            room_execution_mode=self.room_execution_mode.currentData(),
            login_timeout_seconds=self.login_timeout.value(),
            account_interval_seconds=self.account_interval.value(),
            observation_enabled=self.observation_enabled.isChecked(),
            show_live_comments=self.show_comments.isChecked(),
            observation_timeout_seconds=self.observation_timeout.value(),
            message_attempts=self.message_attempts.value(),
            message_retry_delay_seconds=self.message_retry_delay.value(),
            listener_connect_timeout_seconds=self.listener_timeout.value(),
            listener_connect_retries=self.listener_retries.value(),
            listener_retry_delay_seconds=self.listener_retry_delay.value(),
            heartbeat_interval_seconds=self.heartbeat_interval.value(),
            room_id_cookie=self.room_cookie.text().strip(),
            observer_proxy=self.observer_proxy.text().strip(),
        ))
        self.close()
