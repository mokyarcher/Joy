"""Joy 主界面：模型 / 聊天 / 使用说明，默认三路共用一套 API。"""

from __future__ import annotations

import os
import sys
import time
from pathlib import Path

from PySide6.QtCore import QPoint, QProcess, Qt, QThread, QTimer, Signal
from PySide6.QtGui import (
    QAction,
    QColor,
    QFont,
    QIcon,
    QKeySequence,
    QPainter,
    QPixmap,
    QPolygon,
)
from PySide6.QtWidgets import (
    QApplication,
    QComboBox,
    QFormLayout,
    QFrame,
    QGroupBox,
    QHBoxLayout,
    QKeySequenceEdit,
    QLabel,
    QLineEdit,
    QMenu,
    QMessageBox,
    QPushButton,
    QScrollArea,
    QSpinBox,
    QSystemTrayIcon,
    QTabWidget,
    QTextBrowser,
    QVBoxLayout,
    QWidget,
)

from joy.core.chat_presets import (
    RELATION_PRESETS,
    STYLE_PRESETS,
    normalize_relation,
    normalize_style,
)
from joy.core.config import JoyConfig, ProviderSlotConfig, get_api_key
from joy.core.secrets import save_user_env_batch
from joy.app.toggle_switch import ToggleSwitch
from joy.providers.presets import TEXT_PRESETS, VISION_PRESETS, resolve_endpoint


def _combo_arrow_url() -> str:
    """生成下拉箭头图标路径，供 QSS url() 使用。"""
    path = Path(os.environ.get("TEMP") or os.environ.get("TMP") or ".") / "joy_combo_down.png"
    try:
        if not path.is_file():
            pm = QPixmap(14, 14)
            pm.fill(QColor(0, 0, 0, 0))
            p = QPainter(pm)
            p.setRenderHint(QPainter.RenderHint.Antialiasing)
            p.setBrush(QColor("#4B5563"))
            p.setPen(Qt.PenStyle.NoPen)
            p.drawPolygon(QPolygon([QPoint(2, 4), QPoint(12, 4), QPoint(7, 11)]))
            p.end()
            pm.save(str(path), "PNG")
    except Exception:
        return ""
    return path.resolve().as_posix()


def _theme_qss() -> str:
    arrow = _combo_arrow_url()
    qss = _THEME
    if arrow:
        return qss.replace("__JOY_COMBO_ARROW__", arrow)
    return qss.replace(
        "QComboBox::down-arrow {\n    image: url(__JOY_COMBO_ARROW__);\n    width: 12px;\n    height: 12px;\n}\n",
        "",
    )


_THEME = """
QWidget#JoyRoot {
    background: #F4F5F7;
    color: #1A1D26;
    font-size: 13px;
}
QLabel#BrandTitle {
    font-size: 22px;
    font-weight: 700;
    letter-spacing: 0.5px;
    color: #12151C;
}
QLabel#BrandMark {
    color: #0D9488;
    font-size: 22px;
    font-weight: 700;
}
QTabWidget::pane {
    border: 1px solid #E2E5EB;
    border-radius: 10px;
    background: #FFFFFF;
    top: -1px;
}
QTabBar::tab {
    background: transparent;
    color: #6B7280;
    padding: 10px 18px;
    margin-right: 2px;
    border: none;
    font-weight: 500;
}
QTabBar::tab:selected {
    color: #0D9488;
    border-bottom: 2px solid #0D9488;
}
QTabBar::tab:hover:!selected {
    color: #374151;
}
QScrollArea#SettingsScroll {
    background: transparent;
    border: none;
}
QScrollArea#SettingsScroll > QWidget > QWidget {
    background: transparent;
}
QScrollBar:vertical {
    background: transparent;
    width: 10px;
    margin: 4px 2px 4px 0;
}
QScrollBar::handle:vertical {
    background: #C5CAD3;
    border-radius: 4px;
    min-height: 28px;
}
QScrollBar::handle:vertical:hover {
    background: #9CA3AF;
}
QScrollBar::add-line:vertical,
QScrollBar::sub-line:vertical {
    height: 0;
}
QScrollBar::add-page:vertical,
QScrollBar::sub-page:vertical {
    background: transparent;
}
QGroupBox {
    font-weight: 600;
    border: 1px solid #E8EAEE;
    border-radius: 10px;
    margin-top: 18px;
    padding: 16px 12px 12px 12px;
    background: #FAFBFC;
}
QGroupBox::title {
    subcontrol-origin: margin;
    subcontrol-position: top left;
    left: 12px;
    top: 2px;
    padding: 2px 8px 2px 4px;
    color: #374151;
    /* 盖住分组顶边，避免可勾选标题的勾选框顶边被边框裁成「缺顶」 */
    background: #FAFBFC;
}
QGroupBox::indicator {
    width: 15px;
    height: 15px;
    border-radius: 4px;
    border: 1px solid #C5CAD3;
    background: #FFFFFF;
    margin-right: 6px;
}
QGroupBox::indicator:checked {
    background: #0D9488;
    border-color: #0D9488;
}
QLineEdit, QComboBox, QSpinBox {
    background: #FFFFFF;
    border: 1px solid #D8DCE3;
    border-radius: 8px;
    padding: 7px 10px;
    min-height: 18px;
    selection-background-color: #99F6E4;
}
QComboBox {
    padding-right: 36px;
}
QLineEdit:focus, QComboBox:focus, QSpinBox:focus {
    border: 1px solid #0D9488;
}
QComboBox::drop-down {
    subcontrol-origin: padding;
    subcontrol-position: top right;
    width: 30px;
    border: none;
    border-left: 1px solid #D8DCE3;
    border-top-right-radius: 7px;
    border-bottom-right-radius: 7px;
    background: #F3F4F6;
}
QComboBox::drop-down:hover {
    background: #E5E7EB;
}
QComboBox::drop-down:pressed {
    background: #D1D5DB;
}
QComboBox::down-arrow {
    image: url(__JOY_COMBO_ARROW__);
    width: 12px;
    height: 12px;
}
QPushButton {
    background: #FFFFFF;
    border: 1px solid #D8DCE3;
    border-radius: 8px;
    padding: 8px 16px;
    font-weight: 500;
    color: #1A1D26;
}
QPushButton:hover {
    background: #F8FAFB;
    border-color: #B8BEC8;
}
QPushButton:pressed { background: #EEF0F3; }
QPushButton#PrimaryBtn {
    background: #0D9488;
    color: white;
    border: none;
    padding: 9px 20px;
    font-weight: 600;
}
QPushButton#PrimaryBtn:hover { background: #0F766E; }
QPushButton#PrimaryBtn:pressed { background: #115E59; }
QPushButton#GhostBtn {
    background: transparent;
    border: none;
    color: #6B7280;
    padding: 8px 10px;
}
QPushButton#GhostBtn:hover { color: #0D9488; }
QTextBrowser {
    background: transparent;
    border: none;
    color: #374151;
    font-size: 13px;
    line-height: 1.5;
}
"""


class _PingWorker(QThread):
    ok = Signal(str)
    err = Signal(str)

    def __init__(self, cfg: JoyConfig, slot_name: str, parent=None) -> None:
        super().__init__(parent)
        self._cfg = cfg
        self._slot_name = slot_name

    def run(self) -> None:
        try:
            from joy.providers.resolve import ping_provider_slot
            from joy.util.logging import redact

            reply = ping_provider_slot(self._cfg, self._slot_name)
            self.ok.emit(reply[:200])
        except Exception as exc:
            from joy.util.logging import redact

            self.err.emit(redact(str(exc)))


class ProviderCard(QGroupBox):
    def __init__(
        self,
        title: str,
        slot: ProviderSlotConfig,
        presets: dict,
        parent: QWidget | None = None,
        *,
        key_hint: str = "",
        flat: bool = False,
    ) -> None:
        super().__init__("" if flat else title, parent)
        if flat:
            self.setFlat(True)
            self.setStyleSheet(
                "QGroupBox{border:none;margin:0;padding:0;background:transparent;}"
            )
        self._presets = presets

        self.preset = QComboBox()
        for pid, p in presets.items():
            self.preset.addItem(p.name, pid)
            tip = (p.notes or p.name).strip()
            if tip:
                self.preset.setItemData(
                    self.preset.count() - 1, tip, Qt.ItemDataRole.ToolTipRole
                )
        idx = self.preset.findData(slot.preset)
        if idx >= 0:
            self.preset.setCurrentIndex(idx)

        url, model = resolve_endpoint(presets, slot.preset, slot.base_url, slot.model)
        self.base_url = QLineEdit(url or "")
        # 官网固定地址也预填为正常可编辑文本，不要灰字只读
        self.base_url.setPlaceholderText("https://...")

        self.model = QComboBox()
        self.model.setEditable(True)
        self.model.setInsertPolicy(QComboBox.InsertPolicy.NoInsert)
        self.model.lineEdit().setPlaceholderText("选择或输入模型 id")
        self._reload_models(slot.preset, model)

        self.api_key = QLineEdit(get_api_key(slot))
        self.api_key.setEchoMode(QLineEdit.EchoMode.Password)
        self.api_key.setPlaceholderText(key_hint or "API Key")

        form = QFormLayout(self)
        form.setContentsMargins(10, 12, 10, 10)
        form.setSpacing(10)
        form.setLabelAlignment(Qt.AlignmentFlag.AlignRight)
        form.addRow("预设", self.preset)
        form.addRow("Base URL", self.base_url)
        form.addRow("模型", self.model)
        form.addRow("API Key", self.api_key)

        self.preset.currentIndexChanged.connect(self._on_preset_changed)

    def _model_text(self) -> str:
        return self.model.currentText().strip()

    def _set_model_text(self, text: str) -> None:
        text = (text or "").strip()
        if not text:
            return
        idx = self.model.findText(text)
        if idx >= 0:
            self.model.setCurrentIndex(idx)
        else:
            self.model.setEditText(text)

    def _reload_models(self, preset_id: str, current: str = "") -> None:
        """按预设填充模型下拉；保留当前值；可编辑以便手填。"""
        p = self._presets.get(preset_id)
        choices: list[str] = []
        if p:
            if p.default_model:
                choices.append(p.default_model)
            for mid in p.models:
                if mid and mid not in choices:
                    choices.append(mid)
        cur = (current or "").strip()
        if cur and cur not in choices:
            choices.insert(0, cur)

        self.model.blockSignals(True)
        self.model.clear()
        for mid in choices:
            self.model.addItem(mid)
        if cur:
            self._set_model_text(cur)
        elif choices:
            self.model.setCurrentIndex(0)
        else:
            self.model.setEditText("")
        self.model.blockSignals(False)

    def _on_preset_changed(self) -> None:
        pid = self.preset.currentData()
        p = self._presets.get(pid)
        if not p:
            return
        # 切换预设：预填官网 Base URL + 默认模型（正常文本，可改）
        if p.base_url:
            self.base_url.setText(p.base_url)
        elif pid == "custom":
            self.base_url.clear()
        self._reload_models(pid, p.default_model or "")

    def apply_to(self, slot: ProviderSlotConfig) -> None:
        slot.preset = self.preset.currentData() or "custom"
        slot.base_url = self.base_url.text().strip()
        slot.model = self._model_text()
        key = self.api_key.text().strip()
        if slot.api_key_env and key:
            os.environ[slot.api_key_env] = key

    def key_value(self) -> str:
        return self.api_key.text().strip()

    def resolved(self) -> tuple[str, str]:
        return resolve_endpoint(
            self._presets,
            self.preset.currentData() or "custom",
            self.base_url.text().strip(),
            self._model_text(),
        )

    def copy_from(self, other: ProviderCard) -> None:
        idx = self.preset.findData(other.preset.currentData())
        if idx >= 0:
            self.preset.blockSignals(True)
            self.preset.setCurrentIndex(idx)
            self.preset.blockSignals(False)
        self.base_url.setText(other.base_url.text())
        self._reload_models(
            other.preset.currentData() or "custom",
            other._model_text(),
        )
        if other.api_key.text().strip():
            self.api_key.setText(other.api_key.text())

    def load_from_slot(self, slot: ProviderSlotConfig, *, clear_key: bool = False) -> None:
        """用槽位配置刷新控件（恢复默认用）。"""
        idx = self.preset.findData(slot.preset)
        self.preset.blockSignals(True)
        if idx >= 0:
            self.preset.setCurrentIndex(idx)
        self.preset.blockSignals(False)
        url, model = resolve_endpoint(
            self._presets, slot.preset, slot.base_url, slot.model
        )
        self.base_url.setText(url or "")
        self._reload_models(slot.preset, model)
        if clear_key:
            self.api_key.clear()
        else:
            self.api_key.setText(get_api_key(slot))


def _providers_look_shared(cfg: JoyConfig) -> bool:
    a, b, c = cfg.providers.vision, cfg.providers.analyze, cfg.providers.draft
    return a.preset == b.preset == c.preset and (
        (a.base_url or "") == (b.base_url or "") == (c.base_url or "")
    ) and ((a.model or "") == (b.model or "") == (c.model or ""))


def _make_tray_icon() -> QIcon:
    """绿色圆点 + J，无外部 ico 资源。"""
    pm = QPixmap(64, 64)
    pm.fill(QColor(0, 0, 0, 0))
    p = QPainter(pm)
    p.setRenderHint(QPainter.RenderHint.Antialiasing)
    p.setBrush(QColor("#07C160"))
    p.setPen(Qt.PenStyle.NoPen)
    p.drawEllipse(4, 4, 56, 56)
    p.setPen(QColor("#FFFFFF"))
    f = QFont("Segoe UI", 24, QFont.Weight.Bold)
    p.setFont(f)
    p.drawText(pm.rect(), int(Qt.AlignmentFlag.AlignCenter), "J")
    p.end()
    return QIcon(pm)


class SettingsWindow(QWidget):
    def __init__(self, config: JoyConfig) -> None:
        super().__init__()
        self.cfg = config
        self.setObjectName("JoyRoot")
        self.setWindowTitle("Joy")
        self.setMinimumSize(540, 600)
        self.resize(580, 660)
        self.setStyleSheet(_theme_qss())
        font = QFont("Segoe UI", 10)
        self.setFont(font)
        self.setWindowIcon(_make_tray_icon())

        root = QVBoxLayout(self)
        root.setContentsMargins(20, 18, 20, 16)
        root.setSpacing(14)

        header = QHBoxLayout()
        header.setSpacing(6)
        mark = QLabel("Joy")
        mark.setObjectName("BrandMark")
        title = QLabel("看屏副驾")
        title.setObjectName("BrandTitle")
        header.addWidget(mark)
        header.addWidget(title)
        header.addStretch(1)
        self.btn_open = QPushButton("打开副驾")
        self.btn_open.setObjectName("PrimaryBtn")
        self.btn_open.setCursor(Qt.CursorShape.PointingHandCursor)
        self.btn_open.clicked.connect(self._open_preview)
        header.addWidget(self.btn_open)
        root.addLayout(header)

        self.tabs = QTabWidget()
        self.tabs.addTab(self._build_model_tab(), "模型")
        self.tabs.addTab(self._build_chat_tab(), "聊天与发送")
        self.tabs.addTab(self._build_prefs_tab(), "设置")
        self.tabs.addTab(self._build_help_tab(), "使用说明")
        root.addWidget(self.tabs, stretch=1)

        self._preview: QWidget | None = None
        self._ping_worker: _PingWorker | None = None
        self._tray: QSystemTrayIcon | None = None
        self._sync_share_visibility()
        self._setup_tray()

    def _wrap_in_scroll(self, inner: QWidget) -> QScrollArea:
        """设置页内容可滚动，避免塞进固定视窗被裁切。"""
        scroll = QScrollArea()
        scroll.setObjectName("SettingsScroll")
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        scroll.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        scroll.setWidget(inner)
        return scroll

    def _add_switch_row(
        self,
        parent_lay: QVBoxLayout,
        label: str,
        *,
        checked: bool,
        tooltip: str = "",
    ) -> ToggleSwitch:
        """标签 + 右侧药丸滑块（与副驾自动模式同款）。"""
        row = QHBoxLayout()
        row.setSpacing(10)
        lbl = QLabel(label)
        lbl.setStyleSheet("font-weight:500;color:#1A1D26;")
        sw = ToggleSwitch()
        sw.setChecked(bool(checked))
        if tooltip:
            sw.setToolTip(tooltip)
            lbl.setToolTip(tooltip)
        row.addWidget(lbl, stretch=1)
        row.addWidget(sw, stretch=0, alignment=Qt.AlignmentFlag.AlignVCenter)
        parent_lay.addLayout(row)
        return sw

    def _hint(self, text: str) -> QLabel:
        """一行功能说明：说清用途，不写实现细节。"""
        lbl = QLabel(text)
        lbl.setWordWrap(True)
        lbl.setStyleSheet("color:#6B7280;font-size:12px;")
        return lbl

    def _add_page_actions(
        self,
        parent_lay: QVBoxLayout,
        *,
        on_save,
        on_reset,
    ) -> None:
        """页底「保存配置 / 恢复默认」（放在分区框外）。"""
        row = QHBoxLayout()
        row.setSpacing(8)
        btn_save = QPushButton("保存配置")
        btn_save.setObjectName("PrimaryBtn")
        btn_save.setCursor(Qt.CursorShape.PointingHandCursor)
        btn_save.clicked.connect(on_save)
        btn_reset = QPushButton("恢复默认")
        btn_reset.setCursor(Qt.CursorShape.PointingHandCursor)
        btn_reset.clicked.connect(on_reset)
        row.addWidget(btn_save)
        row.addStretch(1)
        row.addWidget(btn_reset)
        parent_lay.addLayout(row)

    def _build_model_tab(self) -> QWidget:
        page = QWidget()
        lay = QVBoxLayout(page)
        lay.setContentsMargins(16, 16, 16, 12)
        lay.setSpacing(12)

        api_box = QGroupBox("API")
        api_lay = QVBoxLayout(api_box)
        api_lay.setSpacing(10)
        api_lay.setContentsMargins(12, 14, 12, 12)

        self.share_apis = self._add_switch_row(
            api_lay,
            "三路共用同一套 API",
            checked=_providers_look_shared(self.cfg),
            tooltip="开：识图 / 判断 / 起草共用；关：可分别设置",
        )
        self.share_apis.toggled.connect(self._on_share_toggled)
        api_lay.addWidget(self._hint("开启后三路共用下方配置；关闭可分别设置。"))

        self.shared_card = ProviderCard(
            "API",
            self.cfg.providers.vision,
            VISION_PRESETS,
            key_hint="API Key",
            flat=True,
        )
        api_lay.addWidget(self.shared_card)

        self.advanced = QTabWidget()
        self.vision = ProviderCard(
            "识图", self.cfg.providers.vision, VISION_PRESETS, flat=True
        )
        self.analyze = ProviderCard(
            "判断",
            self.cfg.providers.analyze,
            TEXT_PRESETS,
            key_hint="可留空",
            flat=True,
        )
        self.draft = ProviderCard(
            "起草",
            self.cfg.providers.draft,
            TEXT_PRESETS,
            key_hint="可留空",
            flat=True,
        )
        self.advanced.addTab(self.vision, "识图")
        self.advanced.addTab(self.analyze, "判断")
        self.advanced.addTab(self.draft, "起草")
        api_lay.addWidget(self.advanced)

        self.btn_ping = QPushButton("测试连通")
        self.btn_ping.setCursor(Qt.CursorShape.PointingHandCursor)
        self.btn_ping.setToolTip(
            "共用模式测当前配置；分路模式测当前选中的识图 / 判断 / 起草"
        )
        self.btn_ping.clicked.connect(self._ping_active_api)
        api_lay.addWidget(self.btn_ping, alignment=Qt.AlignmentFlag.AlignLeft)

        lay.addWidget(api_box)
        self._add_page_actions(
            lay, on_save=self._save_model, on_reset=self._reset_model
        )
        lay.addStretch(1)
        return self._wrap_in_scroll(page)

    def _on_share_toggled(self, shared: bool) -> None:
        if shared:
            self.shared_card.copy_from(self.vision)
        else:
            self.vision.copy_from(self.shared_card)
            self.analyze.copy_from(self.shared_card)
            self.draft.copy_from(self.shared_card)
        self._sync_share_visibility()

    def _sync_share_visibility(self) -> None:
        shared = self.share_apis.isChecked()
        self.shared_card.setVisible(shared)
        self.advanced.setVisible(not shared)

    def _build_chat_tab(self) -> QWidget:
        page = QWidget()
        lay = QVBoxLayout(page)
        lay.setContentsMargins(16, 16, 16, 12)
        lay.setSpacing(12)

        basic = QGroupBox("对话")
        form = QFormLayout(basic)
        form.setSpacing(10)
        form.setLabelAlignment(Qt.AlignmentFlag.AlignRight)
        self.relation = QComboBox()
        for pid, label, _ in RELATION_PRESETS:
            self.relation.addItem(label, pid)
        idx = self.relation.findData(normalize_relation(self.cfg.chat.relation))
        self.relation.setCurrentIndex(idx if idx >= 0 else 0)

        self.style = QComboBox()
        for pid, label, _ in STYLE_PRESETS:
            self.style.addItem(label, pid)
        idx = self.style.findData(normalize_style(self.cfg.chat.style))
        self.style.setCurrentIndex(idx if idx >= 0 else 0)

        self.ctx_n = QSpinBox()
        self.ctx_n.setRange(3, 30)
        self.ctx_n.setValue(self.cfg.chat.context_messages)
        form.addRow("关系", self.relation)
        form.addRow("说话风格", self.style)
        form.addRow("上下文条数", self.ctx_n)
        lay.addWidget(basic)

        mem = QGroupBox("同步上下文")
        mem_form = QFormLayout(mem)
        mem_form.setSpacing(10)
        mem_form.setLabelAlignment(Qt.AlignmentFlag.AlignRight)
        self.pull_scroll_n = QSpinBox()
        self.pull_scroll_n.setRange(6, 80)
        self.pull_scroll_n.setValue(
            int(getattr(self.cfg.chat, "memory_pull_scroll_steps", 24) or 24)
        )
        self.pull_scroll_n.setToolTip(
            "副驾「同步上下文」多选后上滑的步数；越大取样越多，耗时也更长"
        )
        mem_form.addRow("上滑取样步数", self.pull_scroll_n)
        mem_form.addRow(
            "",
            self._hint("步数越大，从微信多选拉取的历史越多（默认 24，原先固定 12）。"),
        )
        lay.addWidget(mem)

        adv = QGroupBox("微信布局")
        adv_lay = QVBoxLayout(adv)
        adv_lay.setSpacing(8)
        adv_lay.addWidget(self._hint("填入位置自动识别；偏了可点下方校准，或在副驾菜单用「重构布局」。"))
        self.btn_recal_layout = QPushButton("重新校准微信布局")
        self.btn_recal_layout.setCursor(Qt.CursorShape.PointingHandCursor)
        self.btn_recal_layout.clicked.connect(self._recalibrate_wechat_layout)
        self.layout_status = self._hint("")
        adv_lay.addWidget(self.btn_recal_layout)
        adv_lay.addWidget(self.layout_status)
        lay.addWidget(adv)

        self._add_page_actions(
            lay, on_save=self._save_chat, on_reset=self._reset_chat
        )
        lay.addStretch(1)
        self._refresh_layout_status()
        return self._wrap_in_scroll(page)

    def _build_prefs_tab(self) -> QWidget:
        """通用开关 / 参数（后续选项往这里加）。"""
        page = QWidget()
        lay = QVBoxLayout(page)
        lay.setContentsMargins(16, 16, 16, 12)
        lay.setSpacing(12)

        send = QGroupBox("发送行为")
        send_lay = QVBoxLayout(send)
        send_lay.setSpacing(8)
        # 旧版「允许一键发送」关闭时，视为不自动回车
        auto_send = bool(getattr(self.cfg.chat, "auto_send", True))
        if not self.cfg.chat.allow_one_click_send:
            auto_send = False
        self.opt_auto_send = self._add_switch_row(
            send_lay,
            "自动发送",
            checked=auto_send,
            tooltip="开：填入并回车发送；关：只填入，需你在微信回车",
        )
        send_lay.addWidget(self._hint("关闭后只填入微信，不会自动发送。"))
        lay.addWidget(send)

        boot = QGroupBox("启动")
        boot_lay = QVBoxLayout(boot)
        boot_lay.setSpacing(8)
        self.opt_auto_open = self._add_switch_row(
            boot_lay,
            "启动时自动打开副驾",
            checked=bool(getattr(self.cfg.ui, "auto_open_preview", False)),
            tooltip="已配置 API Key 时启动后自动打开副驾",
        )
        boot_lay.addWidget(self._hint("需已配置 API Key，否则仍只打开本设置页。"))
        lay.addWidget(boot)

        dock = QGroupBox("侧窗")
        dock_lay = QVBoxLayout(dock)
        dock_lay.setSpacing(8)
        self.opt_auto_collapse = self._add_switch_row(
            dock_lay,
            "最小化时自动收起至 △",
            checked=bool(getattr(self.cfg.ui, "auto_collapse_on_minimize", True)),
            tooltip="开：最小化收起到微信顶部 △；关：进任务栏",
        )
        dock_lay.addWidget(self._hint("仅侧窗模式生效。"))
        lay.addWidget(dock)

        drive = QGroupBox("驾驶 / 快捷键")
        drive_lay = QVBoxLayout(drive)
        drive_lay.setSpacing(8)
        drive_lay.addWidget(
            self._hint("智能驾驶：出候选后短暂停留，自动发送首条；紧急制动回手动。")
        )
        delay_row = QHBoxLayout()
        delay_row.addWidget(QLabel("智能发送延迟"))
        self.spin_smart_delay = QSpinBox()
        self.spin_smart_delay.setRange(300, 5000)
        self.spin_smart_delay.setSingleStep(100)
        self.spin_smart_delay.setSuffix(" ms")
        self.spin_smart_delay.setValue(
            int(getattr(self.cfg.ui, "smart_drive_delay_ms", 900) or 900)
        )
        delay_row.addWidget(self.spin_smart_delay)
        delay_row.addStretch(1)
        drive_lay.addLayout(delay_row)

        hk = getattr(self.cfg.ui, "hotkeys", None)
        form = QFormLayout()
        form.setSpacing(8)
        self.hk_analyze = QKeySequenceEdit(
            QKeySequence(getattr(hk, "manual_analyze", None) or "F1")
        )
        self.hk_auto = QKeySequenceEdit(
            QKeySequence(getattr(hk, "auto_drive", None) or "F2")
        )
        self.hk_smart = QKeySequenceEdit(
            QKeySequence(getattr(hk, "smart_drive", None) or "F3")
        )
        self.hk_estop = QKeySequenceEdit(
            QKeySequence(getattr(hk, "emergency_stop", None) or "F4")
        )
        for w in (self.hk_analyze, self.hk_auto, self.hk_smart, self.hk_estop):
            w.setClearButtonEnabled(True)
            w.setMaximumSequenceLength(1)
        form.addRow("手动分析", self.hk_analyze)
        form.addRow("自动驾驶", self.hk_auto)
        form.addRow("智能驾驶", self.hk_smart)
        form.addRow("紧急制动", self.hk_estop)
        drive_lay.addLayout(form)
        drive_lay.addWidget(self._hint("点输入框后按新键即可改。"))
        lay.addWidget(drive)

        self._add_page_actions(
            lay, on_save=self._save_prefs, on_reset=self._reset_prefs
        )
        lay.addStretch(1)
        return self._wrap_in_scroll(page)

    def _build_help_tab(self) -> QWidget:
        page = QWidget()
        lay = QVBoxLayout(page)
        lay.setContentsMargins(8, 8, 8, 8)

        body = QTextBrowser()
        body.setOpenExternalLinks(True)
        body.setHtml(
            """
            <style>
              h3 {
                color:#12151C; margin:18px 0 8px 0; font-size:14px;
                font-weight:700; letter-spacing:0.2px;
              }
              h3:first-child { margin-top:4px; }
              p { color:#4B5563; line-height:1.55; margin:0 0 8px 0; }
              ol, ul { color:#4B5563; line-height:1.6; margin:4px 0 8px 0; padding-left:1.35em; }
              li { margin:3px 0; }
              b { color:#1A1D26; font-weight:600; }
              code {
                background:#F3F4F6; color:#374151;
                padding:1px 5px; border-radius:4px; font-size:12px;
              }
              .lead { color:#1A1D26; font-size:13px; font-weight:500; }
              .muted { color:#9CA3AF; font-size:12px; }
            </style>

            <h3>三步上手</h3>
            <p class="lead">看屏识图 → 给出候选 → 你点选填入或发送。</p>
            <ol>
              <li>打开微信，在「模型」填好 API，点<strong>保存配置</strong></li>
              <li>右上角<strong>打开副驾</strong>（设置窗收进托盘绿 J，单击可再打开）</li>
              <li>点<strong>手动分析</strong>，或开自动驾驶 / 智能驾驶，再选用候选</li>
            </ol>

            <h3>驾驶模式</h3>
            <ul>
              <li><b>手动</b>：你点分析才跑</li>
              <li><b>自动驾驶</b>：对面有新消息时自动出候选，<b>不自动发</b></li>
              <li><b>智能驾驶</b>：出候选后短暂停留，自动发送首条</li>
              <li><b>紧急制动</b>：立刻回手动，并中止进行中的分析</li>
            </ul>
            <p class="muted">副驾底栏模式灯可点；快捷键默认 F1～F4，可在「设置」里改。</p>

            <h3>副驾</h3>
            <ul>
              <li><b>全视</b>：预览截图 + 操作 + 候选</li>
              <li><b>侧窗</b>：贴在微信旁（优先右侧），跟微信移动；可收起为标题栏 △</li>
              <li>右上角 <b>···</b>：分析 / 驾驶、上下文、重构布局、回到主界面、切换全视·侧窗</li>
              <li>点候选气泡 → 填入底栏；再点发送（是否回车看「设置 · 自动发送」）</li>
            </ul>

            <h3>模型</h3>
            <ul>
              <li>默认<strong>三路共用</strong>一套 API；关掉可分别设识图 / 判断 / 起草</li>
              <li>判断、起草的 Key 可留空，自动继承上一路</li>
              <li><b>测试连通</b>在 API 区内：共用测当前配置；分路测当前选中的那一路</li>
              <li>各页有独立的<strong>保存配置 / 恢复默认</strong></li>
            </ul>

            <h3>聊天与布局</h3>
            <ul>
              <li>关系、说话风格、上下文条数影响起草语气</li>
              <li>填入位置自动识别；偏了用副驾「重构布局」，或本页「重新校准微信布局」</li>
            </ul>

            <h3>上下文记忆</h3>
            <ul>
              <li>识图结果会写入当前联系人的本地记忆，供后续起草</li>
              <li><b>导入上下文</b>：粘贴聊天记录</li>
              <li><b>同步上下文</b>：先打开目标会话，再让副驾从微信拉取</li>
              <li><b>清除上下文</b>：只清本地，不影响微信</li>
            </ul>

            <h3>安全</h3>
            <ul>
              <li>API Key 只存本机环境变量，不进 <code>config.json</code></li>
              <li>不做支付 / 转账相关建议</li>
              <li>点 × 只收进托盘，不退出；托盘可打开设置、副驾，或退出 Joy</li>
            </ul>
            """
        )
        lay.addWidget(body, stretch=1)

        line = QFrame()
        line.setFrameShape(QFrame.Shape.HLine)
        line.setStyleSheet("color:#E5E7EB;")
        lay.addWidget(line)

        row = QHBoxLayout()
        btn_ep = QPushButton("查看当前端点")
        btn_ep.setObjectName("GhostBtn")
        btn_ep.setCursor(Qt.CursorShape.PointingHandCursor)
        btn_ep.clicked.connect(self._show_endpoints)
        row.addWidget(btn_ep)
        row.addStretch(1)
        lay.addLayout(row)
        return page

    def _refresh_layout_status(self) -> None:
        lbl = getattr(self, "layout_status", None)
        if lbl is None:
            return
        try:
            from joy.core.wechat_layout import load_cache

            cache = load_cache()
            if not cache.layouts:
                lbl.setText("尚未校准")
                return
            latest = max(cache.layouts.values(), key=lambda L: float(L.updated_at or 0))
            when = time.strftime(
                "%m-%d %H:%M", time.localtime(float(latest.updated_at or 0))
            )
            lbl.setText(f"已校准 · {when}")
        except Exception:
            lbl.setText("")

    def _recalibrate_wechat_layout(self) -> None:
        if self._preview is None or not self._preview.isVisible():
            QMessageBox.information(self, "重新校准", "请先打开副驾，再点校准。")
            return
        req = getattr(self._preview, "request_layout_recalibrate", None)
        if not callable(req):
            QMessageBox.warning(self, "重新校准", "当前副驾无法校准布局。")
            return
        self.btn_recal_layout.setEnabled(False)
        self.btn_recal_layout.setText("校准中…")
        if getattr(self, "layout_status", None) is not None:
            self.layout_status.setText("校准中…")

        def _done(ok: bool, msg: str) -> None:
            # 指纹等细节只打日志
            import logging

            logging.getLogger("joy.layout").info(
                "layout recalibrate ok=%s detail=%s", ok, msg
            )
            self.btn_recal_layout.setEnabled(True)
            self.btn_recal_layout.setText("重新校准微信布局")
            self._refresh_layout_status()
            if not ok:
                QMessageBox.warning(self, "重新校准", "校准失败，请稍后重试")

        req(on_done=_done)

    def _open_preview(self) -> None:
        self._collect_all()
        from joy.app.preview_window import PreviewWindow

        if self._preview is not None:
            # 侧窗可能被「收起」（隐藏 + △）；再开副驾应展开而非新建
            bind = getattr(self._preview, "bind_main_host", None)
            if callable(bind):
                bind(self)
            expand = getattr(self._preview, "_expand_from_wake", None)
            if callable(expand) and getattr(self._preview, "_dock_collapsed", False):
                expand()
            elif self._preview.isVisible():
                self._preview.raise_()
                self._preview.activateWindow()
            else:
                self._preview.show()
                sync = getattr(self._preview, "sync_after_open", None)
                if callable(sync):
                    sync()
                self._preview.raise_()
            self._hide_to_tray()
            return
        self._preview = PreviewWindow(self.cfg)
        bind = getattr(self._preview, "bind_main_host", None)
        if callable(bind):
            bind(self)
        self._preview.show()
        self._preview.sync_after_open()
        if self._preview.isVisible():
            self._preview.raise_()
        # 副驾打开后主设置窗收进系统托盘
        self._hide_to_tray()

    def _setup_tray(self) -> None:
        if not QSystemTrayIcon.isSystemTrayAvailable():
            self._tray = None
            return
        app = QApplication.instance()
        if app is not None:
            app.setQuitOnLastWindowClosed(False)

        icon = _make_tray_icon()
        tray = QSystemTrayIcon(icon, self)
        tray.setToolTip("Joy 看屏副驾")
        menu = QMenu()
        act_show = QAction("打开设置", self)
        act_show.triggered.connect(self._show_from_tray)
        act_preview = QAction("打开副驾", self)
        act_preview.triggered.connect(self._open_preview)
        act_restart = QAction("重启 Joy", self)
        act_restart.triggered.connect(self._restart_app)
        act_quit = QAction("退出 Joy", self)
        act_quit.triggered.connect(self._quit_app)
        menu.addAction(act_show)
        menu.addAction(act_preview)
        menu.addSeparator()
        menu.addAction(act_restart)
        menu.addAction(act_quit)
        tray.setContextMenu(menu)
        tray.activated.connect(self._on_tray_activated)
        tray.show()
        self._tray = tray

    def _hide_to_tray(self) -> None:
        if self._tray is not None:
            self.hide()
            if not getattr(self, "_tray_tip_shown", False):
                self._tray.showMessage(
                    "Joy",
                    "设置已收进托盘（任务栏右下角 ^）。单击图标可打开设置。",
                    QSystemTrayIcon.MessageIcon.Information,
                    3500,
                )
                self._tray_tip_shown = True
        else:
            self.showMinimized()

    def _show_from_tray(self) -> None:
        self.showNormal()
        self.raise_()
        self.activateWindow()

    def _on_tray_activated(self, reason: QSystemTrayIcon.ActivationReason) -> None:
        if reason in (
            QSystemTrayIcon.ActivationReason.Trigger,
            QSystemTrayIcon.ActivationReason.DoubleClick,
        ):
            if self.isVisible() and not self.isMinimized():
                self._hide_to_tray()
            else:
                self._show_from_tray()

    def _quit_app(self) -> None:
        if self._preview is not None:
            try:
                self._preview.close()
            except Exception:
                pass
            self._preview = None
        if self._tray is not None:
            self._tray.hide()
        QApplication.instance().quit()

    def _restart_app(self) -> None:
        """拉起新进程后退出当前进程（用于窗体尺寸异常等快速自救）。"""
        if getattr(sys, "frozen", False):
            program = sys.executable
            arguments: list[str] = []
        else:
            program = sys.executable
            arguments = ["-m", "joy", *sys.argv[1:]]

        # 先松托盘，再延迟退出，给新进程一点时间
        if self._preview is not None:
            try:
                self._preview.close()
            except Exception:
                pass
            self._preview = None
        if self._tray is not None:
            self._tray.hide()

        ok = QProcess.startDetached(program, arguments)
        if not ok and self._tray is not None:
            self._tray.show()
            QMessageBox.warning(self, "Joy", "重启失败：无法启动新进程")
            return
        QTimer.singleShot(150, QApplication.instance().quit)

    def closeEvent(self, event) -> None:  # noqa: N802
        # 点 ×：有托盘则收起，不退出（副驾可继续跑）
        if self._tray is not None:
            event.ignore()
            self._hide_to_tray()
            return
        super().closeEvent(event)

    def _collect_providers(self) -> None:
        if self.share_apis.isChecked():
            self.shared_card.apply_to(self.cfg.providers.vision)
            self.vision.copy_from(self.shared_card)
            self.analyze.copy_from(self.shared_card)
            self.draft.copy_from(self.shared_card)
            self.vision.apply_to(self.cfg.providers.vision)
            self.analyze.apply_to(self.cfg.providers.analyze)
            self.draft.apply_to(self.cfg.providers.draft)
            key = self.shared_card.key_value()
            for env in (
                self.cfg.providers.vision.api_key_env,
                self.cfg.providers.analyze.api_key_env,
                self.cfg.providers.draft.api_key_env,
            ):
                if env and key:
                    os.environ[env] = key
        else:
            self.vision.apply_to(self.cfg.providers.vision)
            self.analyze.apply_to(self.cfg.providers.analyze)
            self.draft.apply_to(self.cfg.providers.draft)

    def _collect_chat(self) -> None:
        self.cfg.chat.relation = normalize_relation(
            str(self.relation.currentData() or "朋友")
        )
        self.cfg.chat.style = normalize_style(
            str(self.style.currentData() or "简洁口语")
        )
        self.cfg.chat.context_messages = int(self.ctx_n.value())
        self.cfg.chat.memory_pull_scroll_steps = int(self.pull_scroll_n.value())

    def _collect_prefs(self) -> None:
        self.cfg.chat.auto_send = self.opt_auto_send.isChecked()
        # 始终允许从副驾填入；是否回车由 auto_send 决定
        self.cfg.chat.allow_one_click_send = True
        self.cfg.chat.confirm_before_send = False
        # fill_click_* 由布局探测写入，设置页不再手改
        self.cfg.ui.auto_open_preview = self.opt_auto_open.isChecked()
        self.cfg.ui.auto_collapse_on_minimize = self.opt_auto_collapse.isChecked()
        self.cfg.ui.smart_drive_delay_ms = int(self.spin_smart_delay.value())
        from joy.core.config import HotkeysConfig

        def _seq(edit: QKeySequenceEdit, fallback: str) -> str:
            s = edit.keySequence().toString(QKeySequence.SequenceFormat.PortableText)
            return (s or fallback).strip() or fallback

        self.cfg.ui.hotkeys = HotkeysConfig(
            manual_analyze=_seq(self.hk_analyze, "F1"),
            auto_drive=_seq(self.hk_auto, "F2"),
            smart_drive=_seq(self.hk_smart, "F3"),
            emergency_stop=_seq(self.hk_estop, "F4"),
        )

    def _collect_all(self) -> None:
        self._collect_providers()
        self._collect_chat()
        self._collect_prefs()

    def _persist_keys(self) -> None:
        if self.share_apis.isChecked():
            key = self.shared_card.key_value()
            pairs = {
                self.cfg.providers.vision.api_key_env: key,
                self.cfg.providers.analyze.api_key_env: key,
                self.cfg.providers.draft.api_key_env: key,
            }
        else:
            pairs = {
                self.cfg.providers.vision.api_key_env: self.vision.key_value(),
                self.cfg.providers.analyze.api_key_env: self.analyze.key_value(),
                self.cfg.providers.draft.api_key_env: self.draft.key_value(),
            }
        save_user_env_batch({k: v for k, v in pairs.items() if k})

    def _after_save(self, _path=None) -> None:
        if self._preview is not None:
            reload_hk = getattr(self._preview, "reload_hotkeys", None)
            if callable(reload_hk):
                reload_hk()
        QMessageBox.information(self, "Joy", "配置修改成功")

    def _save_model(self) -> None:
        try:
            self._collect_providers()
            self._persist_keys()
            path = self.cfg.save()
            self._after_save(path)
        except Exception as exc:
            from joy.util.logging import redact

            QMessageBox.warning(self, "保存失败", redact(str(exc)))

    def _save_chat(self) -> None:
        try:
            self._collect_chat()
            path = self.cfg.save()
            self._after_save(path)
        except Exception as exc:
            from joy.util.logging import redact

            QMessageBox.warning(self, "保存失败", redact(str(exc)))

    def _save_prefs(self) -> None:
        try:
            self._collect_prefs()
            path = self.cfg.save()
            self._after_save(path)
        except Exception as exc:
            from joy.util.logging import redact

            QMessageBox.warning(self, "保存失败", redact(str(exc)))

    def _reset_model(self) -> None:
        from joy.core.config import ProvidersConfig

        defaults = ProvidersConfig()
        self.share_apis.blockSignals(True)
        self.share_apis.setChecked(True)
        self.share_apis.blockSignals(False)
        self.shared_card.load_from_slot(defaults.vision, clear_key=True)
        self.vision.load_from_slot(defaults.vision, clear_key=True)
        self.analyze.load_from_slot(defaults.analyze, clear_key=True)
        self.draft.load_from_slot(defaults.draft, clear_key=True)
        self._sync_share_visibility()
        QMessageBox.information(self, "Joy", "已恢复模型默认（尚未写入磁盘，请点保存配置）")

    def _reset_chat(self) -> None:
        from joy.core.config import ChatConfig

        d = ChatConfig()
        idx = self.relation.findData(normalize_relation(d.relation))
        self.relation.setCurrentIndex(idx if idx >= 0 else 0)
        idx = self.style.findData(normalize_style(d.style))
        self.style.setCurrentIndex(idx if idx >= 0 else 0)
        self.ctx_n.setValue(int(d.context_messages))
        self.pull_scroll_n.setValue(int(d.memory_pull_scroll_steps))
        QMessageBox.information(self, "Joy", "已恢复对话默认（尚未写入磁盘，请点保存配置）")

    def _reset_prefs(self) -> None:
        from joy.core.config import ChatConfig, UiConfig

        chat_d = ChatConfig()
        ui_d = UiConfig()
        self.opt_auto_send.setChecked(bool(chat_d.auto_send))
        self.opt_auto_open.setChecked(bool(ui_d.auto_open_preview))
        self.opt_auto_collapse.setChecked(bool(ui_d.auto_collapse_on_minimize))
        self.spin_smart_delay.setValue(int(ui_d.smart_drive_delay_ms))
        hk = ui_d.hotkeys
        self.hk_analyze.setKeySequence(QKeySequence(hk.manual_analyze or "F1"))
        self.hk_auto.setKeySequence(QKeySequence(hk.auto_drive or "F2"))
        self.hk_smart.setKeySequence(QKeySequence(hk.smart_drive or "F3"))
        self.hk_estop.setKeySequence(QKeySequence(hk.emergency_stop or "F4"))
        QMessageBox.information(self, "Joy", "已恢复设置默认（尚未写入磁盘，请点保存配置）")

    def _show_endpoints(self) -> None:
        self._collect_providers()
        if self.share_apis.isChecked():
            url, model = self.shared_card.resolved()
            msg = f"{url or '(空)'}\n{model or '(空)'}"
        else:
            v_url, v_model = self.vision.resolved()
            a_url, a_model = self.analyze.resolved()
            d_url, d_model = self.draft.resolved()
            msg = (
                f"识图\n{v_url or '(空)'}\n{v_model or '(空)'}\n\n"
                f"判断\n{a_url or '(空)'}\n{a_model or '(空)'}\n\n"
                f"起草\n{d_url or '(空)'}\n{d_model or '(空)'}"
            )
        QMessageBox.information(self, "当前端点", msg)

    def _active_ping_slot(self) -> str:
        """共用 → vision；分路 → 当前高级页签。"""
        if self.share_apis.isChecked():
            return "vision"
        idx = self.advanced.currentIndex()
        return ("vision", "analyze", "draft")[max(0, min(idx, 2))]

    def _ping_active_api(self) -> None:
        if self._ping_worker and self._ping_worker.isRunning():
            QMessageBox.information(self, "Joy", "连通测试进行中…")
            return
        self._collect_providers()
        slot = self._active_ping_slot()
        labels = {"vision": "识图", "analyze": "判断", "draft": "起草"}
        self.btn_ping.setEnabled(False)
        self.btn_ping.setText(f"测试中…({labels.get(slot, slot)})")
        self._ping_worker = _PingWorker(self.cfg, slot)
        self._ping_worker.ok.connect(self._on_ping_ok)
        self._ping_worker.err.connect(self._on_ping_err)
        self._ping_worker.finished.connect(self._on_ping_done)
        self._ping_worker.start()

    def _on_ping_ok(self, reply: str) -> None:
        QMessageBox.information(self, "连通成功", f"模型回复：\n{reply}")

    def _on_ping_err(self, text: str) -> None:
        QMessageBox.warning(self, "连通失败", text)

    def _on_ping_done(self) -> None:
        self.btn_ping.setEnabled(True)
        self.btn_ping.setText("测试连通")
