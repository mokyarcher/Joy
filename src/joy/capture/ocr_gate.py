"""本地 OCR 门禁：用「左右栏谁更靠下」判断最新一条是谁发的。

法则（对应微信头像高低差，不靠魔法偏移量）：
  1. 裁消息区（顶栏下沿 → 输入栏上沿，右栏聊天区）
  2. OCR 全部有效文字块
  3. 按水平位置分栏：偏左=对方，偏右=我（看文字右缘，兼容长气泡）
  4. 每栏取底边 y 最大者；哪边更大 = 最新发言方
  5. me → 跳过 vision；other → 放行；两边接近 / 无字 → unknown 放行

blank_chat → 跳过。引擎失败 → unknown 放行。
"""

from __future__ import annotations

import logging
import re
import time
from dataclasses import dataclass
from typing import Any, Literal

import numpy as np

log = logging.getLogger("joy.ocr_gate")

Side = Literal["me", "other", "unknown"]

# 分栏：看文字块右缘 / 中心，兼容「我」方长气泡中心偏左
_ME_X_RIGHT = 0.58
_ME_X_CENTER = 0.55
_OTHER_X_RIGHT = 0.52
_OTHER_X_CENTER = 0.48
# 同屏两栏底边差小于此（ROI 高度比例）则视为并列，不强判
_YB_CLEAR_RATIO = 0.025
_SIDEBAR_X_MAX = 0.14
_MIN_SCORE = 0.50
_MIN_TEXT_LEN = 2

_engine: Any = None
_engine_failed = False

_NOISE_LATIN = re.compile(r"[A-Za-z]{3,}\d|\d+[A-Za-z]{2,}|Magic|price|http", re.I)


@dataclass(frozen=True)
class OcrGateResult:
    side: Side
    text: str = ""
    conf: float = 0.0
    reason: str = ""


def _get_engine() -> Any | None:
    global _engine, _engine_failed
    if _engine_failed:
        return None
    if _engine is not None:
        return _engine
    try:
        from rapidocr_onnxruntime import RapidOCR

        _engine = RapidOCR()
        log.info("ocr_gate engine ready (RapidOCR)")
        return _engine
    except Exception as exc:
        _engine_failed = True
        log.warning("ocr_gate engine unavailable: %s", exc)
        return None


def reset_engine_for_tests() -> None:
    global _engine, _engine_failed
    _engine = None
    _engine_failed = False


def _box_stats(box: object) -> tuple[float, float, float, float, float] | None:
    """返回 (cx, cy, y_bottom, width, x_right)。"""
    try:
        pts = np.asarray(box, dtype=np.float64)
        if pts.ndim != 2 or pts.shape[0] < 4 or pts.shape[1] < 2:
            return None
        xs = pts[:, 0]
        ys = pts[:, 1]
        return (
            float(xs.mean()),
            float(ys.mean()),
            float(ys.max()),
            float(xs.max() - xs.min()),
            float(xs.max()),
        )
    except Exception:
        return None


def _roi_mostly_blank(roi: np.ndarray, *, thr: float = 0.90) -> bool:
    if roi.size == 0:
        return True
    r = roi[:, :, 0].astype(np.float32)
    g = roi[:, :, 1].astype(np.float32)
    b = roi[:, :, 2].astype(np.float32)
    luma = 0.299 * r + 0.587 * g + 0.114 * b
    return float((luma >= 242.0).mean()) >= thr


def _is_noise_text(text: str, *, cx_r: float, box_w_ratio: float) -> bool:
    t = (text or "").strip()
    if len(t) < _MIN_TEXT_LEN:
        return True
    if cx_r < _SIDEBAR_X_MAX:
        return True
    if box_w_ratio < 0.06 and cx_r < 0.25:
        return True
    if _NOISE_LATIN.search(t) and cx_r < 0.35:
        return True
    if t.replace(":", "").replace("-", "").replace(" ", "").isdigit() and len(t) <= 8:
        return True
    return False


def _assign_column(cx_r: float, xr_r: float) -> Side | None:
    """文字块归左栏(other) / 右栏(me)；中间模糊返回 None。"""
    if xr_r >= _ME_X_RIGHT or cx_r >= _ME_X_CENTER:
        return "me"
    if xr_r <= _OTHER_X_RIGHT and cx_r <= _OTHER_X_CENTER:
        return "other"
    return None


def _crop_message_roi(
    rgb: np.ndarray,
    *,
    compose_top_ratio: float | None,
    message_top_ratio: float | None,
    contact_left_ratio: float,
) -> tuple[np.ndarray, dict[str, int]] | None:
    """消息区全高：顶栏下 → 输入栏上（不用再砍掉上 35%，否则会丢掉真正的最低行）。"""
    h, w = int(rgb.shape[0]), int(rgb.shape[1])
    if h < 160 or w < 240:
        return None

    left = max(0.28, min(0.48, float(contact_left_ratio) + 0.06))
    x0 = int(w * left)
    x1 = int(w * 0.995)
    y0 = int(h * max(0.04, min(0.30, float(message_top_ratio or 0.07))))
    compose = float(compose_top_ratio) if compose_top_ratio is not None else 0.80
    compose = max(0.55, min(0.94, compose))
    y1 = int(h * compose) - max(4, int(h * 0.01))
    if y1 - y0 < 60 or x1 - x0 < 80:
        return None
    roi = np.ascontiguousarray(rgb[y0:y1, x0:x1, :3])
    meta = {"x0": x0, "y0": y0, "x1": x1, "y1": y1}
    return roi, meta


def probe_latest_side(
    rgb: np.ndarray,
    *,
    compose_top_ratio: float | None = None,
    message_top_ratio: float | None = None,
    contact_left_ratio: float = 0.22,
) -> OcrGateResult:
    """
    左右栏文字底边比高低：哪栏更靠下 = 最新发言方。
    """
    if rgb is None or getattr(rgb, "size", 0) == 0 or getattr(rgb, "ndim", 0) != 3:
        return OcrGateResult("unknown", reason="empty_frame")

    engine = _get_engine()
    if engine is None:
        return OcrGateResult("unknown", reason="no_engine")

    cropped = _crop_message_roi(
        rgb,
        compose_top_ratio=compose_top_ratio,
        message_top_ratio=message_top_ratio,
        contact_left_ratio=contact_left_ratio,
    )
    if cropped is None:
        return OcrGateResult("unknown", reason="roi_bad")
    roi, _meta = cropped
    from joy.capture.frame_ops import resize_max_side

    roi = resize_max_side(roi, 720)
    rh, rw = int(roi.shape[0]), int(roi.shape[1])
    blank = _roi_mostly_blank(roi)
    clear_px = max(4.0, float(rh) * _YB_CLEAR_RATIO)

    t_ocr = time.perf_counter()
    try:
        bgr = np.ascontiguousarray(roi[:, :, ::-1])
        raw, _elapse = engine(bgr)
    except Exception as exc:
        log.warning("ocr_gate run failed: %s", exc)
        return OcrGateResult("unknown", reason="ocr_error")
    ocr_ms = (time.perf_counter() - t_ocr) * 1000.0

    if not raw:
        if blank:
            log.info("ocr_gate blank_chat (no_text) ocr=%.0fms roi=%sx%s", ocr_ms, rw, rh)
            return OcrGateResult("unknown", reason="blank_chat")
        log.info("ocr_gate no_text ocr=%.0fms roi=%sx%s", ocr_ms, rw, rh)
        return OcrGateResult("unknown", reason="no_text")

    # 每栏：底边最大的一条
    best_me: tuple[float, float, float, str] | None = None  # yb, xr, sc, text
    best_other: tuple[float, float, float, str] | None = None
    rejected = 0
    mid_n = 0

    for item in raw:
        if not item or len(item) < 3:
            continue
        box, text, score = item[0], item[1], item[2]
        text_s = str(text or "").strip()
        try:
            sc = float(score)
        except (TypeError, ValueError):
            continue
        if sc < _MIN_SCORE:
            continue
        st = _box_stats(box)
        if st is None:
            continue
        cx, _cy, yb, bw, x_right = st
        cx_r = cx / max(1.0, float(rw))
        xr_r = x_right / max(1.0, float(rw))
        bw_r = bw / max(1.0, float(rw))
        if _is_noise_text(text_s, cx_r=cx_r, box_w_ratio=bw_r):
            rejected += 1
            continue
        col = _assign_column(cx_r, xr_r)
        if col is None:
            mid_n += 1
            continue
        pack = (yb, xr_r, sc, text_s)
        if col == "me":
            if best_me is None or yb > best_me[0]:
                best_me = pack
        else:
            if best_other is None or yb > best_other[0]:
                best_other = pack

    if best_me is None and best_other is None:
        if blank or rejected > 0:
            log.info(
                "ocr_gate blank_chat (noise_only reject=%s mid=%s) ocr=%.0fms",
                rejected,
                mid_n,
                ocr_ms,
            )
            return OcrGateResult("unknown", reason="blank_chat")
        return OcrGateResult("unknown", reason="no_valid_line")

    def _preview(t: str) -> str:
        return t if len(t) <= 24 else t[:24] + "…"

    me_yb = best_me[0] if best_me else -1.0
    ot_yb = best_other[0] if best_other else -1.0

    # 只有一栏有字 → 该栏即最新
    if best_me is not None and best_other is None:
        yb, xr_r, sc, text_s = best_me
        log.info(
            "ocr_gate side=me (only_right) xr=%.2f yb=%.0f sc=%.2f ocr=%.0fms text=%s",
            xr_r,
            yb,
            sc,
            ocr_ms,
            _preview(text_s),
        )
        return OcrGateResult("me", text=text_s, conf=sc, reason="only_right")
    if best_other is not None and best_me is None:
        yb, xr_r, sc, text_s = best_other
        log.info(
            "ocr_gate side=other (only_left) xr=%.2f yb=%.0f sc=%.2f ocr=%.0fms text=%s",
            xr_r,
            yb,
            sc,
            ocr_ms,
            _preview(text_s),
        )
        return OcrGateResult("other", text=text_s, conf=sc, reason="only_left")

    assert best_me is not None and best_other is not None
    # 两栏都有：比谁更靠下（高低差）
    if me_yb > ot_yb + clear_px:
        yb, xr_r, sc, text_s = best_me
        log.info(
            "ocr_gate side=me (lower_than_left Δ=%.0f) xr=%.2f yb=%.0f/%.0f ocr=%.0fms text=%s",
            me_yb - ot_yb,
            xr_r,
            me_yb,
            ot_yb,
            ocr_ms,
            _preview(text_s),
        )
        return OcrGateResult("me", text=text_s, conf=sc, reason="lower_right")
    if ot_yb > me_yb + clear_px:
        yb, xr_r, sc, text_s = best_other
        log.info(
            "ocr_gate side=other (lower_than_right Δ=%.0f) xr=%.2f yb=%.0f/%.0f ocr=%.0fms text=%s",
            ot_yb - me_yb,
            xr_r,
            ot_yb,
            me_yb,
            ocr_ms,
            _preview(text_s),
        )
        return OcrGateResult("other", text=text_s, conf=sc, reason="lower_left")

    log.info(
        "ocr_gate side=unknown (tie me_yb=%.0f ot_yb=%.0f) ocr=%.0fms me=%s ot=%s",
        me_yb,
        ot_yb,
        ocr_ms,
        _preview(best_me[3]),
        _preview(best_other[3]),
    )
    return OcrGateResult(
        "unknown",
        text=best_other[3],
        conf=min(best_me[2], best_other[2]),
        reason="tie",
    )
