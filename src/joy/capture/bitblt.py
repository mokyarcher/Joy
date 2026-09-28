"""窗口位图截取：PrintWindow（含 PW_RENDERFULLCONTENT）+ BitBlt 回退。"""

from __future__ import annotations

import ctypes
from ctypes import wintypes

import numpy as np
import win32gui
import win32ui

PW_RENDERFULLCONTENT = 0x00000002
_user32 = ctypes.windll.user32
_user32.PrintWindow.argtypes = [wintypes.HWND, wintypes.HDC, wintypes.UINT]
_user32.PrintWindow.restype = wintypes.BOOL


def capture_hwnd_printwindow(hwnd: int) -> np.ndarray | None:
    """返回 BGR uint8；失败返回 None。"""
    try:
        left, top, right, bottom = win32gui.GetClientRect(hwnd)
        w, h = right - left, bottom - top
        if w <= 1 or h <= 1:
            return None

        hwnd_dc = win32gui.GetWindowDC(hwnd)
        mfc_dc = win32ui.CreateDCFromHandle(hwnd_dc)
        save_dc = mfc_dc.CreateCompatibleDC()
        bitmap = win32ui.CreateBitmap()
        bitmap.CreateCompatibleBitmap(mfc_dc, w, h)
        save_dc.SelectObject(bitmap)

        ok = _user32.PrintWindow(int(hwnd), save_dc.GetSafeHdc(), PW_RENDERFULLCONTENT)
        if not ok:
            ok = _user32.PrintWindow(int(hwnd), save_dc.GetSafeHdc(), 0)
        if not ok:
            win32gui.DeleteObject(bitmap.GetHandle())
            save_dc.DeleteDC()
            mfc_dc.DeleteDC()
            win32gui.ReleaseDC(hwnd, hwnd_dc)
            return None

        bmpinfo = bitmap.GetInfo()
        bmpstr = bitmap.GetBitmapBits(True)
        img = np.frombuffer(bmpstr, dtype=np.uint8)
        img.shape = (bmpinfo["bmHeight"], bmpinfo["bmWidth"], 4)
        bgr = np.ascontiguousarray(img[:, :, :3])

        win32gui.DeleteObject(bitmap.GetHandle())
        save_dc.DeleteDC()
        mfc_dc.DeleteDC()
        win32gui.ReleaseDC(hwnd, hwnd_dc)
        return bgr
    except Exception:
        return None


def bgr_to_rgb(bgr: np.ndarray) -> np.ndarray:
    return np.ascontiguousarray(bgr[:, :, ::-1], dtype=np.uint8)
