"""Positive Python integer input, without QSpinBox's signed 32-bit ceiling."""
from PySide6.QtCore import QSize, Signal
from PySide6.QtGui import QValidator
from PySide6.QtWidgets import QAbstractSpinBox


class PositiveCountBox(QAbstractSpinBox):
    valueChanged = Signal(object)

    def __init__(self, parent=None):
        super().__init__(parent)
        self._value = 1
        self._suffix = ''
        self.lineEdit().setText('1')
        self.editingFinished.connect(self._commit)

    def _parse(self, text):
        if self._suffix and text.endswith(self._suffix):
            text = text[:-len(self._suffix)]
        if not text or not text.isascii() or not text.isdecimal():
            raise ValueError('Positive integer required')
        value = int(text)
        if value < 1:
            raise ValueError('Positive integer required')
        return value

    def value(self):
        try:
            return self._parse(self.lineEdit().text())
        except ValueError:
            return self._value

    def setValue(self, value):
        value = max(1, int(value))
        changed = value != self._value
        self._value = value
        self.lineEdit().setText(str(value) + self._suffix)
        if changed:
            self.valueChanged.emit(value)

    def setSuffix(self, suffix):
        self._suffix = suffix
        self.setValue(self._value)

    def _commit(self):
        self.setValue(self.value())

    def validate(self, text, pos):
        raw = text[:-len(self._suffix)] if self._suffix and text.endswith(self._suffix) else text
        if not raw or raw == '0':
            return QValidator.State.Intermediate, text, pos
        try:
            self._parse(text)
        except ValueError:
            return QValidator.State.Invalid, text, pos
        return QValidator.State.Acceptable, text, pos

    def fixup(self, text):
        return str(self._value) + self._suffix

    def stepBy(self, steps):
        self.setValue(max(1, self.value() + steps))

    def stepEnabled(self):
        flags = QAbstractSpinBox.StepEnabledFlag.StepUpEnabled
        if self.value() > 1:
            flags |= QAbstractSpinBox.StepEnabledFlag.StepDownEnabled
        return flags

    def sizeHint(self):
        return QSize(120, super().sizeHint().height())
