"""微信客户区布局：像素扫描（不依赖大模型）。

策略（自底向上，抗「输入框被拉高」+ 白底消息区误爬）：
1. 在右栏底部找到高方差底栏（发送/语音图标）
2. 向上穿过输入白区
3. 白区足够后，遇到的第一条灰分割线 / 表情栏上沿 → compose 顶
   （不再因「分割线上方仍是白」而继续爬，避免钻进白底消息区）
"""

from __future__ import annotations

import logging
from typing import Any

import numpy as np

log = logging.getLogger("joy.layout_pixel")


def _row_stats(band: np.ndarray, y: int) -> tuple[float, float]:
    row = band[y]
    return float(row.mean()), float(row.std())


def _is_white_row(mean: float, std: float) -> bool:
    return mean >= 244 and std <= 12


def _is_sep_row(mean: float, std: float) -> bool:
    """消息区与输入区之间的细灰线。"""
    return std <= 16 and 185 <= mean <= 245


def detect_compose_ratios(rgb: np.ndarray) -> dict[str, Any] | None:
    """
    从 RGB 整窗客户区图检测布局比例。
    成功返回可喂给 layout_from_dict 的 dict；失败返回 None。
    """
    if rgb is None or getattr(rgb, "size", 0) == 0 or rgb.ndim != 3:
        return None
    h, w = int(rgb.shape[0]), int(rgb.shape[1])
    if h < 200 or w < 200:
        return None

    # 右侧聊天主栏（避开左侧会话列表）
    x0, x1 = int(w * 0.42), max(int(w * 0.42) + 8, int(w * 0.98))
    band = rgb[:, x0:x1, :3].astype(np.float32)

    y_min = int(h * 0.50)  # 输入区再高也不该越过中部
    y_bot = h - 2

    # —— 1) 底部工具条：自底向上找高方差行 ——
    toolbar_y: int | None = None
    for y in range(y_bot, max(y_min, int(h * 0.72)), -1):
        _m, std = _row_stats(band, y)
        if std >= 26:
            toolbar_y = y
            break
    if toolbar_y is None:
        search_lo = max(y_min, int(h * 0.88))
        best_std = -1.0
        for y in range(y_bot, search_lo, -1):
            _m, std = _row_stats(band, y)
            if std > best_std:
                best_std = std
                toolbar_y = y
        if toolbar_y is None or best_std < 12:
            log.info("layout_pixel: toolbar not found")
            return None

    # 工具条顶：从命中行再往上，直到方差明显下降（进入输入白区）
    toolbar_top = toolbar_y
    for y in range(toolbar_y, y_min, -1):
        _m, std = _row_stats(band, y)
        if std < 14:
            toolbar_top = y + 1
            break
        toolbar_top = y

    # —— 2) 自工具条向上：白区走稳后，第一条分割线即分界 ——
    compose_top = toolbar_top
    white_run = 0
    found = False
    y = toolbar_top - 1
    while y >= y_min:
        mean, std = _row_stats(band, y)

        if _is_white_row(mean, std):
            white_run += 1
            compose_top = y
            y -= 1
            continue

        if _is_sep_row(mean, std):
            if white_run >= 8:
                # 下方已是输入白区 → 真分界（即使上方消息区也是白底）
                compose_top = y + 1
                found = True
                break
            # 白区太短：底栏附近杂线，跳过
            y -= 1
            continue

        # 表情栏等高方差：白区足够则认为输入区到此为止，
        # 再向上探一小段找分割线；找不到就停在表情带下沿
        if white_run >= 8:
            probe = y
            sep_at = None
            for y2 in range(y, max(y_min, y - int(h * 0.06)) - 1, -1):
                m2, s2 = _row_stats(band, y2)
                if _is_sep_row(m2, s2):
                    sep_at = y2 + 1
                    break
                if _is_white_row(m2, s2):
                    # 表情上方又变白：多半已是消息区，停在进入白之前
                    sep_at = y2 + 1
                    break
                probe = y2
            compose_top = sep_at if sep_at is not None else (probe + 1)
            found = True
            break

        y -= 1

    # 短白区兜底：找最靠下的「下白」分割线
    height_now = h - compose_top
    if (not found) or height_now < int(h * 0.10):
        for y in range(toolbar_top - 1, y_min - 1, -1):
            mean, std = _row_stats(band, y)
            if not _is_sep_row(mean, std):
                continue
            below = float(band[y + 1 : min(h, y + 14)].mean())
            if below >= 242:
                cand = y + 1
                if (h - cand) > height_now:
                    compose_top = cand
                    found = True
                break

    compose_top = int(max(y_min, min(h - 8, compose_top)))

    # —— 3) 用表情栏位置回校正：若顶边落在白底消息区（顶边下方很远才有高方差），下压到表情带附近 ——
    emoji_y = None
    scan_hi = min(toolbar_top - 4, compose_top + max(40, int(h * 0.18)))
    for y in range(compose_top, scan_hi):
        _m, std = _row_stats(band, y)
        if std >= 22:
            emoji_y = y
            break
    if emoji_y is not None and (emoji_y - compose_top) > 28:
        snapped = None
        for y in range(emoji_y, max(compose_top, emoji_y - 16), -1):
            m2, s2 = _row_stats(band, y)
            if _is_sep_row(m2, s2):
                snapped = y + 1
                break
        if snapped is None:
            snapped = max(compose_top, emoji_y - 6)
        log.info(
            "layout_pixel snap compose_top %s → %s (emoji_y=%s)",
            compose_top,
            snapped,
            emoji_y,
        )
        compose_top = snapped

    compose_top = int(max(y_min, min(h - 8, compose_top)))
    top_r = compose_top / float(h)
    height_r = 1.0 - top_r

    # 允许被用户拉高的输入区：上限约 40%
    if height_r < 0.09 or height_r > 0.42 or top_r < 0.52:
        log.info(
            "layout_pixel: reject top=%.3f h=%.3f toolbar=%s",
            top_r,
            height_r,
            toolbar_y,
        )
        return None

    cx = round((0.42 + 0.98) / 2.0, 3)
    cy = round(min(0.97, top_r + height_r * 0.35), 3)
    msg_bottom = max(0.05, top_r - 0.01)

    result = {
        "message_area": {"top": 0.06, "bottom": round(msg_bottom, 3)},
        "compose_area": {"top": round(top_r, 3), "bottom": 1.0},
        "compose_input_center": {"x": cx, "y": cy},
        "_pixel_sep_y": compose_top,
        "_pixel_toolbar_y": toolbar_y,
        "_pixel_h": h,
        "_pixel_w": w,
    }
    log.info(
        "layout_pixel ok compose=%.3f..1.000 (%.1f%% / %spx) toolbar_y=%s/%s found_sep=%s",
        top_r,
        height_r * 100,
        int(round(height_r * h)),
        toolbar_y,
        h,
        found,
    )
    return result


def detect_compose_from_jpeg(jpeg: bytes) -> dict[str, Any] | None:
    if not jpeg:
        return None
    try:
        from io import BytesIO

        from PIL import Image

        im = Image.open(BytesIO(jpeg)).convert("RGB")
        rgb = np.asarray(im, dtype=np.uint8)
        return detect_compose_ratios(rgb)
    except Exception as exc:
        log.warning("layout_pixel jpeg decode failed: %s", exc)
        return None
