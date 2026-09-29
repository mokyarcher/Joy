"""微信窗口探测保护。"""

from __future__ import annotations

from joy.capture.windows import WindowInfo, has_wechat_window


def test_has_wechat_window_empty() -> None:
    assert has_wechat_window([]) is False


def test_has_wechat_window_filters_non_wechat() -> None:
    fake = [
        WindowInfo(
            hwnd=1,
            title="记事本",
            class_name="Notepad",
            pid=1,
            exe_name="notepad.exe",
        )
    ]
    assert has_wechat_window(fake) is False
