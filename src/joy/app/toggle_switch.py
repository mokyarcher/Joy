"""简易滑块药丸开关（与副驾自动模式同款）。"""

from __future__ import annotations

from PySide6.QtCore import QSize, Qt
from PySide6.QtGui import QColor, QPainter
from PySide6.QtWidgets import QAbstractButton, QWidget


class ToggleSwitch(QAbstractButton):
    """开=微信绿滑轨，关=浅灰。"""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setCheckable(True)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setFixedSize(48, 28)
        self.toggled.connect(lambda _=False: self.update())

    def sizeHint(self) -> QSize:  # noqa: N802
        return QSize(48, 28)

    def paintEvent(self, event) -> None:  # noqa: N802
        del event
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        r = self.rect().adjusted(1, 1, -1, -1)
        on = self.isChecked()
        track = QColor("#07C160" if on else "#E5E5E5")
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(track)
        p.drawRoundedRect(r, 13, 13)
        kn = 22
        y = (self.height() - kn) // 2
        x = self.width() - kn - 4 if on else 4
        p.setBrush(QColor("#FFFFFF"))
        p.drawEllipse(x, y, kn, kn)
        p.end()
