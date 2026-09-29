from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, Field, model_validator

from joy.core.dock_bar import DEFAULT_DOCK_BAR_SLOTS, normalize_dock_bar_slots


def default_config_path() -> Path:
    """优先项目根 config.json，否则用户目录。"""
    cwd = Path.cwd() / "config.json"
    if cwd.is_file():
        return cwd
    return Path.home() / ".joy" / "config.json"


class CropRatio(BaseModel):
    left: float = 0.22
    top: float = 0.08
    right: float = 0.02
    bottom: float = 0.12


class CaptureConfig(BaseModel):
    paused: bool = False
    target: str = "wechat_win"
    poll_interval_ms: int = 500
    # 识图上行：略降分辨率/质量，显著减视觉 API 耗时
    max_image_side: int = 960
    jpeg_quality: int = 72
    crop: CropRatio = Field(default_factory=CropRatio)


class ChatConfig(BaseModel):
    # 仅使用 chat_presets 中的固定 id，见 normalize_*
    relation: str = "朋友"
    style: str = "简洁口语"
    context_messages: int = 10
    # 兼容旧配置：是否允许从副驾操作微信输入框
    allow_one_click_send: bool = True
    # True=粘贴后回车发送；False=只填入，由你在微信回车
    auto_send: bool = True
    confirm_before_send: bool = False
    # 填入前点击位置：相对客户区比例（避开底部截图等工具栏）
    fill_click_x_ratio: float = 0.62
    fill_click_y_ratio: float = 0.80
    # 按联系人持久化识图/导入记忆，起草时带尾部上下文
    memory_enabled: bool = True
    # 磁盘最多保留条数
    memory_keep_max: int = 400
    # 每次请求塞进 prompt 的最近条数（可大于单帧识图条数）
    memory_prompt_max: int = 30
    # 同步上下文：多选后上滑步数（越大拉取越久/越多；约每步滚一截消息区）
    memory_pull_scroll_steps: int = 24
    # 纯文字优先 OCR，跳过 Vision（有图/视频痕迹仍走 VLM）
    prefer_ocr_text_pipeline: bool = True
    # 识图时是否简述图片气泡内容（默认关，只标 [图片]，更快更稳）
    vision_describe_images: bool = False

    @model_validator(mode="after")
    def _coerce_presets(self) -> ChatConfig:
        from joy.core.chat_presets import normalize_relation, normalize_style

        self.relation = normalize_relation(self.relation)
        self.style = normalize_style(self.style)
        return self


class ProviderSlotConfig(BaseModel):
    preset: str = "custom"
    base_url: str = ""
    model: str = ""
    api_key_env: str = ""


class ProvidersConfig(BaseModel):
    vision: ProviderSlotConfig = Field(
        default_factory=lambda: ProviderSlotConfig(
            preset="sensenova",
            model="deepseek-v4-flash",
            api_key_env="JOY_VISION_API_KEY",
        )
    )
    analyze: ProviderSlotConfig = Field(
        default_factory=lambda: ProviderSlotConfig(
            preset="sensenova",
            model="deepseek-v4-flash",
            api_key_env="JOY_LLM_API_KEY",
        )
    )
    draft: ProviderSlotConfig = Field(
        default_factory=lambda: ProviderSlotConfig(
            preset="sensenova",
            model="deepseek-v4-flash",
            api_key_env="JOY_DRAFT_API_KEY",
        )
    )


class HotkeysConfig(BaseModel):
    """副驾快捷键（可在设置里改）。"""

    manual_analyze: str = "F1"
    auto_drive: str = "F2"
    smart_drive: str = "F3"
    emergency_stop: str = "F4"


class UiConfig(BaseModel):
    check_update_on_start: bool = False
    confirm_vision_upload: bool = False
    # normal = 大窗；dock = 侧窗（贴微信右侧）。启动/打开副驾时用此默认
    display_mode: Literal["normal", "dock"] = "dock"
    # 侧窗是否显示微信截图预览带（全视模式始终显示）
    dock_show_preview: bool = True
    # 底栏可配槽（4 个）；槽 5=驾驶灯、槽 6=急停 固定。见 joy.core.dock_bar
    dock_bar_slots: list[str] = Field(
        default_factory=lambda: list(DEFAULT_DOCK_BAR_SLOTS)
    )
    # 自动驾驶：本地监测画面变化 → 自动调 API 生成候选（不自动发送）
    auto_mode: bool = False
    # 智能驾驶：出候选后短暂停留，自动发送首条
    smart_drive: bool = False
    smart_drive_delay_ms: int = 900
    # 画面稳定后再分析；过长会拖慢「对面新消息→开打 API」
    auto_settle_ms: int = 900
    # 两次自动分析最短间隔（秒）；一轮结束后即可再跟差异画面
    auto_cooldown_sec: float = 2.0
    # 自动分析前本地 OCR 门禁：最新气泡像「我」则跳过 vision（失败则放行）
    ocr_gate_enabled: bool = True
    # 已配置 API Key 时，启动后自动打开副驾
    auto_open_preview: bool = False
    # 侧窗被最小化（用户点最小化 / 系统调度最小化）时，收起为微信标题栏 △
    auto_collapse_on_minimize: bool = True
    # 副驾动态指引：走完或跳过后为 True；「使用说明」可强制重看
    coach_tour_done: bool = False
    hotkeys: HotkeysConfig = Field(default_factory=HotkeysConfig)

    @model_validator(mode="after")
    def _normalize_dock_bar_slots(self) -> UiConfig:
        self.dock_bar_slots = normalize_dock_bar_slots(self.dock_bar_slots)
        return self


class JoyConfig(BaseModel):
    capture: CaptureConfig = Field(default_factory=CaptureConfig)
    chat: ChatConfig = Field(default_factory=ChatConfig)
    providers: ProvidersConfig = Field(default_factory=ProvidersConfig)
    ui: UiConfig = Field(default_factory=UiConfig)

    @classmethod
    def try_load(cls, path: Path | None = None) -> tuple[JoyConfig, str | None]:
        """返回 (配置, 警告信息)。损坏时回退默认并备份 .bad。"""
        import logging

        log = logging.getLogger("joy.config")
        p = path or default_config_path()
        if not p.is_file():
            return cls(), None
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
            return cls.model_validate(data), None
        except Exception as exc:
            log.warning("config load failed (%s): %s — using defaults", p, exc)
            try:
                bad = p.with_suffix(p.suffix + ".bad")
                bad.write_text(p.read_text(encoding="utf-8", errors="replace"), encoding="utf-8")
            except OSError:
                bad = None
            msg = f"配置文件损坏，已用默认值启动。\n{p}"
            if bad:
                msg += f"\n备份：{bad}"
            return cls(), msg

    @classmethod
    def load(cls, path: Path | None = None) -> JoyConfig:
        cfg, _ = cls.try_load(path)
        return cfg

    def save(self, path: Path | None = None) -> Path:
        p = path or default_config_path()
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(
            json.dumps(self.model_dump(), ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        return p


SlotName = Literal["vision", "analyze", "draft"]


def get_api_key(slot: ProviderSlotConfig, explicit: str | None = None) -> str:
    """从显式值或环境变量读取 Key；从不写入 config.json。"""
    if explicit:
        return explicit.strip()
    env_name = slot.api_key_env.strip()
    if not env_name:
        return ""
    from joy.core.secrets import load_user_env

    return load_user_env(env_name)


def set_api_key_env(slot: ProviderSlotConfig, value: str) -> None:
    """写入进程环境变量，并持久化到用户环境（Windows 注册表）。"""
    from joy.core.secrets import save_user_env

    if not slot.api_key_env:
        return
    save_user_env(slot.api_key_env, value)


def config_public_dict(cfg: JoyConfig) -> dict[str, Any]:
    """可安全日志化的配置视图（无密钥）。"""
    return cfg.model_dump()
