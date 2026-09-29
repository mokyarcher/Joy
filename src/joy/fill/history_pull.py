"""从微信「多选 → 选择到这里 → 复制」拉取聊天文本到剪贴板。

策略：带字按钮优先本地 OCR 定位（免 429）；气泡可用视觉。
关键步骤定位失败则中止，不再用易点到「收藏」的瞎偏移。
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING, Callable

import numpy as np
import win32api
import win32con
import win32gui

from joy.capture.windows import client_rect_screen
from joy.fill.wechat import (
    _click_screen,
    _read_clipboard_text,
    _set_clipboard_text,
    _tap,
    focus_window,
)

if TYPE_CHECKING:
    from joy.core.config import JoyConfig

log = logging.getLogger("joy.fill.history")

ProgressCb = Callable[[str], None]


@dataclass
class PullHistoryResult:
    ok: bool
    text: str = ""
    detail: str = ""


def _right_click_screen(x: int, y: int) -> None:
    win32api.SetCursorPos((x, y))
    time.sleep(0.04)
    win32api.mouse_event(win32con.MOUSEEVENTF_RIGHTDOWN, 0, 0, 0, 0)
    win32api.mouse_event(win32con.MOUSEEVENTF_RIGHTUP, 0, 0, 0, 0)


def _wheel_up(steps: int = 6, *, delta: int = 120) -> None:
    for _ in range(max(1, steps)):
        win32api.mouse_event(win32con.MOUSEEVENTF_WHEEL, 0, 0, int(delta), 0)
        time.sleep(0.08)


def _wheel_down(steps: int = 6, *, delta: int = 120) -> None:
    for _ in range(max(1, steps)):
        win32api.mouse_event(win32con.MOUSEEVENTF_WHEEL, 0, 0, -int(delta), 0)
        time.sleep(0.05)


def _scroll_chat_to_latest(
    hwnd: int,
    *,
    msg_left: float,
    scroll_steps: int,
    on_progress: ProgressCb | None = None,
) -> None:
    """取样上滑后滚回会话底部（最新消息），避免用户还要自己滑回去。"""

    def _p(msg: str) -> None:
        if on_progress:
            on_progress(msg)
        log.info("pull: %s", msg)

    # 多滚几步，确保过冲到最底（多滚无害）
    steps = max(12, int(scroll_steps) + 16)
    mid = _client_xy(hwnd, msg_left + (1.0 - msg_left) * 0.45, 0.50)
    if mid:
        focus_window(hwnd)
        time.sleep(0.06)
        _click_screen(*mid)
        time.sleep(0.10)
    _p(f"滚回最新消息（下滑 {steps} 步）…")
    _wheel_down(steps, delta=240)
    time.sleep(0.20)


def _client_xy(hwnd: int, xr: float, yr: float) -> tuple[int, int] | None:
    rect = client_rect_screen(hwnd)
    if rect is None:
        return None
    left, top, right, bottom = rect
    w, h = right - left, bottom - top
    if w < 80 or h < 120:
        return None
    x = left + int(w * max(0.0, min(1.0, xr)))
    y = top + int(h * max(0.0, min(1.0, yr)))
    return x, y


def _grab_rgb(hwnd: int, cfg: JoyConfig, *, screen: bool = False) -> np.ndarray | None:
    """
    screen=True：mss 区域截屏（能采到右键弹出菜单；PrintWindow 采不到独立 popup）。
    screen=False：走 grabber（默认 PrintWindow，适合无菜单的气泡定位）。
    """
    try:
        if screen:
            from joy.capture.screen_region import capture_hwnd_region

            rgb = capture_hwnd_region(int(hwnd))
            if rgb is None or getattr(rgb, "size", 0) == 0:
                return None
            return np.asarray(rgb)
        from joy.capture.grabber import FrameGrabber
        from joy.capture.windows import WindowInfo

        win = WindowInfo(
            hwnd=int(hwnd),
            title=win32gui.GetWindowText(hwnd) or "",
            class_name=win32gui.GetClassName(hwnd) or "",
            pid=0,
        )
        grabber = FrameGrabber(cfg.capture)
        attempt = grabber.grab_with_reason(
            win,
            want_jpeg=False,
            force=True,
            auto_restore=False,
            crop=False,
            resize=False,
        )
        frame = attempt.frame
        if frame is None or frame.rgb is None:
            return None
        return np.asarray(frame.rgb)
    except Exception as exc:
        log.warning("pull grab rgb failed screen=%s: %s", screen, exc)
        return None


def _grab_jpeg(hwnd: int, cfg: JoyConfig, *, screen: bool = False) -> bytes | None:
    """同步定位专用：整窗不裁切；菜单步骤必须 screen=True。"""
    try:
        from joy.capture.frame_ops import to_jpeg_bytes

        q = max(80, int(getattr(cfg.capture, "jpeg_quality", 72) or 72))
        if screen:
            from joy.capture.screen_region import capture_hwnd_region

            rgb = capture_hwnd_region(int(hwnd))
            if rgb is None or getattr(rgb, "size", 0) == 0:
                return None
            return to_jpeg_bytes(np.asarray(rgb), q)

        from joy.capture.grabber import FrameGrabber
        from joy.capture.windows import WindowInfo

        win = WindowInfo(
            hwnd=int(hwnd),
            title=win32gui.GetWindowText(hwnd) or "",
            class_name=win32gui.GetClassName(hwnd) or "",
            pid=0,
        )
        grabber = FrameGrabber(cfg.capture)
        attempt = grabber.grab_with_reason(
            win,
            want_jpeg=True,
            force=True,
            auto_restore=False,
            crop=False,
            resize=False,
        )
        frame = attempt.frame
        if frame is None:
            return None
        if frame.jpeg:
            return frame.jpeg
        return to_jpeg_bytes(frame.rgb, q)
    except Exception as exc:
        log.warning("pull grab jpeg failed screen=%s: %s", screen, exc)
        return None


def _ocr_label(
    hwnd: int,
    cfg: JoyConfig,
    needles: list[str],
    *,
    prefer_lower: bool = False,
    click_xy: tuple[int, int] | None = None,
    screen: bool = True,
) -> tuple[float, float] | None:
    from joy.fill.ocr_click import find_text_ratio

    rgb = _grab_rgb(hwnd, cfg, screen=screen)
    if rgb is None:
        return None
    prefer_right = None
    if click_xy is not None:
        rect = client_rect_screen(hwnd)
        if rect is not None:
            left, top, _, _ = rect
            prefer_right = float(click_xy[0] - left)
    hit = find_text_ratio(
        rgb,
        needles,
        prefer_lower=prefer_lower,
        prefer_right_of=prefer_right,
    )
    if hit is None:
        return None
    return hit[0], hit[1]


def _ocr_multiselect(
    hwnd: int,
    cfg: JoyConfig,
    *,
    click_xy: tuple[int, int] | None = None,
) -> tuple[float, float] | None:
    from joy.fill.ocr_click import find_context_multiselect

    # 右键菜单是独立 HWND，必须用屏幕区域截取
    rgb = _grab_rgb(hwnd, cfg, screen=True)
    if rgb is None:
        return None
    prefer_right = None
    if click_xy is not None:
        rect = client_rect_screen(hwnd)
        if rect is not None:
            left, top, _, _ = rect
            prefer_right = float(click_xy[0] - left)
    hit = find_context_multiselect(rgb, prefer_right_of=prefer_right)
    if hit is None:
        return None
    log.info("pull ocr multiselect via %s ratio=(%.3f,%.3f)", hit[2], hit[0], hit[1])
    return hit[0], hit[1]


def _vision_label(
    cfg: JoyConfig,
    hwnd: int,
    name: str,
    *,
    hint: str = "",
    screen: bool = False,
) -> tuple[float, float] | None:
    from joy.providers.ui_click_probe import probe_ui_click_points

    jpeg = _grab_jpeg(hwnd, cfg, screen=screen)
    if not jpeg:
        return None
    try:
        pts = probe_ui_click_points(cfg, jpeg, [name], hint=hint)
        return pts.get(name)
    except Exception as exc:
        log.info("pull vision %s failed: %s", name, exc)
        return None


def _pixel_nth_bubble(
    hwnd: int,
    cfg: JoyConfig,
    *,
    n: int,
    contact_left_ratio: float,
    compose_top_ratio: float,
) -> tuple[float, float] | None:
    """屏幕/窗口像素找第 n 条气泡中心（从下往上，无偏移估算）。"""
    from joy.fill.bubble_locate import locate_nth_from_bottom

    # PrintWindow 即可（无菜单）；失败再试屏幕
    rgb = _grab_rgb(hwnd, cfg, screen=False)
    if rgb is None:
        rgb = _grab_rgb(hwnd, cfg, screen=True)
    if rgb is None:
        return None
    return locate_nth_from_bottom(
        rgb,
        n,
        contact_left_ratio=contact_left_ratio,
        compose_top_ratio=compose_top_ratio,
    )


def _click_ratio(hwnd: int, xr: float, yr: float, *, right: bool = False) -> bool:
    pt = _client_xy(hwnd, xr, yr)
    if pt is None:
        return False
    if right:
        _right_click_screen(*pt)
    else:
        _click_screen(*pt)
    return True


def _dismiss_multiselect(hwnd: int, *, msg_left: float) -> None:
    """退出多选：点消息区空白。勿用 Esc——微信 Esc 常会关掉当前会话窗。"""
    mid = _client_xy(hwnd, msg_left + (1.0 - msg_left) * 0.55, 0.42)
    if mid:
        _click_screen(*mid)
        time.sleep(0.15)


def _locate(
    hwnd: int,
    cfg: JoyConfig | None,
    *,
    label: str,
    needles: list[str],
    prefer_lower: bool = False,
    click_xy: tuple[int, int] | None = None,
    use_vision: bool = True,
    vision_hint: str = "",
) -> tuple[float, float] | None:
    """OCR 优先；可选视觉兜底。绝不返回瞎猜偏移。"""
    if cfg is not None:
        pt = _ocr_label(
            hwnd,
            cfg,
            needles,
            prefer_lower=prefer_lower,
            click_xy=click_xy,
        )
        if pt is not None:
            return pt
        if use_vision:
            time.sleep(0.8)  # 错开 contact identify，减轻 429
            return _vision_label(cfg, hwnd, label, hint=vision_hint)
    return None


def _open_menu_and_find_multiselect(
    hwnd: int,
    cfg: JoyConfig,
    *,
    bx_r: float,
    by_r: float,
    on_progress: ProgressCb | None,
) -> tuple[float, float] | None:
    """右键打开菜单并 OCR 找「多选」。"""

    def _p(msg: str) -> None:
        if on_progress:
            on_progress(msg)
        log.info("pull: %s", msg)

    attempts: list[tuple[float, float, str]] = [(bx_r, by_r, "倒数第二条")]
    # 仍偏下时再略上移一次
    if by_r > 0.62:
        up = max(0.35, by_r - 0.12)
        if abs(up - by_r) > 0.04:
            attempts.append((bx_r, up, "再上移"))

    for xr, yr, tag in attempts:
        focus_window(hwnd)
        time.sleep(0.08)
        _p(f"右键菜单 ({xr:.2f},{yr:.2f}) · {tag}")
        click_pt = _client_xy(hwnd, xr, yr)
        if not _click_ratio(hwnd, xr, yr, right=True):
            continue
        time.sleep(0.65)
        _p("OCR 定位：多选（屏幕截屏）…")
        multi = _ocr_multiselect(hwnd, cfg, click_xy=click_pt)
        if multi is not None:
            return multi
        if tag == "倒数第二条":
            time.sleep(0.35)
            vis = _vision_label(
                cfg,
                hwnd,
                "多选",
                hint="右键菜单里「多选」，在「收藏」正下方；不要点收藏",
                screen=True,
            )
            if vis is not None:
                return vis
        _tap(win32con.VK_ESCAPE)
        time.sleep(0.25)

    return None


def pull_chat_via_multiselect(
    hwnd: int,
    *,
    cfg: JoyConfig | None = None,
    compose_top_ratio: float = 0.78,
    contact_left_ratio: float = 0.30,
    scroll_steps: int = 24,
    me_names: str | None = None,
    on_progress: ProgressCb | None = None,
) -> PullHistoryResult:
    del me_names

    def _p(msg: str) -> None:
        if on_progress:
            on_progress(msg)
        log.info("pull: %s", msg)

    if not win32gui.IsWindow(hwnd):
        return PullHistoryResult(False, detail="窗口无效")
    if cfg is None:
        return PullHistoryResult(False, detail="缺少配置，无法定位")

    if not focus_window(hwnd):
        log.warning("pull: focus weak, continue anyway")

    marker = f"__JOY_PULL_MARK_{time.time_ns()}__"
    try:
        _set_clipboard_text(marker)
    except Exception as exc:
        return PullHistoryResult(False, detail=f"无法写入剪贴板: {exc}")

    compose = max(0.55, min(0.92, float(compose_top_ratio)))
    msg_left = max(0.28, min(0.45, float(contact_left_ratio)))

    # —— 1) 像素定位倒数第二条气泡（色块中心，不估偏移）——
    _p("像素定位倒数第二条消息…")
    bubble = _pixel_nth_bubble(
        hwnd,
        cfg,
        n=2,
        contact_left_ratio=msg_left,
        compose_top_ratio=compose,
    )
    if bubble is None:
        # 仅一条可见时退化为最后一条（仍是色块中心）
        bubble = _pixel_nth_bubble(
            hwnd,
            cfg,
            n=1,
            contact_left_ratio=msg_left,
            compose_top_ratio=compose,
        )
        if bubble is not None:
            _p(f"仅找到一条气泡，用最后一条 ({bubble[0]:.2f},{bubble[1]:.2f})")
        else:
            return PullHistoryResult(
                False,
                detail="像素未找到聊天气泡，请保证会话区可见后再试",
            )
    else:
        _p(f"倒数第二条气泡 ({bubble[0]:.2f},{bubble[1]:.2f})")
    bx_r, by_r = bubble

    # —— 2) 右键 + OCR「多选」（贴底会长菜单；可上移重开）——
    multi = _open_menu_and_find_multiselect(
        hwnd,
        cfg,
        bx_r=bx_r,
        by_r=by_r,
        on_progress=on_progress,
    )
    if multi is None:
        _tap(win32con.VK_ESCAPE)  # 仅关右键菜单
        return PullHistoryResult(
            False,
            detail="未找到菜单「多选」（长菜单可贴底；已尝试上移右键仍失败）",
        )
    _p(f"点击多选 ({multi[0]:.2f},{multi[1]:.2f})")
    _click_ratio(hwnd, multi[0], multi[1])
    time.sleep(0.55)

    # —— 3) 上滑 ——
    mid = _client_xy(hwnd, msg_left + (1.0 - msg_left) * 0.45, 0.45)
    if mid:
        _click_screen(*mid)
        time.sleep(0.12)
    _p(f"上滑 {scroll_steps} 步…")
    _wheel_up(int(scroll_steps), delta=180)
    time.sleep(0.45)
    scrolled_up = True

    try:
        # —— 4) 「选择到这里」——
        _p("OCR 定位：选择到这里…")
        sel = _locate(
            hwnd,
            cfg,
            label="选择到这里",
            needles=["选择到这里", "选到这里"],
            prefer_lower=False,
            use_vision=True,
            vision_hint="多选模式下的「↓ 选择到这里」按钮",
        )
        if sel is None:
            _dismiss_multiselect(hwnd, msg_left=msg_left)
            return PullHistoryResult(
                False, detail="未找到「选择到这里」，请上滑后再试或改用导入"
            )
        _p(f"点击选择到这里 ({sel[0]:.2f},{sel[1]:.2f})")
        _click_ratio(hwnd, sel[0], sel[1])
        time.sleep(0.50)

        # —— 5) 「复制」——
        _p("OCR 定位：复制…")
        copy_pt = _locate(
            hwnd,
            cfg,
            label="复制",
            needles=["复制"],
            prefer_lower=True,
            use_vision=True,
            vision_hint="多选底栏的「复制」，不要点删除",
        )
        if copy_pt is None:
            _dismiss_multiselect(hwnd, msg_left=msg_left)
            return PullHistoryResult(False, detail="未找到底栏「复制」")
        # 安全：复制不应太靠右下（那里常是删除）
        if copy_pt[0] > 0.82:
            _dismiss_multiselect(hwnd, msg_left=msg_left)
            return PullHistoryResult(
                False, detail="「复制」定位过右，疑似点到删除，已中止"
            )
        _p(f"点击复制 ({copy_pt[0]:.2f},{copy_pt[1]:.2f})")
        _click_ratio(hwnd, copy_pt[0], copy_pt[1])
        time.sleep(0.40)

        text = (_read_clipboard_text() or "").strip()
        # 复制后多选往往已自动退出；切勿 Esc（会关掉当前聊天窗）
        _dismiss_multiselect(hwnd, msg_left=msg_left)

        if not text or text == marker or text.startswith("__JOY_PULL_MARK_"):
            return PullHistoryResult(
                False,
                detail="剪贴板未拿到聊天内容，请改用「导入上下文」",
            )
        if len(text) < 4:
            return PullHistoryResult(False, detail="剪贴板内容过短")

        log.info("pull: clipboard ok chars=%s", len(text))
        return PullHistoryResult(True, text=text, detail=f"已复制 {len(text)} 字")
    finally:
        # 无论成败：只要上滑过，就帮用户滚回最新消息
        if scrolled_up:
            try:
                _scroll_chat_to_latest(
                    hwnd,
                    msg_left=msg_left,
                    scroll_steps=int(scroll_steps),
                    on_progress=on_progress,
                )
            except Exception:
                log.exception("pull: scroll back to latest failed")
