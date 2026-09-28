"""填入目标窗口输入框；可选模拟回车发送（仅在用户点选某条候选时）。"""

from __future__ import annotations

import logging
import time
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Iterator

import win32api
import win32clipboard
import win32con
import win32gui

from joy.capture.windows import client_rect_screen, is_minimized

log = logging.getLogger("joy.fill")


@dataclass
class FillResult:
    ok: bool
    pasted: bool
    sent: bool
    detail: str = ""


def _read_clipboard_text() -> str | None:
    try:
        win32clipboard.OpenClipboard()
        try:
            if win32clipboard.IsClipboardFormatAvailable(win32con.CF_UNICODETEXT):
                data = win32clipboard.GetClipboardData(win32con.CF_UNICODETEXT)
                return data if isinstance(data, str) else None
        finally:
            win32clipboard.CloseClipboard()
    except Exception:
        return None
    return None


def _set_clipboard_text(text: str) -> None:
    win32clipboard.OpenClipboard()
    try:
        win32clipboard.EmptyClipboard()
        win32clipboard.SetClipboardData(win32con.CF_UNICODETEXT, text)
    finally:
        win32clipboard.CloseClipboard()


@contextmanager
def _temporary_clipboard(text: str) -> Iterator[None]:
    """写入剪贴板供 Ctrl+V，结束后尽量恢复原文。"""
    previous = _read_clipboard_text()
    _set_clipboard_text(text)
    try:
        yield
    finally:
        if previous is None:
            return
        try:
            time.sleep(0.05)
            _set_clipboard_text(previous)
        except Exception as exc:
            log.debug("restore clipboard failed: %s", exc)


def _key_combo(*vk_codes: int) -> None:
    for vk in vk_codes:
        win32api.keybd_event(vk, 0, 0, 0)
    for vk in reversed(vk_codes):
        win32api.keybd_event(vk, 0, win32con.KEYEVENTF_KEYUP, 0)


def _tap(vk: int) -> None:
    win32api.keybd_event(vk, 0, 0, 0)
    win32api.keybd_event(vk, 0, win32con.KEYEVENTF_KEYUP, 0)


def _click_screen(x: int, y: int) -> None:
    win32api.SetCursorPos((x, y))
    time.sleep(0.03)
    win32api.mouse_event(win32con.MOUSEEVENTF_LEFTDOWN, 0, 0, 0, 0)
    win32api.mouse_event(win32con.MOUSEEVENTF_LEFTUP, 0, 0, 0, 0)


def focus_window(hwnd: int) -> bool:
    try:
        if is_minimized(hwnd):
            win32gui.ShowWindow(hwnd, win32con.SW_RESTORE)
            time.sleep(0.15)
        win32gui.SetForegroundWindow(hwnd)
        time.sleep(0.12)
        return win32gui.GetForegroundWindow() == hwnd
    except Exception as exc:
        log.warning("focus failed: %s", exc)
        return False


def click_input_area(
    hwnd: int,
    *,
    x_ratio: float = 0.62,
    y_ratio: float = 0.80,
) -> bool:
    """点击聊天输入区（在工具栏上方），避免点到截图(Alt+A)等图标。"""
    rect = client_rect_screen(hwnd)
    if rect is None:
        return False
    left, top, right, bottom = rect
    w, h = right - left, bottom - top
    if w < 40 or h < 80:
        return False
    xr = max(0.35, min(0.90, float(x_ratio)))
    yr = max(0.55, min(0.88, float(y_ratio)))
    x = left + int(w * xr)
    y = top + int(h * yr)
    log.debug("click input at ratio=(%.2f,%.2f) screen=(%s,%s)", xr, yr, x, y)
    _click_screen(x, y)
    time.sleep(0.1)
    return True


def paste_text(
    hwnd: int,
    text: str,
    *,
    focus_input: bool = True,
    x_ratio: float = 0.62,
    y_ratio: float = 0.80,
) -> FillResult:
    if not text.strip():
        return FillResult(False, False, False, "空文本")
    if not win32gui.IsWindow(hwnd):
        return FillResult(False, False, False, "窗口无效")

    focused = focus_window(hwnd)
    clicked = True
    if focus_input:
        clicked = click_input_area(hwnd, x_ratio=x_ratio, y_ratio=y_ratio)
    if not focused and not clicked:
        return FillResult(False, False, False, "无法聚焦目标窗口，未粘贴")

    try:
        with _temporary_clipboard(text):
            _key_combo(win32con.VK_CONTROL, ord("V"))
            time.sleep(0.12)
    except Exception as exc:
        return FillResult(False, False, False, f"剪贴板/粘贴失败: {exc}")

    note = "已填入输入框"
    if not focused:
        note += "（窗口置前可能失败，请确认粘贴位置）"
    return FillResult(True, True, False, note)


def paste_and_send(
    hwnd: int,
    text: str,
    *,
    x_ratio: float = 0.62,
    y_ratio: float = 0.80,
) -> FillResult:
    """填入后按 Enter 发送。仅应在用户明确点选某条候选时调用。"""
    r = paste_text(hwnd, text, focus_input=True, x_ratio=x_ratio, y_ratio=y_ratio)
    if not r.pasted:
        return r
    time.sleep(0.08)
    _tap(win32con.VK_RETURN)
    time.sleep(0.05)
    return FillResult(True, True, True, "已填入并发送（模拟回车）")
