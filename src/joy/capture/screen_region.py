"""屏幕区域截取（mss）：窗口可见时可靠；被遮挡时截到的是遮挡内容。"""

from __future__ import annotations

import numpy as np

from joy.capture.windows import client_rect_screen


def capture_hwnd_region(hwnd: int) -> np.ndarray | None:
    """返回 RGB uint8。"""
    rect = client_rect_screen(hwnd)
    if rect is None:
        return None
    left, top, right, bottom = rect
    w, h = right - left, bottom - top
    if w <= 1 or h <= 1:
        return None

    import mss

    with mss.mss() as sct:
        monitor = {"left": left, "top": top, "width": w, "height": h}
        shot = sct.grab(monitor)
        # BGRA
        arr = np.asarray(shot, dtype=np.uint8)
        rgb = np.ascontiguousarray(arr[:, :, [2, 1, 0]], dtype=np.uint8)
        return rgb
