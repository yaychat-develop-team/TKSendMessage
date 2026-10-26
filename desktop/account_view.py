"""Whitelist account summaries for the desktop, without retaining credentials."""

import hashlib
import re
from dataclasses import dataclass
from pathlib import Path

from PySide6.QtCore import QEvent, QPointF, Qt, Signal
from PySide6.QtGui import QColor, QPen, QIcon, QPainter, QPixmap
from PySide6.QtWidgets import (QAbstractItemView, QHeaderView, QStyledItemDelegate,
                               QTableWidget, QTableWidgetItem)
from tiktok_live.account_store import load_account_store
from .delivery_details import UNCERTAIN, reasons_for, format_room_details


@dataclass(frozen=True)
class AccountSummary:
    username: str
    short_id: str
    account_id: str
    status: str = '已保存'


def load_account_summaries(path: Path) -> list[AccountSummary]:
    summaries = []
    for account in load_account_store(path)['accounts']:
        raw_id = account['id']
        short_id = (raw_id[:12] if re.fullmatch(r'[a-f0-9]{64}', raw_id)
                    else hashlib.sha256(raw_id.encode('utf-8')).hexdigest()[:12])
        username = re.sub(r'[^A-Za-z0-9._-]', '',
                          str(account.get('username') or '').lstrip('@'))[:64]
        summaries.append(AccountSummary(username or '未获取用户名', short_id, raw_id))
    return summaries


class _SendCheckDelegate(QStyledItemDelegate):
    @staticmethod
    def _check_state(index) -> Qt.CheckState:
        value = index.data(Qt.ItemDataRole.CheckStateRole)
        try:
            return Qt.CheckState(value)
        except (TypeError, ValueError):
            return Qt.CheckState.Unchecked

    def paint(self, painter, option, index):
        painter.save()
        painter.fillRect(option.rect, option.palette.base())
        size = 18
        x = option.rect.center().x() - size // 2
        y = option.rect.center().y() - size // 2
        checked = self._check_state(index) == Qt.CheckState.Checked
        painter.setRenderHint(painter.RenderHint.Antialiasing)
        painter.setPen(QPen(QColor('#1c9f5e' if checked else '#9aa3af'), 1.5))
        painter.setBrush(QColor('#1c9f5e' if checked else '#ffffff'))
        painter.drawRoundedRect(x, y, size, size, 4, 4)
        if checked:
            painter.setPen(QPen(QColor('#ffffff'), 2.5, Qt.PenStyle.SolidLine,
                                Qt.PenCapStyle.RoundCap, Qt.PenJoinStyle.RoundJoin))
            painter.drawLine(QPointF(x + 4, y + 9), QPointF(x + 8, y + 13))
            painter.drawLine(QPointF(x + 8, y + 13), QPointF(x + 14, y + 5))
        painter.restore()

    def editorEvent(self, event, model, option, index):
        if not (index.flags() & Qt.ItemFlag.ItemIsEnabled
                and index.flags() & Qt.ItemFlag.ItemIsUserCheckable):
            return False
        if event.type() == QEvent.Type.MouseButtonPress:
            return True
        if event.type() == QEvent.Type.MouseButtonRelease:
            if (event.button() != Qt.MouseButton.LeftButton
                    or not option.rect.contains(event.position().toPoint())):
                return False
        elif event.type() == QEvent.Type.KeyPress:
            if event.key() not in (Qt.Key.Key_Space, Qt.Key.Key_Select):
                return False
        else:
            return False
        state = (Qt.CheckState.Unchecked if self._check_state(index)
                 == Qt.CheckState.Checked else Qt.CheckState.Checked)
        return model.setData(index, state, Qt.ItemDataRole.CheckStateRole)


class AccountTable(QTableWidget):
    delivery_details_requested = Signal(str)

    def __init__(self, parent=None):
        super().__init__(0, 5, parent)
        self.setObjectName('accountTable')
        self.setHorizontalHeaderLabels(['发送', '账号用户名', '账号 ID', '状态', '消息展示'])
        self.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.verticalHeader().hide()
        self.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Stretch)
        self.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeMode.Fixed)
        self.setColumnWidth(0, 64)
        self.setItemDelegateForColumn(0, _SendCheckDelegate(self))
        self.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOn)
        self.verticalScrollBar().setAccessibleName('上下查看已保存账号')
        self._account_ids = []
        self._delivery = {}
        self._selection_locked = False
        self.cellClicked.connect(self._open_delivery_details)
        self.cellActivated.connect(self._open_delivery_details)

    def _open_delivery_details(self, row, column):
        if column == 4 and 0 <= row < len(self._account_ids):
            self.delivery_details_requested.emit(self._account_ids[row])

    def delivery_details(self, key):
        state = self._delivery.get(key)
        return (state['tooltip'] if state else '本轮未测试此账号。') + '\n\n只有旁观监听收到消息才确认展示；未确认不等于不可见。'

    def set_accounts(self, summaries: list[AccountSummary]) -> None:
        checked = set(self.checked_account_ids())
        self._account_ids = [summary.account_id for summary in summaries]
        self.setRowCount(len(summaries))
        for row, summary in enumerate(summaries):
            check = QTableWidgetItem()
            check.setFlags(Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsUserCheckable)
            check.setCheckState(Qt.CheckState.Checked if summary.account_id in checked
                                else Qt.CheckState.Unchecked)
            self.setItem(row, 0, check)
            for column, value in enumerate((summary.username, summary.short_id, summary.status), 1):
                item = QTableWidgetItem(value)
                item.setFlags(item.flags() & ~Qt.ItemFlag.ItemIsEditable)
                self.setItem(row, column, item)
            self._render_delivery(row, summary.account_id)
        self.set_selection_locked(self._selection_locked)

    def set_selection_locked(self, locked):
        self._selection_locked = bool(locked)
        for row in range(self.rowCount()):
            item = self.item(row, 0)
            if item is None:
                continue
            flags = Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsSelectable
            if not locked:
                flags |= Qt.ItemFlag.ItemIsUserCheckable
            if item.flags() != flags:
                item.setFlags(flags)

    def begin_delivery(self, account_ids, total):
        self._delivery = {key: {'total': total, 'delivered': 0, 'pending': True,
                               'tooltip': '等待消息展示确认'} for key in account_ids}
        for row, key in enumerate(self._account_ids):
            self._render_delivery(row, key)

    def update_delivery(self, result, *, finished=False):
        for row, key in enumerate(self._account_ids):
            state = self._delivery.get(key)
            if state is None:
                continue
            short_id = (key[:12] if re.fullmatch(r'[a-f0-9]{12,64}', key)
                        else hashlib.sha256(key.encode('utf-8')).hexdigest()[:12])
            entries = [(room.get('target', ''), account)
                       for room in result.get('rooms', [])
                       for account in room.get('accounts', []) if account.get('id') == short_id]
            if entries:
                state['delivered'] = sum(a.get('delivered', 0) for _, a in entries)
                state['sent'] = sum(a.get('sent', 0) for _, a in entries)
                codes = {code for _, a in entries for code in reasons_for(a)}
                state['restriction'] = ('已封禁' if 'account_banned' in codes else
                                        '已禁言' if 'account_silenced' in codes else
                                        '房间禁评' if 'room_comment_restricted' in codes else
                                        '连续未确认停发' if 'consecutive_not_observed' in codes else None)
                state['pending'] = any(a.get('status') in ('queued', 'running') for _, a in entries)
                state['running'] = any(a.get('status') == 'running' for _, a in entries)
                state['cancelled'] = any(a.get('cancelled') or a.get('status') == 'cancelled' for _, a in entries)
                state['uncertain'] = any(a.get('acceptedNotObserved') or a.get('listenerUnavailable')
                    or set(reasons_for(a)) & UNCERTAIN for _, a in entries)
                state['hard_failure'] = all(
                    a.get('status') not in ('queued', 'running', 'cancelled')
                    and not (a.get('success', 0) or a.get('delivered', 0)
                             or a.get('acceptedNotObserved', 0) or a.get('listenerUnavailable', 0))
                    and not set(reasons_for(a)) & UNCERTAIN
                    and ((set(reasons_for(a)) - UNCERTAIN - {'run_cancelled'})
                    or a.get('expired', 0) or a.get('temporaryError', 0)
                    or a.get('failed', 0) > a.get('acceptedNotObserved', 0) + a.get('listenerUnavailable', 0)
                       and not set(reasons_for(a)) & UNCERTAIN)
                    for _, a in entries)
                details = []
                for room, account in entries:
                    details.append(format_room_details(room, account))
                state['tooltip'] = '\n\n'.join(details)
            if finished:
                state['pending'] = False
                state['running'] = False
                state['cancelled'] = state.get('cancelled', False) or result.get('cancelled', False)
                if not entries:
                    state['tooltip'] += '\n任务已结束，未取得展示确认。'
            self._render_delivery(row, key)

    def _render_delivery(self, row, key):
        state = self._delivery.get(key)
        text, color, tip = '未测试', '#667085', '本轮未发送'
        if state:
            delivered, total = state['delivered'], state['total']
            count = f'{delivered}/{total}'
            tip = state['tooltip'] + '\n绿色仅表示旁观监听已确认；未确认不代表消息一定不可见。'
            if state['pending']:
                phase = '正在运行' if state.get('running') else '等待执行'
                text = (f"{phase} · 已发 {state['sent']}/{total} · 展示 {count}" if state.get('sent')
                        else phase + ' · 观察中 ' + count)
                color = '#175cd3' if state.get('running') else '#667085'
            elif delivered >= total and total > 0:
                text, color = '已展示 ' + count, '#15803d'
            elif delivered > 0:
                text, color = '部分展示 ' + count, '#b54708'
                if state.get('restriction'):
                    text += ' · ' + state['restriction']
            elif state.get('hard_failure'):
                text, color = (state.get('restriction') or '发送失败') + ' · 展示 ' + count, '#b42318'
            elif state.get('uncertain'):
                text, color = (state.get('restriction') or '未确认展示') + ' · 展示 ' + count, '#b54708'
            elif state.get('cancelled'):
                text, color = '已停止 ' + count, '#667085'
            else:
                text, color = '未确认展示 ' + count, '#b54708'
        status = self.item(row, 3)
        if status is not None and state is not None:
            status.setText('正在运行' if state.get('running') else
                           '等待执行' if state.get('pending') else '已结束')
        elif status is not None:
            status.setText('已保存')
        item = QTableWidgetItem(text)
        # Keep native-resolution variants, including fractional Windows scales.
        # QIcon can choose again when the window moves to a different screen.
        icon = QIcon()
        for ratio in (1, 1.25, 1.5, 1.75, 2, 2.5, 3, 4):
            pixmap = QPixmap(round(16 * ratio), round(16 * ratio))
            pixmap.setDevicePixelRatio(ratio)
            pixmap.fill(Qt.GlobalColor.transparent)
            painter = QPainter(pixmap)
            painter.setRenderHint(QPainter.RenderHint.Antialiasing)
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(QColor(color))
            painter.drawEllipse(1, 1, 14, 14)
            painter.setPen(QPen(QColor('white'), 1.6, Qt.PenStyle.SolidLine,
                                Qt.PenCapStyle.RoundCap, Qt.PenJoinStyle.RoundJoin))
            if color == '#15803d':
                painter.drawLine(4, 8, 7, 11)
                painter.drawLine(7, 11, 12, 5)
            elif color == '#b42318':
                painter.drawLine(5, 5, 11, 11)
                painter.drawLine(5, 11, 11, 5)
            else:
                painter.drawLine(5, 8, 11, 8)
            painter.end()
            icon.addPixmap(pixmap)
        item.setIcon(icon)
        item.setFlags(Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsSelectable)
        item.setForeground(QColor(color))
        item.setToolTip(tip + '\n点击此列查看完整详情。')
        self.setItem(row, 4, item)

    def checked_account_ids(self) -> list[str]:
        return [account_id for row, account_id in enumerate(self._account_ids)
                if self.item(row, 0) is not None
                and self.item(row, 0).checkState() == Qt.CheckState.Checked]

    def set_all_checked(self, checked: bool) -> None:
        if self._selection_locked:
            return
        state = Qt.CheckState.Checked if checked else Qt.CheckState.Unchecked
        for row in range(self.rowCount()):
            item = self.item(row, 0)
            if item is not None:
                item.setCheckState(state)

    def selected_account_id(self) -> str | None:
        row = self.currentRow()
        if 0 <= row < len(self._account_ids):
            return self._account_ids[row]
        return None
