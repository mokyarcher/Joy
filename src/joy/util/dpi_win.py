"""Windows DPI：打包 exe 与 python.exe 清单不同，需在创建 QApplication 前声明感知。"""

from __future__ import annotations

import logging
import sys

log = logging.getLogger("joy.dpi")


def enable_dpi_awareness() -> str:
    """尽量启用 Per-Monitor V2；返回实际采用的策略名（供日志）。"""
    if sys.platform != "win32":
        return "n/a"
    try:
        import ctypes

        # DPI_AWARENESS_CONTEXT_PER_MONITOR_AWARE_V2 = -4
        set_ctx = getattr(ctypes.windll.user32, "SetProcessDpiAwarenessContext", None)
        if callable(set_ctx):
            if set_ctx(ctypes.c_void_p(-4)):
                return "PerMonitorV2"
            # 可能已设置；再查当前值
        try:
            awareness = ctypes.c_int()
            if ctypes.windll.shcore.GetProcessDpiAwareness(  # type: ignore[attr-defined]
                0, ctypes.byref(awareness)
            ) == 0 and int(awareness.value) >= 2:
                return f"already:{int(awareness.value)}"
        except Exception:
            pass
        # PROCESS_PER_MONITOR_DPI_AWARE = 2
        try:
            if ctypes.windll.shcore.SetProcessDpiAwareness(2) == 0:  # type: ignore[attr-defined]
                return "PerMonitor"
        except Exception:
            pass
        if ctypes.windll.user32.SetProcessDPIAware():
            return "SystemAware"
    except Exception as exc:
        log.debug("enable_dpi_awareness failed: %s", exc)
        return "failed"
    return "unknown"


def process_dpi_awareness_level() -> int | None:
    """0=unaware 1=system 2=per-monitor；读失败返回 None。"""
    if sys.platform != "win32":
        return None
    try:
        import ctypes

        awareness = ctypes.c_int()
        if (
            ctypes.windll.shcore.GetProcessDpiAwareness(0, ctypes.byref(awareness))
            == 0
        ):
            return int(awareness.value)
    except Exception:
        pass
    return None


def resolve_dock_anchor(
    *,
    wr: tuple[int, int, int, int],
    dpr: float,
    screen_logical_h: int | None = None,
) -> tuple[tuple[int, int, int, int], float, str]:
    """校正微信外框并给出贴边 scale。

    返回 (rect, scale, reason)。rect 与 scale 配套：logical = rect_size / scale。
    """
    dpr = max(1.0, float(dpr or 1.0))
    left, top, right, bottom = (int(x) for x in wr)
    w = max(1, right - left)
    h = max(1, bottom - top)
    screen_h = int(screen_logical_h or 0) or 900

    # 半高读数（日志 wechat_h=380、屏却很高）：优先抬一倍
    if dpr >= 1.5 and h < max(420, int(screen_h * 0.50)):
        nw = int(round(w * dpr))
        nh = int(round(h * dpr))
        if 360 <= nh <= int(screen_h * 1.08):
            return (left, top, left + nw, top + nh), 1.0, "inflate"

    cands: list[tuple[float, float, str, tuple[int, int, int, int]]] = [
        (h / dpr, dpr, "physical", (left, top, right, bottom)),
        (float(h), 1.0, "logical", (left, top, right, bottom)),
    ]

    def _score(logical_h: float) -> float:
        if logical_h < 300:
            return -1e9
        if logical_h > screen_h * 1.08:
            return -1e9
        ratio = logical_h / float(screen_h)
        score = -abs(ratio - 0.72)
        # 主聊天窗很少低于屏高 45% / 420px；短条（如 380）大概率是半尺寸误读
        if logical_h < max(420, int(screen_h * 0.45)):
            score -= 0.55
        return score

    _lh, scale, why, rect = max(cands, key=lambda t: _score(t[0]))
    return rect, float(scale), why


def dock_coord_scale(
    *,
    win32_h: int,
    dpr: float,
    screen_logical_h: int | None = None,
) -> float:
    """兼容旧调用：只返回 scale。"""
    dummy = (0, 0, 100, int(win32_h))
    _rect, scale, _why = resolve_dock_anchor(
        wr=dummy, dpr=dpr, screen_logical_h=screen_logical_h
    )
    return scale
