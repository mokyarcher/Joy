"""截屏采集（M1）：窗口枚举 + PrintWindow/区域截取 + 帧差分。"""

from joy.capture.grabber import CapturedFrame, FrameGrabber
from joy.capture.windows import (
    WindowInfo,
    find_wechat_windows,
    list_top_windows,
    restore_window,
)

__all__ = [
    "CapturedFrame",
    "FrameGrabber",
    "WindowInfo",
    "find_wechat_windows",
    "list_top_windows",
    "restore_window",
]
