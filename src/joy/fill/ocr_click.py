"""本地 OCR 定位屏幕上的按钮/菜单文字中心（不耗 vision token）。"""

from __future__ import annotations

import logging
from typing import Any

import numpy as np

log = logging.getLogger("joy.ocr_click")

_ocr_engine: Any = None
_ocr_engine_failed = False


def _get_engine() -> Any | None:
    global _ocr_engine, _ocr_engine_failed
    if _ocr_engine_failed:
        return None
    if _ocr_engine is not None:
        return _ocr_engine
    try:
        from rapidocr_onnxruntime import RapidOCR

        _ocr_engine = RapidOCR()
        return _ocr_engine
    except Exception as exc:
        _ocr_engine_failed = True
        log.warning("ocr_click engine unavailable: %s", exc)
        return None


def _box_center(box: object) -> tuple[float, float] | None:
    try:
        pts = np.asarray(box, dtype=np.float64)
        if pts.ndim != 2 or pts.shape[0] < 4:
            return None
        return float(pts[:, 0].mean()), float(pts[:, 1].mean())
    except Exception:
        return None


def _box_height(box: object) -> float:
    try:
        pts = np.asarray(box, dtype=np.float64)
        if pts.ndim != 2 or pts.shape[0] < 4:
            return 0.0
        return float(pts[:, 1].max() - pts[:, 1].min())
    except Exception:
        return 0.0


def _run_ocr(rgb: np.ndarray) -> list[tuple[float, float, float, float, str]]:
    """返回 [(cy, cx, score, box_h, text), ...]。"""
    eng = _get_engine()
    if eng is None:
        return []
    try:
        bgr = np.ascontiguousarray(rgb[:, :, ::-1])
        raw, _ = eng(bgr)
    except Exception as exc:
        log.warning("ocr_click run failed: %s", exc)
        return []
    if not raw:
        return []
    out: list[tuple[float, float, float, float, str]] = []
    for item in raw:
        if not item or len(item) < 3:
            continue
        box, text, score = item[0], item[1], item[2]
        text_s = str(text or "").strip().replace(" ", "")
        if not text_s:
            continue
        try:
            sc = float(score)
        except (TypeError, ValueError):
            sc = 0.0
        if sc < 0.4:
            continue
        ctr = _box_center(box)
        if ctr is None:
            continue
        cx, cy = ctr
        out.append((cy, cx, sc, _box_height(box), text_s))
    return out


def find_text_ratio(
    rgb: np.ndarray,
    needles: list[str],
    *,
    prefer_lower: bool = False,
    prefer_right_of: float | None = None,
) -> tuple[float, float, str] | None:
    """
    在整图中找包含 needle 的 OCR 块，返回 (x_ratio, y_ratio, matched_text)。
    prefer_lower: 多个命中时取更靠下的（菜单「多选」常在收藏下面）。
    prefer_right_of: 优先中心 x 大于该像素值的块（右键菜单多在点击右侧）。
    """
    if rgb is None or getattr(rgb, "size", 0) == 0 or rgb.ndim != 3:
        return None
    h, w = int(rgb.shape[0]), int(rgb.shape[1])
    if h < 40 or w < 40:
        return None

    needles_n = [n.strip() for n in needles if n and n.strip()]
    if not needles_n:
        return None

    rows = _run_ocr(rgb)
    hits: list[tuple[float, float, float, str]] = []  # y, x, score, text
    for cy, cx, sc, _bh, text_s in rows:
        matched = None
        for n in needles_n:
            nn = n.replace(" ", "")
            if nn in text_s or text_s in nn:
                matched = text_s
                break
        if matched is None:
            continue
        adj = sc
        if prefer_right_of is not None and cx < prefer_right_of - 8:
            adj -= 0.15
        hits.append((cy, cx, adj, matched))

    if not hits:
        log.info("ocr_click miss needles=%s", needles_n)
        return None

    if prefer_lower:
        hits.sort(key=lambda t: (t[0], t[2]), reverse=True)
    else:
        hits.sort(key=lambda t: t[2], reverse=True)

    cy, cx, sc, matched = hits[0]
    xr, yr = cx / max(1.0, float(w)), cy / max(1.0, float(h))
    log.info(
        "ocr_click hit=%s score=%.2f ratio=(%.3f,%.3f) needles=%s",
        matched,
        sc,
        xr,
        yr,
        needles_n,
    )
    return xr, yr, matched


def find_context_multiselect(
    rgb: np.ndarray,
    *,
    prefer_right_of: float | None = None,
) -> tuple[float, float, str] | None:
    """
    右键菜单里找「多选」中心。
    1) 直接 OCR「多选」
    2) 若只有「收藏」可见（菜单贴底/OCR 漏字）：在收藏正下方一格点「多选」
    3) 若看到「提醒」：在其正上方一格（多选在提醒之上）
    """
    if rgb is None or getattr(rgb, "size", 0) == 0 or rgb.ndim != 3:
        return None
    h, w = int(rgb.shape[0]), int(rgb.shape[1])
    if h < 40 or w < 40:
        return None

    rows = _run_ocr(rgb)
    if not rows:
        return None

    def _near_menu(cx: float) -> bool:
        if prefer_right_of is None:
            return True
        return cx >= prefer_right_of - 40

    def _pick(label: str) -> tuple[float, float, float, float] | None:
        cand: list[tuple[float, float, float, float]] = []
        for cy, cx, sc, bh, text_s in rows:
            if label not in text_s and text_s not in label:
                continue
            if not _near_menu(cx):
                continue
            cand.append((cy, cx, sc, max(bh, 22.0)))
        if not cand:
            return None
        # 菜单项取分数最高；同级偏下
        cand.sort(key=lambda t: (t[2], t[0]), reverse=True)
        return cand[0]

    multi = _pick("多选")
    if multi is not None:
        cy, cx, sc, _bh = multi
        xr, yr = cx / float(w), cy / float(h)
        log.info("ocr_click multiselect direct ratio=(%.3f,%.3f) score=%.2f", xr, yr, sc)
        return xr, yr, "多选"

    fav = _pick("收藏")
    if fav is not None:
        cy, cx, sc, bh = fav
        # 微信菜单一行 ≈ 文字框高的 1.15~1.35；点收藏正下方一格
        row = max(26.0, min(48.0, bh * 1.25))
        cy2 = cy + row
        if cy2 < h - 4:
            xr, yr = cx / float(w), cy2 / float(h)
            log.info(
                "ocr_click multiselect via 收藏+%.0fpx ratio=(%.3f,%.3f)",
                row,
                xr,
                yr,
            )
            return xr, yr, "多选(via收藏)"

    remind = _pick("提醒")
    if remind is not None:
        cy, cx, sc, bh = remind
        row = max(26.0, min(48.0, bh * 1.25))
        cy2 = cy - row
        if cy2 > 4:
            xr, yr = cx / float(w), cy2 / float(h)
            log.info(
                "ocr_click multiselect via 提醒-%.0fpx ratio=(%.3f,%.3f)",
                row,
                xr,
                yr,
            )
            return xr, yr, "多选(via提醒)"

    log.info(
        "ocr_click multiselect miss (no 多选/收藏/提醒); sample=%s",
        [t[4] for t in rows[:24]],
    )
    return None
