"""像素定位微信聊天气泡中心（不估偏移、不耗 vision）。"""

from __future__ import annotations

import logging
from dataclasses import dataclass

import numpy as np

log = logging.getLogger("joy.bubble_locate")


@dataclass(frozen=True)
class BubbleHit:
    x_ratio: float
    y_ratio: float
    x0: int
    y0: int
    x1: int
    y1: int
    kind: str  # me | other


# 微信气泡中心色（用户标定）
_ME_RGB = (157, 242, 159)  # #9df29f 我方绿
_OTHER_RGB = (238, 238, 240)  # #eeeef0 对方白/浅灰
# 截屏/缩放/抗锯齿容差（对方须避开聊天底 ~250,250,250）
_ME_TOL = 28
_OTHER_TOL = 10


def _green_mask(rgb: np.ndarray) -> np.ndarray:
    """我方绿气泡：贴近 #9df29f / rgb(157,242,159)。"""
    cr, cg, cb = _ME_RGB
    r = rgb[:, :, 0].astype(np.int16)
    g = rgb[:, :, 1].astype(np.int16)
    b = rgb[:, :, 2].astype(np.int16)
    tol = _ME_TOL
    return (
        (np.abs(r - cr) <= tol)
        & (np.abs(g - cg) <= tol)
        & (np.abs(b - cb) <= tol)
        & (g > r + 30)
        & (g > b + 25)
    )


def _other_mask(rgb: np.ndarray) -> np.ndarray:
    """对方气泡：贴近 #eeeef0 / rgb(238,238,240)；不含近白聊天底。"""
    cr, cg, cb = _OTHER_RGB
    r = rgb[:, :, 0].astype(np.int16)
    g = rgb[:, :, 1].astype(np.int16)
    b = rgb[:, :, 2].astype(np.int16)
    tol = _OTHER_TOL
    return (
        (np.abs(r - cr) <= tol)
        & (np.abs(g - cg) <= tol)
        & (np.abs(b - cb) <= tol)
        & (np.abs(r - g) <= 6)
        & (np.abs(g - b) <= 8)
        & (np.abs(r - b) <= 8)
    )


def _components_from_mask(
    mask: np.ndarray,
    *,
    kind: str,
    ox: int,
    oy: int,
    full_w: int,
    full_h: int,
    min_area: int,
    max_area: int,
) -> list[BubbleHit]:
    import cv2

    u8 = np.ascontiguousarray(mask.astype(np.uint8) * 255)
    # 填小洞、粘连气泡圆角
    kernel = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (5, 5))
    u8 = cv2.morphologyEx(u8, cv2.MORPH_CLOSE, kernel, iterations=2)
    n, _labels, stats, _cent = cv2.connectedComponentsWithStats(u8, connectivity=8)
    hits: list[BubbleHit] = []
    for i in range(1, n):
        area = int(stats[i, cv2.CC_STAT_AREA])
        if area < min_area or area > max_area:
            continue
        bw = int(stats[i, cv2.CC_STAT_WIDTH])
        bh = int(stats[i, cv2.CC_STAT_HEIGHT])
        if bw < 28 or bh < 18:
            continue
        # 头像大致方形；气泡通常更扁或更宽
        if kind == "other" and bw < 40 and bh < 40 and abs(bw - bh) < 8:
            continue
        x = int(stats[i, cv2.CC_STAT_LEFT])
        y = int(stats[i, cv2.CC_STAT_TOP])
        x0, y0 = ox + x, oy + y
        x1, y1 = x0 + bw, y0 + bh
        cx = (x0 + x1) / 2.0
        cy = (y0 + y1) / 2.0
        hits.append(
            BubbleHit(
                x_ratio=cx / max(1.0, float(full_w)),
                y_ratio=cy / max(1.0, float(full_h)),
                x0=x0,
                y0=y0,
                x1=x1,
                y1=y1,
                kind=kind,
            )
        )
    return hits


def _merge_overlaps(hits: list[BubbleHit], *, iou_min: float = 0.25) -> list[BubbleHit]:
    """合并重叠框，保留较大者。"""
    if len(hits) <= 1:
        return hits

    def area(b: BubbleHit) -> int:
        return max(1, (b.x1 - b.x0) * (b.y1 - b.y0))

    def iou(a: BubbleHit, b: BubbleHit) -> float:
        ix0 = max(a.x0, b.x0)
        iy0 = max(a.y0, b.y0)
        ix1 = min(a.x1, b.x1)
        iy1 = min(a.y1, b.y1)
        iw = max(0, ix1 - ix0)
        ih = max(0, iy1 - iy0)
        inter = iw * ih
        if inter <= 0:
            return 0.0
        return inter / float(area(a) + area(b) - inter)

    ordered = sorted(hits, key=area, reverse=True)
    kept: list[BubbleHit] = []
    for h in ordered:
        if any(iou(h, k) >= iou_min for k in kept):
            continue
        kept.append(h)
    return kept


def locate_bubbles(
    rgb: np.ndarray,
    *,
    contact_left_ratio: float = 0.30,
    compose_top_ratio: float = 0.78,
    header_bottom_ratio: float = 0.10,
) -> list[BubbleHit]:
    """
    在消息区找所有聊天气泡（绿=我 / 白=对方），按底边从下到上排序（[0]=最后一条）。
    ROI = 顶栏下 → 输入栏上（布局测到的 compose），不做额外偏移。
    """
    if rgb is None or getattr(rgb, "size", 0) == 0 or rgb.ndim != 3:
        return []
    h, w = int(rgb.shape[0]), int(rgb.shape[1])
    if h < 160 or w < 200:
        return []

    left = max(0.22, min(0.50, float(contact_left_ratio)))
    compose = max(0.55, min(0.94, float(compose_top_ratio)))
    header = max(0.06, min(0.20, float(header_bottom_ratio)))

    x0 = int(w * left)
    y0 = int(h * header)
    y1 = max(y0 + 40, int(h * compose) - 4)
    if y1 - y0 < 40 or w - x0 < 80:
        return []

    roi = np.ascontiguousarray(rgb[y0:y1, x0:w, :3])
    rh, rw = roi.shape[:2]
    min_area = max(400, int(rw * rh * 0.0015))
    max_area = int(rw * rh * 0.55)

    greens = _components_from_mask(
        _green_mask(roi),
        kind="me",
        ox=x0,
        oy=y0,
        full_w=w,
        full_h=h,
        min_area=min_area,
        max_area=max_area,
    )
    others = _components_from_mask(
        _other_mask(roi),
        kind="other",
        ox=x0,
        oy=y0,
        full_w=w,
        full_h=h,
        min_area=min_area,
        max_area=max_area,
    )
    # 白气泡：对方在消息区左/中
    filtered_others: list[BubbleHit] = []
    chat_w = max(1, w - x0)
    for b in others:
        bw = b.x1 - b.x0
        if bw > chat_w * 0.88:
            continue
        rel_x = ((b.x0 + b.x1) / 2.0 - x0) / float(chat_w)
        if rel_x > 0.72 or rel_x < 0.02:
            continue
        filtered_others.append(b)

    # 绿气泡：偏右才像「我」
    filtered_me: list[BubbleHit] = []
    for b in greens:
        rel_x = ((b.x0 + b.x1) / 2.0 - x0) / float(chat_w)
        if rel_x < 0.28:
            continue
        filtered_me.append(b)

    hits = _merge_overlaps(filtered_me + filtered_others)
    hits.sort(key=lambda b: b.y1, reverse=True)
    log.debug(
        "bubble_locate n=%s me=%s other=%s bottoms=%s",
        len(hits),
        sum(1 for b in hits if b.kind == "me"),
        sum(1 for b in hits if b.kind == "other"),
        [round(b.y1 / float(h), 3) for b in hits[:6]],
    )
    return hits


def latest_message_side(
    rgb: np.ndarray,
    *,
    contact_left_ratio: float = 0.30,
    compose_top_ratio: float = 0.78,
    header_bottom_ratio: float = 0.10,
) -> tuple[str, BubbleHit] | None:
    """
    左右栏色块高低差（同 OCR 法则，不依赖整块连通域）：
      右半栏绿色像素最底行 = 我；左半栏灰白像素最底行 = 对方；
      谁更靠下谁最新。长绿气泡被裁碎时仍能靠像素行比高低。
    """
    if rgb is None or getattr(rgb, "size", 0) == 0 or rgb.ndim != 3:
        return None
    h, w = int(rgb.shape[0]), int(rgb.shape[1])
    if h < 160 or w < 200:
        return None

    left = max(0.22, min(0.50, float(contact_left_ratio)))
    compose = max(0.55, min(0.94, float(compose_top_ratio)))
    header = max(0.06, min(0.20, float(header_bottom_ratio)))
    x0 = int(w * left)
    y0 = int(h * header)
    y1 = max(y0 + 40, int(h * compose) - 4)
    if y1 - y0 < 40 or w - x0 < 80:
        return None

    roi = np.ascontiguousarray(rgb[y0:y1, x0:w, :3])
    rh, rw = int(roi.shape[0]), int(roi.shape[1])
    # 右栏偏右一点，减少中间误检；左栏偏左
    right_x0 = int(rw * 0.42)
    left_x1 = int(rw * 0.58)

    gmask = _green_mask(roi)
    omask = _other_mask(roi)

    def _max_y(mask: np.ndarray, x_a: int, x_b: int) -> int | None:
        strip = mask[:, x_a:x_b]
        if strip.size == 0 or not bool(strip.any()):
            return None
        # 每行有足够像素才算（抗噪点）
        row_n = strip.sum(axis=1)
        need = max(8, (x_b - x_a) // 40)
        ys = np.where(row_n >= need)[0]
        if ys.size == 0:
            return None
        return int(ys.max())

    me_y = _max_y(gmask, right_x0, rw)
    ot_y = _max_y(omask, 0, left_x1)
    clear = max(4, int(rh * 0.012))

    def _hit(kind: str, local_y: int) -> BubbleHit:
        # 合成一个代表框，供日志用
        yy = y0 + local_y
        if kind == "me":
            xx0, xx1 = x0 + right_x0, w
        else:
            xx0, xx1 = x0, x0 + left_x1
        return BubbleHit(
            x_ratio=((xx0 + xx1) / 2.0) / max(1.0, float(w)),
            y_ratio=yy / max(1.0, float(h)),
            x0=xx0,
            y0=max(y0, yy - 8),
            x1=xx1,
            y1=min(y1, yy + 2),
            kind=kind,
        )

    if me_y is None and ot_y is None:
        # 连通域兜底（旧路径）
        hits = locate_bubbles(
            rgb,
            contact_left_ratio=contact_left_ratio,
            compose_top_ratio=compose_top_ratio,
            header_bottom_ratio=header_bottom_ratio,
        )
        if not hits:
            return None
        me_hits = [b for b in hits if b.kind == "me"]
        ot_hits = [b for b in hits if b.kind == "other"]
        best_me = max(me_hits, key=lambda b: b.y1) if me_hits else None
        best_ot = max(ot_hits, key=lambda b: b.y1) if ot_hits else None
        if best_me is None and best_ot is None:
            return None
        if best_me is not None and best_ot is None:
            return "me", best_me
        if best_ot is not None and best_me is None:
            return "other", best_ot
        assert best_me is not None and best_ot is not None
        gap = max(6, int(h * 0.01))
        if best_me.y1 > best_ot.y1 + gap:
            return "me", best_me
        if best_ot.y1 > best_me.y1 + gap:
            return "other", best_ot
        return None

    if me_y is not None and ot_y is None:
        return "me", _hit("me", me_y)
    if ot_y is not None and me_y is None:
        return "other", _hit("other", ot_y)
    assert me_y is not None and ot_y is not None
    if me_y > ot_y + clear:
        return "me", _hit("me", me_y)
    if ot_y > me_y + clear:
        return "other", _hit("other", ot_y)
    return None


def locate_nth_from_bottom(
    rgb: np.ndarray,
    n: int = 1,
    *,
    contact_left_ratio: float = 0.30,
    compose_top_ratio: float = 0.78,
) -> tuple[float, float] | None:
    """
    取从下往上第 n 条气泡中心（n=1 最后一条，n=2 倒数第二条）。
    中心取色块几何中心，不做人为偏移估算。
    """
    if n < 1:
        return None
    hits = locate_bubbles(
        rgb,
        contact_left_ratio=contact_left_ratio,
        compose_top_ratio=compose_top_ratio,
    )
    if len(hits) < n:
        log.info("bubble_locate need n=%s have=%s", n, len(hits))
        return None
    hit = hits[n - 1]
    log.info(
        "bubble_locate pick n=%s kind=%s ratio=(%.3f,%.3f) box=(%s,%s)-(%s,%s)",
        n,
        hit.kind,
        hit.x_ratio,
        hit.y_ratio,
        hit.x0,
        hit.y0,
        hit.x1,
        hit.y1,
    )
    return hit.x_ratio, hit.y_ratio
