"""侧窗底栏快捷槽：动作目录与配置归一化。"""

from __future__ import annotations

from typing import Any

# 用户可配槽位数（槽 5=驾驶灯、槽 6=急停 固定，不进可配列表）
DOCK_BAR_USER_SLOTS = 4

# 固定槽（运行时拼在可配槽之后）
FIXED_DRIVE_ACTION = "drive"

# 默认可配四槽
DEFAULT_DOCK_BAR_SLOTS: list[str] = [
    "analyze",
    "preview",
    "clear",
    "detail",
]

# id -> (显示名, 短名, 悬停说明, 线稿图标 kind)
DOCK_BAR_ACTIONS: dict[str, tuple[str, str, str, str]] = {
    "empty": ("空", "空", "空槽（不显示按钮）", "clear"),
    "analyze": ("手动分析", "手动分析", "立即解析当前会话", "analyze"),
    "drive": ("驾驶模式", "驾驶", "点击循环切换 · 显示当前驾驶状态", "auto"),
    "detail": ("对话详情", "对话详情", "展开 / 收起解析后的对话详情", "detail"),
    "preview": ("截屏预览", "截屏预览", "侧窗显示 / 隐藏微信截屏预览", "preview"),
    "clear": ("清理屏幕", "清理屏幕", "清空对话详情与候选", "clear"),
    "context": ("语境参数", "语境参数", "上下文导入 / 同步 / 清除", "context"),
    "mode": ("大窗模式", "大窗模式", "切换大窗模式与侧窗模式", "mode"),
    "recal": ("重构布局", "重构布局", "重新校准微信布局", "refresh"),
    "back_main": ("回主界面", "回主界面", "打开 Joy 设置主界面", "home"),
}

# 设置页可选（驾驶已锁死；回主界面置底）
DOCK_BAR_CHOICES: list[str] = [
    "empty",
    "analyze",
    "detail",
    "preview",
    "clear",
    "context",
    "mode",
    "recal",
    "back_main",
]


def action_label(action_id: str) -> str:
    meta = DOCK_BAR_ACTIONS.get(action_id)
    return meta[0] if meta else action_id


def action_short_label(action_id: str) -> str:
    meta = DOCK_BAR_ACTIONS.get(action_id)
    return meta[1] if meta else action_id


def action_tip(action_id: str) -> str:
    meta = DOCK_BAR_ACTIONS.get(action_id)
    return meta[2] if meta else action_id


def action_icon_kind(action_id: str) -> str:
    meta = DOCK_BAR_ACTIONS.get(action_id)
    return meta[3] if meta else "clear"


def normalize_dock_bar_slots(raw: Any) -> list[str]:
    """归一化为长度 4 的可配槽；剥离 drive；未知 id→empty；去重。"""
    if raw is None:
        return list(DEFAULT_DOCK_BAR_SLOTS)
    if isinstance(raw, str):
        items: list[str] = [raw]
    elif isinstance(raw, (list, tuple)):
        items = list(raw)
    else:
        return list(DEFAULT_DOCK_BAR_SLOTS)
    if not items:
        return list(DEFAULT_DOCK_BAR_SLOTS)

    out: list[str] = []
    seen: set[str] = set()
    for item in items:
        aid = str(item or "").strip().lower()
        if aid in ("drive_cycle", "drive_mode", "drive", "estop", "emergency"):
            # 驾驶 / 急停不进可配槽
            continue
        if aid not in DOCK_BAR_ACTIONS:
            aid = "empty"
        if aid != "empty" and aid in seen:
            aid = "empty"
        if aid != "empty":
            seen.add(aid)
        out.append(aid)
        if len(out) >= DOCK_BAR_USER_SLOTS:
            break
    while len(out) < DOCK_BAR_USER_SLOTS:
        out.append("empty")
    return out[:DOCK_BAR_USER_SLOTS]
