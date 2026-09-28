"""帧处理：裁剪、哈希、压缩；默认不落盘。"""

from __future__ import annotations

import hashlib
import io

import numpy as np
from PIL import Image

from joy.core.config import CropRatio


def bgr_to_rgb(frame: np.ndarray) -> np.ndarray:
    if frame.ndim != 3 or frame.shape[2] < 3:
        return frame
    if frame.shape[2] == 4:
        return frame[:, :, [2, 1, 0, 3]]
    return frame[:, :, ::-1]


def ensure_rgb(frame: np.ndarray) -> np.ndarray:
    """输入 BGRA/BGR/RGB，输出 RGB uint8。"""
    if frame.ndim != 3:
        raise ValueError("frame must be HxWxC")
    arr = frame
    if arr.shape[2] == 4:
        # 假定 BGRA（win32/mss 常见）
        arr = arr[:, :, :3][:, :, ::-1]
    elif arr.shape[2] == 3:
        # 调用方应标明；默认当 BGR
        arr = arr[:, :, ::-1]
    return np.ascontiguousarray(arr, dtype=np.uint8)


def mss_bgra_to_rgb(frame: np.ndarray) -> np.ndarray:
    if frame.shape[2] == 4:
        return np.ascontiguousarray(frame[:, :, [2, 1, 0]], dtype=np.uint8)
    return ensure_rgb(frame)


def apply_crop(rgb: np.ndarray, crop: CropRatio) -> np.ndarray:
    h, w = rgb.shape[:2]
    left = int(w * max(0.0, min(crop.left, 0.45)))
    top = int(h * max(0.0, min(crop.top, 0.45)))
    right = int(w * max(0.0, min(crop.right, 0.45)))
    bottom = int(h * max(0.0, min(crop.bottom, 0.45)))
    x1, x2 = left, max(left + 1, w - right)
    y1, y2 = top, max(top + 1, h - bottom)
    return rgb[y1:y2, x1:x2]


def resize_max_side(rgb: np.ndarray, max_side: int) -> np.ndarray:
    if max_side <= 0:
        return rgb
    h, w = rgb.shape[:2]
    m = max(h, w)
    if m <= max_side:
        return rgb
    scale = max_side / m
    nh, nw = max(1, int(h * scale)), max(1, int(w * scale))
    img = Image.fromarray(rgb)
    img = img.resize((nw, nh), Image.Resampling.BILINEAR)
    return np.asarray(img, dtype=np.uint8)


def average_hash(rgb: np.ndarray, hash_size: int = 8) -> str:
    """简易 aHash，用于帧差分。"""
    img = Image.fromarray(rgb).convert("L").resize(
        (hash_size, hash_size), Image.Resampling.BILINEAR
    )
    pixels = np.asarray(img, dtype=np.float32)
    avg = pixels.mean()
    bits = (pixels > avg).astype(np.uint8).flatten()
    # 转 hex
    value = 0
    for b in bits:
        value = (value << 1) | int(b)
    width = (hash_size * hash_size + 3) // 4
    return f"{value:0{width}x}"


def hamming(a: str, b: str) -> int:
    if not a or not b or len(a) != len(b):
        return 64
    return int(bin(int(a, 16) ^ int(b, 16)).count("1"))


def is_mostly_black(rgb: np.ndarray, threshold: float = 8.0) -> bool:
    if rgb.size == 0:
        return True
    return float(np.mean(rgb)) < threshold


def to_jpeg_bytes(rgb: np.ndarray, quality: int = 85) -> bytes:
    img = Image.fromarray(rgb)
    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=int(quality), optimize=True)
    return buf.getvalue()


def crop_chat_header_jpeg(
    rgb: np.ndarray,
    *,
    quality: int = 70,
    left_ratio: float = 0.28,
    bottom_ratio: float = 0.12,
    right_ratio: float = 0.90,
    pad_left_px: int = 12,
    scale: float = 1.0,
) -> bytes:
    """裁右栏顶条（会话名区域），供轻量识名，避免整窗大图。"""
    strip = crop_chat_header_rgb(
        rgb,
        left_ratio=left_ratio,
        bottom_ratio=bottom_ratio,
        right_ratio=right_ratio,
        pad_left_px=pad_left_px,
    )
    if strip is None or strip.size == 0:
        return b""
    if scale and scale > 1.01:
        try:
            from PIL import Image

            h, w = int(strip.shape[0]), int(strip.shape[1])
            img = Image.fromarray(strip)
            img = img.resize(
                (max(1, int(w * scale)), max(1, int(h * scale))),
                Image.Resampling.LANCZOS,
            )
            strip = np.asarray(img)
        except Exception:
            pass
    return to_jpeg_bytes(np.ascontiguousarray(strip), quality)


def crop_chat_header_rgb(
    rgb: np.ndarray,
    *,
    left_ratio: float = 0.28,
    bottom_ratio: float = 0.10,
    right_ratio: float = 0.90,
    pad_left_px: int = 12,
) -> np.ndarray | None:
    """右栏顶条 RGB，供指纹 / 识名共用。

    默认比旧 0.35 更靠左，并向左多留 pad，避免英文首字母（L/m）被裁掉。
    右侧裁掉窗控图标，减少干扰。
    """
    if rgb is None or getattr(rgb, "size", 0) == 0 or rgb.ndim != 3:
        return None
    h, w = int(rgb.shape[0]), int(rgb.shape[1])
    x0 = int(w * max(0.0, min(0.55, left_ratio))) - max(0, int(pad_left_px))
    x0 = max(0, x0)
    x1 = int(w * max(0.55, min(1.0, right_ratio)))
    if x1 <= x0 + 8:
        x1 = w
    y1 = max(8, int(h * max(0.04, min(0.25, bottom_ratio))))
    strip = rgb[0:y1, x0:x1]
    if strip.size == 0:
        return None
    return np.ascontiguousarray(strip)


def header_fingerprint(rgb: np.ndarray) -> str:
    """会话顶栏指纹：切换聊天时会变；小抖动（未读点）通常不变。"""
    strip = crop_chat_header_rgb(rgb)
    if strip is None:
        return ""
    return average_hash(strip)


def header_fp_changed(prev: str, cur: str, *, max_hamming: int = 8) -> bool:
    """顶栏是否实质变化。aHash 允许少量位差，避免预览/另截抖动误触发识名。"""
    a = (prev or "").strip()
    b = (cur or "").strip()
    if not b:
        return False
    if not a:
        return True
    if a == b:
        return False
    try:
        x = int(a, 16) ^ int(b, 16)
    except ValueError:
        return a != b
    return int(x.bit_count()) > int(max_hamming)


def content_fingerprint(rgb: np.ndarray) -> str:
    """内容指纹：aHash + 尺寸 + 中心区均值，降低不同对话误碰撞。"""
    h = average_hash(rgb)
    hh, ww = int(rgb.shape[0]), int(rgb.shape[1])
    y0, y1 = hh // 4, hh * 3 // 4
    x0, x1 = ww // 4, ww * 3 // 4
    center = rgb[y0:y1, x0:x1]
    mean = int(center.mean()) if center.size else 0
    raw = f"{h}:{ww}x{hh}:{mean}"
    return hashlib.sha1(raw.encode("ascii")).hexdigest()[:16]
