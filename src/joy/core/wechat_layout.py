"""微信客户区布局缓存：相对比例 + 尺寸指纹。"""

from __future__ import annotations

import json
import logging
import time
from pathlib import Path

from pydantic import BaseModel, Field, field_validator

log = logging.getLogger("joy.wechat_layout")

DEFAULT_COMPOSE_MIN = 72
DEFAULT_COMPOSE_RATIO = 0.18  # 无缓存时的兜底相对高度（贴近微信输入带）
# 侧窗玻璃托盘外圈露出：底边略紧（2px）贴近微信；左右仍用布局里的 4px
COMPOSE_BOTTOM_GAP_PX = 2


class VertSpan(BaseModel):
    """相对客户区高度的竖向区间 [top, bottom]，0~1。"""

    top: float
    bottom: float

    @field_validator("top", "bottom")
    @classmethod
    def _clamp01(cls, v: float) -> float:
        return max(0.0, min(1.05, float(v)))


class PointRatio(BaseModel):
    x: float
    y: float

    @field_validator("x", "y")
    @classmethod
    def _clamp01(cls, v: float) -> float:
        return max(0.0, min(1.0, float(v)))


class WeChatLayout(BaseModel):
    fingerprint: str
    client_w: int
    client_h: int
    dpr: float = 1.0
    message_area: VertSpan | None = None
    compose_area: VertSpan
    compose_input_center: PointRatio | None = None
    model: str = ""
    updated_at: float = Field(default_factory=time.time)


class LayoutCacheFile(BaseModel):
    """多指纹缓存；侧窗按当前微信客户区指纹取用。"""

    layouts: dict[str, WeChatLayout] = Field(default_factory=dict)
    last_fingerprint: str = ""


def default_layout_cache_path() -> Path:
    return Path.home() / ".joy" / "wechat_layout.json"


def make_fingerprint(client_w: int, client_h: int, dpr: float) -> str:
    """逻辑客户区尺寸指纹（宽高取整，dpr 两位）。"""
    return f"{int(client_w)}x{int(client_h)}@{float(dpr):.2f}"


def validate_layout(layout: WeChatLayout) -> str | None:
    """通过返回 None，否则返回失败原因。"""
    c = layout.compose_area
    if c.top >= c.bottom:
        return "compose.top >= bottom"
    h = c.bottom - c.top
    # 微信底部输入带：默认约 12%~28%；用户拉高后可达 ~40%
    if h < 0.10 or h > 0.45:
        return f"compose height {h:.3f} out of range"
    if c.bottom < 0.92:
        return f"compose.bottom {c.bottom:.3f} too far from bottom"
    if c.top < 0.50:
        return f"compose.top {c.top:.3f} too high"
    if layout.message_area is not None:
        m = layout.message_area
        if m.top >= m.bottom:
            return "message.top >= bottom"
        if m.bottom > c.top + 0.08:
            # 允许少量重叠，但不该整个盖住输入区
            pass
    if layout.compose_input_center is not None:
        p = layout.compose_input_center
        if not (0.35 <= p.x <= 0.95 and 0.70 <= p.y <= 0.98):
            return f"input center ({p.x:.2f},{p.y:.2f}) out of range"
        # 历史坏缓存：模型照抄旧示例
        if (
            abs(c.top - 0.72) < 0.002
            and abs(p.x - 0.62) < 0.002
            and abs(p.y - 0.86) < 0.002
        ):
            return "echoed example layout"
    return None


def load_cache(path: Path | None = None) -> LayoutCacheFile:
    p = path or default_layout_cache_path()
    if not p.is_file():
        return LayoutCacheFile()
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
        return LayoutCacheFile.model_validate(data)
    except Exception as exc:
        log.warning("layout cache load failed: %s", exc)
        return LayoutCacheFile()


def save_cache(cache: LayoutCacheFile, path: Path | None = None) -> Path:
    p = path or default_layout_cache_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(
        json.dumps(cache.model_dump(), ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    return p


def get_cached(fingerprint: str, path: Path | None = None) -> WeChatLayout | None:
    cache = load_cache(path)
    lay = cache.layouts.get(fingerprint)
    if lay is None:
        return None
    if validate_layout(lay) is not None:
        return None
    return lay


def put_cached(layout: WeChatLayout, path: Path | None = None) -> None:
    err = validate_layout(layout)
    if err:
        raise ValueError(f"invalid layout: {err}")
    cache = load_cache(path)
    cache.layouts[layout.fingerprint] = layout
    cache.last_fingerprint = layout.fingerprint
    # 控制体积：最多保留 12 条
    if len(cache.layouts) > 12:
        ordered = sorted(cache.layouts.values(), key=lambda x: x.updated_at)
        for old in ordered[:-12]:
            cache.layouts.pop(old.fingerprint, None)
    save_cache(cache, path)


def clear_cache(path: Path | None = None) -> None:
    p = path or default_layout_cache_path()
    try:
        if p.is_file():
            p.unlink()
    except OSError as exc:
        log.warning("layout cache clear failed: %s", exc)


def compose_height_px(
    layout: WeChatLayout | None,
    client_h_logical: float,
    *,
    min_h: int = DEFAULT_COMPOSE_MIN,
    joy_h_logical: float | None = None,
) -> int:
    """侧窗 compose_box 目标高度（逻辑像素）。

    以微信客户区 compose 比例为主，再用侧窗自身高度封顶，避免 DPI/指纹
    算错时把输入区撑到半窗以上。
    """
    H = max(180.0, float(client_h_logical))
    if layout is None:
        ratio = DEFAULT_COMPOSE_RATIO
    else:
        ratio = max(0.10, min(0.42, layout.compose_area.bottom - layout.compose_area.top))
    h = int(round(ratio * H))
    max_h = int(H * 0.45)
    if joy_h_logical is not None and joy_h_logical > 0:
        max_h = min(max_h, max(min_h, int(float(joy_h_logical) * 0.45)))
    return max(min_h, min(max_h, h))


def compose_height_aligned(
    layout: WeChatLayout,
    *,
    wechat_client_top: float,
    wechat_client_h: float,
    joy_client_bottom: float,
    joy_h: float,
    min_h: int = DEFAULT_COMPOSE_MIN,
    bottom_inset: int = 0,
) -> int:
    """微信输入带逻辑高度（优先），必要时用顶对齐结果缩短。"""
    wh = max(1.0, float(wechat_client_h))
    span = max(0.10, min(0.42, float(layout.compose_area.bottom) - float(layout.compose_area.top)))
    ratio_h = int(round(span * wh))
    top = float(wechat_client_top) + float(layout.compose_area.top) * wh
    inset = max(0, int(bottom_inset))
    aligned = int(round(float(joy_client_bottom) - inset - top))

    # 只允许比微信矮，不允许更高
    if aligned >= min_h:
        h = min(aligned, ratio_h)
    else:
        h = ratio_h

    max_h = max(min_h, int(max(180.0, float(joy_h)) * 0.45))
    max_h = min(max_h, ratio_h)
    return max(min_h, min(max_h, h))
