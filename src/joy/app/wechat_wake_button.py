"""标题栏风格 △ 浮层：侧窗收起 / 微信旁唤醒。"""

from __future__ import annotations

from PySide6.QtCore import QPoint, QPointF, Qt, QTimer, Signal
from PySide6.QtGui import QColor, QCursor, QGuiApplication, QPainter, QPen, QPolygonF
from PySide6.QtWidgets import QToolTip, QWidget

from joy.capture.windows import window_screen_rect

# 标题栏一格：宽度略宽方便点中，高度贴合标题栏不溢出
_CAPTION_SLOT_W = 48
_CAPTION_SLOT_H = 30


class CaptionTriangleButton(QWidget):
    """
    细灰线空心三角，风格贴近微信/系统标题栏按钮。
    点击发出 activated。
    图层跟随宿主窗，不使用 StayOnTop（避免压到 Cursor 等其它软件上）。
    """

    activated = Signal()

    def __init__(self, tooltip: str = "", parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("CaptionTriangleButton")
        self.setWindowFlags(
            Qt.WindowType.Tool
            | Qt.WindowType.FramelessWindowHint
            | Qt.WindowType.WindowDoesNotAcceptFocus
        )
        self.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating, True)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
        self.setAttribute(Qt.WidgetAttribute.WA_Hover, True)
        # Tool + Frameless 上系统 tooltip 经常不弹，改手动 QToolTip
        self.setAttribute(Qt.WidgetAttribute.WA_AlwaysShowToolTips, True)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setFixedSize(_CAPTION_SLOT_W, _CAPTION_SLOT_H)
        self.setMouseTracking(True)
        self._tip = (tooltip or "").strip()
        if self._tip:
            self.setToolTip(self._tip)
        self._hover = False
        self._owner_hwnd = 0
        self._tip_timer = QTimer(self)
        self._tip_timer.setSingleShot(True)
        self._tip_timer.setInterval(400)
        self._tip_timer.timeout.connect(self._show_tip)

    def set_tip(self, text: str) -> None:
        self._tip = (text or "").strip()
        self.setToolTip(self._tip)

    def _show_tip(self) -> None:
        if not self._tip or not self._hover or not self.isVisible():
            return
        # 浮层 Tool 窗：显式弹出，避免系统 tooltip 被吃掉
        QToolTip.showText(QCursor.pos() + QPoint(12, 16), self._tip, self)

    def enterEvent(self, event) -> None:  # noqa: N802
        self._hover = True
        self.update()
        if self._tip:
            self._tip_timer.start()
        super().enterEvent(event)

    def leaveEvent(self, event) -> None:  # noqa: N802
        self._hover = False
        self._tip_timer.stop()
        QToolTip.hideText()
        self.update()
        super().leaveEvent(event)

    def mousePressEvent(self, event) -> None:  # noqa: N802
        if event.button() == Qt.MouseButton.LeftButton:
            self._tip_timer.stop()
            QToolTip.hideText()
            self.activated.emit()
            event.accept()
            return
        super().mousePressEvent(event)

    def mouseReleaseEvent(self, event) -> None:  # noqa: N802
        # 按下已触发；吞掉 release，避免穿透到下层微信标题栏
        if event.button() == Qt.MouseButton.LeftButton:
            event.accept()
            return
        super().mouseReleaseEvent(event)

    def paintEvent(self, event) -> None:  # noqa: N802
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        # Win + WA_TranslucentBackground：全透明像素不吃鼠标。
        # 先铺一层几乎看不见的底色，让整块槽位都可命中。
        p.fillRect(self.rect(), QColor(255, 255, 255, 1))
        if self._hover:
            p.fillRect(self.rect(), QColor(0, 0, 0, 22))
        pen = QPen(QColor(150, 150, 150))
        pen.setWidthF(1.2)
        pen.setJoinStyle(Qt.PenJoinStyle.MiterJoin)
        p.setPen(pen)
        p.setBrush(Qt.BrushStyle.NoBrush)
        cx, cy = self.width() / 2.0, self.height() / 2.0
        half_w, half_h = 5.5, 4.5
        poly = QPolygonF(
            [
                QPointF(cx, cy - half_h),
                QPointF(cx + half_w, cy + half_h * 0.7),
                QPointF(cx - half_w, cy + half_h * 0.7),
            ]
        )
        p.drawPolygon(poly)
        p.end()

    def _dpr_at(self, phys_x: int, phys_y: int) -> float:
        screen = QGuiApplication.screenAt(QPoint(phys_x, phys_y))
        dpr = float(screen.devicePixelRatio()) if screen is not None else 1.0
        return dpr if dpr > 0 else 1.0

    def pin_above_hwnd(self, host_hwnd: int) -> None:
        """把 △ 插到宿主窗正上方，跟随宿主 Z 序（不 StayOnTop、不改宿主 Z 序）。"""
        self._owner_hwnd = int(host_hwnd or 0)
        if not self._owner_hwnd or not self.isVisible():
            return
        try:
            import win32con
            import win32gui

            me = int(self.winId())
            host = int(host_hwnd)
            if not win32gui.IsWindow(me) or not win32gui.IsWindow(host):
                return
            if win32gui.GetWindow(host, win32con.GW_HWNDPREV) == me:
                return
            flags = (
                win32con.SWP_NOMOVE
                | win32con.SWP_NOSIZE
                | win32con.SWP_NOACTIVATE
            )
            # 只动 △：插到宿主当前上方窗之后；绝不 SetWindowPos(宿主, …)
            above = win32gui.GetWindow(host, win32con.GW_HWNDPREV)
            insert_after = above if above else win32con.HWND_TOP
            win32gui.SetWindowPos(me, insert_after, 0, 0, 0, 0, flags)
        except Exception:
            pass

    def _place_left_of_caption(
        self,
        *,
        right_phys: int,
        top_phys: int,
        dpr: float,
        clear_slots: int,
        above_hwnd: int = 0,
    ) -> None:
        """
        clear_slots: 右边已有系统按钮格数（从右数）。
        △ 紧贴在其左（几乎无缝），右缘对齐最小化左缘。
        """
        # Win 标题栏系统钮格宽约 46
        sys_slot = int(46 * dpr)
        bw_phys = int(self.width() * dpr)
        bh_phys = int(self.height() * dpr)
        # 紧贴：本钮右缘 = 最小化左缘，再右挪 2px 吃掉缝隙
        x_phys = right_phys - clear_slots * sys_slot - bw_phys + int(2 * dpr)
        # 垂直居中到标题栏（约 32px）
        title_h = int(32 * dpr)
        y_phys = top_phys + max(0, (title_h - bh_phys) // 2)
        self.move(int(x_phys / dpr), int(y_phys / dpr))
        if not self.isVisible():
            self.show()
        if above_hwnd:
            self.pin_above_hwnd(above_hwnd)


class WeChatWakeButton(CaptionTriangleButton):
    """贴在微信标题栏：置顶左侧，点击展开副驾。"""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__("展开 Joy 副驾", parent)

    def follow_wechat(self, hwnd: int) -> None:
        rect = window_screen_rect(hwnd)
        if rect is None:
            self.hide()
            return
        left, top, right, _bottom = rect
        dpr = self._dpr_at(int((left + right) / 2), int(top + 20))
        # 右起 4 格：关/最大化/最小化/置顶 → △ 在置顶左隔壁
        self._place_left_of_caption(
            right_phys=right,
            top_phys=top,
            dpr=dpr,
            clear_slots=4,
            above_hwnd=int(hwnd),
        )


class DockCollapseButton(CaptionTriangleButton):
    """贴在侧窗标题栏：最小化左侧，点击收起副驾。"""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__("收起副驾（展开请点微信标题栏 △）", parent)

    def follow_host(self, host: QWidget) -> None:
        """跟随侧窗外框右上角（系统 最小化/最大化/关闭 左侧）。"""
        if not host.isVisible():
            self.hide()
            return
        fg = host.frameGeometry()
        # frameGeometry 已是逻辑像素；换算到与 screenAt 一致的物理坐标
        dpr = float(host.devicePixelRatioF() or 1.0)
        if dpr <= 0:
            dpr = 1.0
        right_phys = int((fg.x() + fg.width()) * dpr)
        top_phys = int(fg.y() * dpr)
        try:
            host_hwnd = int(host.winId())
        except Exception:
            host_hwnd = 0
        # 右起 3 格：关/最大化/最小化 → △ 在最小化左隔壁
        self._place_left_of_caption(
            right_phys=right_phys,
            top_phys=top_phys,
            dpr=dpr,
            clear_slots=3,
            above_hwnd=host_hwnd,
        )
