"""纯文本聊天管线：OCR 抽气泡，避免纯文字还走 Vision。

对应 docs/微信个人Agent智能回复优化方案.md §十一 / 进度看板 P1。
有大图/视频痕迹时仍回退 Vision。
"""

from __future__ import annotations

import logging
import re
import time
from dataclasses import dataclass

import numpy as np

from joy.core.models import ChatMessage, ScreenContext

log = logging.getLogger("joy.ocr_chat")

_MIN_SCORE = 0.45
_YB_MERGE = 0.045  # 同侧相邻行合并为一条气泡（相对 ROI 高）


@dataclass(frozen=True)
class TextPipelineResult:
    context: ScreenContext
    used_ocr: bool
    reason: str  # ocr_ok | needs_vision | ocr_empty | no_engine | …
    ocr_ms: float = 0.0


def _crop_message_roi(
    rgb: np.ndarray,
    *,
    contact_left_ratio: float = 0.28,
    message_top_ratio: float = 0.08,
    compose_top_ratio: float = 0.82,
) -> tuple[np.ndarray, dict[str, int]] | None:
    h, w = int(rgb.shape[0]), int(rgb.shape[1])
    if h < 160 or w < 240:
        return None
    left = max(0.22, min(0.48, float(contact_left_ratio)))
    x0 = int(w * left)
    x1 = int(w * 0.995)
    y0 = int(h * max(0.04, min(0.25, float(message_top_ratio))))
    compose = max(0.55, min(0.94, float(compose_top_ratio)))
    y1 = int(h * compose) - max(4, int(h * 0.01))
    if y1 - y0 < 60 or x1 - x0 < 80:
        return None
    roi = np.ascontiguousarray(rgb[y0:y1, x0:x1, :3])
    return roi, {"x0": x0, "y0": y0, "x1": x1, "y1": y1, "rw": x1 - x0, "rh": y1 - y0}


def message_area_needs_vision(
    rgb: np.ndarray,
    *,
    contact_left_ratio: float = 0.28,
    message_top_ratio: float = 0.08,
    compose_top_ratio: float = 0.82,
) -> tuple[bool, str]:
    """消息区是否像含大图/视频：高饱和大色块且不像气泡。"""
    cropped = _crop_message_roi(
        rgb,
        contact_left_ratio=contact_left_ratio,
        message_top_ratio=message_top_ratio,
        compose_top_ratio=compose_top_ratio,
    )
    if cropped is None:
        return True, "roi_bad"
    roi, _ = cropped
    r = roi[:, :, 0].astype(np.int16)
    g = roi[:, :, 1].astype(np.int16)
    b = roi[:, :, 2].astype(np.int16)
    # 近白底 / 绿气泡 / 浅灰气泡以外的「花」像素
    near_white = (r > 245) & (g > 245) & (b > 245)
    me_green = (g > r + 30) & (g > b + 25) & (g > 180) & (g < 255)
    other_gray = (
        (np.abs(r - g) <= 8)
        & (np.abs(g - b) <= 8)
        & (r > 220)
        & (r < 248)
    )
    colorful = ~(near_white | me_green | other_gray)
    # 饱和感：通道差大
    sat = (np.abs(r - g) > 28) | (np.abs(g - b) > 28) | (np.abs(r - b) > 28)
    media = colorful & sat
    ratio = float(media.mean()) if media.size else 0.0
    if ratio >= 0.12:
        return True, f"media_ratio={ratio:.3f}"
    # 大连通花色块
    try:
        import cv2

        u8 = (media.astype(np.uint8) * 255)
        n, _labels, stats, _ = cv2.connectedComponentsWithStats(u8, connectivity=8)
        area = int(roi.shape[0] * roi.shape[1])
        for i in range(1, n):
            if int(stats[i, cv2.CC_STAT_AREA]) >= max(2500, int(area * 0.06)):
                return True, "large_media_blob"
    except Exception:
        pass
    return False, "text_likely"


def _box_center_bottom(box: object) -> tuple[float, float] | None:
    try:
        pts = np.asarray(box, dtype=np.float64).reshape(-1, 2)
        if pts.shape[0] < 2:
            return None
        cx = float(pts[:, 0].mean())
        yb = float(pts[:, 1].max())
        return cx, yb
    except Exception:
        return None


def extract_messages_ocr(
    rgb: np.ndarray,
    *,
    title: str = "",
    contact_left_ratio: float = 0.28,
    message_top_ratio: float = 0.08,
    compose_top_ratio: float = 0.82,
    max_messages: int = 12,
) -> tuple[list[ChatMessage], float, str]:
    """OCR 消息区 → ChatMessage 列表（按 y 排序，同侧近邻合并）。"""
    from joy.capture.frame_ops import resize_max_side
    from joy.capture.ocr_gate import _get_engine
    from joy.core.message_noise import (
        drop_ui_timestamps,
        fold_speaker_name_messages,
        is_ui_timestamp,
    )

    engine = _get_engine()
    if engine is None:
        return [], 0.0, "no_engine"
    cropped = _crop_message_roi(
        rgb,
        contact_left_ratio=contact_left_ratio,
        message_top_ratio=message_top_ratio,
        compose_top_ratio=compose_top_ratio,
    )
    if cropped is None:
        return [], 0.0, "roi_bad"
    roi, meta = cropped
    roi_s = resize_max_side(roi, 900)
    rh, rw = int(roi_s.shape[0]), int(roi_s.shape[1])
    t0 = time.perf_counter()
    try:
        bgr = np.ascontiguousarray(roi_s[:, :, ::-1])
        raw, _ = engine(bgr)
    except Exception as exc:
        log.warning("ocr_chat failed: %s", exc)
        return [], (time.perf_counter() - t0) * 1000.0, "ocr_error"
    ocr_ms = (time.perf_counter() - t0) * 1000.0
    if not raw:
        return [], ocr_ms, "no_text"

    lines: list[tuple[float, str, str]] = []  # y_full_norm, side, text
    fh = float(rgb.shape[0])
    roi_y0 = float(meta["y0"])
    roi_h = float(meta["rh"])
    # 合并阈值：原相对 ROI，换算到整图比例
    y_merge = _YB_MERGE * (roi_h / max(1.0, fh))
    for item in raw:
        if not item or len(item) < 3:
            continue
        box, text, score = item[0], item[1], item[2]
        try:
            sc = float(score)
        except (TypeError, ValueError):
            continue
        if sc < _MIN_SCORE:
            continue
        text_s = str(text or "").strip()
        if len(text_s) < 1:
            continue
        # 滤掉居中时间灰字（10:53）、纯标点
        if is_ui_timestamp(text_s):
            continue
        if len(text_s) <= 1 and not re.search(r"[\u4e00-\u9fffA-Za-z0-9]", text_s):
            continue
        st = _box_center_bottom(box)
        if st is None:
            continue
        cx, yb = st
        xr = cx / max(1.0, float(rw))
        # 居中短数字串（OCR 略糊、冒号丢了时）仍当时间条丢掉
        if 0.38 <= xr <= 0.62 and len(text_s) <= 8 and re.fullmatch(
            r"[\d\s:：.]+", text_s
        ):
            continue
        side = "me" if xr >= 0.58 else "other"
        # 与 emoji_match 的 y 对齐：相对整图高度
        y_full = (roi_y0 + (yb / max(1.0, float(rh))) * roi_h) / max(1.0, fh)
        lines.append((y_full, side, text_s))

    if not lines:
        return [], ocr_ms, "no_lines"
    lines.sort(key=lambda x: x[0])

    merged: list[ChatMessage] = []
    cur_side = ""
    cur_y = -1.0
    cur_parts: list[str] = []
    cur_ys: list[float] = []
    for yb, side, text in lines:
        if cur_parts and side == cur_side and abs(yb - cur_y) <= y_merge:
            cur_parts.append(text)
            cur_ys.append(yb)
            cur_y = yb
            continue
        if cur_parts:
            merged.append(
                ChatMessage(
                    side=cur_side,  # type: ignore[arg-type]
                    text="".join(cur_parts).strip(),
                    y_norm=float(sum(cur_ys) / len(cur_ys)),
                )
            )
        cur_side, cur_y, cur_parts, cur_ys = side, yb, [text], [yb]
    if cur_parts:
        merged.append(
            ChatMessage(
                side=cur_side,  # type: ignore[arg-type]
                text="".join(cur_parts).strip(),
                y_norm=float(sum(cur_ys) / len(cur_ys)),
            )
        )

    merged = fold_speaker_name_messages(
        drop_ui_timestamps([m for m in merged if (m.text or "").strip()])
    )
    if len(merged) > max_messages:
        merged = merged[-max_messages:]
    return merged, ocr_ms, "ok"


_LINK_CARD_HINTS = (
    "版权所有",
    "未经许可",
    "总台记者",
    "央视新闻",
    "点击查看",
    "微信公众号",
    "阅读原文",
)


def _looks_like_link_or_article(messages: list[ChatMessage]) -> bool:
    """公众号/新闻卡片 OCR 会碎成多段长文且无可靠发言人 → 应交 Vision。"""
    texts = [(m.text or "").strip() for m in messages if (m.text or "").strip()]
    if not texts:
        return False
    joined = "\n".join(texts)
    if any(h in joined for h in _LINK_CARD_HINTS):
        return True
    long_n = sum(1 for t in texts if len(t) >= 36)
    named = sum(1 for m in messages if m.name)
    # 多段长文却几乎没挂上昵称
    return long_n >= 3 and named == 0


def _looks_like_group_title(title: str) -> bool:
    t = title or ""
    return bool(re.search(r"群|\(\d+\)|（\d+）", t))


def try_text_pipeline(
    rgb: np.ndarray,
    *,
    title: str = "",
    contact_left_ratio: float = 0.28,
    message_top_ratio: float = 0.08,
    compose_top_ratio: float = 0.82,
) -> TextPipelineResult:
    """纯文本可走 OCR；群聊 / 有图 / OCR 失败 → used_ocr=False（交 Vision）。"""
    # 多人群：OCR 挂名极不稳，直接 Vision（省一次白跑 OCR）
    if _looks_like_group_title(title):
        log.info("text_pipeline → vision (group chat) title=%s", (title or "")[:40])
        return TextPipelineResult(
            context=ScreenContext(title=title, messages=[], source="ocr_local"),
            used_ocr=False,
            reason="needs_vision:group_chat",
        )
    needs, why = message_area_needs_vision(
        rgb,
        contact_left_ratio=contact_left_ratio,
        message_top_ratio=message_top_ratio,
        compose_top_ratio=compose_top_ratio,
    )
    if needs:
        return TextPipelineResult(
            context=ScreenContext(title=title, messages=[]),
            used_ocr=False,
            reason=f"needs_vision:{why}",
        )
    msgs, ocr_ms, reason = extract_messages_ocr(
        rgb,
        title=title,
        contact_left_ratio=contact_left_ratio,
        message_top_ratio=message_top_ratio,
        compose_top_ratio=compose_top_ratio,
    )
    if len(msgs) < 1:
        return TextPipelineResult(
            context=ScreenContext(title=title, messages=[], source="ocr_local"),
            used_ocr=False,
            reason=f"ocr_empty:{reason}",
            ocr_ms=ocr_ms,
        )
    if _looks_like_link_or_article(msgs):
        log.info(
            "text_pipeline → vision (link/article card) msgs=%s ocr=%.0fms",
            len(msgs),
            ocr_ms,
        )
        return TextPipelineResult(
            context=ScreenContext(title=title, messages=[], source="ocr_local"),
            used_ocr=False,
            reason="needs_vision:link_or_article",
            ocr_ms=ocr_ms,
        )
    ctx = ScreenContext(title=title, messages=msgs, source="ocr_local")
    named = sum(1 for m in msgs if m.name)
    log.info(
        "text_pipeline ocr ok msgs=%s named=%s ocr=%.0fms title=%s",
        len(msgs),
        named,
        ocr_ms,
        (title or "")[:32],
    )
    return TextPipelineResult(
        context=ctx, used_ocr=True, reason="ocr_ok", ocr_ms=ocr_ms
    )
