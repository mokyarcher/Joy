"""副驾动态指引：半透明遮罩 + 高亮目标 + 分步文案（不改「使用说明」正文）。"""

from __future__ import annotations

from dataclasses import dataclass

from PySide6.QtCore import QPoint, QRect, QRectF, Qt, Signal
from PySide6.QtGui import QColor, QFont, QPainter, QPainterPath, QPen
from PySide6.QtWidgets import (
    QHBoxLayout,
    QLabel,
    QPushButton,
    QVBoxLayout,
    QWidget,
)


@dataclass(frozen=True)
class CoachStep:
    """一步指引。target_ids 对应 PreviewWindow 解析的控件键；空则只高亮整窗。"""

    title: str
    body: str
    target_ids: tuple[str, ...] = ()


def default_dock_steps() -> list[CoachStep]:
    return [
        CoachStep(
            title="欢迎使用 Joy 副驾",
            body="侧窗会贴在微信旁边。下面用几步标出关键位置，可随时跳过。",
            target_ids=(),
        ),
        CoachStep(
            title="顶栏：会话与更多",
            body="左侧显示当前会话；右侧菜单按钮，点击下拉菜单进行各项操作配置。",
            target_ids=("chat_title", "btn_more"),
        ),
        CoachStep(
            title="收起到微信顶部",
            body="标题栏中间的 △：点击可将侧窗收起到微信顶部；再点微信旁的 △ 即可展开。",
            target_ids=("collapse_btn",),
        ),
        CoachStep(
            title="快捷按钮栏",
            body="快捷按钮栏，支持自定义（详情至设置页查看）。",
            target_ids=("compose_bar",),
        ),
        CoachStep(
            title="驾驶灯与急停",
            body="显示当前驾驶模式，同时可以快捷切换；红色电源提供急停操作。",
            target_ids=("drive_badge", "estop_badge"),
        ),
        CoachStep(
            title="发送",
            body="快速发送消息。",
            target_ids=("btn_compose_send",),
        ),
    ]


def default_normal_steps() -> list[CoachStep]:
    """大窗无标题栏收起 △，跳过该步。"""
    dock = default_dock_steps()
    return [
        CoachStep(
            title="欢迎使用 Joy 副驾",
            body="当前是大窗模式，预览区更大。也可在 ··· 里切回侧窗贴微信。",
            target_ids=(),
        ),
        *[s for s in dock[1:] if "collapse_btn" not in s.target_ids],
    ]


class CoachTourOverlay(QWidget):
    """盖在副驾客户区上的分步指引。"""

    finished = Signal(bool)  # True=走完；False=跳过

    def __init__(self, host: QWidget) -> None:
        super().__init__(host)
        self._host = host
        self.setObjectName("CoachTourOverlay")
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)

        self._steps: list[CoachStep] = []
        self._index = 0
        self._hole = QRect()
        self._resolve = None  # Callable[[str], QWidget | None]

        self._card = QWidget(self)
        self._card.setObjectName("CoachCard")
        card_lay = QVBoxLayout(self._card)
        card_lay.setContentsMargins(14, 12, 14, 12)
        card_lay.setSpacing(8)

        self._step_lbl = QLabel("")
        self._step_lbl.setObjectName("CoachStepIndex")
        self._title = QLabel("")
        self._title.setObjectName("CoachTitle")
        self._title.setWordWrap(True)
        self._body = QLabel("")
        self._body.setObjectName("CoachBody")
        self._body.setWordWrap(True)

        card_lay.addWidget(self._step_lbl)
        card_lay.addWidget(self._title)
        card_lay.addWidget(self._body)

        btn_row = QHBoxLayout()
        btn_row.setSpacing(8)
        self._btn_skip = QPushButton("跳过")
        self._btn_skip.setObjectName("CoachGhost")
        self._btn_skip.setCursor(Qt.CursorShape.PointingHandCursor)
        self._btn_skip.clicked.connect(self._on_skip)
        self._btn_next = QPushButton("下一步")
        self._btn_next.setObjectName("CoachPrimary")
        self._btn_next.setCursor(Qt.CursorShape.PointingHandCursor)
        self._btn_next.clicked.connect(self._on_next)
        btn_row.addWidget(self._btn_skip)
        btn_row.addStretch(1)
        btn_row.addWidget(self._btn_next)
        card_lay.addLayout(btn_row)

        self._card.setStyleSheet(
            """
            QWidget#CoachCard {
                background: #FFFFFF;
                border: 1px solid #D8D8D8;
                border-radius: 10px;
            }
            QLabel#CoachStepIndex {
                color: #07C160;
                font-size: 12px;
                font-family: "Microsoft YaHei UI","微软雅黑","Segoe UI";
            }
            QLabel#CoachTitle {
                color: #191919;
                font-size: 15px;
                font-weight: 600;
                font-family: "Microsoft YaHei UI","微软雅黑","Segoe UI";
            }
            QLabel#CoachBody {
                color: #666666;
                font-size: 13px;
                font-family: "Microsoft YaHei UI","微软雅黑","Segoe UI";
            }
            QPushButton#CoachPrimary {
                background: #00C375;
                color: #FFFFFF;
                border: none;
                border-radius: 6px;
                padding: 6px 14px;
                font-size: 13px;
                min-width: 72px;
            }
            QPushButton#CoachPrimary:hover { background: #00B06A; }
            QPushButton#CoachGhost {
                background: transparent;
                color: #888888;
                border: none;
                padding: 6px 8px;
                font-size: 13px;
            }
            QPushButton#CoachGhost:hover { color: #191919; }
            """
        )
        self.hide()

    def start(
        self,
        steps: list[CoachStep],
        resolve_target,
    ) -> None:
        if not steps:
            return
        self._steps = list(steps)
        self._index = 0
        self._resolve = resolve_target
        self.setGeometry(self._host.rect())
        self.show()
        self.raise_()
        self.setFocus(Qt.FocusReason.ActiveWindowFocusReason)
        self._show_step()

    def is_active(self) -> bool:
        return self.isVisible() and bool(self._steps)

    def relayout(self) -> None:
        if not self.isVisible():
            return
        self.setGeometry(self._host.rect())
        self._show_step()

    def paintEvent(self, event) -> None:  # noqa: N802
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        dim = QColor(0, 0, 0, 110)
        full = QPainterPath()
        full.addRect(QRectF(self.rect()))
        if self._hole.isValid() and self._hole.width() > 4 and self._hole.height() > 4:
            hole = QPainterPath()
            hole.addRoundedRect(QRectF(self._hole).adjusted(-4, -4, 4, 4), 8, 8)
            full = full.subtracted(hole)
            p.fillPath(full, dim)
            pen = QPen(QColor("#07C160"), 2.0)
            p.setPen(pen)
            p.setBrush(Qt.BrushStyle.NoBrush)
            p.drawRoundedRect(QRectF(self._hole).adjusted(-4, -4, 4, 4), 8, 8)
        else:
            p.fillPath(full, dim)
        p.end()

    def resizeEvent(self, event) -> None:  # noqa: N802
        super().resizeEvent(event)
        if self.isVisible():
            self._show_step()

    def keyPressEvent(self, event) -> None:  # noqa: N802
        key = event.key()
        if key in (Qt.Key.Key_Escape,):
            self._on_skip()
            return
        if key in (Qt.Key.Key_Return, Qt.Key.Key_Enter, Qt.Key.Key_Right, Qt.Key.Key_Space):
            self._on_next()
            return
        super().keyPressEvent(event)

    def mousePressEvent(self, event) -> None:  # noqa: N802
        # 点遮罩空白不关闭，避免误触；只让卡片按钮响应
        event.accept()

    def _show_step(self) -> None:
        if not self._steps:
            return
        step = self._steps[self._index]
        n = len(self._steps)
        self._step_lbl.setText(f"{self._index + 1} / {n}")
        self._title.setText(step.title)
        self._body.setText(step.body)
        last = self._index >= n - 1
        self._btn_next.setText("完成" if last else "下一步")

        self._hole = self._union_targets(step.target_ids)
        self._place_card()
        self.update()

    def _union_targets(self, ids: tuple[str, ...]) -> QRect:
        if not ids or self._resolve is None:
            return QRect()
        rects: list[QRect] = []
        for tid in ids:
            w = self._resolve(tid)
            if w is None:
                continue
            # 独立 Tool 浮层（如标题栏 △）用全局坐标；子控件同样适用
            try:
                visible = bool(w.isVisible())
            except RuntimeError:
                continue
            if not visible and tid != "collapse_btn":
                continue
            g = w.mapToGlobal(QPoint(0, 0))
            top_left = self.mapFromGlobal(g)
            r = QRect(top_left, w.size())
            # 标题栏 △ 落在客户区外：在顶沿做示意高亮，引导看向上方
            if tid == "collapse_btn" or not self.rect().intersects(r):
                r = QRect(max(8, self.width() // 2 - 32), 4, 64, 26)
            if r.width() > 2 and r.height() > 2:
                rects.append(r)
        if not rects:
            return QRect()
        out = QRect(rects[0])
        for r in rects[1:]:
            out = out.united(r)
        # 夹在 overlay 内
        return out.intersected(self.rect().adjusted(4, 2, -4, -6))

    def _place_card(self) -> None:
        max_w = max(200, min(300, self.width() - 24))
        self._card.setFixedWidth(max_w)
        self._card.adjustSize()
        cw = self._card.width()
        ch = self._card.sizeHint().height()
        self._card.resize(cw, max(ch, self._card.height()))

        margin = 12
        if self._hole.isValid():
            # 优先放在高亮下方；放不下则上方 / 居中
            x = self._hole.center().x() - cw // 2
            y = self._hole.bottom() + 12
            if y + self._card.height() > self.height() - margin:
                y = self._hole.top() - self._card.height() - 12
            if y < margin:
                y = max(margin, (self.height() - self._card.height()) // 2)
        else:
            x = (self.width() - cw) // 2
            y = max(margin, int(self.height() * 0.28))

        x = max(margin, min(x, self.width() - cw - margin))
        y = max(margin, min(y, self.height() - self._card.height() - margin))
        self._card.move(x, y)
        self._card.raise_()
        self._card.show()

    def _on_next(self) -> None:
        if self._index >= len(self._steps) - 1:
            self._finish(completed=True)
            return
        self._index += 1
        self._show_step()

    def _on_skip(self) -> None:
        self._finish(completed=False)

    def _finish(self, *, completed: bool) -> None:
        self.hide()
        self._steps = []
        self._resolve = None
        self.finished.emit(bool(completed))
