"""微信内置小表情比对。

按 docs/Joy_微信Emoji_Matcher_本次误识别分析与优化建议.md：
  扩大裁剪 → 前景 mask → pHash/Edge/Color 多路召回
  → 平移/缩放对齐 → Foreground SSIM/Pixel + Edge + Color
  → Top1/Margin 置信度（低置信不污染记忆）
"""

from __future__ import annotations

import json
import logging
import re
import time
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path

import numpy as np

from joy.core.models import ChatMessage, ScreenContext, Side
from joy.core.wechat_emoji import emoji_codes, official_dir, tag, tile_path

log = logging.getLogger("joy.emoji_match")

_CANVAS = 64
_RECALL_K = 30
# 方案 §18
_HIGH_SCORE = 0.92
_HIGH_MARGIN = 0.10
_MED_SCORE = 0.80
_ACCEPT_MARGIN = 0.05  # medium 至少要有这点分差才采纳
_FLOOR_SCORE = 0.50
# 方案 §14：前景 SSIM/Pixel，提高 Edge
_WEIGHT_SSIM = 0.40
_WEIGHT_PIXEL = 0.25
_WEIGHT_EDGE = 0.25
_WEIGHT_COLOR = 0.10
_TRANSLATE_R = 8
# 精排只对 TopN；缩放档位收紧（方案仍覆盖 0.9~1.1 意图，用 3 档）
_FINE_N = 12
_SCALES = (0.95, 1.0, 1.05)

@dataclass(frozen=True)
class EmojiMatch:
    tag: str
    name: str
    filename: str
    score: float
    margin: float
    confidence: str  # high | medium | low
    alternatives: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class EmojiHit:
    """兼容旧接口：定位到的一次命中。"""

    code: str
    score: float
    y: float
    x: float
    w: int
    h: int
    confidence: str = "low"
    margin: float = 0.0


# ---------------------------------------------------------------------------
# 归一化 / 特征
# ---------------------------------------------------------------------------


def _composite_rgba(rgba: np.ndarray, bg: tuple[int, int, int] = (245, 245, 245)) -> np.ndarray:
    rgb = rgba[:, :, :3].astype(np.float32)
    if rgba.shape[2] >= 4:
        a = (rgba[:, :, 3].astype(np.float32) / 255.0)[..., None]
        base = np.empty_like(rgb)
        base[:] = bg
        return np.clip(rgb * a + base * (1.0 - a), 0, 255).astype(np.uint8)
    return rgba[:, :, :3].astype(np.uint8)


def normalize_emoji_patch(rgb: np.ndarray, *, canvas: int = _CANVAS) -> np.ndarray:
    """去气泡底 → 前景 bbox 等比居中 → canvas×canvas RGB。"""
    import cv2

    if rgb is None or rgb.size == 0:
        return np.full((canvas, canvas, 3), 245, dtype=np.uint8)
    img = np.ascontiguousarray(rgb[:, :, :3])
    r = img[:, :, 0].astype(np.int16)
    g = img[:, :, 1].astype(np.int16)
    b = img[:, :, 2].astype(np.int16)
    # 近白/气泡灰(245) 不当作内容，否则外扩框会把脸「缩小」进 canvas
    near_bg = (r > 228) & (g > 228) & (b > 218) & (
        (np.abs(r - g) < 12) & (np.abs(g - b) < 16)
    )
    green_bubble = (g > 150) & (g >= r + 18) & (g >= b + 18) & (r < 210)
    sat = np.max(img, axis=2).astype(np.int16) - np.min(img, axis=2).astype(np.int16)
    mask = (~near_bg & ~green_bubble) | (sat > 22)
    ys, xs = np.where(mask)
    if len(xs) < 8:
        content = img
    else:
        y0, y1 = int(ys.min()), int(ys.max()) + 1
        x0, x1 = int(xs.min()), int(xs.max()) + 1
        # 略留边，避免裁掉抗锯齿轮廓
        pad = 1
        y0 = max(0, y0 - pad)
        x0 = max(0, x0 - pad)
        y1 = min(img.shape[0], y1 + pad)
        x1 = min(img.shape[1], x1 + pad)
        content = img[y0:y1, x0:x1]
    h, w = content.shape[:2]
    if h < 2 or w < 2:
        return np.full((canvas, canvas, 3), 245, dtype=np.uint8)
    scale = (canvas * 0.88) / float(max(h, w))
    nh, nw = max(1, int(h * scale)), max(1, int(w * scale))
    resized = cv2.resize(content, (nw, nh), interpolation=cv2.INTER_AREA)
    canvas_img = np.full((canvas, canvas, 3), 245, dtype=np.uint8)
    y_off = (canvas - nh) // 2
    x_off = (canvas - nw) // 2
    canvas_img[y_off : y_off + nh, x_off : x_off + nw] = resized
    return canvas_img


def _to_gray(rgb: np.ndarray) -> np.ndarray:
    import cv2

    return cv2.cvtColor(rgb, cv2.COLOR_RGB2GRAY)


def compute_phash(rgb: np.ndarray, hash_size: int = 8) -> int:
    """简化 pHash：DCT 低频 → 比特串。"""
    import cv2

    gray = _to_gray(rgb).astype(np.float32)
    small = cv2.resize(gray, (32, 32), interpolation=cv2.INTER_AREA)
    dct = cv2.dct(small)
    block = dct[:hash_size, :hash_size]
    med = float(np.median(block))
    bits = (block > med).astype(np.uint8).flatten()
    value = 0
    for b in bits:
        value = (value << 1) | int(b)
    return int(value)


def hamming64(a: int, b: int) -> int:
    return int((a ^ b).bit_count())


def compute_ssim(a_rgb: np.ndarray, b_rgb: np.ndarray) -> float:
    import cv2

    a = _to_gray(a_rgb).astype(np.float64)
    b = _to_gray(b_rgb).astype(np.float64)
    C1 = (0.01 * 255) ** 2
    C2 = (0.03 * 255) ** 2
    mu_a = cv2.GaussianBlur(a, (11, 11), 1.5)
    mu_b = cv2.GaussianBlur(b, (11, 11), 1.5)
    mu_a2 = mu_a * mu_a
    mu_b2 = mu_b * mu_b
    mu_ab = mu_a * mu_b
    sigma_a2 = cv2.GaussianBlur(a * a, (11, 11), 1.5) - mu_a2
    sigma_b2 = cv2.GaussianBlur(b * b, (11, 11), 1.5) - mu_b2
    sigma_ab = cv2.GaussianBlur(a * b, (11, 11), 1.5) - mu_ab
    ssim_map = ((2 * mu_ab + C1) * (2 * sigma_ab + C2)) / (
        (mu_a2 + mu_b2 + C1) * (sigma_a2 + sigma_b2 + C2) + 1e-12
    )
    return float(np.clip(ssim_map.mean(), 0.0, 1.0))


def compute_pixel_similarity(a_rgb: np.ndarray, b_rgb: np.ndarray) -> float:
    diff = np.abs(a_rgb.astype(np.float32) - b_rgb.astype(np.float32)).mean()
    return float(np.clip(1.0 - diff / 255.0, 0.0, 1.0))


def _canny_edges(rgb: np.ndarray) -> np.ndarray:
    import cv2

    return cv2.Canny(_to_gray(rgb), 60, 160)


def compute_edge_similarity(a_rgb: np.ndarray, b_rgb: np.ndarray) -> float:
    return _edge_map_similarity(_canny_edges(a_rgb), _canny_edges(b_rgb))


def _edge_map_similarity(ea: np.ndarray, eb: np.ndarray) -> float:
    af = ea.astype(np.float32).ravel()
    bf = eb.astype(np.float32).ravel()
    if af.std() < 1e-6 or bf.std() < 1e-6:
        return 0.0
    af = (af - af.mean()) / (af.std() + 1e-6)
    bf = (bf - bf.mean()) / (bf.std() + 1e-6)
    return float(np.clip((af * bf).mean(), 0.0, 1.0))


def compute_color_hist(rgb: np.ndarray, bins: int = 16) -> np.ndarray:
    """只在前景像素上统计色史，避免气泡底主导相关。"""
    import cv2

    mask = foreground_mask(rgb)
    mask_u8 = (mask.astype(np.uint8) * 255)
    if int(mask_u8.sum()) < 32 * 255:
        mask_u8 = None
    hsv = cv2.cvtColor(rgb, cv2.COLOR_RGB2HSV)
    hist = cv2.calcHist([hsv], [0, 1], mask_u8, [bins, bins], [0, 180, 0, 256])
    hist = cv2.normalize(hist, hist).flatten()
    return hist.astype(np.float32)


def compute_color_similarity(ha: np.ndarray, hb: np.ndarray) -> float:
    import cv2

    corr = float(cv2.compareHist(ha, hb, cv2.HISTCMP_CORREL))
    return float(np.clip((corr + 1.0) * 0.5, 0.0, 1.0))


def foreground_mask(rgb: np.ndarray) -> np.ndarray:
    """气泡底 / 近白 → 背景；其余为表情前景。"""
    import cv2

    r = rgb[:, :, 0].astype(np.int16)
    g = rgb[:, :, 1].astype(np.int16)
    b = rgb[:, :, 2].astype(np.int16)
    near_bg = (r > 228) & (g > 228) & (b > 218) & (
        (np.abs(r - g) < 12) & (np.abs(g - b) < 16)
    )
    green_bubble = (g > 150) & (g >= r + 18) & (g >= b + 18) & (r < 210)
    mask = ~(near_bg | green_bubble)
    u8 = (mask.astype(np.uint8) * 255)
    k = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (3, 3))
    u8 = cv2.morphologyEx(u8, cv2.MORPH_OPEN, k)
    u8 = cv2.morphologyEx(u8, cv2.MORPH_CLOSE, k)
    return u8 > 0


def paint_background(
    rgb: np.ndarray, mask: np.ndarray | None = None, *, bg: int = 245
) -> np.ndarray:
    if mask is None:
        mask = foreground_mask(rgb)
    out = np.ascontiguousarray(rgb[:, :, :3]).copy()
    out[~mask] = bg
    return out


def _shift_rgb(rgb: np.ndarray, dx: int, dy: int, *, fill: int = 245) -> np.ndarray:
    h, w = rgb.shape[:2]
    out = np.full_like(rgb, fill)
    if dx >= 0:
        x0_src, x0_dst, tw = 0, dx, w - dx
    else:
        x0_src, x0_dst, tw = -dx, 0, w + dx
    if dy >= 0:
        y0_src, y0_dst, th = 0, dy, h - dy
    else:
        y0_src, y0_dst, th = -dy, 0, h + dy
    if tw <= 0 or th <= 0:
        return out
    out[y0_dst : y0_dst + th, x0_dst : x0_dst + tw] = rgb[
        y0_src : y0_src + th, x0_src : x0_src + tw
    ]
    return out


def _scale_centered(rgb: np.ndarray, scale: float, *, fill: int = 245) -> np.ndarray:
    import cv2

    if abs(scale - 1.0) < 1e-3:
        return rgb
    h, w = rgb.shape[:2]
    nh = max(1, int(round(h * scale)))
    nw = max(1, int(round(w * scale)))
    interp = cv2.INTER_AREA if scale < 1.0 else cv2.INTER_LINEAR
    resized = cv2.resize(rgb, (nw, nh), interpolation=interp)
    out = np.full((h, w, 3), fill, dtype=np.uint8)
    if nh >= h and nw >= w:
        y0 = (nh - h) // 2
        x0 = (nw - w) // 2
        return np.ascontiguousarray(resized[y0 : y0 + h, x0 : x0 + w])
    y_off = (h - nh) // 2
    x_off = (w - nw) // 2
    out[y_off : y_off + nh, x_off : x_off + nw] = resized
    return out


def final_score(
    ssim: float, pixel: float, edge: float, color: float
) -> float:
    return float(
        _WEIGHT_SSIM * ssim
        + _WEIGHT_PIXEL * pixel
        + _WEIGHT_EDGE * edge
        + _WEIGHT_COLOR * color
    )


def confidence_of(score: float, margin: float) -> str:
    """方案 §18：high 需高分+大间隔；否则 medium / low。"""
    if score < _MED_SCORE:
        return "low"
    if score >= _HIGH_SCORE and margin >= _HIGH_MARGIN:
        return "high"
    return "medium"


def _score_pair(
    query_rgb: np.ndarray,
    tmpl_rgb: np.ndarray,
    *,
    query_fg: np.ndarray | None = None,
    query_hist: np.ndarray | None = None,
    query_edge: np.ndarray | None = None,
    tmpl_fg: np.ndarray | None = None,
    tmpl_hist: np.ndarray | None = None,
    tmpl_edge: np.ndarray | None = None,
) -> tuple[float, dict[str, float]]:
    """前景 SSIM/Pixel + Edge + Color（方案 §14）。"""
    q_fg = query_fg if query_fg is not None else paint_background(query_rgb)
    t_fg = tmpl_fg if tmpl_fg is not None else paint_background(tmpl_rgb)
    ssim_s = compute_ssim(q_fg, t_fg)
    pix = compute_pixel_similarity(q_fg, t_fg)
    if query_edge is not None and tmpl_edge is not None:
        edge = _edge_map_similarity(query_edge, tmpl_edge)
    else:
        edge = compute_edge_similarity(q_fg, t_fg)
    qh = query_hist if query_hist is not None else compute_color_hist(q_fg)
    th = tmpl_hist if tmpl_hist is not None else compute_color_hist(t_fg)
    col = compute_color_similarity(qh, th)
    fs = final_score(ssim_s, pix, edge, col)
    return fs, {"ssim": ssim_s, "pixel": pix, "edge": edge, "color": col}


def _cheap_rank_score(
    *,
    query_fg: np.ndarray,
    query_edge: np.ndarray,
    query_hist: np.ndarray,
    tmpl: "_TmplFeat",
) -> float:
    """粗排快分：不做 SSIM（避免对 60+ 候选狂打高斯模糊）。"""
    pix = compute_pixel_similarity(query_fg, tmpl.fg)
    edge = _edge_map_similarity(query_edge, tmpl.edge)
    col = compute_color_similarity(query_hist, tmpl.color_hist)
    return float(0.45 * pix + 0.45 * edge + 0.10 * col)


def _best_shift_matchtemplate(
    q_gray: np.ndarray, t_gray: np.ndarray, *, radius: int = _TRANSLATE_R
) -> tuple[int, int, float]:
    """一次 matchTemplate 搜完 ±radius 平移（替代 81 次逐点打分）。"""
    import cv2

    r = int(radius)
    padded = cv2.copyMakeBorder(
        q_gray, r, r, r, r, borderType=cv2.BORDER_CONSTANT, value=245
    )
    res = cv2.matchTemplate(padded, t_gray, cv2.TM_CCOEFF_NORMED)
    # res 形状应为 (2r+1, 2r+1)：每个位置对应模板相对原图的平移
    _, max_val, _, max_loc = cv2.minMaxLoc(res)
    dx = int(max_loc[0] - r)
    dy = int(max_loc[1] - r)
    return dx, dy, float(max_val)


def _local_face_score(a_rgb: np.ndarray, b_rgb: np.ndarray) -> float:
    """方案 §16：眼睛/嘴局部再比（混淆表情二次精匹配）。"""
    h, w = a_rgb.shape[:2]
    eye_a, eye_b = a_rgb[: h // 2], b_rgb[: h // 2]
    mouth_a = a_rgb[h // 2 :, w // 4 : 3 * w // 4]
    mouth_b = b_rgb[h // 2 :, w // 4 : 3 * w // 4]
    return 0.55 * compute_ssim(eye_a, eye_b) + 0.45 * compute_ssim(mouth_a, mouth_b)


_CONFUSE_GROUP = frozenset({"微笑", "憨笑", "奸笑", "坏笑", "愉快", "笑脸"})


# ---------------------------------------------------------------------------
# 模板特征缓存
# ---------------------------------------------------------------------------


@dataclass
class _TmplFeat:
    name: str
    filename: str
    rgb: np.ndarray
    fg: np.ndarray
    gray: np.ndarray
    phash: int
    color_hist: np.ndarray
    edge: np.ndarray


def features_cache_path() -> Path:
    return official_dir() / "emoji_features.json"


@lru_cache(maxsize=1)
def _load_template_feats() -> tuple[_TmplFeat, ...]:
    from PIL import Image

    try:
        from joy.core.wechat_emoji import package_dir, sync_official_from_package

        src = package_dir()
        if "DirectCrop" in src.name:
            sync_official_from_package()
    except Exception as exc:
        log.debug("emoji package sync skip: %s", exc)

    feats: list[_TmplFeat] = []
    for code in emoji_codes():
        p = tile_path(code)
        if p is None or not p.is_file():
            continue
        try:
            rgba = np.asarray(Image.open(p).convert("RGBA"))
            rgb = normalize_emoji_patch(_composite_rgba(rgba))
            fg = paint_background(rgb)
            feats.append(
                _TmplFeat(
                    name=code,
                    filename=f"{code}.png",
                    rgb=rgb,
                    fg=fg,
                    gray=_to_gray(fg),
                    phash=compute_phash(fg),
                    color_hist=compute_color_hist(fg),
                    edge=_canny_edges(fg),
                )
            )
        except Exception as exc:
            log.debug("tmpl feat skip %s: %s", code, exc)
    log.info("emoji template feats ready n=%s source=direct_crop_white", len(feats))
    try:
        meta = {
            t.name: {"phash": t.phash, "filename": t.filename} for t in feats
        }
        features_cache_path().write_text(
            json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8"
        )
    except Exception:
        pass
    return tuple(feats)


class WeChatEmojiMatcher:
    """大师方案：多路召回 + 前景分 + 平移/缩放对齐 + margin 置信度。"""

    def __init__(self) -> None:
        self._tmpls = _load_template_feats()

    def _multi_recall(self, patch: np.ndarray) -> tuple[list[int], dict[str, int | None]]:
        """pHash ∪ Edge ∪ Color 各 TopK → 并集（方案 §6/§13）。"""
        q_fg = paint_background(patch)
        q_ph = compute_phash(q_fg)
        q_edge = _canny_edges(q_fg)
        q_hist = compute_color_hist(q_fg)
        n = len(self._tmpls)
        ph_rank = sorted(
            range(n), key=lambda i: hamming64(q_ph, self._tmpls[i].phash)
        )
        ed_rank = sorted(
            range(n),
            key=lambda i: -_edge_map_similarity(q_edge, self._tmpls[i].edge),
        )
        co_rank = sorted(
            range(n),
            key=lambda i: -compute_color_similarity(q_hist, self._tmpls[i].color_hist),
        )
        k = min(_RECALL_K, n)
        union: list[int] = []
        seen: set[int] = set()
        for idx in list(ph_rank[:k]) + list(ed_rank[:k]) + list(co_rank[:k]):
            if idx not in seen:
                seen.add(idx)
                union.append(idx)

        def _rank_of(name: str, order: list[int]) -> int | None:
            for r, i in enumerate(order, 1):
                if self._tmpls[i].name == name:
                    return r
            return None

        debug = {
            "phash_rank_色": _rank_of("色", ph_rank),
            "edge_rank_色": _rank_of("色", ed_rank),
            "color_rank_色": _rank_of("色", co_rank),
            "in_union_色": int(
                any(self._tmpls[i].name == "色" for i in union)
            ),
            "candidate_count": len(union),
        }
        return union, debug

    def _best_aligned_score(
        self,
        query: np.ndarray,
        tmpl: _TmplFeat,
        *,
        do_search: bool,
        query_fg: np.ndarray,
        query_gray: np.ndarray,
        query_hist: np.ndarray,
        query_edge: np.ndarray,
    ) -> tuple[float, dict[str, float], tuple[int, int, float]]:
        """scale×translate：用 matchTemplate 一次搜平移，再精算前景分。"""
        if not do_search:
            fs, parts = _score_pair(
                query,
                tmpl.rgb,
                query_fg=query_fg,
                query_hist=query_hist,
                query_edge=query_edge,
                tmpl_fg=tmpl.fg,
                tmpl_hist=tmpl.color_hist,
                tmpl_edge=tmpl.edge,
            )
            return fs, parts, (0, 0, 1.0)

        best_ncc = -1.0
        best_pose = (0, 0, 1.0)
        for scale in _SCALES:
            if abs(scale - 1.0) < 1e-3:
                t_gray = tmpl.gray
                t_rgb = tmpl.rgb
            else:
                t_rgb = _scale_centered(tmpl.rgb, scale)
                t_gray = _to_gray(paint_background(t_rgb))
            dx, dy, ncc = _best_shift_matchtemplate(
                query_gray, t_gray, radius=_TRANSLATE_R
            )
            if ncc > best_ncc:
                best_ncc = ncc
                best_pose = (dx, dy, scale)

        dx, dy, scale = best_pose
        if abs(scale - 1.0) < 1e-3 and dx == 0 and dy == 0:
            aligned = tmpl.rgb
            t_fg, t_edge, t_hist = tmpl.fg, tmpl.edge, tmpl.color_hist
        else:
            aligned = _shift_rgb(_scale_centered(tmpl.rgb, scale), dx, dy)
            t_fg = paint_background(aligned)
            t_edge = _canny_edges(t_fg)
            t_hist = None
        fs, parts = _score_pair(
            query,
            aligned,
            query_fg=query_fg,
            query_hist=query_hist,
            query_edge=query_edge,
            tmpl_fg=t_fg,
            tmpl_hist=t_hist,
            tmpl_edge=t_edge,
        )
        return fs, parts, best_pose

    def match(self, image: np.ndarray) -> EmojiMatch | None:
        if image is None or getattr(image, "size", 0) == 0:
            return None
        if not self._tmpls:
            return None
        patch = normalize_emoji_patch(image[:, :, :3] if image.ndim == 3 else image)
        if _reject_non_wechat_pack(patch):
            return EmojiMatch(
                tag="",
                name="",
                filename="",
                score=0.0,
                margin=0.0,
                confidence="low",
                alternatives=[],
            )

        union, recall_dbg = self._multi_recall(patch)
        q_fg = paint_background(patch)
        q_gray = _to_gray(q_fg)
        q_hist = compute_color_hist(q_fg)
        q_edge = _canny_edges(q_fg)

        # 粗排：快分（无 SSIM）→ TopN
        coarse: list[tuple[float, int]] = []
        for i in union:
            cheap = _cheap_rank_score(
                query_fg=q_fg,
                query_edge=q_edge,
                query_hist=q_hist,
                tmpl=self._tmpls[i],
            )
            coarse.append((cheap, i))
        coarse.sort(key=lambda x: -x[0])
        fine_n = min(_FINE_N, len(coarse))
        fine_idxs = [i for _, i in coarse[:fine_n]]

        # 若 identity 全分已 high，跳过对齐搜索（截图对齐良好时省掉大头）
        scored: list[tuple[float, _TmplFeat, dict[str, float]]] = []
        identity_full: list[tuple[float, _TmplFeat, dict[str, float]]] = []
        for i in fine_idxs[:5]:
            t = self._tmpls[i]
            fs, parts = _score_pair(
                patch,
                t.rgb,
                query_fg=q_fg,
                query_hist=q_hist,
                query_edge=q_edge,
                tmpl_fg=t.fg,
                tmpl_hist=t.color_hist,
                tmpl_edge=t.edge,
            )
            identity_full.append((fs, t, {**parts, "dx": 0.0, "dy": 0.0, "scale": 1.0}))
        identity_full.sort(key=lambda x: -x[0])
        if identity_full:
            i1, i2 = identity_full[0][0], (
                identity_full[1][0] if len(identity_full) > 1 else 0.0
            )
            if confidence_of(i1, i1 - i2) == "high":
                scored = identity_full
            else:
                # 精排：TopN 做 matchTemplate 平移 + 少量缩放
                for i in fine_idxs:
                    t = self._tmpls[i]
                    fs, parts, pose = self._best_aligned_score(
                        patch,
                        t,
                        do_search=True,
                        query_fg=q_fg,
                        query_gray=q_gray,
                        query_hist=q_hist,
                        query_edge=q_edge,
                    )
                    parts = {
                        **parts,
                        "dx": float(pose[0]),
                        "dy": float(pose[1]),
                        "scale": pose[2],
                    }
                    scored.append((fs, t, parts))
                scored.sort(key=lambda x: -x[0])
        else:
            scored = []

        if not scored:
            return EmojiMatch(
                tag="",
                name="",
                filename="",
                score=0.0,
                margin=0.0,
                confidence="low",
                alternatives=[],
            )

        top1_s, top1_t, parts = scored[0]
        top2_s = scored[1][0] if len(scored) > 1 else 0.0
        margin = float(top1_s - top2_s)

        if (
            len(scored) >= 2
            and confidence_of(top1_s, margin) != "high"
            and top1_t.name in _CONFUSE_GROUP
            and scored[1][1].name in _CONFUSE_GROUP
        ):
            loc1 = _local_face_score(q_fg, top1_t.fg)
            loc2 = _local_face_score(q_fg, scored[1][1].fg)
            if loc2 > loc1 + 0.03:
                scored[0], scored[1] = scored[1], scored[0]
                top1_s, top1_t, parts = scored[0]
                top2_s = scored[1][0]
                margin = float(top1_s - top2_s)
                parts = {**parts, "local_swap": 1.0}

        conf = confidence_of(top1_s, margin)
        top3 = [
            (
                t.name,
                round(float(s), 3),
                {k: round(float(v), 3) for k, v in p.items()},
            )
            for s, t, p in scored[:3]
        ]
        alts = [tag(t.name) for _, t, _ in scored[:5]]
        se_rank = next(
            (i + 1 for i, (_s, t, _p) in enumerate(scored) if t.name == "色"),
            None,
        )
        se_score = next(
            (round(float(s), 3) for s, t, _p in scored if t.name == "色"),
            None,
        )
        log.debug(
            "emoji_match recall=%s top3=%s pick=%s score=%.3f margin=%.3f conf=%s "
            "se_rank=%s se_score=%s",
            recall_dbg,
            [(n, sc) for n, sc, _ in top3],
            top1_t.name,
            top1_s,
            margin,
            conf,
            se_rank,
            se_score,
        )
        log.debug("emoji_match top3_detail=%s", top3)

        accept = (
            top1_s >= _FLOOR_SCORE
            and conf != "low"
            and (conf == "high" or margin >= _ACCEPT_MARGIN)
        )
        if not accept:
            return EmojiMatch(
                tag="",
                name="",
                filename="",
                score=top1_s,
                margin=margin,
                confidence=conf,
                alternatives=alts,
            )
        return EmojiMatch(
            tag=tag(top1_t.name),
            name=top1_t.name,
            filename=top1_t.filename,
            score=top1_s,
            margin=margin,
            confidence=conf,
            alternatives=alts,
        )


def _reject_non_wechat_pack(patch_rgb: np.ndarray) -> bool:
    """系统 Unicode emoji / 非黄脸物件的粗拒识（避免 😂→炸弹）。"""
    r = patch_rgb[:, :, 0].astype(np.float32)
    g = patch_rgb[:, :, 1].astype(np.float32)
    b = patch_rgb[:, :, 2].astype(np.float32)
    yellow = (r > 165) & (g > 135) & (b < 175) & (r > b + 18) & (g > b + 8)
    y_ratio = float(yellow.mean())
    dark = (r + g + b) < 200
    d_ratio = float(dark.mean())
    # 典型微信黄脸：黄像素不少；炸弹类暗色多
    # Unicode 😂 黄也多，但仍会在综合分上输给阈值——这里只拒「明显不像包内图」的
    if y_ratio < 0.08 and d_ratio > 0.22:
        return True  # 偏黑物件且几乎无黄
    return False


# ---------------------------------------------------------------------------
# 区域定位（只裁表情本体，不扫整屏硬匹配）
# ---------------------------------------------------------------------------


def _message_roi(
    rgb: np.ndarray,
    *,
    contact_left_ratio: float = 0.28,
    message_top_ratio: float = 0.08,
    compose_top_ratio: float = 0.82,
) -> tuple[np.ndarray, int, int] | None:
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
    return np.ascontiguousarray(rgb[y0:y1, x0:x1, :3]), x0, y0


def _pad_box(
    x: int, y: int, w: int, h: int, rw: int, rh: int, *, pad_ratio: float = 0.18
) -> tuple[int, int, int, int]:
    """方案 §8：外扩约 6~8px，避免裁死表情本体。"""
    pad = max(6, int(pad_ratio * max(w, h)))
    x0 = max(0, x - pad)
    y0 = max(0, y - pad)
    x1 = min(rw, x + w + pad)
    y1 = min(rh, y + h + pad)
    return x0, y0, x1 - x0, y1 - y0


def _expand_match_box(
    x: int, y: int, w: int, h: int, rw: int, rh: int, *, pad: int = 6
) -> tuple[int, int, int, int]:
    """单表情：外扩约 6~8px，目标边长约 52~56。密排行不要用大框（会吃到邻居）。"""
    side = int(min(56, max(52, max(w, h) + 2 * pad)))
    cx = x + w * 0.5
    cy = y + h * 0.5
    bx = int(round(cx - side * 0.5))
    by = int(round(cy - side * 0.5))
    bx = max(0, min(max(0, rw - side), bx))
    by = max(0, min(max(0, rh - side), by))
    side_w = min(side, rw - bx)
    side_h = min(side, rh - by)
    side = max(1, min(side_w, side_h))
    return bx, by, side, side


def _light_pad_box(
    x: int, y: int, w: int, h: int, rw: int, rh: int, *, pad: int = 3
) -> tuple[int, int, int, int]:
    """密排等分格：只轻微外扩，避免并排串味。"""
    x0 = max(0, x - pad)
    y0 = max(0, y - pad)
    x1 = min(rw, x + w + pad)
    y1 = min(rh, y + h + pad)
    return x0, y0, x1 - x0, y1 - y0


def _box_content_ratio(roi: np.ndarray, box: tuple[int, int, int, int]) -> float:
    """非近白像素占比，用于行扫时跳过空白格。"""
    x, y, w, h = box
    if w < 8 or h < 8:
        return 0.0
    patch = roi[y : y + h, x : x + w]
    r = patch[:, :, 0].astype(np.int16)
    g = patch[:, :, 1].astype(np.int16)
    b = patch[:, :, 2].astype(np.int16)
    near_white = (r > 238) & (g > 235) & (b > 225)
    return float((~near_white).mean())


def _box_yellow_ratio(roi: np.ndarray, box: tuple[int, int, int, int]) -> float:
    x, y, w, h = box
    if w < 8 or h < 8:
        return 0.0
    patch = roi[y : y + h, x : x + w]
    r = patch[:, :, 0].astype(np.float32)
    g = patch[:, :, 1].astype(np.float32)
    b = patch[:, :, 2].astype(np.float32)
    yellow = (r > 155) & (g > 125) & (b < 180) & (r > b + 15) & (g > b + 6)
    return float(yellow.mean())


def _emoji_seed_boxes(roi: np.ndarray) -> list[tuple[int, int, int, int]]:
    """黄脸连通域 → 种子框（不含彩色杂块，避免头像/阴影误检）。"""
    import cv2

    r = roi[:, :, 0].astype(np.int16)
    g = roi[:, :, 1].astype(np.int16)
    b = roi[:, :, 2].astype(np.int16)
    yellow = (r > 155) & (g > 125) & (b < 180) & (r > b + 15) & (g > b + 6)
    bright_flat = (r > 235) & (g > 230) & (b > 200)
    mask = yellow & ~bright_flat
    u8 = (mask.astype(np.uint8) * 255)
    # 只用 OPEN，避免 CLOSE 把并排表情粘成一条宽块
    k = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (2, 2))
    u8 = cv2.morphologyEx(u8, cv2.MORPH_OPEN, k)
    n, _labels, stats, _ = cv2.connectedComponentsWithStats(u8, connectivity=8)
    boxes: list[tuple[int, int, int, int]] = []
    rh, rw = roi.shape[:2]
    for i in range(1, n):
        x, y, w, h, area = (
            int(stats[i, 0]),
            int(stats[i, 1]),
            int(stats[i, 2]),
            int(stats[i, 3]),
            int(stats[i, 4]),
        )
        if h < 14 or h > 100:
            continue
        if area < 90:
            continue
        ar = w / max(1, h)
        # 一行粘连：按近似正方形切开
        if ar > 1.45 and w <= rw * 0.95:
            n_cell = max(2, int(round(w / float(h))))
            cell_w = max(14, w // n_cell)
            for j in range(n_cell):
                cx = x + j * cell_w
                cw = cell_w if j < n_cell - 1 else (x + w - cx)
                if cw < 14 or area / n_cell < 70:
                    continue
                boxes.append(_pad_box(cx, y, cw, h, rw, rh))
            continue
        if w < 14 or w > 100:
            continue
        if area > 6500:
            continue
        if ar < 0.55 or ar > 1.55:
            continue
        boxes.append(_pad_box(x, y, w, h, rw, rh))
    return boxes


def _expand_row_scan(
    roi: np.ndarray, seeds: list[tuple[int, int, int, int]]
) -> list[tuple[int, int, int, int]]:
    """按行：统计个数 → 等分方格切开 → 从左到右（与直裁库同思路）。"""
    if not seeds:
        return []
    rh, rw = roi.shape[:2]
    items = sorted(
        ((x + w * 0.5, y + h * 0.5, w, h, x, y) for x, y, w, h in seeds),
        key=lambda t: (t[1], t[0]),
    )
    rows: list[list[tuple[float, float, int, int, int, int]]] = []
    for it in items:
        if rows and abs(rows[-1][-1][1] - it[1]) <= max(10.0, rows[-1][-1][3] * 0.45):
            rows[-1].append(it)
        else:
            rows.append([it])

    out: list[tuple[int, int, int, int]] = []
    for row in rows:
        # 单粒：直接用种子方格，避免内容投影把框拉歪
        if len(row) == 1:
            _cx, _cy, w, h, sx, sy = row[0]
            side = int(max(20, min(48, round((w + h) * 0.5))))
            cx = sx + w * 0.5
            cy = sy + h * 0.5
            bx = int(round(cx - side * 0.5))
            by = int(round(cy - side * 0.5))
            bx = max(0, min(rw - side, bx))
            by = max(0, min(rh - side, by))
            out.append(_expand_match_box(bx, by, side, side, rw, rh))
            log.info("emoji_seg row y=%s n=1 cell=%s", by, out[-1][2])
            continue

        hs = [t[3] for t in row]
        ws = [t[2] for t in row]
        cell = int(np.median([min(w, h) for w, h in zip(ws, hs)]))
        cell = max(20, min(48, cell))
        cy = float(np.mean([t[1] for t in row]))
        y0 = int(max(0, round(cy - cell * 0.5)))
        y0 = min(y0, max(0, rh - cell))
        y1 = min(rh, y0 + cell)

        # 用「非白内容」定左右边界（含色的红心，不单靠黄脸）
        x_range = _strip_content_x_range(roi, y0, y1)
        if x_range is None:
            left = min(t[4] for t in row)
            right = max(t[4] + t[2] for t in row)
        else:
            left, right = x_range
            # 与种子取并集，防止内容投影偏窄
            left = min(left, min(t[4] for t in row))
            right = max(right, max(t[4] + t[2] for t in row))

        span = max(cell, right - left)
        # 个数：内容宽度 / 格子边长；至少覆盖已有种子数
        n = int(round(span / float(cell)))
        n = max(len(row), min(16, max(1, n)))
        # 用个数反推等间距，保证顺序稳定
        if n == 1:
            cx = (left + right) * 0.5
            bx = int(round(cx - cell * 0.5))
            bx = max(0, min(rw - cell, bx))
            out.append(_light_pad_box(bx, y0, cell, cell, rw, rh))
            log.info("emoji_seg row y=%s n=1 cell=%s", y0, out[-1][2])
            continue

        pitch = span / float(n)
        # 密排时匹配格不能大于间距，否则一框吃两脸
        match_cell = min(cell, max(20, int(round(pitch)) - 2))
        # 左缘对齐
        x0 = float(left)
        boxes: list[tuple[int, int, int, int]] = []
        for i in range(n):
            cx = x0 + (i + 0.5) * pitch
            bx = int(round(cx - match_cell * 0.5))
            bx = max(0, min(rw - match_cell, bx))
            box = (bx, y0, match_cell, match_cell)
            if _box_content_ratio(roi, box) < 0.08:
                continue
            boxes.append(_light_pad_box(bx, y0, match_cell, match_cell, rw, rh, pad=2))
        # 若过滤后太少，回退为不滤空格（仍等分）
        if len(boxes) < max(1, n - 1):
            boxes = []
            for i in range(n):
                cx = x0 + (i + 0.5) * pitch
                bx = int(round(cx - match_cell * 0.5))
                bx = max(0, min(rw - match_cell, bx))
                boxes.append(
                    _light_pad_box(bx, y0, match_cell, match_cell, rw, rh, pad=2)
                )
        log.info(
            "emoji_seg row y=%s n=%s cell=%s match_cell=%s span=%s kept=%s",
            y0,
            n,
            cell,
            match_cell,
            span,
            len(boxes),
        )
        out.extend(boxes)

    out.sort(key=lambda b: (b[1], b[0]))
    return out


def _strip_content_x_range(
    roi: np.ndarray, y0: int, y1: int
) -> tuple[int, int] | None:
    """一行带内非气泡底像素的左右范围。"""
    if y1 <= y0:
        return None
    strip = roi[y0:y1]
    r = strip[:, :, 0].astype(np.int16)
    g = strip[:, :, 1].astype(np.int16)
    b = strip[:, :, 2].astype(np.int16)
    near_white = (r > 232) & (g > 232) & (b > 222)
    content = ~near_white
    cols = content.any(axis=0)
    xs = np.where(cols)[0]
    if len(xs) < 4:
        return None
    return int(xs.min()), int(xs.max()) + 1


def _filter_weak_row_hits(hits: list[EmojiHit]) -> list[EmojiHit]:
    """同行里明显更弱的误检丢掉（如 [菜刀] 夹在真表情两侧）。"""
    rows = group_hits_by_row(hits)
    kept: list[EmojiHit] = []
    for row in rows:
        if len(row) == 1:
            kept.extend(row)
            continue
        best = max(h.score for h in row)
        for h in row:
            # 比同行最佳低太多 → 当作滑窗误检
            if h.score < best - 0.12 and h.score < 0.72:
                continue
            if h.score < 0.50:
                continue
            kept.append(h)
    kept.sort(key=lambda h: (h.y, h.x))
    return kept


def _content_blob_seeds(roi: np.ndarray) -> list[tuple[int, int, int, int]]:
    """非黄脸物件（菜刀/握手等）：用非白连通域找近似方块种子。"""
    import cv2

    r = roi[:, :, 0].astype(np.int16)
    g = roi[:, :, 1].astype(np.int16)
    b = roi[:, :, 2].astype(np.int16)
    near_white = (r > 232) & (g > 232) & (b > 222)
    green_bubble = (g > 150) & (g >= r + 18) & (g >= b + 18) & (r < 210)
    mask = ~(near_white | green_bubble)
    u8 = (mask.astype(np.uint8) * 255)
    k = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (2, 2))
    u8 = cv2.morphologyEx(u8, cv2.MORPH_OPEN, k)
    n, _labels, stats, _ = cv2.connectedComponentsWithStats(u8, connectivity=8)
    boxes: list[tuple[int, int, int, int]] = []
    rh, rw = roi.shape[:2]
    for i in range(1, n):
        x, y, w, h, area = (
            int(stats[i, 0]),
            int(stats[i, 1]),
            int(stats[i, 2]),
            int(stats[i, 3]),
            int(stats[i, 4]),
        )
        if h < 14 or h > 100 or w < 14 or w > 100:
            continue
        # 聊天图片/大图预览：面积或边长过大，不当作小表情
        if area < 80 or area > 2800 or max(w, h) > 72:
            continue
        ar = w / max(1, h)
        if ar < 0.55 or ar > 1.55:
            if ar > 1.45 and w <= rw * 0.95:
                n_cell = max(2, int(round(w / float(h))))
                cell_w = max(14, w // n_cell)
                for j in range(n_cell):
                    cx = x + j * cell_w
                    cw = cell_w if j < n_cell - 1 else (x + w - cx)
                    if cw >= 14 and cw * h <= 2800:
                        boxes.append(_pad_box(cx, y, cw, h, rw, rh))
            continue
        boxes.append(_pad_box(x, y, w, h, rw, rh))
    return boxes


def _merge_seed_boxes(
    a: list[tuple[int, int, int, int]], b: list[tuple[int, int, int, int]]
) -> list[tuple[int, int, int, int]]:
    """合并黄脸/物件种子，近距去重。有黄脸时丢掉过大物件框（防图片切片冒充表情）。"""
    yellow = list(a)
    content = list(b)
    if yellow:
        content = [
            box
            for box in content
            if max(box[2], box[3]) <= 64 and box[2] * box[3] <= 3600
        ]
    all_b = yellow + content
    all_b.sort(key=lambda t: (t[1], t[0]))
    kept: list[tuple[int, int, int, int]] = []
    for x, y, w, h in all_b:
        cx, cy = x + w * 0.5, y + h * 0.5
        if any(
            abs(cx - (kx + kw * 0.5)) < max(w, kw) * 0.45
            and abs(cy - (ky + kh * 0.5)) < max(h, kh) * 0.45
            for kx, ky, kw, kh in kept
        ):
            continue
        kept.append((x, y, w, h))
    return kept


def _emoji_candidate_boxes(roi: np.ndarray) -> list[tuple[int, int, int, int]]:
    """黄脸 + 物件种子 → 按行计数等分切开 → 左右顺序方格。"""
    seeds = _merge_seed_boxes(_emoji_seed_boxes(roi), _content_blob_seeds(roi))
    if not seeds:
        return []
    return _expand_row_scan(roi, seeds)


def group_hits_by_row(
    hits: list[EmojiHit], *, y_tol: float = 0.028
) -> list[list[EmojiHit]]:
    """按归一化 y 聚成「同一气泡行」，行内按 x 从左到右。"""
    usable = sorted((h for h in hits if h.code), key=lambda h: (h.y, h.x))
    rows: list[list[EmojiHit]] = []
    for h in usable:
        if rows and abs(rows[-1][-1].y - h.y) <= y_tol:
            rows[-1].append(h)
        else:
            rows.append([h])
    for row in rows:
        row.sort(key=lambda h: h.x)
    return rows


def _tags_from_row(row: list[EmojiHit]) -> str:
    return "".join(tag(h.code) for h in row if h.code)


@lru_cache(maxsize=1)
def _matcher() -> WeChatEmojiMatcher:
    return WeChatEmojiMatcher()


def match_emojis_in_frame(
    rgb: np.ndarray | None,
    *,
    contact_left_ratio: float = 0.28,
    message_top_ratio: float = 0.08,
    compose_top_ratio: float = 0.82,
    min_score: float = _FLOOR_SCORE,
    max_hits: int = 32,
) -> list[EmojiHit]:
    """定位候选表情块 → 多路召回+前景对齐比对；仅采纳 high/可靠 medium。"""
    if rgb is None:
        return []
    t0 = time.perf_counter()
    cropped = _message_roi(
        rgb,
        contact_left_ratio=contact_left_ratio,
        message_top_ratio=message_top_ratio,
        compose_top_ratio=compose_top_ratio,
    )
    if cropped is None:
        return []
    roi, ox, oy = cropped
    fh = float(rgb.shape[0])
    fw = float(rgb.shape[1])
    boxes = _emoji_candidate_boxes(roi)
    matcher = _matcher()
    hits: list[EmojiHit] = []
    for x, y, w, h in boxes:
        patch = roi[y : y + h, x : x + w]
        m = matcher.match(patch)
        if m is None or not m.name:
            continue
        if m.score < min_score:
            continue
        # 低置信不进 hits，避免 reconcile 写脏 Memory
        if m.confidence == "low":
            continue
        hits.append(
            EmojiHit(
                code=m.name,
                score=m.score,
                y=(oy + y + h * 0.5) / fh,
                x=(ox + x + w * 0.5) / fw,
                w=w,
                h=h,
                confidence=m.confidence,
                margin=m.margin,
            )
        )
    # 保持从左到右、从上到下的切开顺序（回填靠顺序，不靠乱序分数）
    hits.sort(key=lambda h: (h.y, h.x))
    # 同位置去重（保留先出现的）
    kept: list[EmojiHit] = []
    for h in hits:
        if any(abs(h.y - k.y) < 0.018 and abs(h.x - k.x) < 0.018 for k in kept):
            continue
        kept.append(h)
    kept = _filter_weak_row_hits(kept)
    kept = kept[:max_hits]
    ms = (time.perf_counter() - t0) * 1000
    rows = group_hits_by_row(kept)
    log.info(
        "emoji_match hits=%s rows=%s ms=%.0f boxes=%s order=%s",
        len(kept),
        len(rows),
        ms,
        len(boxes),
        [h.code for h in kept],
    )
    return kept


def format_emoji_match_hint(hits: list[EmojiHit]) -> str:
    if not hits:
        return ""
    parts = [
        f"{tag(h.code)}({h.score:.2f}/{h.confidence})"
        for h in hits
        if h.code
    ]
    if not parts:
        return ""
    return (
        "本地模板已匹配到气泡内微信小表情（从上到下，请优先采用）："
        + "、".join(parts)
    )


def apply_emoji_matches_to_context(
    ctx: ScreenContext,
    hits: list[EmojiHit],
    *,
    override_score: float = _HIGH_SCORE,
) -> ScreenContext:
    """替换 VL 占位；OCR 纯文字时按 y 把 Matcher 行拼进/插入消息。"""
    import re

    _ = override_score
    if not ctx.messages and not hits:
        return ctx
    # 先清洗 VL 乱贴的微信标签（文档：Matcher 主判，VLM 不自由起名）
    if ctx.messages:
        ctx = reconcile_vl_emoji_guesses(ctx, hits)

    if not hits:
        return ctx
    rows = group_hits_by_row([h for h in hits if h.code])
    if not rows:
        return ctx

    need = any(
        (m.text or "").strip() in {"[表情]", "[动画表情]", "[贴纸]"}
        or bool(re.fullmatch(r"(?:\[[^\[\]]{1,16}\]\s*)+", (m.text or "").strip() or ""))
        for m in ctx.messages
    )
    if need:
        qi = 0
        new_msgs: list[ChatMessage] = []
        applied = 0
        tag_re = re.compile(r"^(?:\[[^\[\]]{1,16}\]\s*)+$")
        uni_re = re.compile(
            r"[\U0001F300-\U0001FAFF\U00002700-\U000027BF\U0001F600-\U0001F64F]+"
        )
        for m in ctx.messages:
            t = (m.text or "").strip()
            if uni_re.search(t) or (
                re.search(r"[\u4e00-\u9fff]{2,}", t) and not tag_re.match(t)
            ):
                new_msgs.append(m)
                continue
            if qi >= len(rows):
                new_msgs.append(m)
                continue
            row = rows[qi]
            joined = _tags_from_row(row)
            if t in {"[表情]", "[动画表情]", "[贴纸]"} or tag_re.match(t):
                if joined and joined != t:
                    new_msgs.append(m.model_copy(update={"text": joined}))
                    applied += 1
                else:
                    new_msgs.append(m)
                qi += 1
                continue
            new_msgs.append(m)
        # 占位用尽后仍有 Matcher 行 → 按 y 插入剩余
        if qi < len(rows):
            rest = rows[qi:]
            merged = _merge_emoji_rows_by_y(new_msgs, rest)
            if applied:
                log.info("emoji_match applied %s into context (+y-merge rest)", applied)
            return ctx.model_copy(update={"messages": merged})
        if applied:
            log.info("emoji_match applied %s into context", applied)
        return ctx.model_copy(update={"messages": new_msgs})

    # OCR 纯文字：无占位，按纵坐标拼进同侧近邻气泡或插入新行
    merged = _merge_emoji_rows_by_y(list(ctx.messages), rows)
    if merged != list(ctx.messages):
        log.info(
            "emoji_match y-merge msgs %s→%s tags=%s",
            len(ctx.messages),
            len(merged),
            [_tags_from_row(r) for r in rows],
        )
    return ctx.model_copy(update={"messages": merged})


_Y_ATTACH = 0.035  # 整图相对高度：文字与表情须很近才算同一气泡


def _merge_emoji_rows_by_y(
    messages: list[ChatMessage],
    rows: list[list[EmojiHit]],
) -> list[ChatMessage]:
    """把 Matcher 行挂到最近同侧文字后，或作为独立消息插入。

    不把表情标签硬拼到「离得很远」的纯文字上，避免无表情聊天被污染。
    """
    if not rows:
        return messages

    items: list[list[object]] = []
    n = max(1, len(messages))
    for i, m in enumerate(messages):
        y = m.y_norm if m.y_norm is not None else (i + 0.5) / (n + 1)
        items.append([float(y), i, m])

    extras: list[list[object]] = []
    for j, row in enumerate(rows):
        tags = _tags_from_row(row)
        if not tags or tags == "[表情]":
            continue
        # 低置信命中不往正文上粘
        if any(
            (h.confidence == "low") or (h.score < 0.72) for h in row
        ):
            continue
        ey = float(sum(h.y for h in row) / len(row))
        ex = float(sum(h.x for h in row) / len(row))
        side: Side = "me" if ex >= 0.58 else "other"
        best_i = -1
        best_d = 1e9
        for idx, (ty, _ord, m) in enumerate(items):
            assert isinstance(m, ChatMessage)
            if m.side != side:
                continue
            d = abs(float(ty) - ey)
            if d < best_d:
                best_d = d
                best_i = idx
        if best_i >= 0 and best_d <= _Y_ATTACH:
            m = items[best_i][2]
            assert isinstance(m, ChatMessage)
            t = (m.text or "").strip()
            if tags in t:
                continue
            # 纯文字气泡：仅当纵坐标极近才后缀；已是占位/标签则替换或拼接
            if t in {"[表情]", "[动画表情]", "[贴纸]"}:
                new_t = tags
            elif re.fullmatch(r"(?:\[[^\[\]]{1,16}\]\s*)+", t or ""):
                new_t = f"{t}{tags}"
            else:
                new_t = f"{t}{tags}"
            items[best_i][2] = m.model_copy(
                update={"text": new_t.strip(), "y_norm": float(items[best_i][0])}
            )
            continue
        extras.append(
            [
                ey,
                1000 + j,
                ChatMessage(side=side, text=tags, y_norm=ey),
            ]
        )

    items.extend(extras)
    items.sort(key=lambda x: (float(x[0]), int(x[1])))  # type: ignore[arg-type]
    return [x[2] for x in items]  # type: ignore[misc]


_WECHAT_TAG_RE = re.compile(r"\[([^\[\]]{1,16})\]")


def reconcile_vl_emoji_guesses(
    ctx: ScreenContext, hits: list[EmojiHit]
) -> ScreenContext:
    """按「从上到下」把 Matcher 行对齐到表情气泡；同行多标签拼在一起。

    Matcher 有命中时：多出来的 VL 纯表情气泡直接丢掉（常见是图片被当成表情）。
    无命中时：已知微信标签降为 [表情]，留给上层兜底。
    """
    from joy.core.wechat_emoji import is_known_code

    rows = group_hits_by_row([h for h in (hits or []) if h.code])
    qi = 0
    new_msgs: list[ChatMessage] = []
    changed = 0
    dropped = 0

    for m in ctx.messages:
        t = (m.text or "").strip()
        if not t:
            new_msgs.append(m)
            continue

        t2 = re.sub(r"\[图片\]\s*", "", t).strip() or t
        only_tags = bool(re.fullmatch(r"(?:\[[^\[\]]{1,16}\]\s*)+", t2))
        parts = _WECHAT_TAG_RE.findall(t2)
        wechat_names = [
            n
            for n in parts
            if is_known_code(n) and n not in {"表情", "动画表情", "贴纸"}
        ]

        # 纯表情气泡（VL 常见：[图片][嘿哈] / [嘿哈] / [表情] / 一串标签）
        if only_tags or (
            wechat_names and not re.search(r"[\u4e00-\u9fffA-Za-z]{2,}", t2)
        ):
            if qi < len(rows):
                new_t = _tags_from_row(rows[qi])
                qi += 1
                if new_t != t:
                    changed += 1
                    new_msgs.append(m.model_copy(update={"text": new_t}))
                else:
                    new_msgs.append(m)
            elif rows:
                # 已有 Matcher 结果：多出来的 VL 表情/占位是幻觉，丢弃
                dropped += 1
                changed += 1
            else:
                # 无 Matcher：不信任 VL 点名，统一降为 [表情]
                new_t = "[表情]"
                if new_t != t:
                    changed += 1
                    new_msgs.append(m.model_copy(update={"text": new_t}))
                else:
                    new_msgs.append(m)
            continue

        # 正文里夹着未确认的微信标签 → 剥掉
        if wechat_names:
            def _repl(mo: re.Match[str]) -> str:
                name = mo.group(1)
                if name in {"表情", "动画表情", "贴纸"}:
                    return mo.group(0)
                if is_known_code(name):
                    return ""
                return mo.group(0)

            cleaned = _WECHAT_TAG_RE.sub(_repl, t2)
            cleaned = re.sub(r"\s+", " ", cleaned).strip() or t2
            if cleaned != t:
                changed += 1
                new_msgs.append(m.model_copy(update={"text": cleaned}))
            else:
                new_msgs.append(m)
            continue

        if t2 != t:
            changed += 1
            new_msgs.append(m.model_copy(update={"text": t2}))
        else:
            new_msgs.append(m)

    if changed or rows:
        log.info(
            "emoji_match reconcile changed=%s dropped=%s used_rows=%s/%s row_tags=%s",
            changed,
            dropped,
            qi,
            len(rows),
            [_tags_from_row(r) for r in rows],
        )
    return ctx.model_copy(update={"messages": new_msgs})
