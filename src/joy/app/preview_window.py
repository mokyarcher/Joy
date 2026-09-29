"""采集 + 识图 + 判断/候选 + 填入/发送（M4）。"""

from __future__ import annotations

import logging
import time
from pathlib import Path

import numpy as np
from PySide6.QtCore import (
    QEasingCurve,
    QEvent,
    QParallelAnimationGroup,
    QPoint,
    QPointF,
    QPropertyAnimation,
    QRect,
    QRectF,
    Qt,
    QThread,
    QTimer,
    Signal,
)
from PySide6.QtGui import (
    QAction,
    QColor,
    QCursor,
    QFont,
    QFontMetrics,
    QGuiApplication,
    QIcon,
    QImage,
    QKeySequence,
    QPainter,
    QPainterPath,
    QPen,
    QPixmap,
    QPolygonF,
    QTextCursor,
)
from PySide6.QtWidgets import (
    QDialog,
    QFrame,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMenu,
    QMessageBox,
    QPlainTextEdit,
    QProgressBar,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QTextEdit,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from joy.capture.windows import (
    WindowInfo,
    client_rect_screen,
    compute_dock_beside,
    find_wechat_windows,
    hwnd_dpi_scale,
    is_hwnd_foreground,
    is_minimized,
    pick_default_window,
    restore_window,
    window_screen_rect,
)
from joy.capture.worker import CaptureWorker
from joy.core.config import JoyConfig
from joy.core.engine import Engine
from joy.core.models import AnalyzeResult, Candidate
from joy.core.wechat_layout import (
    WeChatLayout,
    clear_cache as clear_layout_cache,
    compose_height_aligned,
    compose_height_px,
    make_fingerprint,
    COMPOSE_BOTTOM_GAP_PX,
)
from joy.fill import paste_and_send, paste_text
from joy.app.toggle_switch import ToggleSwitch
from joy.app.wechat_wake_button import DockCollapseButton, WeChatWakeButton
from joy.util.dpi_win import dock_coord_scale, resolve_dock_anchor
from joy.util.logging import redact

log = logging.getLogger("joy.preview")


class _WeChatScrollThumb(QWidget):
    """微信式灰胶囊滑块（叠在内容上，不占布局宽度）。"""

    def __init__(self, host: "WeChatHoverScrollArea") -> None:
        super().__init__(host)
        self._host = host
        self.setFixedWidth(host._THUMB_W)
        self.setCursor(Qt.CursorShape.ArrowCursor)
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, False)
        self._drag_y0 = 0
        self._drag_v0 = 0
        self._dragging = False
        self.hide()

    def paintEvent(self, event) -> None:  # noqa: N802
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(QColor("#C2C2C2"))
        # 上下半圆胶囊（圆角 = 宽/2）
        r = float(self.width()) / 2.0
        p.drawRoundedRect(self.rect(), r, r)
        p.end()

    def mousePressEvent(self, event) -> None:  # noqa: N802
        if event.button() == Qt.MouseButton.LeftButton:
            self._dragging = True
            self._drag_y0 = int(event.globalPosition().y())
            self._drag_v0 = self._host.verticalScrollBar().value()
            self._host._show_bar()
            event.accept()
            return
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event) -> None:  # noqa: N802
        if not self._dragging:
            super().mouseMoveEvent(event)
            return
        dy = int(event.globalPosition().y()) - self._drag_y0
        sb = self._host.verticalScrollBar()
        track = max(1, self._host._thumb_track_h() - self.height())
        delta = int(round(dy * sb.maximum() / track)) if track else 0
        sb.setValue(self._drag_v0 + delta)
        event.accept()

    def mouseReleaseEvent(self, event) -> None:  # noqa: N802
        if event.button() == Qt.MouseButton.LeftButton and self._dragging:
            self._dragging = False
            self._host._hide_timer.start()
            event.accept()
            return
        super().mouseReleaseEvent(event)


class WeChatHoverScrollArea(QScrollArea):
    """微信式滚动：系统条关闭；右侧叠加胶囊滑块，靠近右缘 / 滚轮时显现。"""

    # 微信滑块视觉尺寸（按屏幕倍率折半：原约 17×63 → 8×32）
    _EDGE_PX = 20
    _THUMB_W = 8
    _THUMB_H = 32
    _MARGIN = 6
    _RIGHT_GAP = 4  # 与右缘留缝，避免贴边

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("CandidateScroll")
        self.setMouseTracking(True)
        self.viewport().setMouseTracking(True)
        # 不占布局宽度，避免裁切「发送」钮
        self.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self._bar_visible = False
        self._hide_timer = QTimer(self)
        self._hide_timer.setSingleShot(True)
        self._hide_timer.setInterval(700)
        self._hide_timer.timeout.connect(self._try_hide_bar)
        self._thumb = _WeChatScrollThumb(self)
        sb = self.verticalScrollBar()
        sb.valueChanged.connect(self._on_scrolled)
        sb.rangeChanged.connect(lambda *_: self._relayout_thumb())
        self.viewport().installEventFilter(self)

    def _thumb_track_h(self) -> int:
        return max(1, self.height() - 2 * self._MARGIN)

    def _needs_scroll(self) -> bool:
        return self.verticalScrollBar().maximum() > 0

    def _relayout_thumb(self) -> None:
        sb = self.verticalScrollBar()
        if not self._needs_scroll():
            self._thumb.hide()
            return
        track = self._thumb_track_h()
        # 固定视觉高度（微信约 63px），禁止按比例拉成「上下贯穿」
        thumb_h = min(self._THUMB_H, track)
        y_range = max(0, track - thumb_h)
        y = self._MARGIN + (
            int(round(y_range * sb.value() / sb.maximum())) if sb.maximum() else 0
        )
        # 右缘留缝，不贴死
        x = max(0, self.width() - self._THUMB_W - self._RIGHT_GAP)
        self._thumb.setFixedWidth(self._THUMB_W)
        self._thumb.setGeometry(x, y, self._THUMB_W, thumb_h)
        if self._bar_visible:
            self._thumb.show()
            self._thumb.raise_()
        else:
            self._thumb.hide()

    def _show_bar(self, *, linger: bool = False) -> None:
        if not self._needs_scroll():
            self._thumb.hide()
            return
        self._hide_timer.stop()
        self._bar_visible = True
        self._relayout_thumb()
        self._thumb.show()
        self._thumb.raise_()
        if linger:
            self._hide_timer.start()

    def _try_hide_bar(self) -> None:
        if self._thumb._dragging:
            return
        if self._mouse_near_track():
            return
        self._bar_visible = False
        self._thumb.hide()

    def _mouse_near_track(self) -> bool:
        pos = self.mapFromGlobal(QCursor.pos())
        if not self.rect().contains(pos):
            return False
        return pos.x() >= self.width() - self._EDGE_PX

    def _on_scrolled(self, _value: int = 0) -> None:
        self._relayout_thumb()
        if self._needs_scroll():
            self._show_bar(linger=True)

    def eventFilter(self, obj, event):  # noqa: N802
        et = event.type()
        if et in (QEvent.Type.MouseMove, QEvent.Type.Enter, QEvent.Type.HoverMove):
            if self._mouse_near_track():
                self._show_bar()
            elif not self._thumb._dragging:
                self._hide_timer.start()
        elif et == QEvent.Type.Leave:
            if obj is self.viewport() and not self._thumb._dragging:
                if not self._mouse_near_track():
                    self._hide_timer.start()
        elif et == QEvent.Type.Resize and obj is self.viewport():
            self._relayout_thumb()
        return super().eventFilter(obj, event)

    def leaveEvent(self, event) -> None:  # noqa: N802
        if not self._thumb._dragging:
            self._hide_timer.start()
        super().leaveEvent(event)

    def enterEvent(self, event) -> None:  # noqa: N802
        if self._mouse_near_track():
            self._show_bar()
        super().enterEvent(event)

    def wheelEvent(self, event) -> None:  # noqa: N802
        super().wheelEvent(event)
        if self._needs_scroll():
            self._show_bar(linger=True)

    def resizeEvent(self, event) -> None:  # noqa: N802
        super().resizeEvent(event)
        self._relayout_thumb()


class _GreenCaret(QWidget):
    """视口上的微信绿闪烁光标（自绘，不依赖 stylesheet 背景）。"""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)
        self.setAttribute(Qt.WidgetAttribute.WA_NoSystemBackground, True)
        self.hide()

    def paintEvent(self, event) -> None:  # noqa: N802
        p = QPainter(self)
        # 与 ComposeSend「发送」同色
        p.fillRect(self.rect(), QColor("#00C375"))
        p.end()


class WeChatComposeEdit(QPlainTextEdit):
    """仿微信输入区：可编辑 + 绿色闪烁光标。"""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("ComposeDraft")
        self.setFrameShape(QFrame.Shape.NoFrame)
        self.setTabChangesFocus(True)
        self.setLineWrapMode(QPlainTextEdit.LineWrapMode.WidgetWidth)
        self.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.document().setDocumentMargin(0)
        self.setCursorWidth(0)  # 隐藏系统黑光标，只用绿色覆盖层
        self.setPlaceholderText("")
        self._caret = _GreenCaret(self.viewport())
        self._caret_on = True
        self._caret_timer = QTimer(self)
        self._caret_timer.setInterval(530)
        self._caret_timer.timeout.connect(self._toggle_caret)
        self.cursorPositionChanged.connect(self._sync_caret_pos)
        self.textChanged.connect(self._sync_caret_pos)
        self.selectionChanged.connect(self._sync_caret_pos)

    def _caret_width(self) -> int:
        # 目标约 2.5 物理像素；高 DPI 用 ceil，比「纯 2phys」略粗一丁点
        import math

        dpr = float(self.devicePixelRatioF() or 1.0)
        return max(1, int(math.ceil(2.4 / max(1.0, dpr) - 1e-9)))

    def _sync_caret_pos(self) -> None:
        if not self.hasFocus():
            self._caret.hide()
            return
        if self.textCursor().hasSelection():
            self._caret.hide()
            return
        cr = self.cursorRect()
        fm = self.fontMetrics()
        base_h = cr.height() if cr.height() >= 10 else fm.height()
        # 比字高略长，但比上一版少探出一点
        extra = 2
        h = max(15, base_h + extra)
        y = max(0, (cr.y() if cr.height() >= 10 else max(0, cr.y())) - extra // 2)
        x = max(0, cr.x())
        w = self._caret_width()
        self._caret.setGeometry(x, y, w, h)
        if self._caret_on:
            self._caret.show()
            self._caret.raise_()
            self._caret.update()
        else:
            self._caret.hide()

    def _toggle_caret(self) -> None:
        self._caret_on = not self._caret_on
        self._sync_caret_pos()

    def focusInEvent(self, event) -> None:  # noqa: N802
        super().focusInEvent(event)
        self._caret_on = True
        self._caret_timer.start()
        QTimer.singleShot(0, self._sync_caret_pos)

    def focusOutEvent(self, event) -> None:  # noqa: N802
        super().focusOutEvent(event)
        self._caret_timer.stop()
        self._caret_on = False
        self._caret.hide()

    def resizeEvent(self, event) -> None:  # noqa: N802
        super().resizeEvent(event)
        self._sync_caret_pos()

    def showEvent(self, event) -> None:  # noqa: N802
        super().showEvent(event)
        if self.hasFocus():
            self._sync_caret_pos()


def _numpy_to_qimage(rgb: np.ndarray) -> QImage:
    h, w, _ = rgb.shape
    bytes_per_line = 3 * w
    contiguous = np.ascontiguousarray(rgb)
    qimg = QImage(contiguous.data, w, h, bytes_per_line, QImage.Format.Format_RGB888)
    return qimg.copy()


def _paint_line_icon(
    p: QPainter,
    kind: str,
    *,
    cx: float,
    cy: float,
    color: QColor,
    paused: bool = False,
    mode_dock: bool = False,
) -> None:
    """细线图标绘制（菜单项 / ··· 共用）。描边略细，贴近微信线稿。"""
    pen = QPen(
        color,
        1.35,
        Qt.PenStyle.SolidLine,
        Qt.PenCapStyle.RoundCap,
        Qt.PenJoinStyle.RoundJoin,
    )
    p.setPen(pen)
    p.setBrush(Qt.BrushStyle.NoBrush)
    if kind == "refresh":
        r = 7.5
        rect = QRect(int(cx - r), int(cy - r), int(r * 2), int(r * 2))
        p.drawArc(rect, int(50 * 16), int(270 * 16))
        p.drawLine(int(cx + r - 0.5), int(cy - 3), int(cx + r + 3), int(cy - 7))
        p.drawLine(int(cx + r - 0.5), int(cy - 3), int(cx + r - 4), int(cy - 7))
    elif kind == "pause":
        if paused:
            p.setBrush(color)
            p.setPen(Qt.PenStyle.NoPen)
            poly = QPolygonF(
                [
                    QPointF(cx - 4, cy - 7),
                    QPointF(cx - 4, cy + 7),
                    QPointF(cx + 7, cy),
                ]
            )
            p.drawPolygon(poly)
        else:
            p.setBrush(color)
            p.setPen(Qt.PenStyle.NoPen)
            p.drawRoundedRect(int(cx - 5), int(cy - 6), 3, 12, 1, 1)
            p.drawRoundedRect(int(cx + 2), int(cy - 6), 3, 12, 1, 1)
    elif kind == "restore":
        p.drawRoundedRect(int(cx - 3), int(cy - 7), 11, 9, 1.5, 1.5)
        p.drawRoundedRect(int(cx - 8), int(cy - 3), 11, 9, 1.5, 1.5)
    elif kind == "home":
        # 小房子：屋顶 + 墙身 + 门
        p.setBrush(Qt.BrushStyle.NoBrush)
        p.setPen(pen)
        roof = QPolygonF(
            [
                QPointF(cx, cy - 7.2),
                QPointF(cx + 7.0, cy - 0.8),
                QPointF(cx - 7.0, cy - 0.8),
            ]
        )
        p.drawPolygon(roof)
        p.drawRoundedRect(QRectF(cx - 5.2, cy - 0.8, 10.4, 7.6), 1.0, 1.0)
        p.drawRoundedRect(QRectF(cx - 1.6, cy + 2.2, 3.2, 4.6), 0.6, 0.6)
    elif kind == "clear":
        # 扫把线稿：斜向空心；无顶饰、刷头完整无缺口
        p.save()
        p.translate(cx, cy)
        p.rotate(-40)
        p.setBrush(Qt.BrushStyle.NoBrush)
        p.setPen(pen)
        # 柄
        p.drawRoundedRect(QRectF(-1.65, -7.2, 3.3, 6.4), 1.5, 1.5)
        # 刷身
        p.drawRoundedRect(QRectF(-4.6, -1.5, 9.2, 3.6), 1.3, 1.3)
        # 刷毛：完整矩形外轮廓
        p.drawRoundedRect(QRectF(-5.2, 2.4, 10.4, 4.4), 1.0, 1.0)
        # 刷毛内部分隔线
        p.drawLine(QPointF(-2.6, 3.4), QPointF(-2.6, 5.8))
        p.drawLine(QPointF(0.0, 3.4), QPointF(0.0, 5.8))
        p.drawLine(QPointF(2.6, 3.4), QPointF(2.6, 5.8))
        p.restore()
    elif kind == "mode":
        if mode_dock:
            s, g = 5.5, 3.0
            p.drawLine(int(cx - s), int(cy - s + g), int(cx - s), int(cy - s))
            p.drawLine(int(cx - s), int(cy - s), int(cx - s + g), int(cy - s))
            p.drawLine(int(cx + s - g), int(cy - s), int(cx + s), int(cy - s))
            p.drawLine(int(cx + s), int(cy - s), int(cx + s), int(cy - s + g))
            p.drawLine(int(cx - s), int(cy + s - g), int(cx - s), int(cy + s))
            p.drawLine(int(cx - s), int(cy + s), int(cx - s + g), int(cy + s))
            p.drawLine(int(cx + s - g), int(cy + s), int(cx + s), int(cy + s))
            p.drawLine(int(cx + s), int(cy + s), int(cx + s), int(cy + s - g))
        else:
            p.drawRoundedRect(int(cx - 8), int(cy - 7), 16, 14, 2, 2)
            p.drawLine(int(cx + 2), int(cy - 5), int(cx + 2), int(cy + 5))
    elif kind == "more":
        # 微信式三点：略小，贴近标题栏观感
        p.setBrush(color)
        p.setPen(Qt.PenStyle.NoPen)
        for dx in (-6.2, 0, 6.2):
            p.drawEllipse(QPointF(cx + dx, cy), 1.35, 1.35)
    elif kind == "analyze":
        # 文档 + 放大镜（解析动作；与驾驶状态灯造型脱钩）
        p.setBrush(Qt.BrushStyle.NoBrush)
        p.setPen(pen)
        # 纸张（右上折角）
        sheet = QPainterPath()
        sheet.moveTo(cx - 6.4, cy - 6.2)
        sheet.lineTo(cx + 1.2, cy - 6.2)
        sheet.lineTo(cx + 3.8, cy - 3.6)
        sheet.lineTo(cx + 3.8, cy + 5.8)
        sheet.lineTo(cx - 6.4, cy + 5.8)
        sheet.closeSubpath()
        p.drawPath(sheet)
        # 折角线
        p.drawLine(QPointF(cx + 1.2, cy - 6.2), QPointF(cx + 1.2, cy - 3.6))
        p.drawLine(QPointF(cx + 1.2, cy - 3.6), QPointF(cx + 3.8, cy - 3.6))
        # 文内两行 + 小数据块
        p.drawLine(QPointF(cx - 4.6, cy - 3.4), QPointF(cx - 0.2, cy - 3.4))
        p.drawLine(QPointF(cx - 4.6, cy - 1.4), QPointF(cx + 0.6, cy - 1.4))
        p.setBrush(color)
        p.setPen(Qt.PenStyle.NoPen)
        p.drawRoundedRect(QRectF(cx - 4.6, cy + 0.6, 2.4, 1.8), 0.4, 0.4)
        p.drawRoundedRect(QRectF(cx - 4.6, cy + 3.0, 2.4, 1.8), 0.4, 0.4)
        p.setPen(pen)
        p.setBrush(Qt.BrushStyle.NoBrush)
        p.drawLine(QPointF(cx - 1.6, cy + 1.5), QPointF(cx + 1.6, cy + 1.5))
        p.drawLine(QPointF(cx - 1.6, cy + 3.9), QPointF(cx + 1.6, cy + 3.9))
        # 放大镜压住文档：镜内盖住表格线，再画镜框
        lens = QPointF(cx + 3.6, cy + 3.2)
        lens_r = 3.6
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(QColor("#FFFFFF"))
        p.drawEllipse(lens, lens_r + 0.2, lens_r + 0.2)
        p.setBrush(Qt.BrushStyle.NoBrush)
        p.setPen(pen)
        p.drawEllipse(lens, lens_r, lens_r)
        p.setPen(
            QPen(
                color,
                1.7,
                Qt.PenStyle.SolidLine,
                Qt.PenCapStyle.RoundCap,
                Qt.PenJoinStyle.RoundJoin,
            )
        )
        p.drawLine(
            QPointF(cx + 6.0, cy + 5.6),
            QPointF(cx + 7.8, cy + 7.4),
        )
        p.setPen(pen)
    elif kind == "auto":
        # 自动驾驶：双弧循环
        r = 6.5
        rect = QRectF(cx - r, cy - r, r * 2.0, r * 2.0)
        p.drawArc(rect, int(40 * 16), int(200 * 16))
        p.drawArc(rect, int(220 * 16), int(200 * 16))
        p.setBrush(color)
        p.setPen(Qt.PenStyle.NoPen)
        tip = 1.6
        p.drawPolygon(
            QPolygonF(
                [
                    QPointF(cx + r - 0.2, cy - 3.2),
                    QPointF(cx + r + tip, cy - 6.4),
                    QPointF(cx + r - tip - 0.4, cy - 6.4),
                ]
            )
        )
        p.drawPolygon(
            QPolygonF(
                [
                    QPointF(cx - r + 0.2, cy + 3.2),
                    QPointF(cx - r - tip, cy + 6.4),
                    QPointF(cx - r + tip + 0.4, cy + 6.4),
                ]
            )
        )
    elif kind == "smart":
        # 智能驾驶：小星芒
        tip = 7.0
        p.setBrush(color)
        p.setPen(Qt.PenStyle.NoPen)
        p.drawPolygon(
            QPolygonF(
                [
                    QPointF(cx, cy - tip),
                    QPointF(cx + 1.8, cy - 1.2),
                    QPointF(cx, cy + 1.2),
                    QPointF(cx - 1.8, cy - 1.2),
                ]
            )
        )
        p.drawPolygon(
            QPolygonF(
                [
                    QPointF(cx - tip, cy),
                    QPointF(cx - 1.2, cy - 1.8),
                    QPointF(cx + 1.2, cy),
                    QPointF(cx - 1.2, cy + 1.8),
                ]
            )
        )
        p.drawPolygon(
            QPolygonF(
                [
                    QPointF(cx + tip, cy),
                    QPointF(cx + 1.2, cy - 1.8),
                    QPointF(cx - 1.2, cy),
                    QPointF(cx + 1.2, cy + 1.8),
                ]
            )
        )
        p.drawPolygon(
            QPolygonF(
                [
                    QPointF(cx, cy + tip),
                    QPointF(cx + 1.8, cy + 1.2),
                    QPointF(cx, cy - 1.2),
                    QPointF(cx - 1.8, cy + 1.2),
                ]
            )
        )
    elif kind == "estop":
        # 紧急制动：电源符（线稿）
        ring_r = 6.0
        ring = QRectF(cx - ring_r, cy - ring_r, ring_r * 2.0, ring_r * 2.0)
        p.drawArc(ring, int(140 * 16), int(260 * 16))
        p.drawLine(QPointF(cx, cy - ring_r - 1.0), QPointF(cx, cy - 0.2))
    elif kind == "detail":
        # 识别详情：线稿气泡
        bubble = QRectF(cx - 6.5, cy - 5.0, 13.0, 9.0)
        p.drawRoundedRect(bubble, 2.8, 2.8)
        p.drawLine(QPointF(cx - 4.2, cy + 4.0), QPointF(cx - 5.4, cy + 6.4))
        p.drawLine(QPointF(cx - 5.4, cy + 6.4), QPointF(cx - 1.8, cy + 4.0))
        p.drawLine(QPointF(cx - 3.0, cy - 1.8), QPointF(cx + 3.2, cy - 1.8))
        p.drawLine(QPointF(cx - 3.0, cy + 0.6), QPointF(cx + 1.2, cy + 0.6))
    elif kind == "preview":
        # 截图预览：小显示器
        p.drawRoundedRect(int(cx - 7), int(cy - 6), 14, 10, 1.5, 1.5)
        p.drawLine(int(cx - 3), int(cy + 5), int(cx + 3), int(cy + 5))
        p.drawLine(int(cx), int(cy + 5), int(cx), int(cy + 7))
    elif kind == "context":
        # 上下文：叠层卡片
        p.drawRoundedRect(int(cx - 6), int(cy - 3), 12, 9, 1.5, 1.5)
        p.drawRoundedRect(int(cx - 8), int(cy - 6), 12, 9, 1.5, 1.5)


def _line_icon(
    kind: str,
    *,
    size: int = 20,
    color: str = "#4B4B4B",
    paused: bool = False,
    mode_dock: bool = False,
) -> QIcon:
    pix = QPixmap(size, size)
    pix.fill(Qt.GlobalColor.transparent)
    p = QPainter(pix)
    p.setRenderHint(QPainter.RenderHint.Antialiasing)
    _paint_line_icon(
        p,
        kind,
        cx=size / 2.0,
        cy=size / 2.0,
        color=QColor(color),
        paused=paused,
        mode_dock=mode_dock,
    )
    p.end()
    return QIcon(pix)


class MoreDotsButton(QToolButton):
    """微信式右上角 ··· 菜单钮：点一下开，再点一下关。"""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("MoreDotsBtn")
        self.setToolTip("更多")
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setAutoRaise(True)
        self.setFixedSize(28, 28)
        self.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        # 不用 InstantPopup：自行右对齐弹出，并做开关切换
        self.setPopupMode(QToolButton.ToolButtonPopupMode.DelayedPopup)
        self._suppress_click = False
        self._hide_hooked = False
        self.clicked.connect(self._toggle_menu)

    def setMenu(self, menu: QMenu | None) -> None:  # noqa: N802
        old = self.menu()
        if old is not None and self._hide_hooked:
            try:
                old.aboutToHide.disconnect(self._on_menu_about_to_hide)
            except (TypeError, RuntimeError):
                pass
            self._hide_hooked = False
        super().setMenu(menu)
        if menu is not None:
            menu.aboutToHide.connect(self._on_menu_about_to_hide)
            self._hide_hooked = True

    def _on_menu_about_to_hide(self) -> None:
        # 点在按钮上关菜单时，随后还会触发 clicked → 会立刻再打开；吞掉这一次
        if self.rect().contains(self.mapFromGlobal(QCursor.pos())):
            self._suppress_click = True

    def _toggle_menu(self) -> None:
        if self._suppress_click:
            self._suppress_click = False
            return
        menu = self.menu()
        if menu is None:
            return
        if menu.isVisible():
            menu.hide()
            return
        menu.adjustSize()
        pos = self.mapToGlobal(
            QPoint(self.width() - menu.sizeHint().width(), self.height() + 2)
        )
        menu.popup(pos)

    def enterEvent(self, event) -> None:  # noqa: N802
        super().enterEvent(event)
        self.update()

    def leaveEvent(self, event) -> None:  # noqa: N802
        super().leaveEvent(event)
        self.update()

    def paintEvent(self, event) -> None:  # noqa: N802
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        if self.isDown() or self.underMouse():
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(QColor("#F5F5F5"))
            p.drawRoundedRect(self.rect().adjusted(2, 2, -2, -2), 6, 6)
        color = QColor("#07C160" if self.underMouse() or self.isDown() else "#4B4B4B")
        _paint_line_icon(
            p, "more", cx=self.width() / 2.0, cy=self.height() / 2.0, color=color
        )
        p.end()


class AnalyzeWorker(QThread):
    finished_ok = Signal(object)
    finished_err = Signal(str)
    progress = Signal(str, int)

    def __init__(
        self,
        cfg: JoyConfig,
        window: WindowInfo,
        parent=None,
        *,
        auto: bool = False,
        title_hint: str = "",
    ) -> None:
        super().__init__(parent)
        self._cfg = cfg
        self._window = window
        self._auto = auto
        self._title_hint = (title_hint or "").strip()
        self._cancelled = False

    def cancel(self) -> None:
        self._cancelled = True
        self.requestInterruption()

    def run(self) -> None:
        try:
            if self._cancelled or self.isInterruptionRequested():
                return
            engine = Engine(self._cfg)

            def _on_progress(msg: str, pct: int) -> None:
                if self._cancelled or self.isInterruptionRequested():
                    return
                self.progress.emit(msg, pct)

            result = engine.analyze_once(
                self._window,
                on_progress=_on_progress,
                auto=self._auto,
                title_hint=self._title_hint,
            )
            if self._cancelled or self.isInterruptionRequested():
                return
            self.progress.emit("完成", 100)
            self.finished_ok.emit(result)
        except Exception as exc:
            if self._cancelled or self.isInterruptionRequested():
                return
            self.finished_err.emit(redact(str(exc)))


class HistoryPullWorker(QThread):
    """后台：模拟微信多选复制，再写入联系人记忆。"""

    finished_ok = Signal(int, int, str)  # parsed, added, title
    finished_err = Signal(str)
    progress = Signal(str)

    def __init__(
        self,
        hwnd: int,
        title: str,
        cfg: JoyConfig,
        *,
        me_names: str = "",
        compose_top: float = 0.78,
        contact_left: float = 0.30,
        scroll_steps: int = 24,
        keep_max: int = 400,
        parent=None,
    ) -> None:
        super().__init__(parent)
        self._hwnd = int(hwnd)
        self._title = (title or "").strip()
        self._cfg = cfg
        self._me_names = (me_names or "").strip()
        self._compose_top = compose_top
        self._contact_left = contact_left
        self._scroll_steps = scroll_steps
        self._keep_max = keep_max

    def run(self) -> None:
        try:
            from joy.core.contact_memory import import_transcript
            from joy.fill.history_pull import pull_chat_via_multiselect

            self.progress.emit("OCR/视觉定位：多选 → 选择到这里 → 复制…")
            pull = pull_chat_via_multiselect(
                self._hwnd,
                cfg=self._cfg,
                compose_top_ratio=self._compose_top,
                contact_left_ratio=self._contact_left,
                scroll_steps=self._scroll_steps,
                me_names=self._me_names or None,
                on_progress=lambda s: self.progress.emit(s),
            )
            if not pull.ok:
                self.finished_err.emit(pull.detail or "拉取失败")
                return
            self.progress.emit("正在写入记忆…")
            parsed, added = import_transcript(
                self._title,
                pull.text,
                me_names=self._me_names or None,
                keep_max=self._keep_max,
            )
            self.finished_ok.emit(parsed, added, self._title)
        except Exception as exc:
            self.finished_err.emit(redact(str(exc)))


class LayoutProbeWorker(QThread):
    """截取微信整窗客户区并调用视觉模型解析布局。"""

    finished_ok = Signal(object)  # WeChatLayout
    finished_err = Signal(str)

    def __init__(
        self,
        cfg: JoyConfig,
        window: WindowInfo,
        *,
        client_w: int,
        client_h: int,
        dpr: float,
        parent=None,
    ) -> None:
        super().__init__(parent)
        self._cfg = cfg
        self._window = window
        self._client_w = client_w
        self._client_h = client_h
        self._dpr = dpr

    def run(self) -> None:
        try:
            from joy.capture.grabber import FrameGrabber
            from joy.providers.layout_probe import probe_wechat_layout

            grabber = FrameGrabber(self._cfg.capture)
            attempt = grabber.grab_with_reason(
                self._window,
                want_jpeg=True,
                force=True,
                auto_restore=True,
                crop=False,
                resize=False,  # 布局用原分辨率，避免缩放比例和客户区对不齐
            )
            if attempt.frame is None or not attempt.frame.jpeg:
                raise ValueError(attempt.reason or "布局截屏失败")
            layout = probe_wechat_layout(
                self._cfg,
                attempt.frame.jpeg,
                client_w=self._client_w,
                client_h=self._client_h,
                dpr=self._dpr,
                save=True,
                rgb=attempt.frame.rgb,
                prefer_pixel=True,
            )
            self.finished_ok.emit(layout)
        except Exception as exc:
            self.finished_err.emit(redact(str(exc)))


class ContactIdentifyWorker(QThread):
    """轻量识会话名（只裁顶条，不跑完整分析）。"""

    finished_ok = Signal(str, str)  # title, header_fp
    finished_err = Signal(str)

    def __init__(
        self,
        cfg: JoyConfig,
        window: WindowInfo,
        parent=None,
        *,
        left_ratio: float | None = None,
    ) -> None:
        super().__init__(parent)
        self._cfg = cfg
        self._window = window
        self._left_ratio = left_ratio

    def run(self) -> None:
        try:
            from joy.capture.frame_ops import (
                crop_chat_header_jpeg,
                header_fingerprint,
            )
            from joy.capture.grabber import FrameGrabber
            from joy.providers.vision import identify_chat_contact

            grabber = FrameGrabber(self._cfg.capture)
            # resize=False：避免英文首字母被缩放糊成半截
            attempt = grabber.grab_with_reason(
                self._window,
                want_jpeg=False,
                force=True,
                auto_restore=True,
                crop=False,
                resize=False,
            )
            if attempt.frame is None or attempt.frame.rgb is None:
                raise ValueError(attempt.reason or "识名截屏失败")
            fp = header_fingerprint(attempt.frame.rgb)
            left = self._left_ratio
            if left is None:
                left = float(getattr(self._cfg.capture.crop, "left", 0.28) or 0.28)
            # 分割线略往左，给标题首字母留边；轻度放大便于拉丁字母
            jpeg = crop_chat_header_jpeg(
                attempt.frame.rgb,
                quality=82,
                left_ratio=max(0.22, min(0.40, float(left) - 0.02)),
                pad_left_px=16,
                right_ratio=0.88,
                scale=1.5,
            )
            if not jpeg:
                raise ValueError("会话顶条为空")
            title = identify_chat_contact(self._cfg, jpeg, fallback_full=False)
            self.finished_ok.emit(title, fp or "")
        except Exception as exc:
            self.finished_err.emit(redact(str(exc)))


class HeaderWatchWorker(QThread):
    """只算顶栏指纹，本地很快；变了再触发识名。"""

    finished_ok = Signal(str)  # header_fp
    finished_err = Signal(str)

    def __init__(self, cfg: JoyConfig, window: WindowInfo, parent=None) -> None:
        super().__init__(parent)
        self._cfg = cfg
        self._window = window

    def run(self) -> None:
        try:
            from joy.capture.frame_ops import header_fingerprint
            from joy.capture.grabber import FrameGrabber

            grabber = FrameGrabber(self._cfg.capture)
            attempt = grabber.grab_with_reason(
                self._window,
                want_jpeg=False,
                force=True,
                auto_restore=False,
                crop=False,
                resize=True,
            )
            if attempt.frame is None or attempt.frame.rgb is None:
                raise ValueError(attempt.reason or "顶栏截屏失败")
            fp = header_fingerprint(attempt.frame.rgb)
            self.finished_ok.emit(fp or "")
        except Exception as exc:
            self.finished_err.emit(redact(str(exc)))


_PROGRESS_HINTS = (
    "正在连接模型…",
    "画面已送出，等待识图…",
    "气泡文字解析中…",
    "结合关系与风格判断中…",
    "候选回复生成中…",
    "马上就好…",
)


class CenterToast(QLabel):
    """正中央浅灰提示框（驾驶模式开关等）。"""

    def __init__(self, host: QWidget) -> None:
        super().__init__(host)
        self._host = host
        self.setObjectName("CenterToast")
        self.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.setWordWrap(True)
        self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)
        self.setStyleSheet(
            """
            QLabel#CenterToast {
                background: #E8E8E8;
                color: #333333;
                border: 1px solid #D0D0D0;
                border-radius: 8px;
                padding: 12px 18px;
                font-size: 14px;
                font-family: "Microsoft YaHei UI","微软雅黑","Segoe UI";
            }
            """
        )
        self._hide_timer = QTimer(self)
        self._hide_timer.setSingleShot(True)
        self._hide_timer.timeout.connect(self.hide)
        self.hide()

    def show_toast(self, text: str, *, ms: int = 1800) -> None:
        text = (text or "").strip()
        if not text:
            self.hide()
            return
        self.setText(text)
        self.adjustSize()
        # 限制最大宽度，避免侧窗过窄时撑破
        max_w = max(120, min(260, int(self._host.width() * 0.78)))
        self.setFixedWidth(min(max_w, max(self.sizeHint().width() + 8, 140)))
        self.adjustSize()
        self._reposition()
        self.show()
        self.raise_()
        self._hide_timer.start(max(600, int(ms)))

    def _reposition(self) -> None:
        if self._host is None:
            return
        x = (self._host.width() - self.width()) // 2
        y = (self._host.height() - self.height()) // 2
        self.move(max(4, x), max(4, y))


class DriveModeBadge(QWidget):
    """发送钮旁：当前驾驶模式徽标（手绘精致位图）；点击循环切换。"""

    MODE_MANUAL = "manual"
    MODE_AUTO = "auto"
    MODE_SMART = "smart"
    clicked = Signal()

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("DriveModeBadge")
        self.setFixedSize(28, 28)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self._mode = self.MODE_MANUAL
        self._pressed = False
        self._sync_tip()

    def mode(self) -> str:
        return self._mode

    def set_mode(self, mode: str) -> None:
        m = mode if mode in (self.MODE_MANUAL, self.MODE_AUTO, self.MODE_SMART) else self.MODE_MANUAL
        if m == self._mode:
            self._sync_tip()
            self.update()
            return
        self._mode = m
        self._sync_tip()
        self.update()

    def _sync_tip(self) -> None:
        tips = {
            self.MODE_MANUAL: "点击切换 · 当前手动",
            self.MODE_AUTO: "点击切换 · 当前自动驾驶",
            self.MODE_SMART: "点击切换 · 当前智能驾驶",
        }
        self.setToolTip(tips.get(self._mode, "点击切换驾驶模式"))

    def mousePressEvent(self, event) -> None:  # noqa: N802
        if event.button() == Qt.MouseButton.LeftButton:
            self._pressed = True
            self.update()
            event.accept()
            return
        super().mousePressEvent(event)

    def mouseReleaseEvent(self, event) -> None:  # noqa: N802
        was = self._pressed
        self._pressed = False
        self.update()
        if (
            was
            and event.button() == Qt.MouseButton.LeftButton
            and self.rect().contains(event.position().toPoint())
        ):
            self.clicked.emit()
            event.accept()
            return
        super().mouseReleaseEvent(event)

    def leaveEvent(self, event) -> None:  # noqa: N802
        if self._pressed:
            self._pressed = False
        self.update()
        super().leaveEvent(event)

    def enterEvent(self, event) -> None:  # noqa: N802
        self.update()
        super().enterEvent(event)

    def paintEvent(self, event) -> None:  # noqa: N802
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        # 默认透明；悬停浅灰；按下更深（对齐微信工具栏）
        if self._pressed:
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(QColor("#E6E6E6"))
            p.drawRoundedRect(self.rect().adjusted(1, 1, -1, -1), 7, 7)
        elif self.underMouse():
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(QColor("#F2F2F2"))
            p.drawRoundedRect(self.rect().adjusted(1, 1, -1, -1), 7, 7)

        cx, cy = self.width() / 2.0, self.height() / 2.0
        # 手动橙 / 自动绿 / 智驾青，便于和旁侧灰图标区分
        manual_orange = QColor("#F59E0B")
        green = QColor("#00C375")
        smart_cyan = QColor(0, 180, 205)
        if self._mode == self.MODE_MANUAL:
            self._paint_manual(p, cx, cy, manual_orange)
        elif self._mode == self.MODE_AUTO:
            self._paint_auto(p, cx, cy, green)
        else:
            self._paint_smart(p, cx, cy, smart_cyan)
        p.end()

    @staticmethod
    def _paint_manual(p: QPainter, cx: float, cy: float, color: QColor) -> None:
        # 外缘对齐紧急制动实心圆（r=9）：描边圆环用 8，视觉外径≈9
        pen = QPen(
            color,
            1.4,
            Qt.PenStyle.SolidLine,
            Qt.PenCapStyle.RoundCap,
            Qt.PenJoinStyle.RoundJoin,
        )
        ring_r = 8.0
        p.setPen(pen)
        p.setBrush(Qt.BrushStyle.NoBrush)
        p.drawEllipse(QPointF(cx, cy), ring_r, ring_r)
        p.setBrush(color)
        p.setPen(Qt.PenStyle.NoPen)
        p.drawEllipse(QPointF(cx, cy), 2.2, 2.2)
        # 底部短弧（不超出外圆视觉边界）
        p.setPen(pen)
        p.setBrush(Qt.BrushStyle.NoBrush)
        arc_w, arc_h = 11.5, 8.5
        arc_rect = QRectF(cx - arc_w / 2.0, cy + 2.0, arc_w, arc_h)
        p.drawArc(arc_rect, int(30 * 16), int(120 * 16))

    @staticmethod
    def _paint_auto(p: QPainter, cx: float, cy: float, color: QColor) -> None:
        # 自动驾驶：双弧循环；箭头收在紧急制动外圆内，避免比紧急制动显大
        pen = QPen(
            color,
            1.45,
            Qt.PenStyle.SolidLine,
            Qt.PenCapStyle.RoundCap,
            Qt.PenJoinStyle.RoundJoin,
        )
        p.setPen(pen)
        p.setBrush(Qt.BrushStyle.NoBrush)
        r = 7.0
        rect = QRectF(cx - r, cy - r, r * 2.0, r * 2.0)
        p.drawArc(rect, int(40 * 16), int(200 * 16))
        p.drawArc(rect, int(220 * 16), int(200 * 16))
        # 箭头尖：略探出弧线，整体外缘仍 ≤9
        p.setBrush(color)
        p.setPen(Qt.PenStyle.NoPen)
        tip = 1.8
        p.drawPolygon(
            QPolygonF(
                [
                    QPointF(cx + r - 0.3, cy - 3.8),
                    QPointF(cx + r + tip, cy - 7.2),
                    QPointF(cx + r - tip - 0.6, cy - 7.2),
                ]
            )
        )
        p.drawPolygon(
            QPolygonF(
                [
                    QPointF(cx - r + 0.3, cy + 3.8),
                    QPointF(cx - r - tip, cy + 7.2),
                    QPointF(cx - r + tip + 0.6, cy + 7.2),
                ]
            )
        )

    @staticmethod
    def _paint_smart(p: QPainter, cx: float, cy: float, color: QColor) -> None:
        # 智能驾驶：六芒闪光（尖端对齐紧急制动外圆）
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(color)
        tip = 8.0
        # 主菱形竖
        p.drawPolygon(
            QPolygonF(
                [
                    QPointF(cx, cy - tip),
                    QPointF(cx + 2.2, cy - 1.5),
                    QPointF(cx, cy + 1.5),
                    QPointF(cx - 2.2, cy - 1.5),
                ]
            )
        )
        # 主菱形横
        p.drawPolygon(
            QPolygonF(
                [
                    QPointF(cx - tip, cy),
                    QPointF(cx - 1.5, cy - 2.2),
                    QPointF(cx + 1.5, cy),
                    QPointF(cx - 1.5, cy + 2.2),
                ]
            )
        )
        p.drawPolygon(
            QPolygonF(
                [
                    QPointF(cx + tip, cy),
                    QPointF(cx + 1.5, cy - 2.2),
                    QPointF(cx - 1.5, cy),
                    QPointF(cx + 1.5, cy + 2.2),
                ]
            )
        )
        p.drawPolygon(
            QPolygonF(
                [
                    QPointF(cx, cy + tip),
                    QPointF(cx + 2.2, cy + 1.5),
                    QPointF(cx, cy - 1.5),
                    QPointF(cx - 2.2, cy + 1.5),
                ]
            )
        )
        # 中心亮点
        p.setBrush(QColor("#FFFFFF"))
        p.drawEllipse(QPointF(cx, cy), 1.6, 1.6)


class DetailBadge(QWidget):
    """发送钮旁：查看最近一次识别详情（线稿小气泡，对齐模式灯风格）。"""

    clicked = Signal()

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("DetailBadge")
        self.setFixedSize(28, 28)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self._active = False
        self._pressed = False
        self._sync_tip()

    def set_active(self, on: bool) -> None:
        on = bool(on)
        if on == self._active:
            return
        self._active = on
        self._sync_tip()
        self.update()

    def _sync_tip(self) -> None:
        if self._active:
            self.setToolTip("收起对话详情")
        else:
            self.setToolTip("查看解析后的对话详情")

    def mousePressEvent(self, event) -> None:  # noqa: N802
        if event.button() == Qt.MouseButton.LeftButton:
            self._pressed = True
            self.update()
            event.accept()
            return
        super().mousePressEvent(event)

    def mouseReleaseEvent(self, event) -> None:  # noqa: N802
        was = self._pressed
        self._pressed = False
        self.update()
        if (
            was
            and event.button() == Qt.MouseButton.LeftButton
            and self.rect().contains(event.position().toPoint())
        ):
            self.clicked.emit()
            event.accept()
            return
        super().mouseReleaseEvent(event)

    def leaveEvent(self, event) -> None:  # noqa: N802
        if self._pressed:
            self._pressed = False
        self.update()
        super().leaveEvent(event)

    def enterEvent(self, event) -> None:  # noqa: N802
        self.update()
        super().enterEvent(event)

    def paintEvent(self, event) -> None:  # noqa: N802
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        if self._active:
            ink = QColor("#00C375")
            # 展开中：浅绿选中底；按下更深
            plate = QColor("#E4F1EA") if self._pressed else QColor("#EEF7F2")
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(plate)
            p.drawRoundedRect(self.rect().adjusted(1, 1, -1, -1), 7, 7)
        else:
            ink = QColor(75, 75, 75)
            # 默认透明；悬停浅灰；按下更深
            if self._pressed:
                p.setPen(Qt.PenStyle.NoPen)
                p.setBrush(QColor("#E6E6E6"))
                p.drawRoundedRect(self.rect().adjusted(1, 1, -1, -1), 7, 7)
            elif self.underMouse():
                p.setPen(Qt.PenStyle.NoPen)
                p.setBrush(QColor("#F2F2F2"))
                p.drawRoundedRect(self.rect().adjusted(1, 1, -1, -1), 7, 7)

        # 线稿气泡：略放大，与旁侧驾驶灯视觉对齐
        cx = self.width() / 2.0
        cy = self.height() / 2.0 - 0.2
        pen = QPen(
            ink,
            1.4,
            Qt.PenStyle.SolidLine,
            Qt.PenCapStyle.RoundCap,
            Qt.PenJoinStyle.RoundJoin,
        )
        p.setPen(pen)
        p.setBrush(Qt.BrushStyle.NoBrush)
        bubble = QRectF(cx - 8.2, cy - 6.2, 16.4, 11.0)
        p.drawRoundedRect(bubble, 3.3, 3.3)
        # 左下尾：两笔描边小三角
        p.drawLine(QPointF(cx - 5.4, cy + 4.8), QPointF(cx - 6.8, cy + 7.4))
        p.drawLine(QPointF(cx - 6.8, cy + 7.4), QPointF(cx - 2.5, cy + 4.8))
        # 气泡内两道短线
        p.drawLine(QPointF(cx - 4.0, cy - 2.2), QPointF(cx + 4.2, cy - 2.2))
        p.drawLine(QPointF(cx - 4.0, cy + 0.8), QPointF(cx + 1.8, cy + 0.8))
        p.end()


class WeChatDetailTextEdit(QTextEdit):
    """识别详情：系统滚动条关掉，右侧叠微信式灰胶囊滑块（与候选区同款）。"""

    _EDGE_PX = WeChatHoverScrollArea._EDGE_PX
    _THUMB_W = WeChatHoverScrollArea._THUMB_W
    _THUMB_H = WeChatHoverScrollArea._THUMB_H
    _MARGIN = WeChatHoverScrollArea._MARGIN
    _RIGHT_GAP = WeChatHoverScrollArea._RIGHT_GAP

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("DetailResult")
        self.setMouseTracking(True)
        self.viewport().setMouseTracking(True)
        self.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self._bar_visible = False
        self._hide_timer = QTimer(self)
        self._hide_timer.setSingleShot(True)
        self._hide_timer.setInterval(700)
        self._hide_timer.timeout.connect(self._try_hide_bar)
        self._thumb = _WeChatScrollThumb(self)
        sb = self.verticalScrollBar()
        sb.valueChanged.connect(self._on_scrolled)
        sb.rangeChanged.connect(lambda *_: self._relayout_thumb())
        self.viewport().installEventFilter(self)

    def _thumb_track_h(self) -> int:
        return max(1, self.height() - 2 * self._MARGIN)

    def _needs_scroll(self) -> bool:
        return self.verticalScrollBar().maximum() > 0

    def _relayout_thumb(self) -> None:
        sb = self.verticalScrollBar()
        if not self._needs_scroll():
            self._thumb.hide()
            return
        track = self._thumb_track_h()
        thumb_h = min(self._THUMB_H, track)
        y_range = max(0, track - thumb_h)
        y = self._MARGIN + (
            int(round(y_range * sb.value() / sb.maximum())) if sb.maximum() else 0
        )
        x = max(0, self.width() - self._THUMB_W - self._RIGHT_GAP)
        self._thumb.setFixedWidth(self._THUMB_W)
        self._thumb.setGeometry(x, y, self._THUMB_W, thumb_h)
        if self._bar_visible:
            self._thumb.show()
            self._thumb.raise_()
        else:
            self._thumb.hide()

    def _show_bar(self, *, linger: bool = False) -> None:
        if not self._needs_scroll():
            self._thumb.hide()
            return
        self._hide_timer.stop()
        self._bar_visible = True
        self._relayout_thumb()
        self._thumb.show()
        self._thumb.raise_()
        if linger:
            self._hide_timer.start()

    def _try_hide_bar(self) -> None:
        if self._thumb._dragging:
            return
        if self._mouse_near_track():
            return
        self._bar_visible = False
        self._thumb.hide()

    def _mouse_near_track(self) -> bool:
        pos = self.mapFromGlobal(QCursor.pos())
        if not self.rect().contains(pos):
            return False
        return pos.x() >= self.width() - self._EDGE_PX

    def _on_scrolled(self, _value: int = 0) -> None:
        self._relayout_thumb()
        if self._needs_scroll():
            self._show_bar(linger=True)

    def eventFilter(self, obj, event):  # noqa: N802
        et = event.type()
        if et in (QEvent.Type.MouseMove, QEvent.Type.Enter, QEvent.Type.HoverMove):
            if self._mouse_near_track():
                self._show_bar()
            elif not self._thumb._dragging:
                self._hide_timer.start()
        elif et == QEvent.Type.Leave:
            if obj is self.viewport() and not self._thumb._dragging:
                if not self._mouse_near_track():
                    self._hide_timer.start()
        elif et == QEvent.Type.Resize and obj is self.viewport():
            self._relayout_thumb()
        return super().eventFilter(obj, event)

    def enterEvent(self, event) -> None:  # noqa: N802
        if self._mouse_near_track():
            self._show_bar()
        super().enterEvent(event)

    def wheelEvent(self, event) -> None:  # noqa: N802
        super().wheelEvent(event)
        if self._needs_scroll():
            self._show_bar(linger=True)

    def resizeEvent(self, event) -> None:  # noqa: N802
        super().resizeEvent(event)
        self._relayout_thumb()


class EStopBadge(QWidget):
    """发送钮旁：紧急制动（红色线稿电源符）。"""

    clicked = Signal()

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("EStopBadge")
        self.setFixedSize(28, 28)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setToolTip("紧急制动：回手动并中止分析")
        self._pressed = False

    def set_hotkey_tip(self, key: str) -> None:
        k = (key or "").strip()
        if k:
            self.setToolTip(f"紧急制动：回手动并中止分析（{k}）")
        else:
            self.setToolTip("紧急制动：回手动并中止分析")

    def mousePressEvent(self, event) -> None:  # noqa: N802
        if event.button() == Qt.MouseButton.LeftButton:
            self._pressed = True
            self.update()
            event.accept()
            return
        super().mousePressEvent(event)

    def mouseReleaseEvent(self, event) -> None:  # noqa: N802
        was = self._pressed
        self._pressed = False
        self.update()
        if (
            was
            and event.button() == Qt.MouseButton.LeftButton
            and self.rect().contains(event.position().toPoint())
        ):
            self.clicked.emit()
            event.accept()
            return
        super().mouseReleaseEvent(event)

    def leaveEvent(self, event) -> None:  # noqa: N802
        if self._pressed:
            self._pressed = False
        self.update()
        super().leaveEvent(event)

    def enterEvent(self, event) -> None:  # noqa: N802
        self.update()
        super().enterEvent(event)

    def paintEvent(self, event) -> None:  # noqa: N802
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        # 默认透明；悬停浅灰；按下更深
        if self._pressed:
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(QColor("#E6E6E6"))
            p.drawRoundedRect(self.rect().adjusted(1, 1, -1, -1), 7, 7)
        elif self.underMouse():
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(QColor("#F2F2F2"))
            p.drawRoundedRect(self.rect().adjusted(1, 1, -1, -1), 7, 7)

        # 色反：红线稿电源符，无实心红底
        cx = self.width() / 2.0
        cy = self.height() / 2.0
        red = QColor("#E53935") if not self._pressed else QColor("#C62828")
        stroke = 1.4
        pen = QPen(
            red,
            stroke,
            Qt.PenStyle.SolidLine,
            Qt.PenCapStyle.RoundCap,
            Qt.PenJoinStyle.RoundJoin,
        )
        p.setPen(pen)
        p.setBrush(Qt.BrushStyle.NoBrush)
        ring_r = 7.0
        ring = QRectF(cx - ring_r, cy - ring_r, ring_r * 2.0, ring_r * 2.0)
        # 缺口 ±50° 绕正上方（90°）：140° → 140°+260°
        p.drawArc(ring, int(140 * 16), int(260 * 16))
        p.drawLine(QPointF(cx, cy - ring_r - 1.2), QPointF(cx, cy - 0.2))
        p.end()


class IconSlotBadge(QWidget):
    """底栏可配槽：28×28 图标 only（与详情/驾驶/急停同壳）。"""

    clicked = Signal()

    def __init__(
        self,
        kind: str,
        tip: str,
        parent: QWidget | None = None,
        *,
        active: bool = False,
    ) -> None:
        super().__init__(parent)
        self.setObjectName("IconSlotBadge")
        self.setFixedSize(28, 28)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self._kind = kind or "clear"
        self._tip = tip or ""
        self._active = bool(active)
        self._pressed = False
        self.setToolTip(self._tip)

    def set_kind(self, kind: str, tip: str = "") -> None:
        self._kind = kind or "clear"
        if tip:
            self._tip = tip
            self.setToolTip(tip)
        self.update()

    def set_active(self, on: bool) -> None:
        on = bool(on)
        if on == self._active:
            return
        self._active = on
        self.update()

    def mousePressEvent(self, event) -> None:  # noqa: N802
        if event.button() == Qt.MouseButton.LeftButton:
            self._pressed = True
            self.update()
            event.accept()
            return
        super().mousePressEvent(event)

    def mouseReleaseEvent(self, event) -> None:  # noqa: N802
        was = self._pressed
        self._pressed = False
        self.update()
        if (
            was
            and event.button() == Qt.MouseButton.LeftButton
            and self.rect().contains(event.position().toPoint())
        ):
            self.clicked.emit()
            event.accept()
            return
        super().mouseReleaseEvent(event)

    def leaveEvent(self, event) -> None:  # noqa: N802
        if self._pressed:
            self._pressed = False
        self.update()
        super().leaveEvent(event)

    def enterEvent(self, event) -> None:  # noqa: N802
        self.update()
        super().enterEvent(event)

    def paintEvent(self, event) -> None:  # noqa: N802
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        if self._active:
            plate = QColor("#E4F1EA") if self._pressed else QColor("#EEF7F2")
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(plate)
            p.drawRoundedRect(self.rect().adjusted(1, 1, -1, -1), 7, 7)
            ink = QColor("#00C375")
        elif self._pressed:
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(QColor("#E6E6E6"))
            p.drawRoundedRect(self.rect().adjusted(1, 1, -1, -1), 7, 7)
            ink = QColor(75, 75, 75)
        elif self.underMouse():
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(QColor("#F2F2F2"))
            p.drawRoundedRect(self.rect().adjusted(1, 1, -1, -1), 7, 7)
            ink = QColor(75, 75, 75)
        else:
            ink = QColor(75, 75, 75)
        _paint_line_icon(
            p,
            self._kind,
            cx=self.width() / 2.0,
            cy=self.height() / 2.0,
            color=ink,
        )
        p.end()


class PreviewWindow(QWidget):
    def __init__(self, config: JoyConfig, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.cfg = config
        self._main_host: QWidget | None = None  # SettingsWindow，用于回到主界面
        self.setObjectName("PreviewRoot")
        self.setWindowTitle("Joy 看屏副驾")
        self.setMinimumSize(780, 640)
        self.setWindowFlags(self._normal_window_flags())
        # 贴近微信 PC：微软雅黑 + 14px 正文；微信绿 #07C160
        _wx_font = QFont("Microsoft YaHei UI", 14)
        if not _wx_font.exactMatch():
            _wx_font = QFont("微软雅黑", 14)
        self.setFont(_wx_font)
        self.setStyleSheet(
            """
            QWidget {
                background:#FFFFFF;
                color:#191919;
                font-family:"Microsoft YaHei UI","微软雅黑","Segoe UI";
                font-size:14px;
            }
            /* 层1：侧窗外壳托盘 — 基色 rgb(227,227,232) */
            QWidget#PreviewRoot[dockShell="true"] {
                background: rgb(227, 227, 232);
            }
            QWidget#PreviewRoot[dockShell="false"],
            QWidget#PreviewRoot {
                background: #FFFFFF;
            }
            /* 层2：中间主内容白卡片 */
            QFrame#DockMidCard {
                background: #FFFFFF;
                border: 1px solid #D0D0D0;
                border-radius: 8px;
            }
            QFrame#DockMidCard[flat="true"] {
                background: #FFFFFF;
                border: none;
                border-radius: 0px;
            }
            QFrame#DockMidCard QWidget {
                background: transparent;
            }
            QFrame#DockMidCard QPushButton {
                background: #FFFFFF;
            }
            QFrame#DockMidCard QPushButton#AnalyzeBtn {
                background: transparent;
            }
            QFrame#DockMidCard QPushButton#ComposeSend {
                background: #00C375;
                color: #FFFFFF;
                border: none;
            }
            QFrame#DockMidCard QPushButton#ComposeSend:disabled {
                background: rgb(237, 237, 237);
                color: rgb(170, 170, 170);
            }
            QFrame#DockMidCard QPushButton#ComposeSend:hover:!disabled {
                background: #00B06A;
            }
            QFrame#DockMidCard QPushButton#ComposeSend:pressed:!disabled {
                background: #00965A;
            }
            QFrame#DockMidCard QPushButton#SendBtn {
                background: #FFFFFF;
                color: #00C375;
                border: 1px solid #00C375;
            }
            QFrame#DockMidCard QPushButton#SendBtn:hover {
                background: #EDEDED;
            }
            QFrame#DockMidCard QPushButton#SendBtn:pressed {
                background: #E0E0E0;
            }
            QFrame#DockMidCard QLabel#BubbleText {
                background: #9DF29F;
            }
            QFrame#DockMidCard QTextEdit {
                background:#F7F7F7;
            }
            /* 识别详情：白底圆角，系统条隐藏（滑块由 WeChatDetailTextEdit 自绘） */
            QFrame#DockMidCard QTextEdit#DetailResult,
            QTextEdit#DetailResult {
                background: #FFFFFF;
                color: #191919;
                border: 1px solid #E5E5E5;
                border-radius: 8px;
                padding: 8px 12px 8px 8px;
                font-size: 13px;
                selection-background-color: #D7F5E5;
            }
            QFrame#DockMidCard QProgressBar {
                background:#E5E5E5;
            }
            QPushButton {
                background:#FFFFFF;
                color:#191919;
                border:1px solid #E5E5E5;
                border-radius:4px;
                padding:6px 12px;
                font-size:14px;
            }
            QPushButton:hover { background:#F7F7F7; }
            QPushButton:pressed { background:#EDEDED; }
            QPushButton#AnalyzeBtn {
                background: transparent;
                color: #191919;
                border: none;
                border-radius: 0;
                padding: 2px 4px;
                font-size: 14px;
                font-weight: 400;
                min-width: 0;
            }
            QPushButton#AnalyzeBtn:hover {
                background: transparent;
                color: #07C160;
            }
            QPushButton#AnalyzeBtn:pressed {
                background: transparent;
                color: #059A4C;
            }
            QPushButton#AnalyzeBtn:disabled {
                color: #C8C8C8;
                background: transparent;
                border: none;
            }
            QLabel#PreviewCanvas {
                background:#EDEDED; color:#888888; border-radius:4px;
            }
            QTextEdit {
                background:#F7F7F7;
                color:#191919;
                border:1px solid #E5E5E5;
                border-radius:4px;
                padding:8px;
                font-size:14px;
            }
            QLabel#StatusLabel { color:#888888; font-size:12px; }
            QFrame#TopSep {
                background:#E7E7E7;
                border:none;
                max-height:1px;
                min-height:1px;
            }
            /* 中间层透明规则会盖掉 TopSep，必须再声明一遍 */
            QFrame#DockMidCard QFrame#TopSep {
                background:#E7E7E7;
                border:none;
                max-height:1px;
                min-height:1px;
            }
            QFrame#ComposeBox {
                background:#FFFFFF;
                border:1px solid #E5E5E5;
                border-radius:8px;
            }
            /* 侧窗：完整闭合圆角输入框（左右 4px 由 compose_shell 布局保证） */
            QWidget#PreviewRoot[dockShell="true"] QFrame#ComposeBox {
                background: #FFFFFF;
                border: 1px solid #D9D9D9;
                border-radius: 6px;
            }
            QLabel#ComposeContact {
                color:#888888;
                font-size:12px;
                background:transparent;
                border:none;
            }
            QLabel#ChatTitle {
                color:#191919;
                font-size:14px;
                font-weight:400;
                background:transparent;
                border:none;
                padding: 0;
            }
            QLabel#ChatTitle[empty="true"] {
                color:#B2B2B2;
                font-weight:400;
                font-size:14px;
            }
            QPlainTextEdit#ComposeDraft {
                color:#191919;
                font-size:14px;
                background:transparent;
                border:none;
                padding:0px;
            }
            QPlainTextEdit#ComposeDraft:focus {
                border:none;
                outline:none;
            }
            QFrame#ComposeBar {
                background:transparent;
                border:none;
            }
            QPushButton#ComposeSend {
                background:#00C375;
                color:#FFFFFF;
                border:none;
                border-radius:4px;
                padding:3px 6px;
                font-size:12.2px;
                font-weight:400;
                min-width:36px;
                max-width:48px;
                min-height: 20px;
                max-height: 20px;
            }
            QPushButton#ComposeSend:hover:!disabled { background:#00B06A; }
            QPushButton#ComposeSend:pressed:!disabled { background:#00965A; }
            QPushButton#ComposeSend:disabled {
                background: rgb(237, 237, 237);
                color: rgb(170, 170, 170);
            }
            QLabel#ProgressLabel {
                color:#07C160; font-weight:400; font-size:13px;
            }
            QToolButton#DetailToggle {
                background: transparent; border: none; color:#191919;
                font-weight:400; font-size:14px; padding:4px 0; text-align:left;
            }
            QToolButton#DetailToggle:hover { color:#07C160; }
            QToolButton#MoreDotsBtn {
                background: transparent;
                border: none;
                padding: 0;
                margin: 0;
            }
            QToolButton#MoreDotsBtn:hover { background: transparent; }
            QToolButton#MoreDotsBtn:pressed { background: transparent; }
            QMenu#MoreMenu {
                background: #FFFFFF;
                border: 1px solid #E5E5E5;
                border-radius: 8px;
                padding: 6px 0;
            }
            QMenu#MoreMenu::item {
                padding: 10px 28px 10px 14px;
                color: #191919;
                font-size: 14px;
                border-radius: 4px;
                margin: 0 6px;
            }
            QMenu#MoreMenu::item:selected {
                background: #F5F5F5;
                color: #07C160;
            }
            QMenu#MoreMenu::icon {
                padding-left: 6px;
            }
            QLabel#BubbleText {
                background:#9DF29F;
                color:#191919;
                border-radius:8px;
                padding:10px 12px;
                font-size:14px;
                font-weight:400;
                font-family:"Microsoft YaHei UI","微软雅黑","Segoe UI";
            }
            QScrollArea#CandidateScroll {
                background: transparent;
                border: none;
            }
            QWidget#CandidateInner { background: transparent; }
            QPushButton#SendBtn {
                background:#FFFFFF;
                color:#00C375;
                border:1px solid #00C375;
                border-radius:4px;
                padding:4px 10px;
                font-size:14px;
                font-weight:400;
                min-width:44px;
                max-width:56px;
            }
            QPushButton#SendBtn:hover { background:#EDEDED; }
            QPushButton#SendBtn:pressed { background:#E0E0E0; }
            QPushButton#SendBtn:disabled {
                color:#9FE0C4; border-color:#9FE0C4; background:#FFFFFF;
            }
            QLabel#AutoLabel {
                color:#191919; font-size:14px; font-weight:400;
            }
            QWidget#CandidateRow { background:transparent; }
            QProgressBar {
                border: none; background:#E5E5E5; border-radius:3px; height:8px;
                text-align: center;
            }
            QProgressBar::chunk {
                background:#07C160; border-radius:3px;
            }
            QLabel#TimingLabel {
                color:#B2B2B2;
                font-size:11px;
                font-weight:400;
            }
            """
        )

        self._windows: list[WindowInfo] = []
        self._target_hwnd: int | None = None
        self._worker: CaptureWorker | None = None
        self._analyze: AnalyzeWorker | None = None
        self._analyze_epoch: int = 0
        self._analyze_zombies: list[AnalyzeWorker] = []
        self._layout_probe: LayoutProbeWorker | None = None
        self._contact_probe: ContactIdentifyWorker | None = None
        self._header_watch: HeaderWatchWorker | None = None
        self._contact_header_fp: str = ""
        self._contact_identify_cool_until: float = 0.0
        self._contact_flip_pending: str = ""
        self._contact_identify_pending_fp: str = ""
        self._contact_last_identify_at: float = 0.0
        self._contact_soft_verify_at: float = 0.0  # 首次识名后强制复核一次
        self._contact_watch_armed: bool = False
        self._wechat_layout: WeChatLayout | None = None
        self._layout_fp: str = ""
        self._layout_probe_pending_fp: str = ""
        self._layout_debounce: QTimer | None = None
        self._layout_recal_cb = None
        self._layout_fail_fp: str = ""
        self._layout_fail_until: float = 0.0
        self._last_result: AnalyzeResult | None = None
        self._engine = Engine(config)
        self._candidate_btns: list[QWidget] = []
        self._progress_ceiling = 12
        self._progress_hint_i = 0
        self._analyze_t0 = 0.0
        self._last_round_sec: float | None = None
        self._contact_name = ""
        mode = (config.ui.display_mode or "normal").strip().lower()
        self._display_mode = "dock" if mode == "dock" else "normal"
        self._normal_geo: tuple[int, int, int, int] | None = None
        # 因微信最小化而隐藏侧窗；微信调出后再自动显示（用户主动关窗不触发）
        self._dock_hidden_for_wechat = False
        # 用户主动「收起」侧窗 → 托盘感隐藏 + 微信标题栏 △ 唤醒
        self._dock_collapsed = False
        self._redirecting_minimize = False
        self._wechat_fg_latched = False
        self._z_raise_gen = 0
        self._wake_btn = WeChatWakeButton()
        self._wake_btn.activated.connect(self._expand_from_wake)
        self._wake_btn.hide()
        self._collapse_btn = DockCollapseButton()
        self._collapse_btn.activated.connect(self._collapse_dock)
        self._collapse_btn.hide()
        self._dock_fly_anim: QParallelAnimationGroup | None = None
        self._dock_animating = False
        self._pre_collapse_pos: QPoint | None = None
        self._pre_collapse_side: str = "right"  # Joy 相对微信：right|left
        self._dock_follow = QTimer(self)
        self._dock_follow.setInterval(400)
        self._dock_follow.timeout.connect(self._follow_wechat_dock)
        # 显隐单独快轮询：最小化/呼出更跟手（不碰几何、不扰任务栏）
        self._dock_vis_timer = QTimer(self)
        self._dock_vis_timer.setInterval(50)
        self._dock_vis_timer.timeout.connect(self._tick_dock_visibility)
        # 图层跟随：仅检测「刚回前台」升起沿
        self._dock_z_timer = QTimer(self)
        self._dock_z_timer.setInterval(150)
        self._dock_z_timer.timeout.connect(self._tick_dock_z)
        self._wechat_min_latched: bool | None = None
        # 顶栏指纹轮询：本地很快；变了再打识名 API
        self._contact_poll = QTimer(self)
        # 顶栏指纹本地很快；略密一点，切会话更跟手
        self._contact_poll.setInterval(450)
        self._contact_poll.timeout.connect(self._poll_contact_header)
        self._contact_debounce = QTimer(self)
        self._contact_debounce.setSingleShot(True)
        self._contact_debounce.setInterval(100)
        self._contact_debounce.timeout.connect(self._on_contact_debounce_fire)
        # 自动模式：画面变化稳定后触发分析
        self._auto_armed_at = 0.0
        self._auto_cooldown_until = 0.0
        self._auto_settle = QTimer(self)
        self._auto_settle.setSingleShot(True)
        self._auto_settle.timeout.connect(self._on_auto_settle)

        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)
        self._root_lay = root

        # 层2：中间主内容卡片（侧窗时白底圆角；全视时 flat 铺满）
        self.dock_mid_card = QFrame()
        self.dock_mid_card.setObjectName("DockMidCard")
        self.setProperty("dockShell", "false")
        self.dock_mid_card.setProperty("flat", "true")
        mid = QVBoxLayout(self.dock_mid_card)
        mid.setContentsMargins(16, 14, 16, 12)
        mid.setSpacing(12)
        self._mid_lay = mid

        # 工具栏：右侧 ··· 收纳功能（微信式）
        self.toolbar_host = QWidget()
        self.toolbar_lay = QVBoxLayout(self.toolbar_host)
        self.toolbar_lay.setContentsMargins(0, 0, 0, 0)
        self.toolbar_lay.setSpacing(0)
        self.toolbar_row1 = QHBoxLayout()
        self.toolbar_row1.setSpacing(0)
        self.toolbar_row2 = QHBoxLayout()
        self.toolbar_row2.setSpacing(0)

        self._paused = False
        self._mode_dock = False

        self.act_refresh = QAction(_line_icon("refresh"), "刷新", self)
        self.act_refresh.setToolTip("刷新微信窗口列表")
        self.act_refresh.triggered.connect(self.refresh_windows)

        self.act_pause = QAction(_line_icon("pause"), "暂停预览", self)
        self.act_pause.setCheckable(True)
        self.act_pause.setToolTip("暂停 / 继续预览")
        self.act_pause.triggered.connect(self._on_pause)

        self.act_restore = QAction(_line_icon("restore"), "还原窗口", self)
        self.act_restore.setToolTip("微信最小化时点此还原，再继续采集")
        self.act_restore.triggered.connect(self.restore_target)

        self.act_clear = QAction(_line_icon("clear"), "清理屏幕", self)
        self.act_clear.setToolTip("清空识别详情与候选（不影响预览采集）")
        self.act_clear.triggered.connect(self.clear_results)

        # 保留供同步过程中禁用；菜单改为二级「语境参数」面板
        self.act_pull_mem = QAction("同步上下文", self)
        self.act_pull_mem.setVisible(False)

        self.act_context = QAction(_line_icon("context"), "语境参数", self)
        self.act_context.setToolTip("上下文导入 / 同步 / 清除（Tab 分栏）")
        self.act_context.triggered.connect(self._open_context_hub)

        self.act_recal_layout = QAction(_line_icon("refresh"), "重构布局", self)
        self.act_recal_layout.setToolTip("重新校准微信布局")
        self.act_recal_layout.triggered.connect(self._on_recal_layout)

        self.act_back_main = QAction(_line_icon("home"), "回主界面", self)
        self.act_back_main.setToolTip("打开 Joy 设置主界面，方便改配置")
        self.act_back_main.triggered.connect(self._on_back_to_main)

        self.act_coach_guide = QAction(_line_icon("detail"), "动态指引", self)
        self.act_coach_guide.setToolTip("分步高亮副驾界面（可随时跳过）")
        self.act_coach_guide.triggered.connect(
            lambda: self.start_coach_tour(force=True)
        )

        self.act_mode = QAction(_line_icon("mode"), "侧窗模式", self)
        self.act_mode.setToolTip("大窗模式 / 侧窗模式切换")
        self.act_mode.triggered.connect(self._toggle_display_mode)

        self.act_dock_preview = QAction(_line_icon("preview"), "截屏预览", self)
        self.act_dock_preview.setCheckable(True)
        self.act_dock_preview.setToolTip("侧窗模式显示 / 隐藏微信截屏预览")
        self.act_dock_preview.setChecked(
            bool(getattr(self.cfg.ui, "dock_show_preview", True))
        )
        self.act_dock_preview.toggled.connect(self._on_menu_dock_preview_toggled)

        self.act_analyze = QAction(_line_icon("analyze"), "手动分析", self)
        self.act_analyze.setToolTip("立即解析当前会话")
        self.act_analyze.triggered.connect(lambda: self.run_analyze(auto=False))

        self.act_auto = QAction(_line_icon("auto"), "自动驾驶", self)
        self.act_auto.setCheckable(True)
        self.act_auto.setToolTip(
            "自动驾驶：监测画面变化；有对方新消息时自动识图起草（不自动发送）"
        )
        self.act_auto.toggled.connect(self._on_menu_auto_toggled)

        self.act_smart = QAction(_line_icon("smart"), "智能驾驶", self)
        self.act_smart.setCheckable(True)
        self.act_smart.setToolTip(
            "智能驾驶：出候选后短暂停留，自动发送首条（全自动）"
        )
        self.act_smart.toggled.connect(self._on_menu_smart_toggled)

        self.act_e_stop = QAction(_line_icon("estop"), "紧急制动", self)
        self.act_e_stop.setToolTip(
            "紧急制动：关闭自动驾驶/智能驾驶，回到手动，并中止当前分析"
        )
        self.act_e_stop.triggered.connect(self._emergency_stop)

        self.act_detail = QAction(_line_icon("detail"), "对话详情", self)
        self.act_detail.setCheckable(True)
        self.act_detail.setToolTip("展开 / 折叠解析后的对话详情")
        self.act_detail.toggled.connect(self._on_menu_detail_toggled)

        self.more_menu = QMenu(self)
        self.more_menu.setObjectName("MoreMenu")
        self.more_menu.setToolTipsVisible(True)
        self.more_menu.addAction(self.act_analyze)
        self.more_menu.addAction(self.act_auto)
        self.more_menu.addAction(self.act_smart)
        self.more_menu.addAction(self.act_e_stop)
        self.more_menu.addSeparator()
        # 暂隐：刷新 / 暂停预览 / 还原
        # self.more_menu.addAction(self.act_refresh)
        # self.more_menu.addAction(self.act_pause)
        # self.more_menu.addAction(self.act_restore)
        self.more_menu.addAction(self.act_clear)
        self.more_menu.addAction(self.act_detail)
        self.more_menu.addAction(self.act_context)
        self.more_menu.addAction(self.act_recal_layout)
        self.more_menu.addAction(self.act_dock_preview)
        self.more_menu.addAction(self.act_mode)
        self.more_menu.addAction(self.act_coach_guide)
        self.more_menu.addAction(self.act_back_main)
        self.more_menu.aboutToShow.connect(self._sync_more_menu)

        self.btn_more = MoreDotsButton()
        self.btn_more.setMenu(self.more_menu)

        # 兼容旧引用名（逻辑仍走 QAction）
        self.btn_refresh = self.act_refresh
        self.btn_pause = self.act_pause
        self.btn_restore = self.act_restore
        self.btn_clear = self.act_clear
        self.btn_mode = self.act_mode

        self.btn_analyze = QPushButton("立即分析")
        self.btn_analyze.setObjectName("AnalyzeBtn")
        self.btn_analyze.setFlat(True)
        self.btn_analyze.setCursor(Qt.CursorShape.PointingHandCursor)
        self.btn_analyze.setToolTip("立即解析当前会话")
        self.btn_analyze.clicked.connect(self.run_analyze)

        # 顶栏：左群名/人名 · 右 ···
        self.chat_title = QLabel("—")
        self.chat_title.setObjectName("ChatTitle")
        self.chat_title.setProperty("empty", True)
        self.chat_title.setAlignment(
            Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter
        )
        self.chat_title.setWordWrap(False)
        self.chat_title.setTextInteractionFlags(
            Qt.TextInteractionFlag.TextSelectableByMouse
        )

        self.toolbar_lay.addLayout(self.toolbar_row1)
        self.toolbar_lay.addLayout(self.toolbar_row2)
        mid.addWidget(self.toolbar_host)
        self._rebuild_toolbar(dock=False)

        # 分隔线：顶栏 ↓ 内容；侧窗时线紧贴标题带（无额外顶距）
        self.sep_host = QWidget()
        sep_lay = QVBoxLayout(self.sep_host)
        sep_lay.setContentsMargins(0, 0, 0, 8)
        sep_lay.setSpacing(0)
        self._sep_lay = sep_lay
        self.top_sep = QFrame()
        self.top_sep.setObjectName("TopSep")
        self.top_sep.setFrameShape(QFrame.Shape.NoFrame)
        self.top_sep.setFixedHeight(1)
        sep_lay.addWidget(self.top_sep)
        mid.addWidget(self.sep_host)

        # 分隔线下方：自动模式 + 轻量「立即分析」（绑成一组）
        self.auto_host = QWidget()
        self.auto_host.setObjectName("AutoHost")
        auto_lay = QHBoxLayout(self.auto_host)
        auto_lay.setContentsMargins(0, 0, 0, 8)
        auto_lay.setSpacing(8)
        self._auto_lay = auto_lay
        self.auto_label = QLabel("自动模式")
        self.auto_label.setObjectName("AutoLabel")
        self.btn_auto = ToggleSwitch()
        self.btn_auto.setToolTip(
            "自动驾驶：监测画面变化；有对方新消息时自动识图起草（不自动发送）"
        )
        self.btn_auto.toggled.connect(self._on_auto_toggled)
        auto_lay.addWidget(self.auto_label)
        auto_lay.addWidget(self.btn_auto)
        auto_lay.addSpacing(4)
        auto_lay.addWidget(self.btn_analyze)
        auto_lay.addStretch(1)
        mid.addWidget(self.auto_host)
        # 三项已收进 ··· 菜单，主界面先隐藏
        self.auto_host.setVisible(False)

        self.image = QLabel("正在连接微信预览…")
        self.image.setObjectName("PreviewCanvas")
        self.image.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.image.setMinimumHeight(220)
        mid.addWidget(self.image, stretch=2)

        # 分析进度（真实阶段 + 阶段内假进度）
        self.progress_host = QWidget()
        prog_lay = QVBoxLayout(self.progress_host)
        prog_lay.setContentsMargins(0, 0, 0, 0)
        prog_lay.setSpacing(6)
        prog_head = QHBoxLayout()
        prog_head.setContentsMargins(0, 0, 0, 0)
        prog_head.setSpacing(8)
        self.progress_label = QLabel("")
        self.progress_label.setObjectName("ProgressLabel")
        self.timing_label = QLabel("")
        self.timing_label.setObjectName("TimingLabel")
        self.timing_label.setAlignment(
            Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter
        )
        prog_head.addWidget(self.progress_label, stretch=1)
        prog_head.addWidget(self.timing_label, stretch=0)
        self.progress_bar = QProgressBar()
        self.progress_bar.setRange(0, 100)
        self.progress_bar.setValue(0)
        self.progress_bar.setTextVisible(False)
        self.progress_bar.setFixedHeight(10)
        prog_lay.addLayout(prog_head)
        prog_lay.addWidget(self.progress_bar)
        self.progress_host.setVisible(False)
        mid.addWidget(self.progress_host)

        self._fake_timer = QTimer(self)
        self._fake_timer.setInterval(450)
        self._fake_timer.timeout.connect(self._tick_fake_progress)
        self._elapsed_timer = QTimer(self)
        self._elapsed_timer.setInterval(200)
        self._elapsed_timer.timeout.connect(self._tick_elapsed)
        self._timing_hide = QTimer(self)
        self._timing_hide.setSingleShot(True)
        self._timing_hide.setInterval(4500)
        self._timing_hide.timeout.connect(self._hide_last_timing)
        self._smart_send = QTimer(self)
        self._smart_send.setSingleShot(True)
        self._smart_send.timeout.connect(self._on_smart_send_fire)
        self._smart_pending: Candidate | None = None
        self._drive_pending: str | None = None
        self._drive_commit = QTimer(self)
        self._drive_commit.setSingleShot(True)
        self._drive_commit.setInterval(3000)
        self._drive_commit.timeout.connect(self._commit_drive_mode_pending)

        # 识别详情默认折叠（不再旁挂长摘要提示）
        detail_head = QHBoxLayout()
        detail_head.setSpacing(8)
        self._detail_head = detail_head
        self.btn_detail = QToolButton()
        self.btn_detail.setObjectName("DetailToggle")
        self.btn_detail.setCheckable(True)
        self.btn_detail.setChecked(False)
        self.btn_detail.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextOnly)
        self.btn_detail.setCursor(Qt.CursorShape.PointingHandCursor)
        self.btn_detail.toggled.connect(self._on_detail_toggled)
        self.detail_summary = QLabel("")
        self.detail_summary.setVisible(False)
        self.last_timing_label = QLabel("")
        self.last_timing_label.setObjectName("TimingLabel")
        self.last_timing_label.setAlignment(
            Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter
        )
        self.last_timing_label.setVisible(False)
        detail_head.addStretch(1)
        detail_head.addWidget(self.last_timing_label)
        mid.addLayout(detail_head)
        # 识别详情入口已进 ···；主界面只保留耗时标签
        self.btn_detail.setParent(self)
        self.btn_detail.hide()

        self.result = WeChatDetailTextEdit()
        self.result.setReadOnly(True)
        self.result.setPlaceholderText("对话与判断详情")
        self.result.setMinimumHeight(120)
        self.result.setVisible(False)
        mid.addWidget(self.result, stretch=0)
        self._sync_detail_toggle_label()

        # 候选区可滚动：叠加滑块不占宽；长列表不撑爆定高侧窗
        self.actions_scroll = WeChatHoverScrollArea()
        self.actions_scroll.setWidgetResizable(True)
        self.actions_scroll.setFrameShape(QFrame.Shape.NoFrame)
        self.actions_scroll.setMinimumHeight(0)
        self.actions_inner = QWidget()
        self.actions_inner.setObjectName("CandidateInner")
        self.actions_host = QVBoxLayout(self.actions_inner)
        # 右侧留给叠加滑块 + 右缘缝，避免与「发送」钮重叠
        self.actions_host.setContentsMargins(
            0,
            0,
            WeChatHoverScrollArea._THUMB_W + WeChatHoverScrollArea._RIGHT_GAP + 4,
            0,
        )
        self.actions_host.setSpacing(0)
        self.actions_host.addStretch(1)
        self.actions_scroll.setWidget(self.actions_inner)
        mid.addWidget(self.actions_scroll, stretch=1)

        self.status = QLabel("")
        self.status.setObjectName("StatusLabel")
        self.status.setWordWrap(True)
        self.status.setVisible(False)
        mid.addWidget(self.status)

        # 底部：仿微信「待发送」输入栏（草稿 + 发送）
        self._draft_candidate: Candidate | None = None
        self.compose_box = QFrame()
        self.compose_box.setObjectName("ComposeBox")
        compose_lay = QVBoxLayout(self.compose_box)
        # 贴近微信输入区：顶/左边距约半个字高，文字顶对齐不居中
        compose_lay.setContentsMargins(12, 10, 12, 6)
        compose_lay.setSpacing(6)

        self.compose_draft = WeChatComposeEdit()
        self.compose_draft.textChanged.connect(self._on_compose_text_changed)
        compose_lay.addWidget(self.compose_draft, stretch=1)

        bar = QFrame()
        bar.setObjectName("ComposeBar")
        self._compose_bar = bar
        self._compose_bar_lay = QHBoxLayout(bar)
        self._compose_bar_lay.setContentsMargins(0, 8, 0, 0)
        self._compose_bar_lay.setSpacing(8)
        # 状态灯始终创建（可不在槽位里显示，逻辑仍可更新）
        self.detail_badge = DetailBadge()
        self.detail_badge.clicked.connect(self._on_detail_badge_clicked)
        self.drive_badge = DriveModeBadge()
        self.drive_badge.clicked.connect(self._on_drive_badge_clicked)
        self.estop_badge = EStopBadge()
        self.estop_badge.clicked.connect(self._emergency_stop)
        self.btn_compose_send = QPushButton("发送")
        self.btn_compose_send.setObjectName("ComposeSend")
        self.btn_compose_send.setCursor(Qt.CursorShape.PointingHandCursor)
        self.btn_compose_send.setSizePolicy(
            QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Fixed
        )
        self.btn_compose_send.setEnabled(False)
        self.btn_compose_send.clicked.connect(self._compose_send)
        self._sync_compose_send_label()
        self._dock_icon_badges: dict[str, IconSlotBadge] = {}
        compose_lay.addWidget(bar)
        self.apply_dock_bar_slots()

        self.compose_box.setMinimumHeight(72)
        # 底栏图标 28px + 内边距，禁止布局压成「细线」
        bar.setMinimumHeight(36)
        # 外壳：侧窗左右 4px 离第二层边；通栏分隔线不受影响
        self.compose_shell = QWidget()
        self.compose_shell.setObjectName("ComposeShell")
        self._compose_shell_lay = QVBoxLayout(self.compose_shell)
        self._compose_shell_lay.setContentsMargins(0, 0, 0, 0)
        self._compose_shell_lay.setSpacing(0)
        self._compose_shell_lay.addWidget(self.compose_box)
        mid.addWidget(self.compose_shell)

        # 根布局只托中间层（层1 玻璃露在 mid_card 外圈）
        root.addWidget(self.dock_mid_card, stretch=1)

        # 正中央浅灰提示（叠在内容之上）
        self._center_toast = CenterToast(self)
        self._coach_overlay = None  # lazy：joy.app.coach_tour.CoachTourOverlay

        # 恢复自动模式：只武装 + 抓基准，不立刻分析
        if self.cfg.ui.auto_mode:
            self.btn_auto.blockSignals(True)
            self.btn_auto.setChecked(True)
            self.btn_auto.blockSignals(False)
            self.act_auto.blockSignals(True)
            self.act_auto.setChecked(True)
            self.act_auto.blockSignals(False)
            self._arm_auto_observe()
        if bool(getattr(self.cfg.ui, "smart_drive", False)):
            self.act_smart.blockSignals(True)
            self.act_smart.setChecked(True)
            self.act_smart.blockSignals(False)

        self.refresh_windows()
        self._apply_display_mode(persist=False)
        self.reload_hotkeys()
        self._sync_drive_mode_badge()
        # 进入副驾即自动预览
        QTimer.singleShot(0, self._ensure_preview)
        QTimer.singleShot(200, self._ensure_preview)
        # 大窗：无 sync_after_open 贴边流程，延迟弹出首次指引
        if not self.is_dock_mode():
            QTimer.singleShot(1000, lambda: self._maybe_start_coach_tour(force=False))

    def is_dock_mode(self) -> bool:
        return self._display_mode == "dock"

    @staticmethod
    def _normal_window_flags() -> Qt.WindowType:
        """独立窗：标题栏 + 最小化/最大化/关闭（侧窗 setFixedSize 后最大化自然不可用）。"""
        return (
            Qt.WindowType.Window
            | Qt.WindowType.WindowTitleHint
            | Qt.WindowType.WindowSystemMenuHint
            | Qt.WindowType.WindowMinimizeButtonHint
            | Qt.WindowType.WindowMaximizeButtonHint
            | Qt.WindowType.WindowCloseButtonHint
        )

    def _clear_layout(self, lay: QHBoxLayout | QVBoxLayout) -> None:
        while lay.count():
            item = lay.takeAt(0)
            w = item.widget()
            if w is not None:
                w.setParent(self.toolbar_host)

    def _rebuild_toolbar(self, *, dock: bool) -> None:
        self._clear_layout(self.toolbar_row1)
        self._clear_layout(self.toolbar_row2)
        # 左：群名/人名（最多约占可用宽 75%）；中间弹性空位；右：···
        self.toolbar_row1.addWidget(self.chat_title, stretch=0)
        self.toolbar_row1.addStretch(1)
        self.toolbar_row1.addWidget(self.btn_more, stretch=0)
        self._mode_dock = bool(dock)
        self._sync_more_menu()
        self._set_contact_name(self._contact_name)

    def _sync_more_menu(self) -> None:
        paused = bool(self.act_pause.isChecked())
        dock = bool(self._mode_dock)
        self.act_pause.setIcon(_line_icon("pause", paused=paused))
        self.act_pause.setText("继续预览" if paused else "暂停预览")
        self.act_mode.setIcon(_line_icon("mode", mode_dock=dock))
        self.act_mode.setText("大窗模式" if dock else "侧窗模式")
        self.act_restore.setText("还原" if dock else "还原窗口")
        # 初始化早期 toolbar 重建时，btn_auto / btn_detail 可能尚未创建
        btn_auto = getattr(self, "btn_auto", None)
        btn_detail = getattr(self, "btn_detail", None)
        btn_analyze = getattr(self, "btn_analyze", None)
        if btn_auto is not None:
            self.act_auto.blockSignals(True)
            self.act_auto.setChecked(bool(btn_auto.isChecked()))
            self.act_auto.blockSignals(False)
        if getattr(self, "act_smart", None) is not None:
            self.act_smart.blockSignals(True)
            self.act_smart.setChecked(bool(getattr(self.cfg.ui, "smart_drive", False)))
            self.act_smart.blockSignals(False)
        if btn_detail is not None:
            self.act_detail.blockSignals(True)
            self.act_detail.setChecked(bool(btn_detail.isChecked()))
            self.act_detail.blockSignals(False)
        if btn_analyze is not None:
            self.act_analyze.setEnabled(bool(btn_analyze.isEnabled()))
        if getattr(self, "act_dock_preview", None) is not None:
            show_prev = bool(getattr(self.cfg.ui, "dock_show_preview", True))
            self.act_dock_preview.blockSignals(True)
            self.act_dock_preview.setChecked(show_prev)
            self.act_dock_preview.setEnabled(dock)
            self.act_dock_preview.blockSignals(False)
            self.act_dock_preview.setToolTip(
                "侧窗模式显示 / 隐藏微信截屏预览"
                if dock
                else "仅侧窗模式可用（大窗始终显示预览）"
            )

    def apply_dock_bar_slots(self) -> None:
        """按配置重建底栏：4 可配槽 + 固定驾驶灯 + 固定急停 + 发送。"""
        from joy.core.dock_bar import (
            action_icon_kind,
            action_tip,
            normalize_dock_bar_slots,
        )

        lay = getattr(self, "_compose_bar_lay", None)
        if lay is None:
            return
        slots = normalize_dock_bar_slots(
            getattr(self.cfg.ui, "dock_bar_slots", None)
        )
        self.cfg.ui.dock_bar_slots = list(slots)

        # 拆下旧子控件（核心徽标保留实例，其它 icon 槽删除）
        keep = {
            self.detail_badge,
            self.drive_badge,
            self.estop_badge,
            self.btn_compose_send,
        }
        while lay.count():
            item = lay.takeAt(0)
            w = item.widget()
            if w is None:
                continue
            if w in keep:
                w.setParent(None)
            else:
                w.deleteLater()
        self._dock_icon_badges = {}

        lay.addStretch(1)
        for aid in slots:
            if aid == "empty":
                continue
            if aid == "detail":
                lay.addWidget(
                    self.detail_badge,
                    stretch=0,
                    alignment=Qt.AlignmentFlag.AlignVCenter,
                )
                continue
            badge = IconSlotBadge(
                action_icon_kind(aid),
                action_tip(aid),
                active=self._dock_slot_active(aid),
            )
            badge.clicked.connect(
                lambda _=False, a=aid: self._on_dock_slot_action(a)
            )
            self._dock_icon_badges[aid] = badge
            lay.addWidget(
                badge, stretch=0, alignment=Qt.AlignmentFlag.AlignVCenter
            )

        # 槽 5：驾驶模式灯（固定）；槽 6：急停（固定）
        lay.addWidget(
            self.drive_badge,
            stretch=0,
            alignment=Qt.AlignmentFlag.AlignVCenter,
        )
        lay.addWidget(
            self.estop_badge,
            stretch=0,
            alignment=Qt.AlignmentFlag.AlignVCenter,
        )
        lay.addWidget(
            self.btn_compose_send,
            stretch=0,
            alignment=Qt.AlignmentFlag.AlignVCenter,
        )
        self._sync_dock_slot_visuals()

    def _dock_slot_active(self, action_id: str) -> bool:
        if action_id == "preview":
            return bool(getattr(self.cfg.ui, "dock_show_preview", True)) and bool(
                self.is_dock_mode()
            )
        if action_id == "detail":
            btn = getattr(self, "btn_detail", None)
            return bool(btn is not None and btn.isChecked())
        return False

    def _sync_dock_slot_visuals(self) -> None:
        badges = getattr(self, "_dock_icon_badges", None) or {}
        for aid, badge in badges.items():
            badge.set_active(self._dock_slot_active(aid))
        # 详情灯若在槽内，用自身 active
        if self.detail_badge.parent() is not None:
            btn = getattr(self, "btn_detail", None)
            self.detail_badge.set_active(bool(btn is not None and btn.isChecked()))

    def _on_dock_slot_action(self, action_id: str) -> None:
        aid = (action_id or "").strip().lower()
        if aid == "analyze":
            self.run_analyze(auto=False)
        elif aid == "preview":
            on = not bool(getattr(self.cfg.ui, "dock_show_preview", True))
            if getattr(self, "act_dock_preview", None) is not None:
                self.act_dock_preview.blockSignals(True)
                self.act_dock_preview.setChecked(on)
                self.act_dock_preview.blockSignals(False)
            self._on_menu_dock_preview_toggled(on)
        elif aid == "clear":
            self.clear_results()
        elif aid == "context":
            self._open_context_hub()
        elif aid == "mode":
            self._toggle_display_mode()
        elif aid == "recal":
            self._on_recal_layout()
        elif aid == "back_main":
            self._on_back_to_main()
        self._sync_dock_slot_visuals()

    def _on_menu_dock_preview_toggled(self, on: bool) -> None:
        self.cfg.ui.dock_show_preview = bool(on)
        try:
            self.cfg.save()
        except Exception:
            pass
        self._apply_dock_preview_visibility()
        self._sync_dock_slot_visuals()
        if self.is_dock_mode():
            self._show_center_toast(
                "已显示截屏预览" if on else "已隐藏截屏预览"
            )

    def _apply_dock_preview_visibility(self) -> None:
        """全视始终显示预览；侧窗按配置开关截图预览带。"""
        dock = self.is_dock_mode()
        show = True if not dock else bool(
            getattr(self.cfg.ui, "dock_show_preview", True)
        )
        if dock and show:
            self.image.setVisible(True)
            self.image.setMinimumHeight(120)
            self.image.setMaximumHeight(200)
        elif dock:
            self.image.setVisible(False)
            self.image.setMinimumHeight(0)
            self.image.setMaximumHeight(0)
            self.image.setPixmap(QPixmap())
            self.image.setText("")
        else:
            self.image.setVisible(True)
            self.image.setMinimumHeight(220)
            self.image.setMaximumHeight(16777215)

    def _on_menu_auto_toggled(self, on: bool) -> None:
        self._cancel_drive_commit()
        if bool(self.btn_auto.isChecked()) == bool(on):
            self._sync_drive_mode_badge()
            return
        self.btn_auto.setChecked(bool(on))

    def _on_menu_smart_toggled(self, on: bool) -> None:
        self._cancel_drive_commit()
        self.cfg.ui.smart_drive = bool(on)
        try:
            self.cfg.save()
        except Exception:
            pass
        if not on:
            self._cancel_smart_send()
            self._show_center_toast("智能驾驶已关闭")
        else:
            self._show_center_toast("智能驾驶已开启\n出结果后将自动发送")
        self._sync_drive_mode_badge()

    def _sync_drive_mode_badge(self) -> None:
        badge = getattr(self, "drive_badge", None)
        if badge is None:
            return
        # 徽标点击缓冲中：以预览灯为准，不被菜单状态盖回去
        if self._drive_commit.isActive() and self._drive_pending:
            badge.set_mode(self._drive_pending)
            return
        smart = bool(getattr(self.cfg.ui, "smart_drive", False)) and bool(
            getattr(self, "act_smart", None) and self.act_smart.isChecked()
        )
        auto = bool(getattr(self, "btn_auto", None) and self.btn_auto.isChecked())
        if smart:
            badge.set_mode(DriveModeBadge.MODE_SMART)
        elif auto:
            badge.set_mode(DriveModeBadge.MODE_AUTO)
        else:
            badge.set_mode(DriveModeBadge.MODE_MANUAL)

    def _cancel_drive_commit(self) -> None:
        self._drive_commit.stop()
        self._drive_pending = None

    def _on_drive_badge_clicked(self) -> None:
        """灯色立刻切；自动驾驶/智驾延迟 3s 再生效，方便连点切换。"""
        order = (
            DriveModeBadge.MODE_MANUAL,
            DriveModeBadge.MODE_AUTO,
            DriveModeBadge.MODE_SMART,
        )
        cur = self.drive_badge.mode()
        try:
            idx = order.index(cur)
        except ValueError:
            idx = 0
        nxt = order[(idx + 1) % len(order)]
        self.drive_badge.set_mode(nxt)
        self._drive_pending = nxt
        if nxt == DriveModeBadge.MODE_MANUAL:
            # 回手动立刻生效（停工）
            self._drive_commit.stop()
            self._apply_drive_mode(DriveModeBadge.MODE_MANUAL, announce=True)
            self._drive_pending = None
            return
        names = {
            DriveModeBadge.MODE_AUTO: "自动驾驶",
            DriveModeBadge.MODE_SMART: "智能驾驶",
        }
        self._drive_commit.start(3000)
        self._show_center_toast(
            f"{names.get(nxt, '模式')} · 3秒后生效\n可继续点击切换",
            ms=1600,
        )

    def _commit_drive_mode_pending(self) -> None:
        mode = self._drive_pending
        self._drive_pending = None
        if not mode:
            return
        # 若用户已用菜单改成别的，以灯色目标为准
        self._apply_drive_mode(mode, announce=True)

    def _apply_drive_mode(self, mode: str, *, announce: bool = False) -> None:
        """落地驾驶模式（徽标延迟提交 / 紧急制动等）。"""
        want_auto = mode in (DriveModeBadge.MODE_AUTO, DriveModeBadge.MODE_SMART)
        want_smart = mode == DriveModeBadge.MODE_SMART

        # 自动驾驶
        if getattr(self, "btn_auto", None) is not None:
            self.btn_auto.blockSignals(True)
            self.btn_auto.setChecked(want_auto)
            self.btn_auto.blockSignals(False)
        if getattr(self, "act_auto", None) is not None:
            self.act_auto.blockSignals(True)
            self.act_auto.setChecked(want_auto)
            self.act_auto.blockSignals(False)
        self.cfg.ui.auto_mode = bool(want_auto)
        if want_auto:
            self._arm_auto_observe()
            self._ensure_preview()
            if self.btn_pause.isChecked():
                self.btn_pause.setChecked(False)
                self._on_pause()
        else:
            self._auto_settle.stop()

        # 智能驾驶
        if getattr(self, "act_smart", None) is not None:
            self.act_smart.blockSignals(True)
            self.act_smart.setChecked(want_smart)
            self.act_smart.blockSignals(False)
        self.cfg.ui.smart_drive = bool(want_smart)
        if not want_smart:
            self._cancel_smart_send()

        try:
            self.cfg.save()
        except Exception:
            pass

        self.drive_badge.set_mode(mode)
        if announce:
            if mode == DriveModeBadge.MODE_SMART:
                self._show_center_toast("智能驾驶已生效\n出结果后将自动发送")
            elif mode == DriveModeBadge.MODE_AUTO:
                self._show_center_toast("自动驾驶已生效\n有新消息时将自动分析")
            else:
                self._show_center_toast("已切换为手动模式")

    def _show_center_toast(self, text: str, *, ms: int = 1800) -> None:
        toast = getattr(self, "_center_toast", None)
        if toast is None:
            return
        toast.show_toast(text, ms=ms)

    def _resolve_coach_target(self, target_id: str) -> QWidget | None:
        tid = (target_id or "").strip()
        mapping: dict[str, QWidget | None] = {
            "chat_title": getattr(self, "chat_title", None),
            "btn_more": getattr(self, "btn_more", None),
            "compose_box": getattr(self, "compose_box", None),
            "compose_bar": getattr(self, "_compose_bar", None),
            "drive_badge": getattr(self, "drive_badge", None),
            "estop_badge": getattr(self, "estop_badge", None),
            "btn_compose_send": getattr(self, "btn_compose_send", None),
            "detail_badge": getattr(self, "detail_badge", None),
            "collapse_btn": getattr(self, "_collapse_btn", None),
        }
        if tid in mapping:
            return mapping[tid]
        badges = getattr(self, "_dock_icon_badges", None) or {}
        return badges.get(tid)

    def _maybe_start_coach_tour(self, *, force: bool = False) -> None:
        if not force and bool(getattr(self.cfg.ui, "coach_tour_done", False)):
            return
        if not self.isVisible() or self._dock_collapsed:
            return
        self.start_coach_tour(force=force)

    def start_coach_tour(self, *, force: bool = True) -> None:
        """启动动态指引。force=True 时忽略 coach_tour_done（菜单/使用说明重看）。"""
        if not force and bool(getattr(self.cfg.ui, "coach_tour_done", False)):
            return
        if not self.isVisible():
            self.show()
        if self._dock_collapsed:
            expand = getattr(self, "_expand_from_wake", None)
            if callable(expand):
                expand()
        from joy.app.coach_tour import (
            CoachTourOverlay,
            default_dock_steps,
            default_normal_steps,
        )

        overlay = getattr(self, "_coach_overlay", None)
        if overlay is None:
            overlay = CoachTourOverlay(self)
            overlay.finished.connect(self._on_coach_tour_finished)
            self._coach_overlay = overlay
        elif overlay.is_active():
            return
        # 侧窗指引要看到标题栏 △，先确保收起钮可见
        if self.is_dock_mode():
            show_c = getattr(self, "_show_collapse_btn", None)
            if callable(show_c):
                show_c()
        steps = (
            default_dock_steps() if self.is_dock_mode() else default_normal_steps()
        )
        overlay.start(steps, self._resolve_coach_target)

    def _on_coach_tour_finished(self, completed: bool) -> None:
        # 走完或跳过都记已看过，避免每次打开都弹；使用说明可强制重看
        self.cfg.ui.coach_tour_done = True
        try:
            self.cfg.save()
        except Exception:
            pass
        if completed:
            self._show_center_toast("指引完成 · 可在 ··· 里重看", ms=2200)

    def _on_menu_detail_toggled(self, on: bool) -> None:
        if bool(self.btn_detail.isChecked()) == bool(on):
            return
        self.btn_detail.setChecked(bool(on))

    def reload_hotkeys(self) -> None:
        """从配置刷新 F1~F4（设置页保存后可再调）。"""
        hk = getattr(self.cfg.ui, "hotkeys", None)
        analyze = (getattr(hk, "manual_analyze", None) or "F1").strip() or "F1"
        auto = (getattr(hk, "auto_drive", None) or "F2").strip() or "F2"
        smart = (getattr(hk, "smart_drive", None) or "F3").strip() or "F3"
        estop = (getattr(hk, "emergency_stop", None) or "F4").strip() or "F4"
        for act, seq in (
            (self.act_analyze, analyze),
            (self.act_auto, auto),
            (self.act_smart, smart),
            (self.act_e_stop, estop),
        ):
            try:
                act.setShortcut(QKeySequence(seq))
                act.setShortcutContext(Qt.ShortcutContext.WindowShortcut)
                act.setShortcutVisibleInContextMenu(True)
            except Exception:
                pass
        badge = getattr(self, "estop_badge", None)
        if badge is not None:
            badge.set_hotkey_tip(estop)

    def _emergency_stop(self) -> None:
        """紧急制动：回手动，关自动驾驶/智能驾驶，中止分析与待发送。"""
        self._cancel_drive_commit()
        self._cancel_smart_send()
        self._auto_settle.stop()

        # 关智能驾驶（不弹二次 toast，最后统一提示）
        if getattr(self, "act_smart", None) is not None and self.act_smart.isChecked():
            self.act_smart.blockSignals(True)
            self.act_smart.setChecked(False)
            self.act_smart.blockSignals(False)
            self.cfg.ui.smart_drive = False

        # 关自动驾驶
        if getattr(self, "btn_auto", None) is not None and self.btn_auto.isChecked():
            self.btn_auto.blockSignals(True)
            self.btn_auto.setChecked(False)
            self.btn_auto.blockSignals(False)
            if getattr(self, "act_auto", None) is not None:
                self.act_auto.blockSignals(True)
                self.act_auto.setChecked(False)
                self.act_auto.blockSignals(False)
            self.cfg.ui.auto_mode = False
            self._auto_settle.stop()

        try:
            self.cfg.save()
        except Exception:
            pass

        # 中止当前分析：作废回调代数，线程引用挂到 zombies，避免运行中被 GC 崩进程
        worker = self._analyze
        aborted = False
        self._analyze_epoch = int(getattr(self, "_analyze_epoch", 0)) + 1
        if worker is not None and worker.isRunning():
            aborted = True
            for sig, slot in (
                (worker.progress, self._on_analyze_progress),
                (worker.finished_ok, self._on_analyze_ok),
                (worker.finished_err, self._on_analyze_err),
                (worker.finished, self._on_analyze_finished_ui),
            ):
                try:
                    sig.disconnect(slot)
                except (RuntimeError, TypeError):
                    pass
            try:
                worker.cancel()
            except Exception:
                try:
                    worker.requestInterruption()
                except Exception:
                    pass
            if worker not in self._analyze_zombies:
                self._analyze_zombies.append(worker)
            try:
                worker.finished.connect(
                    lambda *_, w=worker: self._reap_analyze_worker(w)
                )
            except Exception:
                pass
            self._analyze = None
            self.btn_analyze.setEnabled(True)
            self.act_analyze.setEnabled(True)
            self._fake_timer.stop()
            self._elapsed_timer.stop()
            self._analyze_t0 = 0.0
            self.progress_host.setVisible(False)
            self.progress_label.setText("")
            self.timing_label.setText("")
            self.progress_bar.setValue(0)
        elif worker is not None:
            self._analyze = None

        self._sync_drive_mode_badge()
        if aborted:
            self._show_center_toast("紧急制动 · 已回手动并中止分析")
        else:
            self._show_center_toast("紧急制动 · 已回手动模式")
        self._flash_status("紧急制动", linger_ms=2000)

    def _reap_analyze_worker(self, worker: AnalyzeWorker | None) -> None:
        """分析线程结束后安全回收（紧急制动僵尸线程）。"""
        if worker is None:
            return
        try:
            if worker in self._analyze_zombies:
                self._analyze_zombies.remove(worker)
        except Exception:
            pass
        if self._analyze is worker:
            self._analyze = None
        try:
            worker.deleteLater()
        except Exception:
            pass

    def _cancel_smart_send(self) -> None:
        self._smart_send.stop()
        self._smart_pending = None

    def _schedule_smart_send(self, cand: Candidate) -> None:
        if not bool(getattr(self.cfg.ui, "smart_drive", False)):
            return
        if not self.act_smart.isChecked():
            return
        self._smart_pending = cand
        delay = max(300, int(getattr(self.cfg.ui, "smart_drive_delay_ms", 900) or 900))
        self._smart_send.start(delay)
        self._flash_status(f"智能发送 · {delay / 1000:.1f}s 后发出", linger_ms=delay)

    def _on_smart_send_fire(self) -> None:
        cand = self._smart_pending
        self._smart_pending = None
        if cand is None:
            return
        if not bool(getattr(self.cfg.ui, "smart_drive", False)):
            return
        self._fill_candidate(cand, send=True, confirm=False, force_enter=True)

    def _toggle_display_mode(self) -> None:
        self._display_mode = "normal" if self.is_dock_mode() else "dock"
        self._apply_display_mode(persist=True)

    def _joy_dock_side(self) -> str:
        """Joy 相对微信：right（贴右侧）| left（贴左侧）。"""
        window = self._selected_window()
        if window is None:
            return "right"
        wr = window_screen_rect(window.hwnd)
        if wr is None:
            return "right"
        dpr = self._screen_dpr_for_hwnd(window.hwnd) or 1.0
        wx0, _, wx1, _ = (c / dpr for c in wr)
        wechat_cx = (wx0 + wx1) / 2.0
        joy_cx = self.frameGeometry().center().x()
        return "right" if joy_cx >= wechat_cx else "left"

    def _slide_into_wechat_pos(self, pos: QPoint, side: str) -> QPoint:
        """收起终点：y 不变，按外框宽度水平滑进微信。

        注意：顶层窗必须用 pos()/move()，不要用 geometry().topLeft() 去 move，
        否则 Windows 上会把客户区坐标当成窗口坐标，整窗下跳一个标题栏高度。
        """
        span = max(self.frameGeometry().width(), self.width(), 1)
        if side == "right":
            # 在微信右边 → 往左滑进微信
            return QPoint(pos.x() - span, pos.y())
        # 在微信左边 → 往右滑进微信
        return QPoint(pos.x() + span, pos.y())

    def _stop_dock_fly_anim(self) -> None:
        if self._dock_fly_anim is not None:
            try:
                self._dock_fly_anim.finished.disconnect()
            except Exception:
                pass
            self._dock_fly_anim.stop()
            self._dock_fly_anim.deleteLater()
            self._dock_fly_anim = None
        self._dock_animating = False

    def _run_dock_slide_anim(
        self,
        start: QPoint,
        end: QPoint,
        *,
        on_done,
        duration_ms: int = 220,
        ease_in: bool = True,
        under_wechat: bool = False,
    ) -> None:
        """水平平移（不改尺寸 / 不碰 geometry），收起滑进微信 / 展开滑出。"""
        self._stop_dock_fly_anim()
        self._dock_animating = True
        self.setWindowOpacity(1.0)
        # 若 start 已是当前 pos，move 为 no-op；禁止用 geometry 坐标覆盖
        if self.pos() != start:
            self.move(start)
        if under_wechat:
            self._z_order_under_wechat()

        anim = QPropertyAnimation(self, b"pos")
        anim.setDuration(duration_ms)
        anim.setStartValue(QPoint(start))
        anim.setEndValue(QPoint(end))
        anim.setEasingCurve(
            QEasingCurve.Type.InCubic if ease_in else QEasingCurve.Type.OutCubic
        )
        if under_wechat:
            # move 过程中 Win 可能把窗抬到前面，每帧压回微信下
            anim.valueChanged.connect(lambda *_: self._z_order_under_wechat())

        group = QParallelAnimationGroup(self)
        group.addAnimation(anim)

        def _finished() -> None:
            self._dock_fly_anim = None
            self._dock_animating = False
            on_done()

        group.finished.connect(_finished)
        self._dock_fly_anim = group
        group.start()

    def _z_order_under_wechat(self) -> None:
        """把侧窗压到微信下面（滑入/滑出动画时不盖住微信）。"""
        window = self._selected_window()
        if window is None:
            return
        try:
            import win32con
            import win32gui

            joy = int(self.winId())
            wx = int(window.hwnd)
            if not win32gui.IsWindow(joy) or not win32gui.IsWindow(wx):
                return
            # hwndInsertAfter=微信 → Joy 画在微信下方
            win32gui.SetWindowPos(
                joy,
                wx,
                0,
                0,
                0,
                0,
                win32con.SWP_NOMOVE
                | win32con.SWP_NOSIZE
                | win32con.SWP_NOACTIVATE,
            )
        except Exception:
            pass

    def _joy_stacked_under_wechat(self, joy: int, wx: int) -> bool:
        """Joy 是否已在微信之下（中间允许夹着侧窗收起 △）。"""
        try:
            import win32con
            import win32gui

            prev = win32gui.GetWindow(joy, win32con.GW_HWNDPREV)
            if prev == wx:
                return True
            # 常见：微信 → △ → Joy；不能当成「没贴好」去反复 SetWindowPos
            if self._collapse_btn.isVisible():
                tri = int(self._collapse_btn.winId())
                if (
                    tri
                    and prev == tri
                    and win32gui.GetWindow(tri, win32con.GW_HWNDPREV) == wx
                ):
                    return True
        except Exception:
            pass
        return False

    def _show_dock_noactivate(self) -> None:
        """侧窗露出但不抢前台焦点（否则任务栏点微信会先「激活」再最小化）。"""
        if not self.is_dock_mode():
            self.show()
            return
        # Qt：无激活显示；失败则退回普通 show
        try:
            self.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating, True)
            self.show()
        except Exception:
            self.show()
        finally:
            try:
                self.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating, False)
            except Exception:
                pass
        # Win32 再保险：若仍被抬成前台，把前台还回微信
        try:
            import win32con
            import win32gui

            joy = int(self.winId())
            window = self._selected_window()
            if window is None or not win32gui.IsWindow(joy):
                return
            if win32gui.GetForegroundWindow() == joy and not is_minimized(window.hwnd):
                win32gui.SetForegroundWindow(int(window.hwnd))
        except Exception:
            pass

    def _z_order_above_wechat(self) -> None:
        """把侧窗贴到微信正下方（并排可见、盖过 Cursor，微信仍保持最顶）。

        不做 AttachThreadInput：挂接前台线程容易扰乱「任务栏再点=最小化」。
        标题栏最小化是直接缩窗；任务栏是切换逻辑，焦点/线程一乱就会先闪再缩。
        """
        if not self.is_dock_mode() or self._dock_collapsed or self._dock_hidden_for_wechat:
            return
        if not self.isVisible():
            return
        window = self._selected_window()
        if window is None or is_minimized(window.hwnd):
            return
        try:
            import win32con
            import win32gui

            joy = int(self.winId())
            wx = int(window.hwnd)
            if not win32gui.IsWindow(joy) or not win32gui.IsWindow(wx):
                return
            if self._joy_stacked_under_wechat(joy, wx):
                return

            flags = (
                win32con.SWP_NOMOVE
                | win32con.SWP_NOSIZE
                | win32con.SWP_NOACTIVATE
            )
            # hwndInsertAfter=微信 → Joy 在微信下方（仍高于 Cursor 等）
            win32gui.SetWindowPos(joy, wx, 0, 0, 0, 0, flags)
            if self._collapse_btn.isVisible() and not self._dock_collapsed:
                self._collapse_btn.pin_above_hwnd(joy)
        except Exception:
            pass

    def _schedule_z_raise_burst(self) -> None:
        """微信刚回前台：有限次贴层；若期间微信已最小化则自动作废。"""
        self._z_raise_gen += 1
        gen = self._z_raise_gen
        self._z_order_above_wechat()

        def _shot(g: int = gen) -> None:
            if g != self._z_raise_gen:
                return
            window = self._selected_window()
            if window is None or is_minimized(window.hwnd):
                return
            if self._dock_collapsed or self._dock_hidden_for_wechat:
                return
            if not self.isVisible():
                return
            self._z_order_above_wechat()

        # 只补两拍，尽快停手，少干扰随后的任务栏最小化
        for ms in (80, 220):
            QTimer.singleShot(ms, _shot)

    def _sync_dock_z_with_wechat(self, window: WindowInfo) -> None:
        """仅在微信「刚回到前台」时贴一次侧窗图层；稳态不再改 Z 序。"""
        if not self.is_dock_mode() or self._dock_collapsed:
            self._wechat_fg_latched = False
            return
        if self._dock_animating or self._dock_fly_anim is not None:
            return
        if is_minimized(window.hwnd):
            self._z_raise_gen += 1
            self._wechat_fg_latched = False
            return
        if self._dock_hidden_for_wechat or not self.isVisible():
            return

        wx_fg = is_hwnd_foreground(window.hwnd)
        if not wx_fg:
            # 焦点不在微信（在 Cursor / 侧窗等）→ 允许下次切回时再贴层
            self._wechat_fg_latched = False
            return

        # 关键：只在升起沿贴层；前台期间绝不周期性 SetWindowPos
        if not self._wechat_fg_latched:
            self._wechat_fg_latched = True
            self._schedule_z_raise_burst()

    def _collapse_dock(self) -> None:
        """侧窗收起：向微信方向平移滑入（图层在微信下），结束后隐藏。"""
        if not self.is_dock_mode():
            return
        if self._dock_fly_anim is not None or self._dock_animating:
            return
        self._dock_collapsed = True
        self._dock_hidden_for_wechat = False
        self._hide_collapse_btn()
        self._stop_contact_watch()

        start = QPoint(self.pos())
        self._pre_collapse_pos = QPoint(start)
        side = self._joy_dock_side()
        self._pre_collapse_side = side
        end = self._slide_into_wechat_pos(start, side)

        # 先压到微信下面，再滑入，看起来像钻进微信
        self._z_order_under_wechat()

        def _done() -> None:
            self.setWindowOpacity(1.0)
            self.hide()
            # 复位到展开位，避免下次 show 闪在微信内侧（只用 move，不 setGeometry）
            if self._pre_collapse_pos is not None:
                self.move(self._pre_collapse_pos)
            self._show_wake_btn()

        self._run_dock_slide_anim(
            start, end, on_done=_done, duration_ms=220, ease_in=True, under_wechat=True
        )

    def changeEvent(self, event) -> None:  # noqa: N802
        super().changeEvent(event)
        if event.type() != QEvent.Type.WindowStateChange:
            return
        if not self.is_dock_mode():
            return
        if not bool(getattr(self.cfg.ui, "auto_collapse_on_minimize", True)):
            return
        if self._dock_collapsed or self._redirecting_minimize:
            return
        if not (self.windowState() & Qt.WindowState.WindowMinimized):
            return
        # 用户点最小化 / 系统调度最小化 → 改收起为微信顶栏 △
        self._redirecting_minimize = True
        QTimer.singleShot(0, self._redirect_minimize_to_collapse)

    def _redirect_minimize_to_collapse(self) -> None:
        try:
            if not self.is_dock_mode():
                return
            if not bool(getattr(self.cfg.ui, "auto_collapse_on_minimize", True)):
                return
            if self._dock_collapsed:
                return
            # 清掉最小化态，再走收起动画（否则会困在任务栏）
            st = self.windowState()
            if st & Qt.WindowState.WindowMinimized:
                self.setWindowState(st & ~Qt.WindowState.WindowMinimized)
            if not self.isVisible():
                self.show()
            self._collapse_dock()
        finally:
            self._redirecting_minimize = False

    def _expand_from_wake(self) -> None:
        """微信标题栏 △ → 从微信内侧（下层）平移滑出副驾。"""
        if self._dock_fly_anim is not None or self._dock_animating:
            return
        self._dock_collapsed = False
        self._hide_wake_btn()
        if not self.is_dock_mode():
            self.setWindowOpacity(1.0)
            self.show()
            self.raise_()
            return
        window = self._selected_window()
        if window is not None and is_minimized(window.hwnd):
            restore_window(window.hwnd)

        # 目标位置：收起前的 pos；没有则先贴边再读 pos
        end = (
            QPoint(self._pre_collapse_pos)
            if self._pre_collapse_pos is not None
            else None
        )
        if end is None:
            self.setWindowOpacity(0.0)
            self.show()
            self._dock_to_wechat()
            end = QPoint(self.pos())
            self.hide()
            self.setWindowOpacity(1.0)

        side = self._pre_collapse_side or self._joy_dock_side()
        start = self._slide_into_wechat_pos(end, side)

        self.setWindowOpacity(1.0)
        self.move(start)
        self.show()
        # 滑出过程保持在微信下层，结束再抬起
        self._z_order_under_wechat()

        def _done() -> None:
            self._dock_to_wechat()
            self._z_order_above_wechat()
            self._show_collapse_btn()
            self._start_contact_watch()
            QTimer.singleShot(200, lambda: self._refresh_contact_title(force=True))

        self._run_dock_slide_anim(
            start, end, on_done=_done, duration_ms=240, ease_in=False, under_wechat=True
        )

    def _show_wake_btn(self) -> None:
        window = self._selected_window()
        if window is None or is_minimized(window.hwnd):
            self._wake_btn.hide()
            return
        self._wake_btn.follow_wechat(window.hwnd)
        self._wake_btn.pin_above_hwnd(int(window.hwnd))

    def _hide_wake_btn(self) -> None:
        self._wake_btn.hide()

    def _show_collapse_btn(self) -> None:
        if not self.is_dock_mode() or self._dock_collapsed or not self.isVisible():
            self._collapse_btn.hide()
            return
        if self._dock_fly_anim is not None:
            return
        self._collapse_btn.follow_host(self)
        try:
            self._collapse_btn.pin_above_hwnd(int(self.winId()))
        except Exception:
            pass

    def _hide_collapse_btn(self) -> None:
        self._collapse_btn.hide()

    def _on_auto_toggled(self, on: bool) -> None:
        self._cancel_drive_commit()
        self.cfg.ui.auto_mode = bool(on)
        try:
            self.cfg.save()
        except Exception:
            pass
        if on:
            # 开启后先抓基准图，不立刻分析；之后相对基准有变化再 settle
            self._arm_auto_observe()
            self._ensure_preview()
            if self.btn_pause.isChecked():
                self.btn_pause.setChecked(False)
                self._on_pause()
            self._show_center_toast("自动驾驶已开启\n已记录当前画面，有新消息再分析")
        else:
            self._auto_settle.stop()
            self._show_center_toast("自动驾驶已关闭")
        self._sync_drive_mode_badge()

    def _arm_auto_observe(self, *, delay_sec: float = 2.5) -> None:
        """武装观察期：抓当前画面为基准，期内/相对基准无变化不分析。"""
        self._auto_armed_at = time.monotonic() + max(0.5, float(delay_sec))
        self._auto_settle.stop()
        self._request_auto_baseline_seed()
        # 预览线程可能稍后才起来，再补一次种子
        QTimer.singleShot(400, self._request_auto_baseline_seed)
        QTimer.singleShot(
            int(max(0.5, float(delay_sec)) * 1000) + 100,
            self._request_auto_baseline_seed,
        )
        log.info("auto armed %.1fs — baseline only, no analyze until new change", delay_sec)

    def _request_auto_baseline_seed(self) -> None:
        if not getattr(self, "btn_auto", None) or not self.btn_auto.isChecked():
            return
        worker = getattr(self, "_worker", None)
        if worker is None or not worker.isRunning():
            return
        try:
            worker.request_baseline_seed()
        except Exception as exc:
            log.debug("baseline seed request failed: %s", exc)

    def _bump_auto_cooldown(self, *, seconds: float | None = None) -> None:
        sec = float(seconds if seconds is not None else self.cfg.ui.auto_cooldown_sec)
        self._auto_cooldown_until = time.monotonic() + max(0.5, sec)

    def _note_auto_screen_change(self) -> None:
        """本地差分发现画面变化：等稳定后再自动分析。"""
        if not self.btn_auto.isChecked():
            return
        if time.monotonic() < self._auto_armed_at:
            log.debug("auto change ignored (still arming / seeding baseline)")
            return
        if self.btn_pause.isChecked():
            return
        # Generation Version：分析进行中又有新画面 → 作废在途结果，稍后重跑
        if self._analyze and self._analyze.isRunning():
            self._analyze_epoch = int(getattr(self, "_analyze_epoch", 0)) + 1
            try:
                self._analyze.cancel()
            except Exception:
                pass
            log.info("auto: invalidate in-flight analyze (new screen change)")
        settle = max(400, int(self.cfg.ui.auto_settle_ms))
        self._auto_settle.start(settle)
        log.debug("auto_settle armed %dms", settle)

    def _on_auto_settle(self) -> None:
        if not self.btn_auto.isChecked():
            return
        if self.btn_pause.isChecked():
            return
        if self._analyze and self._analyze.isRunning():
            # 稍后再试：作废已在 _note 里完成；等旧线程结束
            self._auto_settle.start(400)
            return
        now = time.monotonic()
        if now < self._auto_armed_at:
            return
        if now < self._auto_cooldown_until:
            # 冷却期内画面已变：到期后立刻再判一轮，避免错过新消息
            remain_ms = int((self._auto_cooldown_until - now) * 1000) + 50
            self._auto_settle.start(max(50, remain_ms))
            return
        window = self._selected_window()
        if window is None or is_minimized(window.hwnd):
            return
        t0 = time.perf_counter()
        # 本地门禁：最新像「我」则跳过 vision
        if bool(getattr(self.cfg.ui, "ocr_gate_enabled", True)):
            if self._ocr_gate_should_skip(window):
                log.info(
                    "auto_settle → skip (ocr) gate=%.0fms",
                    (time.perf_counter() - t0) * 1000,
                )
                return
        log.info(
            "auto_settle → analyze (gate=%.0fms)",
            (time.perf_counter() - t0) * 1000,
        )
        self.run_analyze(auto=True)

    def _ocr_gate_should_skip(self, window: object) -> bool:
        """True = 静默跳过本轮自动分析。

        法则「左右栏谁更靠下」：
          1) 绿/灰像素行比高低（快，抗长气泡碎块）
          2) OCR 左右文字底边比高低（气泡并列/找不到时）
        冲突：气泡 other + OCR lower_right(me) → 信 OCR（绿被裁时）；
              气泡 other + OCR only_right(me) → 信气泡（OCR 漏读左侧）。
        """
        try:
            from joy.capture.grabber import FrameGrabber
            from joy.capture.ocr_gate import probe_latest_side
            from joy.fill.bubble_locate import latest_message_side

            grabber = FrameGrabber(self.cfg.capture)
            attempt = grabber.grab_with_reason(
                window,  # type: ignore[arg-type]
                want_jpeg=False,
                force=True,
                auto_restore=False,
                crop=False,
                resize=True,
            )
            frame = attempt.frame
            if frame is None or frame.rgb is None:
                return False
            layout = getattr(self, "_wechat_layout", None)
            compose_top = None
            msg_top = None
            if layout is not None:
                try:
                    compose_top = float(layout.compose_area.top)
                    if layout.message_area is not None:
                        msg_top = float(layout.message_area.top)
                except Exception:
                    pass
            left = float(getattr(self.cfg.capture.crop, "left", 0.22) or 0.22)
            header = float(msg_top) if msg_top is not None else 0.10
            compose = float(compose_top) if compose_top is not None else 0.78
            h = max(1.0, float(frame.rgb.shape[0]))

            bub = latest_message_side(
                frame.rgb,
                contact_left_ratio=left,
                compose_top_ratio=compose,
                header_bottom_ratio=header,
            )
            if bub is not None and bub[0] == "me":
                log.info(
                    "ocr_gate skip: latest=me (bubble y1=%.3f)",
                    bub[1].y1 / h,
                )
                return True

            gate = probe_latest_side(
                frame.rgb,
                compose_top_ratio=compose_top,
                message_top_ratio=msg_top,
                contact_left_ratio=left,
            )
            if gate.reason == "blank_chat":
                log.info("ocr_gate skip: blank_chat")
                return True

            bub_side = bub[0] if bub is not None else None
            # 两栏都读到且右栏更低 → 自己最新
            if gate.side == "me" and gate.reason == "lower_right":
                log.info(
                    "ocr_gate skip: latest=me (%s; bubble=%s)",
                    gate.reason,
                    bub_side or "none",
                )
                return True
            # OCR 只有右栏：若气泡明确 other，多半漏读了左侧 → 放行
            if gate.side == "me" and gate.reason == "only_right":
                if bub_side == "other":
                    log.info(
                        "ocr_gate pass: side=other (bubble; ocr only_right ignored)"
                    )
                    return False
                log.info("ocr_gate skip: latest=me (%s)", gate.reason)
                return True
            if gate.side == "me":
                log.info(
                    "ocr_gate skip: latest=me (%s; bubble=%s)",
                    gate.reason,
                    bub_side or "none",
                )
                return True
            if bub_side == "other":
                log.info(
                    "ocr_gate pass: side=other (bubble y1=%.3f)",
                    bub[1].y1 / h,
                )
                return False
            if gate.side == "other":
                log.info("ocr_gate pass: side=other (%s)", gate.reason)
                return False
            log.info(
                "ocr_gate pass: side=%s (bubble=%s ocr=%s)",
                gate.side,
                bub_side or "none",
                gate.reason,
            )
            return False
        except Exception as exc:
            log.warning("ocr_gate error → fallback analyze: %s", exc)
            return False

    def _strip_caption_icon(self) -> None:
        """去掉标题栏左上角图标（Win WM_SETICON）。"""
        if not self.is_dock_mode():
            return
        try:
            import win32con
            import win32gui

            hwnd = int(self.winId())
            if not win32gui.IsWindow(hwnd):
                return
            win32gui.SendMessage(hwnd, win32con.WM_SETICON, win32con.ICON_SMALL, 0)
            win32gui.SendMessage(hwnd, win32con.WM_SETICON, win32con.ICON_BIG, 0)
        except Exception:
            pass

    def _apply_caption_glass(self, *, dock: bool) -> None:
        """侧窗：系统标题栏着色贴近仿玻璃灰；全视恢复默认。不改 HWND/贴边。"""
        try:
            import ctypes
            from ctypes import wintypes

            hwnd = int(self.winId())
            if hwnd <= 0:
                return
            # Win11：DWMWA_BORDER_COLOR=34 / CAPTION_COLOR=35；COLOR_DEFAULT=0xFFFFFFFF
            DWMWA_BORDER_COLOR = 34
            DWMWA_CAPTION_COLOR = 35
            DWMWA_COLOR_DEFAULT = 0xFFFFFFFF
            DWMWA_SYSTEMBACKDROP_TYPE = 38
            DWMSBT_NONE = 1
            # 与层1基色 rgb(227,227,232) 一致（COLORREF = 0x00BBGGRR）
            glass = 0x00E8E3E3
            value = ctypes.c_uint32(glass if dock else DWMWA_COLOR_DEFAULT)
            dwm = ctypes.windll.dwmapi
            # 关掉上一版误开的亚克力，避免标题栏发红/发透
            try:
                backdrop = ctypes.c_int(DWMSBT_NONE)
                dwm.DwmSetWindowAttribute(
                    wintypes.HWND(hwnd),
                    DWMWA_SYSTEMBACKDROP_TYPE,
                    ctypes.byref(backdrop),
                    ctypes.sizeof(backdrop),
                )
            except Exception:
                pass
            for attr in (DWMWA_CAPTION_COLOR, DWMWA_BORDER_COLOR):
                dwm.DwmSetWindowAttribute(
                    wintypes.HWND(hwnd),
                    attr,
                    ctypes.byref(value),
                    ctypes.sizeof(value),
                )
        except Exception:
            pass

    def _apply_caption_branding(self, *, dock: bool) -> None:
        """侧窗：隐藏标题文字与标题栏图标；全视：恢复正常标题。

        注意：不可把窗口图标设成透明——Win 任务栏会跟着变空白。
        侧窗用 WM_SETICON 清标题栏图标，任务栏回退到 exe / 应用图标。
        """
        from joy.app.branding import load_app_icon

        self.setWindowIcon(load_app_icon())
        if dock:
            self.setWindowTitle("")
            QTimer.singleShot(0, self._strip_caption_icon)
            QTimer.singleShot(80, self._strip_caption_icon)
            self._apply_caption_glass(dock=True)
            QTimer.singleShot(0, lambda: self._apply_caption_glass(dock=True))
            QTimer.singleShot(100, lambda: self._apply_caption_glass(dock=True))
        else:
            self.setWindowTitle("Joy 看屏副驾")
            self._apply_caption_glass(dock=False)

    def _apply_mid_side_insets(self, *, dock: bool) -> None:
        """侧窗：横线/输入框贴第二层左右边；正文另留内边距。全视恢复统一外边距。"""
        side = 10
        gutter = (
            int(WeChatHoverScrollArea._THUMB_W)
            + int(WeChatHoverScrollArea._RIGHT_GAP)
            + 4
        )
        if dock:
            # 底边距：输入框底边与白卡片底边的间距
            self._mid_lay.setContentsMargins(0, 0, 0, 8)
            self.toolbar_lay.setContentsMargins(side, 0, side, 0)
            if self._auto_lay is not None:
                self._auto_lay.setContentsMargins(side, 0, side, 8)
            if self._detail_head is not None:
                self._detail_head.setContentsMargins(side, 0, side, 0)
            self.actions_host.setContentsMargins(side, 0, gutter, 0)
            if self.progress_host.layout() is not None:
                self.progress_host.layout().setContentsMargins(side, 0, side, 0)
            self.status.setContentsMargins(side, 0, side, 0)
            # 输入框左右 4px，与第二层边留缝；顶部分隔线仍通栏
            if self._compose_shell_lay is not None:
                self._compose_shell_lay.setContentsMargins(4, 0, 4, 0)
        else:
            self._mid_lay.setContentsMargins(16, 14, 16, 8)
            self.toolbar_lay.setContentsMargins(0, 0, 0, 0)
            if self._auto_lay is not None:
                self._auto_lay.setContentsMargins(0, 0, 0, 8)
            if self._detail_head is not None:
                self._detail_head.setContentsMargins(0, 0, 0, 0)
            self.actions_host.setContentsMargins(0, 0, gutter, 0)
            if self.progress_host.layout() is not None:
                self.progress_host.layout().setContentsMargins(0, 0, 0, 0)
            self.status.setContentsMargins(0, 0, 0, 0)
            if self._compose_shell_lay is not None:
                self._compose_shell_lay.setContentsMargins(0, 0, 0, 0)

    def _apply_shell_chrome(self, *, dock: bool) -> None:
        """切换层1玻璃壳 / 层2白卡片外观（不改 HWND、不改贴边数学）。"""
        # 不用 WA_TranslucentBackground：半透明在带标题栏的侧窗上常渲成黑底
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, False)
        self.setAttribute(Qt.WidgetAttribute.WA_NoSystemBackground, False)
        self.setAutoFillBackground(False)
        self.setProperty("dockShell", "true" if dock else "false")
        self.dock_mid_card.setProperty("flat", "false" if dock else "true")
        for w in (self, self.dock_mid_card):
            w.style().unpolish(w)
            w.style().polish(w)
            w.update()

    def _apply_display_mode(self, *, persist: bool) -> None:
        dock = self.is_dock_mode()
        self._mode_dock = bool(dock)
        self._apply_caption_branding(dock=dock)
        self._apply_shell_chrome(dock=dock)
        was_visible = self.isVisible()
        self._rebuild_toolbar(dock=dock)

        # 截图区：侧窗可开关预览带；全视始终显示
        self._apply_dock_preview_visibility()
        if dock:
            self._root_lay.setContentsMargins(4, 2, 4, COMPOSE_BOTTOM_GAP_PX)
            self._root_lay.setSpacing(0)
            self._mid_lay.setSpacing(0)
            self._apply_mid_side_insets(dock=True)
            # 顶栏再略收 2px（小步调）
            self.toolbar_host.setFixedHeight(46)
            if self._sep_lay is not None:
                self._sep_lay.setContentsMargins(0, 0, 0, 4)
            self.sep_host.setVisible(True)
            self.top_sep.setVisible(True)
            self.btn_analyze.setText("立即分析")
            self.setMinimumSize(280, 240)
            self.setMaximumHeight(16777215)
            if self._normal_geo is None and was_visible:
                g = self.geometry()
                self._normal_geo = (g.x(), g.y(), g.width(), g.height())
            elif self._normal_geo is None:
                self._normal_geo = (80, 80, 780, 640)
            # 侧窗只跟随微信，不置顶；最大化按钮保留但 fixed size 下不可用
            flags = self._normal_window_flags()
            if self.windowFlags() != flags:
                self.setWindowFlags(flags)
                if was_visible:
                    self.show()
                # 重建 HWND 后标题/图标会回来，再清一次
                self._apply_caption_branding(dock=True)
            # 切模式后合并贴边，避免连打闪影
            self._schedule_dock_to_wechat(0)
            self._schedule_dock_to_wechat(120)
            self._dock_follow.start()
            self._dock_vis_timer.start()
            self._dock_z_timer.start()
            self._wechat_min_latched = None
            self._start_contact_watch()
            QTimer.singleShot(120, self._show_collapse_btn)
            QTimer.singleShot(200, lambda: self._ensure_wechat_layout(force=True))
            QTimer.singleShot(350, lambda: self._refresh_contact_title(force=True))
        else:
            self.image.setMinimumHeight(220)
            self._root_lay.setContentsMargins(0, 0, 0, 0)
            self._root_lay.setSpacing(0)
            self._mid_lay.setSpacing(12)
            self._apply_mid_side_insets(dock=False)
            self.toolbar_host.setMinimumHeight(0)
            self.toolbar_host.setMaximumHeight(16777215)
            if self._sep_lay is not None:
                self._sep_lay.setContentsMargins(0, 4, 0, 8)
            self.sep_host.setVisible(True)
            self.top_sep.setVisible(True)
            self.btn_analyze.setText("立即分析")
            self.setMinimumSize(780, 640)
            self.setMaximumSize(16777215, 16777215)
            # 解除小窗 setFixedSize
            self.setMinimumHeight(640)
            self.setMaximumHeight(16777215)
            self.setMinimumWidth(780)
            self.setMaximumWidth(16777215)
            self._reset_compose_height_normal()
            self._dock_follow.stop()
            self._dock_vis_timer.stop()
            self._dock_z_timer.stop()
            self._wechat_min_latched = None
            self._stop_contact_watch()
            self._dock_hidden_for_wechat = False
            self._dock_collapsed = False
            self._hide_wake_btn()
            self._hide_collapse_btn()
            flags = self._normal_window_flags()
            if persist and self.windowFlags() != flags:
                self.setWindowFlags(flags)
                if was_visible:
                    self.show()
            if self._normal_geo and persist:
                x, y, w, h = self._normal_geo
                self.setGeometry(x, y, w, h)
            elif persist:
                self.resize(780, 640)

        if self._last_result is not None:
            self._rebuild_candidate_actions(self._last_result)

        if persist:
            self.cfg.ui.display_mode = "dock" if dock else "normal"
            try:
                self.cfg.save()
            except Exception:
                pass
        self._sync_dock_slot_visuals()

    def _screen_dpr_for_hwnd(self, hwnd: int) -> float:
        # 取 Win32 / Qt 中较大者，避免任一路径偶发落到 1.0 把物理像素当逻辑用
        win_dpr = hwnd_dpi_scale(hwnd)
        qt_dpr = 0.0
        rect = window_screen_rect(hwnd)
        if rect:
            own = max(float(self.devicePixelRatioF()), 1.0)
            cx = (rect[0] + rect[2]) // 2
            cy = (rect[1] + rect[3]) // 2
            screen = QGuiApplication.screenAt(QPoint(int(cx / own), int(cy / own)))
            if screen is None:
                screen = QGuiApplication.screenAt(QPoint(cx, cy))
        else:
            screen = QGuiApplication.primaryScreen()
        if screen is not None:
            qt_dpr = float(screen.devicePixelRatio())
        own_dpr = float(self.devicePixelRatioF() or 1.0)
        cands = [d for d in (win_dpr, qt_dpr, own_dpr) if d is not None and d >= 1.0]
        return max(cands) if cands else 1.0

    def _work_area_physical(self, hwnd: int) -> tuple[int, int, int, int]:
        """工作区，物理像素（与 GetWindowRect 一致）。"""
        dpr = self._screen_dpr_for_hwnd(hwnd)
        rect = window_screen_rect(hwnd)
        if rect:
            cx = int(((rect[0] + rect[2]) / 2) / dpr)
            cy = int(((rect[1] + rect[3]) / 2) / dpr)
            screen = QGuiApplication.screenAt(QPoint(cx, cy))
        else:
            screen = None
        if screen is None:
            screen = QGuiApplication.primaryScreen()
        g = screen.availableGeometry() if screen else None
        if g is None:
            return 0, 0, 1920, 1080
        # Qt availableGeometry 是逻辑像素 → 转物理
        return (
            int(g.x() * dpr),
            int(g.y() * dpr),
            int((g.x() + g.width()) * dpr),
            int((g.y() + g.height()) * dpr),
        )

    def _dock_scale_for_wechat(
        self, hwnd: int, wr: tuple[int, int, int, int]
    ) -> tuple[tuple[int, int, int, int], float, str]:
        """校正微信外框 + 贴边除数。返回 (rect, scale, reason)。"""
        dpr = self._screen_dpr_for_hwnd(hwnd) or 1.0
        dpr = max(float(dpr), float(self.devicePixelRatioF() or 1.0), 1.0)
        scr_h = None
        scr = self.screen() or QGuiApplication.primaryScreen()
        if scr is not None:
            scr_h = int(scr.availableGeometry().height())
        rect, scale, why = resolve_dock_anchor(
            wr=wr, dpr=dpr, screen_logical_h=scr_h
        )
        # 物理外框很高却被判成 1.0 时，仍按真实 DPR
        if scale < 1.5 and (rect[3] - rect[1]) >= 1100 and why != "inflate":
            scale = max(dpr, 2.0)
            why = "physical-tall"
        return rect, float(scale), why

    def _work_area_for_dock(
        self, hwnd: int, scale: float
    ) -> tuple[int, int, int, int]:
        """与 window_screen_rect 同单位的工作区。"""
        if scale >= 1.5:
            return self._work_area_physical(hwnd)
        # scale≈1：Win32 矩形已与 Qt 逻辑同空间，工作区不要再 *dpr
        rect = window_screen_rect(hwnd)
        dpr = max(self._screen_dpr_for_hwnd(hwnd) or 1.0, 1.0)
        if rect:
            cx = int((rect[0] + rect[2]) / 2)
            cy = int((rect[1] + rect[3]) / 2)
            # 若 rect 实为物理，中心先试 /dpr 再试原值
            screen = QGuiApplication.screenAt(QPoint(int(cx / dpr), int(cy / dpr)))
            if screen is None:
                screen = QGuiApplication.screenAt(QPoint(cx, cy))
        else:
            screen = QGuiApplication.primaryScreen()
        g = screen.availableGeometry() if screen else None
        if g is None:
            return 0, 0, 1920, 1080
        return int(g.x()), int(g.y()), int(g.x() + g.width()), int(g.y() + g.height())

    def _sync_visibility_with_wechat(self, window: WindowInfo) -> bool:
        """
        侧窗显示跟随微信主窗：最小化→隐藏；调出→再显示。
        用户主动收起时：不自动展开侧窗，只维护微信标题栏 △。
        返回 True 表示当前应对齐贴边；False 表示无需再算侧窗几何。
        """
        if is_minimized(window.hwnd):
            self._dock_hidden_for_wechat = True
            self._z_raise_gen += 1  # 作废抬层定时器
            if self.isVisible():
                self.hide()
            self._hide_wake_btn()
            self._hide_collapse_btn()
            self._flash_status("")
            return False

        # 用户收起：微信可见时只显示 △，侧窗保持隐藏
        if self._dock_collapsed:
            self._dock_hidden_for_wechat = False
            if self.isVisible():
                self.hide()
            self._hide_collapse_btn()
            self._show_wake_btn()
            return False

        if self._dock_hidden_for_wechat:
            self._dock_hidden_for_wechat = False
            self._hide_wake_btn()
            # 不抢焦点；尽快贴边，后续再校正还原动画
            self._show_dock_noactivate()
            self._schedule_dock_to_wechat(0)
            self._schedule_dock_to_wechat(160)
            QTimer.singleShot(40, self._show_collapse_btn)
            return True

        self._hide_wake_btn()
        if self.isVisible():
            self._show_collapse_btn()
        else:
            self._hide_collapse_btn()
        return self.isVisible()

    def showEvent(self, event) -> None:  # noqa: N802
        super().showEvent(event)
        # 侧窗每次露出都清掉标题栏 logo/文字（show 后 HWND 才稳定）
        if self.is_dock_mode():
            self._apply_caption_branding(dock=True)
            self._apply_shell_chrome(dock=True)
            QTimer.singleShot(0, self._ensure_hidden_if_wechat_minimized)
            # show 后合并贴边，避免连打 setFixedSize 闪影
            self._schedule_dock_to_wechat(0)
            self._schedule_dock_to_wechat(160)

    def resizeEvent(self, event) -> None:  # noqa: N802
        super().resizeEvent(event)
        if self._candidate_btns:
            QTimer.singleShot(0, self._refit_all_candidate_bubbles)
        QTimer.singleShot(0, self._elide_chat_title)
        toast = getattr(self, "_center_toast", None)
        if toast is not None and toast.isVisible():
            toast._reposition()
        coach = getattr(self, "_coach_overlay", None)
        if coach is not None and coach.is_active():
            coach.relayout()

    def _ensure_hidden_if_wechat_minimized(self) -> None:
        if not self.is_dock_mode():
            return
        window = self._selected_window()
        if window is None:
            return
        if is_minimized(window.hwnd):
            self._dock_hidden_for_wechat = True
            if self.isVisible():
                self.hide()

    def sync_after_open(self) -> None:
        """设置页打开副驾后调用：侧窗模式下按微信状态决定显示/贴边。"""
        if not self.is_dock_mode():
            return
        window = self._selected_window()
        if window is None:
            self.refresh_windows()
            window = self._selected_window()
        if window is None:
            return
        if is_minimized(window.hwnd):
            self._dock_hidden_for_wechat = True
            self.hide()
            return
        self._schedule_dock_to_wechat(0)
        self._schedule_dock_to_wechat(140)
        # 像素布局很快：每次打开副驾都重算一遍，不吃磁盘旧缓存
        QTimer.singleShot(280, lambda: self._ensure_wechat_layout(force=True))
        QTimer.singleShot(400, lambda: self._refresh_contact_title(force=True))
        self._start_contact_watch()
        # 贴边/布局落稳后再弹首次动态指引，避免和 setFixedSize 抢几何
        QTimer.singleShot(1200, lambda: self._maybe_start_coach_tour(force=False))

    def _wechat_client_logical(self, hwnd: int) -> tuple[int, int, float, float] | None:
        """返回 (client_w, client_h, dpr, client_top) 逻辑像素。"""
        cr = client_rect_screen(hwnd)
        if cr is None:
            return None
        dpr = self._screen_dpr_for_hwnd(hwnd) or 1.0
        phys_w = max(1, cr[2] - cr[0])
        phys_h = max(1, cr[3] - cr[1])
        top = float(cr[1]) / dpr
        return int(round(phys_w / dpr)), int(round(phys_h / dpr)), float(dpr), top

    def _reset_compose_height_normal(self) -> None:
        """全视模式：解除侧窗固定高度。"""
        self.compose_box.setMinimumHeight(100)
        self.compose_box.setMaximumHeight(16777215)

    def _apply_layout_to_compose(
        self,
        layout: WeChatLayout | None,
        client_h: float,
        *,
        client_top: float | None = None,
    ) -> None:
        if not self.is_dock_mode():
            self._reset_compose_height_normal()
            return
        joy_h = float(max(180, self.height()))
        # 侧窗底边与微信一致：离地空隙只靠根布局 margin，不从输入区高度里再扣
        bottom_inset = int(COMPOSE_BOTTOM_GAP_PX)
        if self._root_lay is not None:
            m = self._root_lay.contentsMargins()
            if m.bottom() != bottom_inset:
                self._root_lay.setContentsMargins(
                    m.left(), m.top(), m.right(), bottom_inset
                )

        mid_bot = 0
        if self._mid_lay is not None:
            mid_bot = int(self._mid_lay.contentsMargins().bottom())

        wx_top: float | None = None
        if layout is not None:
            span = float(layout.compose_area.bottom) - float(layout.compose_area.top)
            span = max(0.10, min(0.42, span))
            h = int(round(span * float(client_h)))
            if client_top is not None:
                wx_top = float(client_top) + float(layout.compose_area.top) * float(client_h)
                # 按「微信顶边 → 侧窗底边-内边距」定高，避免底边空隙把输入区顶高
                joy_bottom = float(self.mapToGlobal(QPoint(0, self.height())).y())
                h = compose_height_aligned(
                    layout,
                    wechat_client_top=float(client_top),
                    wechat_client_h=float(client_h),
                    joy_client_bottom=joy_bottom,
                    joy_h=joy_h,
                    bottom_inset=bottom_inset + mid_bot,
                )
        else:
            h = compose_height_px(layout, client_h, joy_h_logical=joy_h)

        h = int(max(72, h))
        cur_h = int(self.compose_box.height()) if self.compose_box is not None else 0
        # 高度未变则不反复 setFixedHeight / activate / repaint（跟随时每 400ms 会进来）
        if abs(h - cur_h) < 2 and self.compose_box.maximumHeight() == self.compose_box.minimumHeight() == cur_h:
            # 仍可能顶边微偏：仅在偏差大时纠一次
            if wx_top is not None:
                actual = float(self.compose_box.mapToGlobal(QPoint(0, 0)).y())
                if abs(float(wx_top) - actual) >= 3:
                    self._sync_compose_top_to_wechat(float(wx_top))
            return

        self.compose_draft.setMinimumHeight(0)
        self.compose_draft.setMaximumHeight(16777215)
        self.compose_box.setMinimumHeight(0)
        self.compose_box.setMaximumHeight(16777215)
        self.compose_box.setFixedHeight(h)
        self.compose_box.updateGeometry()
        lay = self.compose_box.layout()
        if lay is not None:
            lay.activate()
        root = self.layout()
        if root is not None:
            root.activate()

        # 几何落稳后再按屏幕顶边纠一次（避免写死 trim）
        if wx_top is not None:
            top = float(wx_top)
            QTimer.singleShot(0, lambda t=top: self._sync_compose_top_to_wechat(t))

        if layout is not None and layout.compose_input_center is not None:
            self.cfg.chat.fill_click_x_ratio = float(layout.compose_input_center.x)
            self.cfg.chat.fill_click_y_ratio = float(layout.compose_input_center.y)

    def _sync_compose_top_to_wechat(self, wx_compose_top: float) -> None:
        """按全局坐标把侧窗输入顶边对齐到微信输入顶（可增可减高度）。"""
        if not self.is_dock_mode() or not self.isVisible():
            return
        if self._dock_animating or self._dock_fly_anim is not None:
            return
        actual_top = float(self.compose_box.mapToGlobal(QPoint(0, 0)).y())
        # delta>0：侧窗顶偏上（更高）→ 减高；delta<0：顶偏下 → 加高
        delta = int(round(float(wx_compose_top) - actual_top))
        if abs(delta) < 3:
            return
        new_h = max(72, int(self.compose_box.height()) - delta)
        # 不超过侧窗客户区 45%
        new_h = min(new_h, max(72, int(self.height() * 0.45)))
        if abs(new_h - int(self.compose_box.height())) < 2:
            return
        self.compose_box.setFixedHeight(new_h)
        self.compose_box.updateGeometry()
        root = self.layout()
        if root is not None:
            root.activate()

    def _ensure_wechat_layout(self, *, force: bool = False) -> None:
        """侧窗布局：像素扫描为主。force=True 时忽略缓存，立刻重算。"""
        def _fail_cb(msg: str) -> None:
            if not force:
                return
            cb = self._layout_recal_cb
            self._layout_recal_cb = None
            if cb:
                cb(False, msg)

        if not self.is_dock_mode() or self._dock_collapsed:
            _fail_cb("请切换到侧窗模式并展开副驾")
            return
        if self._dock_animating or self._dock_fly_anim is not None:
            _fail_cb("侧窗动画中，请稍后重试")
            return
        window = self._selected_window()
        if window is None or is_minimized(window.hwnd):
            _fail_cb("未找到微信窗口或已最小化")
            return
        dims = self._wechat_client_logical(window.hwnd)
        if dims is None:
            _fail_cb("无法读取微信客户区尺寸")
            return
        cw, ch, dpr, ctop = dims
        fp = make_fingerprint(cw, ch, dpr)

        if not force:
            # 会话内同指纹：用内存结果即可（跟随时每 400ms 会走到这里）
            if fp == self._layout_fp and self._wechat_layout is not None:
                # 勿反复 setFixedHeight（会与顶边校准互抢 → 底部闪动）
                if ctop is not None:
                    lay = self._wechat_layout
                    wx_top = float(ctop) + float(lay.compose_area.top) * float(ch)
                    self._sync_compose_top_to_wechat(wx_top)
                return
            if (
                fp == self._layout_fail_fp
                and time.time() < self._layout_fail_until
            ):
                if ctop is not None and self._wechat_layout is not None:
                    lay = self._wechat_layout
                    wx_top = float(ctop) + float(lay.compose_area.top) * float(ch)
                    self._sync_compose_top_to_wechat(wx_top)
                else:
                    self._apply_layout_to_compose(
                        self._wechat_layout, ch, client_top=ctop
                    )
                return
            if self._layout_probe is not None and self._layout_probe.isRunning():
                return
            if (
                self._layout_probe_pending_fp == fp
                and self._layout_debounce is not None
                and self._layout_debounce.isActive()
            ):
                return

        # 先用已有/默认高度撑住，探测完成后覆盖
        self._apply_layout_to_compose(self._wechat_layout, ch, client_top=ctop)

        prev_pending = self._layout_probe_pending_fp
        self._layout_probe_pending_fp = fp
        if self._layout_debounce is None:
            self._layout_debounce = QTimer(self)
            self._layout_debounce.setSingleShot(True)
            self._layout_debounce.timeout.connect(self._start_layout_probe)
        # 像素扫描很快：启动/强制 50ms；尺寸变化稳定后再算 180ms
        if force:
            self._layout_debounce.start(50)
        elif self._layout_debounce.isActive() and prev_pending == fp:
            return
        else:
            self._layout_debounce.start(180)

    def _start_layout_probe(self) -> None:
        if not self.is_dock_mode() or self._dock_collapsed:
            cb = self._layout_recal_cb
            self._layout_recal_cb = None
            if cb:
                cb(False, "侧窗未展开")
            return
        if self._layout_probe is not None and self._layout_probe.isRunning():
            return
        window = self._selected_window()
        if window is None or is_minimized(window.hwnd):
            cb = self._layout_recal_cb
            self._layout_recal_cb = None
            if cb:
                cb(False, "未找到微信窗口或已最小化")
            return
        dims = self._wechat_client_logical(window.hwnd)
        if dims is None:
            cb = self._layout_recal_cb
            self._layout_recal_cb = None
            if cb:
                cb(False, "无法读取微信客户区尺寸")
            return
        cw, ch, dpr, _ctop = dims
        fp = make_fingerprint(cw, ch, dpr)
        # 防抖期间尺寸又变了：用最新指纹
        if self._layout_probe_pending_fp and fp != self._layout_probe_pending_fp:
            # 仍启动，以当前为准
            pass
        worker = LayoutProbeWorker(
            self.cfg, window, client_w=cw, client_h=ch, dpr=dpr, parent=self
        )
        worker.finished_ok.connect(self._on_layout_probe_ok)
        worker.finished_err.connect(self._on_layout_probe_err)
        self._layout_probe = worker
        worker.start()

    def _on_layout_probe_ok(self, layout: object) -> None:
        self._layout_probe = None
        if not isinstance(layout, WeChatLayout):
            cb = self._layout_recal_cb
            self._layout_recal_cb = None
            if cb:
                cb(False, "无效布局结果")
            return
        self._wechat_layout = layout
        self._layout_fp = layout.fingerprint
        self._layout_fail_fp = ""
        self._layout_fail_until = 0.0
        dims = None
        window = self._selected_window()
        if window is not None:
            dims = self._wechat_client_logical(window.hwnd)
        client_h = float(dims[1]) if dims else float(layout.client_h)
        client_top = float(dims[3]) if dims else None
        self._apply_layout_to_compose(layout, client_h, client_top=client_top)
        # 几何跟随可能刚改完窗高，下一拍再套一次高度，确保顶边对齐
        QTimer.singleShot(120, lambda: self._ensure_wechat_layout(force=False))
        cb = self._layout_recal_cb
        self._layout_recal_cb = None
        if cb:
            cb(True, "")

    def _on_layout_probe_err(self, err: str) -> None:
        self._layout_probe = None
        # 失败不覆盖旧缓存；用默认高度；同指纹冷却，避免状态栏闪烁
        window = self._selected_window()
        dims = self._wechat_client_logical(window.hwnd) if window else None
        client_h = float(dims[1]) if dims else 600.0
        client_top = float(dims[3]) if dims else None
        if dims is not None:
            self._layout_fail_fp = make_fingerprint(dims[0], dims[1], dims[2])
            self._layout_fail_until = time.time() + 90.0
            self._layout_probe_pending_fp = ""
        self._apply_layout_to_compose(self._wechat_layout, client_h, client_top=client_top)
        cb = self._layout_recal_cb
        self._layout_recal_cb = None
        if cb:
            cb(False, err)

    def bind_main_host(self, host: QWidget | None) -> None:
        """由设置主窗绑定，供「回到主界面」唤起。"""
        self._main_host = host

    def _on_back_to_main(self) -> None:
        host = self._main_host
        if host is None:
            # 兜底：在顶层窗口里找设置窗
            from PySide6.QtWidgets import QApplication

            app = QApplication.instance()
            if app is not None:
                for w in app.topLevelWidgets():
                    if w is self:
                        continue
                    if w.metaObject().className() == "SettingsWindow" or (
                        w.windowTitle() or ""
                    ).startswith("Joy"):
                        if hasattr(w, "_show_from_tray") or hasattr(w, "showNormal"):
                            host = w
                            break
        if host is None:
            QMessageBox.information(
                self,
                "回主界面",
                "未找到设置主界面。可从系统托盘图标打开「打开设置」。",
            )
            return
        show = getattr(host, "_show_from_tray", None)
        if callable(show):
            show()
        else:
            host.showNormal()
            host.raise_()
            host.activateWindow()

    def _on_recal_layout(self) -> None:
        """更多菜单：手动强制重构微信布局。"""
        if not self.is_dock_mode() or self._dock_collapsed:
            QMessageBox.information(
                self,
                "重构布局",
                "请先切换到侧窗模式并展开副驾，再点「重构布局」。",
            )
            return
        window = self._selected_window()
        if window is None or is_minimized(window.hwnd):
            QMessageBox.information(
                self,
                "重构布局",
                "请先选中可用的微信窗口（勿最小化）。",
            )
            return
        self.act_recal_layout.setEnabled(False)

        def _done(ok: bool, msg: str) -> None:
            self.act_recal_layout.setEnabled(True)
            if not ok:
                QMessageBox.warning(self, "重构布局失败", msg or "未知错误")

        self.request_layout_recalibrate(on_done=_done)

    def request_layout_recalibrate(self, on_done=None) -> None:
        """设置页 / 菜单：强制重新校准。on_done(ok: bool, msg: str)。"""
        clear_layout_cache()
        self._wechat_layout = None
        self._layout_fp = ""
        self._layout_probe_pending_fp = ""
        self._layout_fail_fp = ""
        self._layout_fail_until = 0.0
        self._layout_recal_cb = on_done
        if self._layout_debounce is not None:
            self._layout_debounce.stop()
        self._ensure_wechat_layout(force=True)

    def _phys_to_dock_logical(
        self, x: int, y: int, w: int, h: int, dpr: float
    ) -> tuple[int, int, int, int]:
        """贴边矩形 → Qt 逻辑外框。

        dpr 实为 dock_coord_scale 结果：可能是真实 DPR，也可能是 1.0
        （Win32 矩形已与 Qt 逻辑同空间）。不要再和 devicePixelRatio 取 max，
        否则会把已判定的 scale=1 又抬回 2，侧窗缩成一半。
        """
        scale = max(1.0, float(dpr or 1.0))
        # 宽度已是 2x 物理侧窗宽、却仍像 scale=1 → 强制按物理像素除
        if w >= 560 and scale < 1.5:
            scale = 2.0
        fx, fy = int(round(x / scale)), int(round(y / scale))
        fw, fh = max(260, int(round(w / scale))), max(200, int(round(h / scale)))
        # 侧窗宽度硬钳：只允许 260~400
        if fw > 400:
            fw = 320
        scr = self.screen() or QGuiApplication.primaryScreen()
        if scr is not None:
            ag = scr.availableGeometry()
            avail_h = ag.height()
            # availableGeometry 偶发给出物理高度
            if avail_h > 1100 and scale >= 1.5:
                avail_h = int(round(avail_h / scale))
            fh = max(200, min(fh, max(200, avail_h)))
        # 消 685↔1370 振荡：仅当「当前已是合理侧窗高」却突然要翻倍时才钳住。
        # 启动首帧常偏矮，必须允许拉高到微信高度，否则会锁死成小白条。
        if self.is_dock_mode() and self.isVisible() and self.height() >= 240:
            cur_h = self.frameGeometry().height()
            if (
                480 <= cur_h <= 900
                and fh >= int(cur_h * 1.8)
                and cur_h >= int(fh * 0.55)
            ):
                fh = cur_h
                fx, fy = self.frameGeometry().x(), self.frameGeometry().y()
        return fx, fy, fw, fh

    def _dock_to_wechat(self) -> None:
        """读取微信外框，贴右侧；外框高度对齐（扣除本窗标题栏增量）。"""
        if not self.is_dock_mode():
            return
        # 缩放动画期间禁止 setFixedSize，否则会和 geometry 动画叠出重影
        if self._dock_animating or self._dock_fly_anim is not None:
            return
        window = self._selected_window()
        if window is None:
            return
        if not self._sync_visibility_with_wechat(window):
            return
        wr_raw = window_screen_rect(window.hwnd)
        if wr_raw is None:
            return
        dpr = self._screen_dpr_for_hwnd(window.hwnd) or 1.0
        wr, scale, why = self._dock_scale_for_wechat(window.hwnd, wr_raw)
        work = self._work_area_for_dock(window.hwnd, scale)
        panel_w = int(320 * scale)
        geo = compute_dock_beside(
            window.hwnd,
            panel_w,
            None,
            gap=0,
            work=work,
            min_w=int(260 * scale),
            anchor_rect=wr,
        )
        if geo is None:
            return
        x, y, w, h = geo
        fx, fy, fw, fh = self._phys_to_dock_logical(x, y, w, h, scale)
        # 几何几乎不变则跳过，避免连打 setFixedSize 闪影
        if self.isVisible():
            cur = self.frameGeometry()
            if (
                abs(cur.x() - fx) <= 2
                and abs(cur.y() - fy) <= 2
                and abs(cur.width() - fw) <= 2
                and abs(cur.height() - fh) <= 2
            ):
                return
        log.info(
            "dock geo scale=%.2f dpr=%.2f why=%s raw_h=%s corr=%sx%s logical=%sx%s@%s,%s",
            scale,
            dpr,
            why,
            wr_raw[3] - wr_raw[1],
            wr[2] - wr[0],
            wr[3] - wr[1],
            fw,
            fh,
            fx,
            fy,
        )
        self._apply_frame_geometry(fx, fy, fw, fh)
        # inflate 校正框与真实 Win32 不同单位，贴缝追高会抖/裁切
        if why != "inflate":
            self._flush_to_wechat_visible(window.hwnd)
        self._show_collapse_btn()
        self._ensure_wechat_layout(force=False)

    def _schedule_dock_to_wechat(self, delay_ms: int = 0) -> None:
        """合并多次贴边请求，减少启动闪影。"""
        t = getattr(self, "_dock_geo_timer", None)
        if t is None:
            t = QTimer(self)
            t.setSingleShot(True)
            t.timeout.connect(self._dock_to_wechat)
            self._dock_geo_timer = t
        t.start(max(0, int(delay_ms)))

    def _flush_to_wechat_visible(self, wechat_hwnd: int) -> None:
        """按可见外框：左右贴缝、顶边对齐，并显式对齐底边。"""
        try:
            hwnd = int(self.winId())
        except Exception:
            return
        wr0 = window_screen_rect(wechat_hwnd)
        if wr0 is None:
            return
        wr, scale, _why = self._dock_scale_for_wechat(wechat_hwnd, wr0)
        al, at, ar, ab = wr

        def _once() -> None:
            b = window_screen_rect(hwnd)
            if b is None:
                return
            bl, bt, br, bb = b
            acx = (al + ar) / 2.0
            bcx = (bl + br) / 2.0
            if bcx >= acx:
                dx_p = ar - bl
            else:
                dx_p = al - br
            dx = int(round(dx_p / scale))
            dy = int(round((at - bt) / scale))
            if dx or dy:
                self.move(self.x() + dx, self.y() + dy)

            b2 = window_screen_rect(hwnd)
            if b2 is None:
                return
            bottom_slack_p = int(b2[3] - ab)
            if abs(bottom_slack_p) >= max(2, int(round(scale))):
                delta = int(round(bottom_slack_p / scale))
                nh = max(180, self.height() - delta)
                if nh > 1000 and scale >= 1.5:
                    nh = max(180, int(round(nh / scale)))
                if abs(nh - self.height()) >= 1:
                    self.setFixedSize(self.width(), nh)

            b3 = window_screen_rect(hwnd)
            if b3 is None:
                return
            dy2 = int(round((at - b3[1]) / scale))
            if dy2:
                self.move(self.x(), self.y() + dy2)
            b4 = window_screen_rect(hwnd)
            if b4 is None:
                return
            slack2 = int(b4[3] - ab)
            if abs(slack2) >= max(2, int(round(scale))):
                delta2 = int(round(slack2 / scale))
                nh2 = max(180, self.height() - delta2)
                if nh2 > 1000 and scale >= 1.5:
                    nh2 = max(180, int(round(nh2 / scale)))
                if abs(nh2 - self.height()) >= 1:
                    self.setFixedSize(self.width(), nh2)

        _once()
        _once()

    def _frame_chrome(self) -> tuple[int, int]:
        """返回 (左右边框合计, 上下边框合计) = 外框 - 客户区。"""
        # 未 show 时 frameGeometry 可能不准，给一个保守默认
        if not self.isVisible():
            return 0, 32
        fg = self.frameGeometry()
        g = self.geometry()
        cw = max(0, fg.width() - g.width())
        ch = max(0, fg.height() - g.height())
        # 异常值保护
        if ch > 80:
            ch = 32
        if cw > 40:
            cw = 16
        return cw, ch

    def _apply_frame_geometry(self, fx: int, fy: int, fw: int, fh: int) -> None:
        """
        使窗口「外框」落在 (fx,fy,fw,fh)。
        Qt setGeometry/setFixedHeight 作用在客户区，若直接用微信外框高会多出标题栏高度。
        高度以 DWM 可见外框为准，不再额外 -6（以前补偿 GetWindowRect 虚高）。
        """
        fh = max(180, fh)

        self.setMinimumSize(0, 0)
        self.setMaximumSize(16777215, 16777215)

        chrome_w, chrome_h = self._frame_chrome()
        client_w = max(240, fw - chrome_w)
        client_h = max(180, fh - chrome_h)

        self.resize(client_w, client_h)
        self.setFixedSize(client_w, client_h)

        fg = self.frameGeometry()
        self.move(self.x() + (fx - fg.x()), self.y() + (fy - fg.y()))

        # 迭代收紧：确保外框高度不超过目标（顶部对齐后只砍底边）
        for _ in range(4):
            chrome_w, chrome_h = self._frame_chrome()
            fg = self.frameGeometry()
            over_h = fg.height() - fh
            over_w = fg.width() - fw
            if over_h <= 0 and over_w <= 0:
                break
            nh = max(180, self.height() - max(0, over_h))
            nw = max(240, self.width() - max(0, over_w))
            self.setFixedSize(nw, nh)
            fg = self.frameGeometry()
            self.move(self.x() + (fx - fg.x()), self.y() + (fy - fg.y()))

    def _tick_dock_visibility(self) -> None:
        """高频显隐：微信最小化/呼出时尽快藏或露侧窗（不改几何、不扰 Z 序）。"""
        if not self.is_dock_mode():
            return
        if self._dock_animating or self._dock_fly_anim is not None:
            return
        window = self._selected_window()
        if window is None:
            return
        minimized = is_minimized(window.hwnd)
        # 状态未变则跳过，避免无意义 show/hide
        if self._wechat_min_latched is not None and self._wechat_min_latched == minimized:
            # 收起态仍要让 △ 跟着微信走
            if self._dock_collapsed and not minimized:
                self._show_wake_btn()
            return
        self._wechat_min_latched = minimized
        need_geo = self._sync_visibility_with_wechat(window)
        # 刚从最小化呼出：立刻贴边一拍，少等 400ms 几何轮询
        if need_geo and not minimized and self.isVisible():
            self._dock_to_wechat()

    def _tick_dock_z(self) -> None:
        """图层跟随：不碰几何，只处理微信回前台时贴侧窗。"""
        if not self.is_dock_mode() or self._dock_collapsed:
            return
        if self._dock_animating or self._dock_fly_anim is not None:
            return
        window = self._selected_window()
        if window is None:
            return
        self._sync_dock_z_with_wechat(window)

    def _follow_wechat_dock(self) -> None:
        if not self.is_dock_mode():
            return
        # 收起/展开缩放动画中：只维护 △，不要改侧窗几何
        if self._dock_animating or self._dock_fly_anim is not None:
            if self._dock_collapsed:
                window = self._selected_window()
                if window is not None and not is_minimized(window.hwnd):
                    self._wake_btn.follow_wechat(window.hwnd)
            return
        window = self._selected_window()
        if window is None:
            return
        # 显隐已由 _dock_vis_timer 高频处理；这里以几何跟随为主
        if not self._sync_visibility_with_wechat(window):
            return
        self._wechat_min_latched = False
        # 图层：跟随里也顺带看一眼升起沿（vis 定时器不改 Z）
        self._sync_dock_z_with_wechat(window)
        wr_raw = window_screen_rect(window.hwnd)
        if wr_raw is None:
            return
        wr, scale, why = self._dock_scale_for_wechat(window.hwnd, wr_raw)
        work = self._work_area_for_dock(window.hwnd, scale)
        # 跟随用当前外框宽，避免客户区宽被当成外框宽再放大
        fg = self.frameGeometry()
        logical_w = min(400, max(260, fg.width()))
        panel_w = int(logical_w * scale)
        geo = compute_dock_beside(
            window.hwnd,
            panel_w,
            None,
            gap=0,
            work=work,
            min_w=int(260 * scale),
            anchor_rect=wr,
        )
        if geo is None:
            return
        x, y, w, h = geo
        fx, fy, fw, fh = self._phys_to_dock_logical(x, y, w, h, scale)
        cur = self.frameGeometry()
        if (
            abs(cur.x() - fx) > 8
            or abs(cur.y() - fy) > 8
            or abs(cur.width() - fw) > 12
            or abs(cur.height() - fh) > 8
        ):
            self._apply_frame_geometry(fx, fy, fw, fh)
            if why != "inflate":
                self._flush_to_wechat_visible(window.hwnd)
        elif why != "inflate":
            self._flush_to_wechat_visible(window.hwnd)
        self._show_collapse_btn()
        self._ensure_wechat_layout(force=False)

    def _sync_detail_toggle_label(self) -> None:
        self.btn_detail.setText("对话详情")

    def _on_detail_badge_clicked(self) -> None:
        """compose 栏气泡：切换识别详情展开/折叠。"""
        btn = getattr(self, "btn_detail", None)
        if btn is None:
            return
        btn.setChecked(not bool(btn.isChecked()))

    def _on_detail_toggled(self, expanded: bool) -> None:
        self.result.setVisible(expanded)
        self._sync_detail_toggle_label()
        badge = getattr(self, "detail_badge", None)
        if badge is not None:
            badge.set_active(bool(expanded))
        act = getattr(self, "act_detail", None)
        if act is not None and bool(act.isChecked()) != bool(expanded):
            act.blockSignals(True)
            act.setChecked(bool(expanded))
            act.blockSignals(False)
        # 展开时给详情区一点高度，折叠时把空间还给预览
        if expanded:
            self.result.setMinimumHeight(140)
        else:
            self.result.setMinimumHeight(0)

    def _fmt_sec(self, sec: float) -> str:
        if sec < 10:
            return f"{sec:.1f}s"
        return f"{sec:.0f}s"

    def _format_round_timing(
        self, wall_sec: float, *, ok: bool, result: AnalyzeResult | None = None
    ) -> str:
        """底部简洁耗时：6.7s · 识图2.6 · 起草4.1"""
        t = self._fmt_sec(wall_sec)
        if not ok:
            return f"失败 · {t}"
        if result is None or result.total_ms <= 0:
            return t
        v = result.vision_ms / 1000.0
        l = result.llm_ms / 1000.0
        src = result.vision_source or "api"
        vtag = "缓存" if src == "cache" else "识图"
        return f"{t} · {vtag}{self._fmt_sec(v)} · 起草{self._fmt_sec(l)}"

    def _tick_elapsed(self) -> None:
        if self._analyze_t0 <= 0:
            return
        elapsed = time.perf_counter() - self._analyze_t0
        self.timing_label.setText(self._fmt_sec(elapsed))

    def _start_timing(self) -> None:
        self._analyze_t0 = time.perf_counter()
        self._last_timing_brief = ""
        self.timing_label.setText("0.0s")
        self._timing_hide.stop()
        self.last_timing_label.clear()
        self.last_timing_label.setVisible(False)
        self._elapsed_timer.start()

    def _hide_last_timing(self) -> None:
        self.last_timing_label.clear()
        self.last_timing_label.setToolTip("")
        self.last_timing_label.setVisible(False)

    def _finish_timing(self, *, ok: bool, result: AnalyzeResult | None = None) -> None:
        self._elapsed_timer.stop()
        if self._analyze_t0 <= 0:
            return
        total = time.perf_counter() - self._analyze_t0
        self._last_round_sec = total
        self._analyze_t0 = 0.0
        text = self._format_round_timing(total, ok=ok, result=result)
        self._last_timing_brief = text
        self.timing_label.setText(text)
        # 顶部不再展示；完成提示统一走底部 status
        self._timing_hide.stop()
        self.last_timing_label.clear()
        self.last_timing_label.setVisible(False)
        tip = "墙钟总耗时"
        if result and result.total_ms > 0:
            tip = (
                f"墙钟 {self._fmt_sec(total)}；"
                f"识图 {result.vision_ms:.0f}ms({result.vision_source or '?'})；"
                f"起草 {result.llm_ms:.0f}ms"
            )
        self.status.setToolTip(tip)

    def _set_progress(self, msg: str, pct: int, *, ceiling: int | None = None) -> None:
        self.progress_host.setVisible(True)
        self.progress_label.setText(msg)
        self.progress_bar.setValue(max(0, min(100, pct)))
        if ceiling is not None:
            self._progress_ceiling = ceiling
        self.status.setText(msg)
        self.status.setVisible(True)
        # 进行中不强制展开详情；会话名由顶栏 chat_title 展示
        if self.result.isVisible():
            self.result.setPlainText(msg)

    def _tick_fake_progress(self) -> None:
        """阶段内缓慢爬升，避免卡在同一百分比。"""
        if not self.progress_host.isVisible():
            return
        cur = self.progress_bar.value()
        cap = min(99, self._progress_ceiling)
        if cur < cap:
            self.progress_bar.setValue(min(cap, cur + 1))
        self._progress_hint_i = (self._progress_hint_i + 1) % len(_PROGRESS_HINTS)
        # 不覆盖真实阶段主文案，只偶尔换副状态
        if self._progress_hint_i % 2 == 0:
            hint = _PROGRESS_HINTS[self._progress_hint_i]
            base = self.progress_label.text().split(" · ")[0]
            self.progress_label.setText(f"{base} · {hint}")

    def _on_analyze_progress(self, msg: str, pct: int, epoch: int | None = None) -> None:
        if epoch is not None and epoch != getattr(self, "_analyze_epoch", 0):
            return
        # 真实阶段：抬高天花板，允许假进度爬到下一阶段前
        ceilings = {
            "准备": 20,
            "识图": 50,
            "判断": 72,
            "起草": 92,
            "整理": 99,
            "完成": 100,
        }
        ceiling = 99
        for key, val in ceilings.items():
            if key in msg:
                ceiling = val
                break
        self._set_progress(msg, pct, ceiling=ceiling)

    def _stop_progress(self, *, ok: bool, result: AnalyzeResult | None = None) -> None:
        self._fake_timer.stop()
        self._finish_timing(ok=ok, result=result)
        if ok:
            self.progress_bar.setValue(100)
            self.progress_label.setText("完成")
            QTimer.singleShot(600, lambda: self.progress_host.setVisible(False))
        else:
            self.progress_host.setVisible(False)

    def clear_results(self) -> None:
        """清空识别详情与候选，不影响预览采集。"""
        if self._analyze and self._analyze.isRunning():
            self.status.setText("分析进行中，请稍后再清理屏幕")
            return
        self._last_result = None
        self._clear_candidate_actions()
        self._fake_timer.stop()
        self._elapsed_timer.stop()
        self._timing_hide.stop()
        self._cancel_smart_send()
        self._analyze_t0 = 0.0
        self.progress_host.setVisible(False)
        self.progress_bar.setValue(0)
        self.progress_label.setText("")
        self.timing_label.setText("")
        self.last_timing_label.clear()
        self.last_timing_label.setToolTip("")
        self.last_timing_label.setVisible(False)
        self.detail_summary.setText("")
        self.result.clear()
        self._set_contact_name("")
        self.btn_detail.blockSignals(True)
        self.btn_detail.setChecked(False)
        self.btn_detail.blockSignals(False)
        self._on_detail_toggled(False)
        self._flash_status("已清理屏幕", linger_ms=2000)

    def _clear_candidate_actions(self) -> None:
        while self.actions_host.count():
            item = self.actions_host.takeAt(0)
            w = item.widget()
            if w is not None:
                w.deleteLater()
        self.actions_host.addStretch(1)
        self._candidate_btns.clear()
        self._set_compose_draft(None)

    def _candidate_bubble_max_width(self, *, send_w: int | None = None) -> int:
        """气泡可用宽度：占满一行再换行（扣掉发送钮、间距与右边距）。"""
        host_w = int(self.actions_scroll.viewport().width() if self.actions_scroll else 0)
        if host_w < 80:
            host_w = max(80, int(self.width()) - 20)
        # SendBtn 紧凑约 44~56；间距 8；左右边距（含滑块沟槽）
        btn = max(44, int(send_w) if send_w is not None else 56)
        m = self.actions_host.contentsMargins()
        inset = int(m.left()) + int(m.right())
        return max(120, host_w - btn - 8 - inset)

    def _fit_candidate_bubble(self, bubble: QLabel, max_w: int) -> None:
        """短文随内容收窄；长文满宽换行。单行统一高度，避免 heightForWidth 乱跳。"""
        text = bubble.text() or ""
        bubble.setWordWrap(True)
        bubble.setFont(QFont("Microsoft YaHei UI", 14))
        bubble.ensurePolished()
        max_w = max(80, int(max_w))
        bubble.setMaximumWidth(max_w)
        fm = QFontMetrics(bubble.font())
        # 与 stylesheet padding:10px 12px 一致
        pad_x, pad_y = 24, 20
        try:
            raw_w = int(fm.horizontalAdvance(text))
        except Exception:
            raw_w = int(fm.boundingRect(text).width())
        inner_max = max(1, max_w - pad_x)
        # 先按满宽测是否真会换行
        br = fm.boundingRect(
            0, 0, inner_max, 10000, int(Qt.TextFlag.TextWordWrap), text
        )
        single = br.height() <= fm.height() + 2
        if single:
            want = min(max_w, max(72, raw_w + pad_x))
            h = int(fm.height()) + pad_y
        else:
            want = max_w
            h = int(br.height()) + pad_y
        bubble.setFixedWidth(want)
        bubble.setFixedHeight(max(int(fm.height()) + pad_y, h))

    def _refit_all_candidate_bubbles(self) -> None:
        vw = int(self.actions_scroll.viewport().width() if self.actions_scroll else 0)
        if vw < 80:
            vw = max(80, int(self.width()) - 20)
        for row in self._candidate_btns:
            row.setMaximumWidth(max(1, vw))
            btn = row.findChild(QPushButton, "SendBtn")
            send_w = 90
            if btn is not None:
                btn.ensurePolished()
                send_w = max(
                    int(btn.sizeHint().width()),
                    int(btn.minimumSizeHint().width()),
                    44,
                )
            max_w = self._candidate_bubble_max_width(send_w=send_w)
            bubble = row.findChild(QLabel, "BubbleText")
            if bubble is not None:
                self._fit_candidate_bubble(bubble, max_w)

    def _set_compose_draft(self, cand: Candidate | None) -> None:
        """底部待发送栏：写入首条/选中候选（可继续编辑）。"""
        self._draft_candidate = cand
        text = (cand.text if cand is not None else "") or ""
        self.compose_draft.blockSignals(True)
        self.compose_draft.setPlainText(text)
        cur = self.compose_draft.textCursor()
        cur.movePosition(QTextCursor.MoveOperation.End)
        self.compose_draft.setTextCursor(cur)
        self.compose_draft.blockSignals(False)
        self.btn_compose_send.setEnabled(bool(text.strip()))
        if text.strip():
            self._sync_compose_send_label()
        # 侧窗模式不自动抢焦点：否则任务栏点微信会先「激活」闪一下，第二次才最小化
        # （标题栏最小化是直接缩窗，不受此影响）
        if text.strip() and not self.is_dock_mode():
            self.compose_draft.setFocus(Qt.FocusReason.OtherFocusReason)
            sync = getattr(self.compose_draft, "_sync_caret_pos", None)
            if callable(sync):
                QTimer.singleShot(0, sync)
        elif text.strip() and self.compose_draft.hasFocus():
            sync = getattr(self.compose_draft, "_sync_caret_pos", None)
            if callable(sync):
                QTimer.singleShot(0, sync)

    def _on_compose_text_changed(self) -> None:
        text = self.compose_draft.toPlainText()
        stripped = text.strip()
        self.btn_compose_send.setEnabled(bool(stripped))
        if not stripped:
            return
        if self._draft_candidate is None:
            self._draft_candidate = Candidate(text=stripped, recommended=False)
        else:
            self._draft_candidate = self._draft_candidate.model_copy(update={"text": text})

    def _sync_compose_send_label(self) -> None:
        if bool(getattr(self.cfg.chat, "auto_send", True)):
            self.btn_compose_send.setText("发送")
            self.btn_compose_send.setToolTip("填入微信并回车发送")
        else:
            self.btn_compose_send.setText("填入")
            self.btn_compose_send.setToolTip("只填入微信输入框，需你自行回车发送")

    def _compose_send(self) -> None:
        text = self.compose_draft.toPlainText().strip()
        if not text:
            return
        if self._draft_candidate is None or (self._draft_candidate.text or "").strip() != text:
            self._draft_candidate = Candidate(text=text, recommended=False)
        else:
            self._draft_candidate = self._draft_candidate.model_copy(update={"text": text})
        self._fill_candidate(self._draft_candidate, send=True, confirm=False)

    def _rebuild_candidate_actions(self, result: AnalyzeResult) -> None:
        self._clear_candidate_actions()
        if not result.candidates:
            tip = result.draft_error or "未生成候选，可再点一次「立即分析」"
            lab = QLabel(f"起草未出结果：{tip}")
            lab.setWordWrap(True)
            lab.setStyleSheet("color:#FA5151; font-size:13px;")
            self.actions_host.insertWidget(max(0, self.actions_host.count() - 1), lab)
            return

        # 首条进入底部待发送栏
        self._set_compose_draft(result.candidates[0])

        auto_send = bool(getattr(self.cfg.chat, "auto_send", True))
        send_label = "发送" if auto_send else "填入"
        send_tip = (
            "填入微信并回车发送" if auto_send else "只填入微信输入框，需你自行回车"
        )
        max_w = self._candidate_bubble_max_width()
        for cand in result.candidates:
            row = QWidget()
            row.setObjectName("CandidateRow")
            lay = QVBoxLayout(row)
            lay.setContentsMargins(0, 6, 0, 2)
            lay.setSpacing(4)

            bubble_wrap = QHBoxLayout()
            bubble_wrap.setContentsMargins(0, 0, 0, 0)
            bubble_wrap.setSpacing(8)
            # 短句靠右；长句用满宽度后再换行
            bubble_wrap.addStretch(1)
            bubble = QLabel(cand.text)
            bubble.setObjectName("BubbleText")
            bubble.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
            bubble.setToolTip("点击填入底部待发送栏")
            bubble.setCursor(Qt.CursorShape.PointingHandCursor)
            bubble.setSizePolicy(
                QSizePolicy.Policy.Maximum, QSizePolicy.Policy.Minimum
            )
            self._fit_candidate_bubble(bubble, max_w)
            bubble.mousePressEvent = (  # type: ignore[method-assign]
                lambda _e, c=cand: self._set_compose_draft(c)
            )
            bubble_wrap.addWidget(bubble, stretch=0)

            btn_send = QPushButton(send_label)
            btn_send.setObjectName("SendBtn")
            btn_send.setCursor(Qt.CursorShape.PointingHandCursor)
            btn_send.setEnabled(True)
            btn_send.setToolTip(send_tip)
            btn_send.setSizePolicy(
                QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Fixed
            )

            def _send_one(_=False, c=cand) -> None:
                self._set_compose_draft(c)
                self._fill_candidate(c, send=True, confirm=False)

            btn_send.clicked.connect(_send_one)
            bubble_wrap.addWidget(btn_send, stretch=0, alignment=Qt.AlignmentFlag.AlignBottom)
            lay.addLayout(bubble_wrap)

            self.actions_host.insertWidget(max(0, self.actions_host.count() - 1), row)
            self._candidate_btns.append(row)

        # 布局落稳后再按真实视口与发送钮宽度收一次，避免右边裁切
        QTimer.singleShot(0, self._refit_all_candidate_bubbles)
        QTimer.singleShot(50, self._refit_all_candidate_bubbles)
        QTimer.singleShot(120, self._refit_all_candidate_bubbles)
        QTimer.singleShot(0, self.actions_scroll._relayout_thumb)
        QTimer.singleShot(80, self.actions_scroll._relayout_thumb)

    def refresh_windows(self) -> None:
        """刷新并锁定默认微信主窗（不再提供下拉选择）。"""
        prev = self._target_hwnd
        self._windows = find_wechat_windows()
        default = pick_default_window(self._windows)

        chosen: WindowInfo | None = None
        if prev is not None:
            for w in self._windows:
                if w.hwnd == prev:
                    chosen = w
                    break
        if chosen is None:
            chosen = default

        self._target_hwnd = chosen.hwnd if chosen else None

        n = len(self._windows)
        if n == 0:
            self._flash_status("未找到微信窗口，请先打开微信主界面后再点「刷新」")
        else:
            # 默认窗体：不再提示「请选择」
            self._flash_status("")
        self._apply_target_window()
        if n > 0:
            QTimer.singleShot(0, self._ensure_preview)

    def _flash_status(self, text: str, *, linger_ms: int = 0) -> None:
        """底部临时灰字提示。"""
        text = (text or "").strip()
        if not text:
            self.status.clear()
            self.status.setToolTip("")
            self.status.setVisible(False)
            return
        self.status.setText(text)
        self.status.setVisible(True)
        if linger_ms > 0:
            token = int(getattr(self, "_status_flash_token", 0)) + 1
            self._status_flash_token = token

            def _clear(t: int = token) -> None:
                if getattr(self, "_status_flash_token", 0) == t:
                    self._flash_status("")

            QTimer.singleShot(linger_ms, _clear)

    def _chat_title_max_width(self) -> int:
        """标题显示宽度：顶栏去掉 ··· 后可用宽度的 75%，避免贴太近。"""
        host_w = int(self.toolbar_host.width()) if self.toolbar_host else 0
        if host_w < 40:
            host_w = max(40, int(self.width()) - 40)
        more_w = int(self.btn_more.width()) if self.btn_more else 36
        avail = max(48, host_w - more_w - 8)
        return max(48, int(avail * 0.75))

    def _elide_chat_title(self) -> None:
        """按最大宽度末尾省略；完整名放 tooltip。"""
        max_w = self._chat_title_max_width()
        self.chat_title.setMaximumWidth(max_w)
        name = (self._contact_name or "").strip()
        if not name:
            self.chat_title.setText("—")
            self.chat_title.setToolTip("识别后显示群名或人名")
            return
        fm = QFontMetrics(self.chat_title.font())
        self.chat_title.setText(
            fm.elidedText(name, Qt.TextElideMode.ElideRight, max_w)
        )
        self.chat_title.setToolTip(name)

    def _set_contact_name(self, name: str) -> None:
        """会话名显示在顶栏（群名/人名），底部不再重复「当前聊天」。"""
        prev = (self._contact_name or "").strip()
        self._contact_name = (name or "").strip()
        if self._contact_name != prev:
            self._contact_flip_pending = ""
        if self._contact_name:
            self.chat_title.setProperty("empty", False)
        else:
            self.chat_title.setProperty("empty", True)
        self._elide_chat_title()
        # 刷新 QSS 对动态 property 的匹配
        self.chat_title.style().unpolish(self.chat_title)
        self.chat_title.style().polish(self.chat_title)
        self.chat_title.update()

    def _start_contact_watch(self) -> None:
        if not self.is_dock_mode() or self._dock_collapsed:
            return
        self._contact_watch_armed = True
        if not self._contact_poll.isActive():
            self._contact_poll.start()

    def _stop_contact_watch(self) -> None:
        self._contact_watch_armed = False
        self._contact_poll.stop()
        self._contact_debounce.stop()

    def _qthread_busy(self, worker) -> bool:
        """线程是否仍在跑；C++ 侧已 deleteLater 时不再抛 RuntimeError。"""
        if worker is None:
            return False
        try:
            return bool(worker.isRunning())
        except RuntimeError:
            return False

    def _release_header_watch(self, worker) -> None:
        if self._header_watch is worker:
            self._header_watch = None
        try:
            worker.deleteLater()
        except RuntimeError:
            pass

    def _release_contact_probe(self, worker) -> None:
        if self._contact_probe is worker:
            self._contact_probe = None
        try:
            worker.deleteLater()
        except RuntimeError:
            pass

    def _on_contact_header_fp(self, fp: str) -> None:
        """顶栏指纹变化 → 防抖后识名。"""
        from joy.capture.frame_ops import header_fp_changed

        fp = (fp or "").strip()
        if not fp or not self._contact_watch_armed:
            return
        named = bool((self._contact_name or "").strip())
        # 已有会话名：明显换会话才立刻重识；小抖动只更新基准
        if named:
            big_switch = header_fp_changed(
                self._contact_header_fp, fp, max_hamming=18
            )
            if not big_switch:
                if self._contact_header_fp:
                    self._contact_header_fp = fp
                return
        else:
            if not header_fp_changed(self._contact_header_fp, fp, max_hamming=6):
                if self._contact_header_fp:
                    self._contact_header_fp = fp
                self._flush_pending_contact_identify()
                return
            big_switch = header_fp_changed(
                self._contact_header_fp, fp, max_hamming=14
            )

        cool_until = float(getattr(self, "_contact_identify_cool_until", 0) or 0)
        now = time.monotonic()
        if now < cool_until:
            if named and not big_switch:
                return
            if named:
                self._contact_identify_pending_fp = fp
                return
            if not big_switch:
                self._contact_identify_pending_fp = fp
                return

        self._contact_identify_pending_fp = ""
        self._contact_header_fp = fp
        if not self._contact_debounce.isActive():
            self._contact_debounce.start()

    def _flush_pending_contact_identify(self) -> None:
        """冷却结束后补打被积压的切会话识名。"""
        pending = (getattr(self, "_contact_identify_pending_fp", "") or "").strip()
        if not pending:
            return
        cool_until = float(getattr(self, "_contact_identify_cool_until", 0) or 0)
        if time.monotonic() < cool_until:
            return
        if self._qthread_busy(self._contact_probe) or self._qthread_busy(self._analyze):
            return
        named = bool((self._contact_name or "").strip())
        if named:
            from joy.capture.frame_ops import header_fp_changed

            if not header_fp_changed(self._contact_header_fp, pending, max_hamming=16):
                self._contact_identify_pending_fp = ""
                self._contact_header_fp = pending
                return
        self._contact_identify_pending_fp = ""
        self._contact_header_fp = pending
        if not self._contact_debounce.isActive():
            self._contact_debounce.start()

    def _on_contact_debounce_fire(self) -> None:
        cool_until = float(getattr(self, "_contact_identify_cool_until", 0) or 0)
        if time.monotonic() < cool_until:
            return
        self._refresh_contact_title(force=True)

    def _contact_verify_due(self) -> bool:
        """首次识名后的强制复核，或周期性轻量重验（防读错名一直挂着）。"""
        now = time.monotonic()
        soft = float(getattr(self, "_contact_soft_verify_at", 0) or 0)
        if soft > 0 and now >= soft:
            return True
        last = float(getattr(self, "_contact_last_identify_at", 0) or 0)
        if last <= 0:
            return False
        return (now - last) >= 0.9

    def _poll_contact_header(self) -> None:
        if not self._contact_watch_armed:
            return
        if not self.is_dock_mode() or self._dock_collapsed:
            return
        if self._dock_animating or self._dock_fly_anim is not None:
            return
        self._flush_pending_contact_identify()
        if self._qthread_busy(self._header_watch):
            return
        if self._qthread_busy(self._contact_probe):
            return
        if self._qthread_busy(self._analyze):
            return
        named = bool((self._contact_name or "").strip())
        cool_until = float(getattr(self, "_contact_identify_cool_until", 0) or 0)
        now = time.monotonic()
        # 到点复核：不依赖指纹变化（首次误读会卡死在旧名）
        if named and self._contact_verify_due() and now >= cool_until:
            self._contact_soft_verify_at = 0.0
            self._refresh_contact_title(force=True)
            return
        # 冷却期内仍盯顶栏指纹：否则刚识名后立刻切会话会感觉「卡住」
        window = self._selected_window()
        if window is None or is_minimized(window.hwnd):
            return
        worker = HeaderWatchWorker(self.cfg, window, parent=self)
        self._header_watch = worker
        worker.finished_ok.connect(self._on_contact_header_fp)
        worker.finished.connect(lambda w=worker: self._release_header_watch(w))
        worker.start()

    def _refresh_contact_title(self, *, force: bool = False) -> None:
        """轻量识对面名字，写入顶栏。"""
        if getattr(self, "_history_pull_busy", False):
            return
        if not self.is_dock_mode() or self._dock_collapsed:
            return
        if self._dock_animating or self._dock_fly_anim is not None:
            return
        if not force and self._contact_name:
            return
        cool_until = float(getattr(self, "_contact_identify_cool_until", 0) or 0)
        if self._contact_name and time.monotonic() < cool_until:
            return
        if self._qthread_busy(self._contact_probe):
            return
        if self._qthread_busy(self._analyze):
            return
        window = self._selected_window()
        if window is None or is_minimized(window.hwnd):
            return
        worker = ContactIdentifyWorker(
            self.cfg,
            window,
            parent=self,
            left_ratio=float(getattr(self.cfg.capture.crop, "left", 0.28) or 0.28),
        )
        self._contact_probe = worker

        def _ok(title: str, fp: str = "") -> None:
            name = (title or "").strip()
            _ = fp
            bad = {
                "微信",
                "WeChat",
                "Weixin",
                "Joy",
                "名称",
                "会话名",
                "联系人",
                "title",
                "name",
            }
            if name in bad or name.lower() in {b.lower() for b in bad}:
                self._contact_last_identify_at = time.monotonic()
                self._contact_identify_cool_until = time.monotonic() + 0.7
                return
            if name:
                from joy.core.contact_memory import (
                    stabilize_contact_title,
                    titles_confusable,
                )

                prev = (self._contact_name or "").strip()
                if prev and titles_confusable(name, prev) and name != prev:
                    pending = (getattr(self, "_contact_flip_pending", "") or "").strip()
                    if pending == name:
                        self._contact_flip_pending = ""
                        log.info(
                            "contact flip confirmed %s → %s (2x identify)",
                            prev[:40],
                            name[:40],
                        )
                    else:
                        self._contact_flip_pending = name
                        log.info(
                            "contact flip pending keep=%s saw=%s (need 2x)",
                            prev[:40],
                            name[:40],
                        )
                        self._contact_last_identify_at = time.monotonic()
                        self._contact_identify_cool_until = time.monotonic() + 0.75
                        self._contact_soft_verify_at = time.monotonic() + 0.75
                        return
                else:
                    self._contact_flip_pending = ""
                    name = stabilize_contact_title(name, prev)
                same = name == prev
                if not same:
                    log.info("contact title %s → %s", prev[:40] or "(空)", name[:40])
                self._set_contact_name(name)
                now = time.monotonic()
                self._contact_last_identify_at = now
                # 约 1s 一轮重验；冷却略短，切会话可更快补识
                self._contact_identify_cool_until = now + 0.7
                self._contact_soft_verify_at = 0.0

        def _err(_msg: str) -> None:
            self._contact_last_identify_at = time.monotonic()
            self._contact_identify_cool_until = time.monotonic() + 3.0

        worker.finished_ok.connect(_ok)
        worker.finished_err.connect(_err)
        worker.finished.connect(lambda w=worker: self._release_contact_probe(w))
        worker.start()

    def _ensure_preview(self) -> None:
        if self._worker and self._worker.isRunning():
            return
        if self._selected_window() is None:
            return
        self.start_preview()

    def _apply_target_window(self) -> None:
        """把当前默认窗体应用到采集与侧窗贴边。"""
        window = self._selected_window()
        if self._worker and self._worker.isRunning() and window is not None:
            self._worker.set_window(window)
        if self.is_dock_mode():
            self._dock_to_wechat()

    def _selected_window(self) -> WindowInfo | None:
        hwnd = self._target_hwnd
        if hwnd is None:
            return pick_default_window(self._windows) if self._windows else None
        for w in self._windows:
            if w.hwnd == hwnd:
                return w
        return pick_default_window(self._windows) if self._windows else None

    def start_preview(self) -> None:
        window = self._selected_window()
        if window is None:
            self.status.setText("未找到微信窗口")
            return
        self.stop_preview()
        self._worker = CaptureWorker(self.cfg.capture)
        self._worker.set_window(window)
        self._worker.set_paused(self.btn_pause.isChecked())
        self._worker.frame_ready.connect(self._on_frame)
        self._worker.status.connect(self._on_status)
        self._worker.error.connect(self._on_error)
        # 预览前尝试还原，避免一直黑屏
        restore_window(window.hwnd)
        self._worker.start()
        self._flash_status("")
        if self.is_dock_mode():
            QTimer.singleShot(0, self._dock_to_wechat)
        # 自动模式：预览起来后立刻把当前画面记为基准，避免首帧当新消息
        if getattr(self, "btn_auto", None) is not None and self.btn_auto.isChecked():
            QTimer.singleShot(0, self._request_auto_baseline_seed)
            QTimer.singleShot(350, self._request_auto_baseline_seed)

    def stop_preview(self) -> None:
        if self._worker:
            try:
                self._worker.frame_ready.disconnect(self._on_frame)
                self._worker.status.disconnect(self._on_status)
                self._worker.error.disconnect(self._on_error)
            except (RuntimeError, TypeError):
                pass
            self._worker.stop()
            self._worker = None

    def restore_target(self) -> None:
        window = self._selected_window()
        if window is None:
            self.status.setText("没有可用窗口")
            return
        ok = restore_window(window.hwnd)
        self.status.setText("已请求还原窗口" if ok else "还原失败：请手动点任务栏微信")
        if self._worker and self._worker.isRunning():
            self._worker.set_window(window)
        if ok and self.is_dock_mode():
            # 微信已调出：立刻把侧窗贴回来（不等下一拍轮询）
            self._dock_hidden_for_wechat = True
            self._schedule_dock_to_wechat(0)
            self._schedule_dock_to_wechat(120)

    def run_analyze(self, *, auto: bool = False) -> None:
        window = self._selected_window()
        if window is None:
            self.status.setText("没有可用窗口")
            return
        if self._analyze and self._analyze.isRunning():
            # 自动：作废旧线程引用，允许跟最新画面；手动：仍提示进行中
            if auto:
                self._analyze_epoch = int(getattr(self, "_analyze_epoch", 0)) + 1
                try:
                    self._analyze.cancel()
                except Exception:
                    pass
                zombie = self._analyze
                self._analyze_zombies.append(zombie)
                self._analyze = None
                log.info("auto: supersede running analyze")
            else:
                self.status.setText("分析进行中…")
                return

        # 暂时取消调用 API 的二次确认，点分析直接跑
        self._cancel_smart_send()
        self.btn_analyze.setEnabled(False)
        self.act_analyze.setEnabled(False)
        self._clear_candidate_actions()
        self._progress_hint_i = 0
        self.btn_detail.setChecked(False)
        self._on_detail_toggled(False)
        self._start_timing()
        self._set_progress("准备中…", 5, ceiling=18)
        self._fake_timer.start()
        self._analyze_epoch = int(getattr(self, "_analyze_epoch", 0)) + 1
        epoch = self._analyze_epoch
        self._analyze = AnalyzeWorker(
            self.cfg,
            window,
            parent=self,
            auto=auto,
            title_hint=self._contact_name or "",
        )
        self._analyze.progress.connect(
            lambda msg, pct, e=epoch: self._on_analyze_progress(msg, pct, epoch=e)
        )
        self._analyze.finished_ok.connect(
            lambda result, e=epoch: self._on_analyze_ok(result, epoch=e)
        )
        self._analyze.finished_err.connect(
            lambda text, e=epoch: self._on_analyze_err(text, epoch=e)
        )
        self._analyze.finished.connect(
            lambda e=epoch: self._on_analyze_finished_ui(epoch=e)
        )
        self._analyze.start()
        # 自动分析也计入冷却，避免连打
        if auto or self.btn_auto.isChecked():
            self._bump_auto_cooldown()

    def _on_analyze_finished_ui(self, epoch: int | None = None) -> None:
        if epoch is not None and epoch != getattr(self, "_analyze_epoch", 0):
            return
        self.btn_analyze.setEnabled(True)
        self.act_analyze.setEnabled(True)
        w = self._analyze
        if w is not None and not w.isRunning():
            self._analyze = None

    def _on_analyze_ok(self, result: object, epoch: int | None = None) -> None:
        if epoch is not None and epoch != getattr(self, "_analyze_epoch", 0):
            return
        assert isinstance(result, AnalyzeResult)
        self._stop_progress(ok=True, result=result)
        self._bump_auto_cooldown()
        self._last_result = result
        self.result.setPlainText(self._engine.format_detail_text(result))
        self.detail_summary.setText(self._engine.format_detail_summary(result))
        # 默认保持折叠，只露出一行摘要
        self.btn_detail.blockSignals(True)
        self.btn_detail.setChecked(False)
        self.btn_detail.blockSignals(False)
        self._on_detail_toggled(False)
        if result.skipped_reason:
            self._clear_candidate_actions()
        else:
            self._rebuild_candidate_actions(result)
        # 分析成功时同步顶栏会话名（易混字母不因单次识图翻盘）
        if result.context.title:
            from joy.core.contact_memory import stabilize_contact_title

            self._set_contact_name(
                stabilize_contact_title(
                    result.context.title, self._contact_name or ""
                )
            )
        timing = (getattr(self, "_last_timing_brief", "") or "").strip()
        if result.skipped_reason:
            if result.skipped_reason == "last_from_me":
                head = "已跳过 · 最新是自己发的"
            elif result.skipped_reason == "self_or_no_new_other":
                head = "已跳过 · 对方无新消息"
            elif result.skipped_reason == "no_reply":
                head = "已跳过 · 此刻不必硬接"
            elif str(result.skipped_reason or "").startswith("policy:"):
                head = "已跳过 · 本地策略"
            else:
                head = "已跳过"
            msg = f"{head} · {timing}" if timing else head
            self._flash_status(msg, linger_ms=4000)
        elif result.candidates:
            n = len(result.candidates)
            head = "自动完成" if self.btn_auto.isChecked() else "完成"
            msg = f"{head} · {n}候选"
            if timing:
                msg = f"{msg} · {timing}"
            pick = next(
                (c for c in result.candidates if getattr(c, "recommended", False)),
                result.candidates[0],
            )
            if bool(getattr(self.cfg.ui, "smart_drive", False)) and self.act_smart.isChecked():
                self._flash_status(msg, linger_ms=1200)
                self._schedule_smart_send(pick)
            else:
                self._flash_status(msg, linger_ms=4500)
        elif result.draft_error:
            msg = f"起草失败 · {timing}" if timing else "起草失败"
            self._flash_status(msg, linger_ms=8000)
            self.btn_detail.setChecked(True)
            self._on_detail_toggled(True)
        else:
            msg = "完成 · 无候选"
            if timing:
                msg = f"{msg} · {timing}"
            self._flash_status(msg, linger_ms=4500)

    def _on_analyze_err(self, text: str, epoch: int | None = None) -> None:
        if epoch is not None and epoch != getattr(self, "_analyze_epoch", 0):
            return
        self._stop_progress(ok=False)
        self._bump_auto_cooldown()
        self._last_result = None
        self._clear_candidate_actions()
        self.detail_summary.setText("分析失败")
        self.result.setPlainText(f"分析失败：\n{text}")
        self.btn_detail.setChecked(True)
        self._on_detail_toggled(True)
        timing = (getattr(self, "_last_timing_brief", "") or "").strip()
        msg = f"分析失败 · {timing}" if timing else "分析失败"
        self._flash_status(msg, linger_ms=6000)
        self.progress_host.setVisible(True)
        self.progress_label.setText("分析失败")
        self.progress_bar.setValue(0)
        QTimer.singleShot(2000, lambda: self.progress_host.setVisible(False))

    def _copy_text(self, text: str) -> None:
        QGuiApplication.clipboard().setText(text)
        self.status.setText("已复制到剪贴板")

    def _fill_candidate(
        self,
        cand: Candidate,
        *,
        send: bool,
        confirm: bool | None = None,
        force_enter: bool = False,
    ) -> None:
        window = self._selected_window()
        if window is None:
            self.status.setText("没有目标窗口")
            return
        xr = self.cfg.chat.fill_click_x_ratio
        yr = self.cfg.chat.fill_click_y_ratio
        auto_send = bool(getattr(self.cfg.chat, "auto_send", True))
        # send=True 表示用户点了发送/填入；是否回车看 auto_send（智能驾驶可强制回车）
        do_enter = bool(send) and (auto_send or force_enter)
        if send:
            need_confirm = self.cfg.chat.confirm_before_send if confirm is None else confirm
            if need_confirm and do_enter and not force_enter:
                ret = QMessageBox.question(
                    self,
                    "确认发送",
                    f"即将填入微信并模拟回车发送：\n\n{cand.text[:200]}\n\n确定？",
                    QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                    QMessageBox.StandardButton.No,
                )
                if ret != QMessageBox.StandardButton.Yes:
                    return
        if do_enter:
            result = paste_and_send(window.hwnd, cand.text, x_ratio=xr, y_ratio=yr)
        else:
            result = paste_text(window.hwnd, cand.text, x_ratio=xr, y_ratio=yr)
        if not result.ok:
            self.status.setText(f"失败：{result.detail}")
            return
        if do_enter:
            self._remember_sent_to_memory(cand.text)
            self._flash_status("已发送", linger_ms=2000)
        else:
            self._flash_status("已填入，请在微信回车发送", linger_ms=2500)

    def _remember_sent_to_memory(self, text: str) -> None:
        if not bool(getattr(self.cfg.chat, "memory_enabled", True)):
            return
        title = (self._contact_name or "").strip()
        if not title:
            return
        try:
            from joy.core.contact_memory import append_sent

            append_sent(
                title,
                text,
                keep_max=int(getattr(self.cfg.chat, "memory_keep_max", 400) or 400),
            )
        except Exception as exc:
            log.warning("memory append sent failed: %s", exc)

    def _open_context_hub(self) -> None:
        """… 菜单 → 上下文二级面板（导入 / 同步 / 清除 Tab）。"""
        from joy.app.context_hub import ContextHubDialog

        title = (self._contact_name or "").strip()
        n, buckets = 0, 0
        if title:
            try:
                from joy.core.contact_memory import count_related_memory

                n, buckets = count_related_memory(title)
            except Exception:
                n, buckets = 0, 0
        steps = max(
            6,
            min(80, int(getattr(self.cfg.chat, "memory_pull_scroll_steps", 24) or 24)),
        )
        dlg = ContextHubDialog(
            self,
            contact_title=title,
            scroll_steps=steps,
            memory_count=n,
            memory_buckets=buckets,
            pull_busy=bool(getattr(self, "_history_pull_busy", False)),
        )
        if dlg.exec() != QDialog.DialogCode.Accepted:
            return
        action = (dlg.result_action or "").strip()
        if action == "import":
            self._run_import_memory(
                dlg.result_raw, me_names=dlg.result_me_names or None
            )
        elif action == "sync":
            self._run_pull_memory(
                me_names=dlg.result_me_names or "",
                scroll_steps=int(dlg.result_scroll_steps),
            )
        elif action == "clear":
            self._run_clear_memory()

    def _run_clear_memory(self) -> None:
        title = (self._contact_name or "").strip()
        if not title:
            QMessageBox.information(
                self,
                "清除上下文",
                "请先识别到会话联系人（顶栏有名字）后再清除。",
            )
            return
        try:
            from joy.core.contact_memory import clear_memory

            removed = clear_memory(title)
        except Exception as exc:
            QMessageBox.warning(self, "清除失败", str(exc))
            return
        self._flash_status(
            f"已清空「{title}」全部上下文 · {removed} 条", linger_ms=4000
        )

    def _run_import_memory(
        self, raw: str, *, me_names: str | None = None
    ) -> None:
        title = (self._contact_name or "").strip()
        if not title:
            QMessageBox.information(
                self,
                "导入上下文",
                "请先识别到会话联系人（顶栏有名字）后再导入。",
            )
            return
        text = (raw or "").strip()
        if not text:
            return
        try:
            from joy.core.contact_memory import import_transcript

            parsed, added = import_transcript(
                title,
                text,
                me_names=(me_names or "").strip() or None,
                keep_max=int(getattr(self.cfg.chat, "memory_keep_max", 400) or 400),
            )
            self._flash_status(
                f"已导入 {parsed} 条 · 净增 {added} · {title}",
                linger_ms=4000,
            )
        except Exception as exc:
            QMessageBox.warning(self, "导入失败", str(exc))

    def _run_pull_memory(
        self, *, me_names: str = "", scroll_steps: int | None = None
    ) -> None:
        title = (self._contact_name or "").strip()
        if not title:
            QMessageBox.information(
                self,
                "同步上下文",
                "请先识别到会话联系人（顶栏有名字），并打开该微信会话。",
            )
            return
        window = self._selected_window()
        if window is None or is_minimized(window.hwnd):
            QMessageBox.information(self, "同步上下文", "请先选中可用的微信窗口。")
            return
        if getattr(self, "_history_pull_busy", False):
            QMessageBox.information(self, "同步上下文", "已有同步任务在进行中。")
            return

        steps = max(
            6,
            min(
                80,
                int(
                    scroll_steps
                    if scroll_steps is not None
                    else getattr(self.cfg.chat, "memory_pull_scroll_steps", 24) or 24
                ),
            ),
        )
        # 写回配置，便于下次默认
        try:
            self.cfg.chat.memory_pull_scroll_steps = steps
            self.cfg.save()
        except Exception:
            pass

        compose_top = 0.78
        contact_left = float(getattr(self.cfg.capture.crop, "left", 0.22) or 0.22)
        layout = getattr(self, "_wechat_layout", None)
        if layout is not None:
            try:
                compose_top = float(layout.compose_area.top)
            except Exception:
                pass

        # 暂停顶栏识名，避免与同步抢 RPM（429 → 瞎偏移点到「收藏」）
        was_watching = bool(self._contact_watch_armed)
        self._history_pull_busy = True
        self._stop_contact_watch()

        self._flash_status("同步上下文中…请勿操作键鼠", linger_ms=8000)
        self.act_pull_mem.setEnabled(False)
        worker = HistoryPullWorker(
            int(window.hwnd),
            title,
            self.cfg,
            me_names=me_names,
            compose_top=compose_top,
            contact_left=max(0.28, contact_left),
            scroll_steps=steps,
            keep_max=int(getattr(self.cfg.chat, "memory_keep_max", 400) or 400),
            parent=self,
        )

        def _resume_watch() -> None:
            self._history_pull_busy = False
            self.act_pull_mem.setEnabled(True)
            if was_watching and self.is_dock_mode():
                self._start_contact_watch()

        def _ok(parsed: int, added: int, t: str) -> None:
            _resume_watch()
            self._flash_status(
                f"同步完成 · 解析 {parsed} · 净增 {added} · {t}",
                linger_ms=5000,
            )

        def _err(msg: str) -> None:
            _resume_watch()
            QMessageBox.warning(
                self,
                "同步失败",
                f"{msg}\n\n可改用「上下文 → 导入」手动粘贴微信复制结果。",
            )

        worker.finished_ok.connect(_ok)
        worker.finished_err.connect(_err)
        worker.progress.connect(lambda s: self._flash_status(s, linger_ms=4000))
        worker.start()
        self._history_pull_worker = worker

    def _on_pause(self) -> None:
        paused = self.act_pause.isChecked()
        self._paused = paused
        self._sync_more_menu()
        if self._worker:
            self._worker.set_paused(paused)
        if paused:
            self._auto_settle.stop()

    def _on_frame(self, frame: object) -> None:
        from joy.capture.grabber import CapturedFrame

        if frame is None:
            self.image.setPixmap(QPixmap())
            self.image.setText("暂停预览（微信最小化或被遮挡）")
            return
        assert isinstance(frame, CapturedFrame)
        # 预览帧顺带盯顶栏：有名后也盯（仅大换会话才识名，不会每帧打 API）
        if (
            self.is_dock_mode()
            and self._contact_watch_armed
            and frame.rgb is not None
        ):
            try:
                from joy.capture.frame_ops import header_fingerprint

                self._on_contact_header_fp(header_fingerprint(frame.rgb))
            except Exception:
                pass
        if not self.is_dock_mode():
            qimg = _numpy_to_qimage(frame.rgb)
            pix = QPixmap.fromImage(qimg)
            # HiDPI：按物理像素缩放，避免 2x 屏上逻辑尺寸拉伸发糊
            dpr = float(self.image.devicePixelRatioF() or 1.0)
            if dpr < 1.0:
                dpr = 1.0
            target = self.image.size() * dpr
            if target.width() >= 2 and target.height() >= 2:
                scaled = pix.scaled(
                    target,
                    Qt.AspectRatioMode.KeepAspectRatio,
                    Qt.TransformationMode.SmoothTransformation,
                )
                scaled.setDevicePixelRatio(dpr)
                self.image.setPixmap(scaled)
                self.image.setText("")
        else:
            # 侧窗也刷新预览（原先 dock 直接跳过，看起来像一直停在糊图）
            if frame.rgb is not None and self.image.isVisible():
                qimg = _numpy_to_qimage(frame.rgb)
                pix = QPixmap.fromImage(qimg)
                dpr = float(self.image.devicePixelRatioF() or 1.0)
                if dpr < 1.0:
                    dpr = 1.0
                target = self.image.size() * dpr
                if target.width() >= 2 and target.height() >= 2:
                    scaled = pix.scaled(
                        target,
                        Qt.AspectRatioMode.KeepAspectRatio,
                        Qt.TransformationMode.SmoothTransformation,
                    )
                    scaled.setDevicePixelRatio(dpr)
                    self.image.setPixmap(scaled)
                    self.image.setText("")
        # 自动模式：画面有变化 → 等稳定后分析
        if frame.changed:
            self._note_auto_screen_change()

    def _on_status(self, text: str) -> None:
        # 最小化/暂停采集不再刷提示（侧窗已跟随微信隐藏/调出）
        if any(k in text for k in ("遮挡", "失效", "失败", "未找到")):
            self._flash_status(text)

    def _on_error(self, text: str) -> None:
        self._flash_status(f"错误：{text}", linger_ms=6000)

    def closeEvent(self, event) -> None:  # noqa: N802
        self._dock_follow.stop()
        self._dock_vis_timer.stop()
        self._dock_z_timer.stop()
        self._auto_settle.stop()
        self._stop_contact_watch()
        self._dock_collapsed = False
        self._hide_wake_btn()
        self._hide_collapse_btn()
        try:
            self._wake_btn.close()
        except Exception:
            pass
        try:
            self._collapse_btn.close()
        except Exception:
            pass
        self.stop_preview()
        if self._qthread_busy(self._header_watch):
            try:
                self._header_watch.wait(800)
            except RuntimeError:
                pass
        if self._qthread_busy(self._contact_probe):
            try:
                self._contact_probe.wait(1500)
            except RuntimeError:
                pass
        self._header_watch = None
        self._contact_probe = None
        # 紧急制动可能留下未结束的分析线程
        self._analyze_epoch = int(getattr(self, "_analyze_epoch", 0)) + 1
        pending = []
        if self._analyze is not None:
            pending.append(self._analyze)
        pending.extend(list(getattr(self, "_analyze_zombies", []) or []))
        for w in pending:
            try:
                if w.isRunning():
                    cancel = getattr(w, "cancel", None)
                    if callable(cancel):
                        cancel()
                    w.wait(1500)
            except Exception:
                pass
        self._analyze = None
        self._analyze_zombies.clear()
        super().closeEvent(event)
