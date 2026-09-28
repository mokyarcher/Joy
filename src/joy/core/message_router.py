"""Message Router：进模型前分流（缓存 / OCR 文本 / Vision）。

对应 docs/微信个人Agent智能回复优化方案.md §八 / 进度看板中期项。
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from typing import Literal

import numpy as np

from joy.core.models import ScreenContext

log = logging.getLogger("joy.message_router")

RoutePath = Literal["cache", "ocr_text", "vision"]


@dataclass(frozen=True)
class RouteDecision:
    path: RoutePath
    reason: str
    context: ScreenContext | None = None  # cache/ocr 时带上；vision 时为空
    ocr_ms: float = 0.0


def route_understand(
    *,
    frame_rgb: np.ndarray | None,
    title_hint: str = "",
    cached: ScreenContext | None = None,
    prefer_ocr: bool = True,
    contact_left_ratio: float = 0.28,
) -> RouteDecision:
    """
    理解层路由（截屏之后、LLM 之前）：
    - cache：画面未变
    - ocr_text：纯文字，本地 OCR
    - vision：含图/视频痕迹或 OCR 不够
    """
    if cached is not None and (cached.messages or cached.title):
        return RouteDecision(path="cache", reason="frame_unchanged", context=cached)

    if not prefer_ocr or frame_rgb is None:
        return RouteDecision(path="vision", reason="ocr_disabled_or_no_rgb")

    # 群聊标题：不跑 OCR，直接 Vision（认人/气泡比本地 OCR 稳）
    title = title_hint or ""
    if re.search(r"群|\(\d+\)|（\d+）", title):
        log.info("router → vision (group chat) title=%s", title[:40])
        return RouteDecision(path="vision", reason="group_chat")

    try:
        from joy.capture.ocr_chat import try_text_pipeline

        tp = try_text_pipeline(
            frame_rgb,
            title=title,
            contact_left_ratio=max(0.22, float(contact_left_ratio)),
        )
        if tp.used_ocr and tp.context.messages:
            log.info(
                "router → ocr_text reason=%s msgs=%s ocr=%.0fms",
                tp.reason,
                len(tp.context.messages),
                tp.ocr_ms,
            )
            return RouteDecision(
                path="ocr_text",
                reason=tp.reason,
                context=tp.context,
                ocr_ms=tp.ocr_ms,
            )
        log.info("router → vision (%s)", tp.reason)
        return RouteDecision(path="vision", reason=tp.reason or "ocr_fallback")
    except Exception as exc:
        log.warning("router ocr failed → vision: %s", exc)
        return RouteDecision(path="vision", reason="ocr_error")
