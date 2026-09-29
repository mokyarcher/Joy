"""上下文二级面板：导入 / 同步 / 清除（Tab 切换配置与操作）。"""

from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QDialog,
    QFileDialog,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPlainTextEdit,
    QPushButton,
    QSpinBox,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)


class ContextHubDialog(QDialog):
    """侧窗「…」→ 上下文：三 Tab 分开展示说明与操作。"""

    TAB_IMPORT = 0
    TAB_SYNC = 1
    TAB_CLEAR = 2

    def __init__(
        self,
        parent: QWidget | None,
        *,
        contact_title: str,
        scroll_steps: int = 24,
        memory_count: int = 0,
        memory_buckets: int = 0,
        pull_busy: bool = False,
        initial_tab: int = TAB_IMPORT,
    ) -> None:
        super().__init__(parent)
        self._contact = (contact_title or "").strip()
        self.setWindowTitle(
            f"语境参数 · {self._contact}" if self._contact else "语境参数"
        )
        self.setModal(True)
        self.resize(500, 460)
        self.setObjectName("ContextHubDialog")
        self.setStyleSheet(
            """
            QDialog#ContextHubDialog {
                background: #FFFFFF;
            }
            QTabWidget#ContextTabs::pane {
                border: 1px solid #E8E8E8;
                border-radius: 8px;
                top: -1px;
                background: #FAFAFA;
                padding: 12px;
            }
            QTabBar::tab {
                background: transparent;
                color: #888888;
                padding: 8px 16px;
                margin-right: 4px;
                border: none;
                font-size: 13px;
            }
            QTabBar::tab:selected {
                color: #07C160;
                font-weight: 600;
                border-bottom: 2px solid #07C160;
            }
            QTabBar::tab:hover:!selected {
                color: #191919;
            }
            QLabel#HubTip {
                color: #666666;
                font-size: 12px;
            }
            QPushButton#HubPrimary {
                background: #07C160;
                color: #FFFFFF;
                border: none;
                border-radius: 6px;
                padding: 8px 18px;
                font-size: 13px;
            }
            QPushButton#HubPrimary:hover { background: #06AD56; }
            QPushButton#HubPrimary:disabled { background: #B7E6C9; color: #F0FFF5; }
            QPushButton#HubDanger {
                background: #FFFFFF;
                color: #FA5151;
                border: 1px solid #FA5151;
                border-radius: 6px;
                padding: 8px 18px;
                font-size: 13px;
            }
            QPushButton#HubDanger:hover { background: #FFF5F5; }
            QPushButton#HubSecondary {
                background: #FFFFFF;
                color: #191919;
                border: 1px solid #D9D9D9;
                border-radius: 6px;
                padding: 8px 14px;
                font-size: 13px;
            }
            QPushButton#HubSecondary:hover { background: #F5F5F5; }
            """
        )

        root = QVBoxLayout(self)
        root.setContentsMargins(14, 12, 14, 12)
        root.setSpacing(10)

        if not self._contact:
            warn = QLabel("尚未识别到会话联系人。请先打开微信会话，等顶栏出现名字后再操作。")
            warn.setWordWrap(True)
            warn.setObjectName("HubTip")
            root.addWidget(warn)

        self.tabs = QTabWidget()
        self.tabs.setObjectName("ContextTabs")
        self.tabs.setDocumentMode(True)
        root.addWidget(self.tabs, stretch=1)

        self._build_import_tab()
        self._build_sync_tab(scroll_steps=scroll_steps, pull_busy=pull_busy)
        self._build_clear_tab(
            memory_count=memory_count, memory_buckets=memory_buckets
        )

        idx = max(0, min(2, int(initial_tab)))
        self.tabs.setCurrentIndex(idx)

        # results filled by accept paths
        self.result_action: str | None = None  # import | sync | clear
        self.result_raw: str = ""
        self.result_me_names: str = ""
        self.result_scroll_steps: int = int(scroll_steps)

    def _build_import_tab(self) -> None:
        page = QWidget()
        lay = QVBoxLayout(page)
        lay.setContentsMargins(4, 8, 4, 4)
        lay.setSpacing(8)
        tip = QLabel(
            "粘贴微信导出或「我: / 对方:」记录到本地记忆，供后续起草使用。\n"
            "私聊：顶栏联系人=对方；其余发言者默认算我。群聊请填「我的昵称」。"
        )
        tip.setObjectName("HubTip")
        tip.setWordWrap(True)
        lay.addWidget(tip)

        me_row = QHBoxLayout()
        me_row.addWidget(QLabel("我的昵称"))
        self.import_me = QLineEdit()
        self.import_me.setPlaceholderText("私聊可空；群聊必填，多个用逗号分隔")
        me_row.addWidget(self.import_me, stretch=1)
        lay.addLayout(me_row)

        self.import_edit = QPlainTextEdit()
        self.import_edit.setPlaceholderText(
            "Moky\n2026年09月25日 22:18\n你好\n\n"
            "对方昵称\n2026年09月25日 22:25\n在的\n"
        )
        lay.addWidget(self.import_edit, stretch=1)

        row = QHBoxLayout()
        btn_file = QPushButton("从文件…")
        btn_file.setObjectName("HubSecondary")
        btn_file.clicked.connect(self._pick_import_file)
        row.addWidget(btn_file)
        row.addStretch(1)
        btn_ok = QPushButton("导入")
        btn_ok.setObjectName("HubPrimary")
        btn_ok.setEnabled(bool(self._contact))
        btn_ok.clicked.connect(self._accept_import)
        row.addWidget(btn_ok)
        lay.addLayout(row)
        self.tabs.addTab(page, "上下文导入")

    def _build_sync_tab(self, *, scroll_steps: int, pull_busy: bool) -> None:
        page = QWidget()
        lay = QVBoxLayout(page)
        lay.setContentsMargins(4, 8, 4, 4)
        lay.setSpacing(10)
        tip = QLabel(
            "自动在微信里多选 → 上滑取样 → 复制，并写入当前联系人记忆。\n"
            "同步期间会暂停联系人识名，请勿操作鼠标键盘。\n"
            "结束后会自动把聊天滚回最新消息。\n"
            "失败时可改用「上下文导入」Tab 手动粘贴。"
        )
        tip.setObjectName("HubTip")
        tip.setWordWrap(True)
        lay.addWidget(tip)

        me_row = QHBoxLayout()
        me_row.addWidget(QLabel("我的昵称"))
        self.sync_me = QLineEdit()
        self.sync_me.setPlaceholderText("私聊可空；群聊填写你的群昵称")
        me_row.addWidget(self.sync_me, stretch=1)
        lay.addLayout(me_row)

        step_row = QHBoxLayout()
        step_row.addWidget(QLabel("上滑步数"))
        self.sync_steps = QSpinBox()
        self.sync_steps.setRange(6, 80)
        self.sync_steps.setValue(max(6, min(80, int(scroll_steps))))
        self.sync_steps.setToolTip("取样越多上下文越全，耗时也更长")
        step_row.addWidget(self.sync_steps)
        step_row.addStretch(1)
        lay.addLayout(step_row)

        lay.addStretch(1)
        row = QHBoxLayout()
        row.addStretch(1)
        btn = QPushButton("开始同步" if not pull_busy else "同步进行中…")
        btn.setObjectName("HubPrimary")
        btn.setEnabled(bool(self._contact) and not pull_busy)
        btn.clicked.connect(self._accept_sync)
        row.addWidget(btn)
        lay.addLayout(row)
        self.tabs.addTab(page, "上下文同步")

    def _build_clear_tab(self, *, memory_count: int, memory_buckets: int) -> None:
        page = QWidget()
        lay = QVBoxLayout(page)
        lay.setContentsMargins(4, 8, 4, 4)
        lay.setSpacing(10)
        name = self._contact or "（未识别联系人）"
        bucket_note = f"，约 {memory_buckets} 个本地桶" if memory_buckets > 1 else ""
        count_note = (
            f"当前约 {memory_count} 条记忆{bucket_note}。"
            if memory_count or memory_buckets
            else "当前未见本地记忆条目。"
        )
        tip = QLabel(
            f"将清空「{name}」的全部 Joy 本地上下文：\n"
            "· 聊天记忆（含同步 / 导入）\n"
            "· 话题状态（事实 / 已讨论点）\n"
            "· 我的回复库（防复读）\n"
            "· 同名 OCR 变体与识图缓存\n\n"
            f"{count_note}\n"
            "只删本地文件，不影响微信。"
        )
        tip.setObjectName("HubTip")
        tip.setWordWrap(True)
        tip.setAlignment(Qt.AlignmentFlag.AlignTop | Qt.AlignmentFlag.AlignLeft)
        lay.addWidget(tip)
        lay.addStretch(1)
        row = QHBoxLayout()
        row.addStretch(1)
        btn = QPushButton("确认清除")
        btn.setObjectName("HubDanger")
        btn.setEnabled(bool(self._contact))
        btn.clicked.connect(self._accept_clear)
        row.addWidget(btn)
        lay.addLayout(row)
        self.tabs.addTab(page, "上下文清除")

    def _pick_import_file(self) -> None:
        path, _ = QFileDialog.getOpenFileName(
            self,
            "选择聊天记录文本",
            "",
            "文本 (*.txt *.log *.md);;所有文件 (*.*)",
        )
        if not path:
            return
        try:
            self.import_edit.setPlainText(Path(path).read_text(encoding="utf-8"))
        except Exception:
            try:
                self.import_edit.setPlainText(
                    Path(path).read_text(encoding="gbk", errors="replace")
                )
            except Exception as exc:
                QMessageBox.warning(self, "读取失败", str(exc))

    def _accept_import(self) -> None:
        raw = self.import_edit.toPlainText().strip()
        if not raw:
            QMessageBox.information(self, "导入上下文", "请先粘贴或选择聊天记录。")
            return
        self.result_action = "import"
        self.result_raw = raw
        self.result_me_names = self.import_me.text().strip()
        self.accept()

    def _accept_sync(self) -> None:
        self.result_action = "sync"
        self.result_me_names = self.sync_me.text().strip()
        self.result_scroll_steps = int(self.sync_steps.value())
        self.accept()

    def _accept_clear(self) -> None:
        ret = QMessageBox.question(
            self,
            "清除上下文",
            "确定清空当前联系人的全部本地上下文？此操作不可恢复。",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if ret != QMessageBox.StandardButton.Yes:
            return
        self.result_action = "clear"
        self.accept()
