"""截帧编排：锁定用户所选 hwnd，不自动换窗；禁止采到遮挡层（如 Joy 自身）。"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from enum import Enum

from joy.capture.bitblt import bgr_to_rgb, capture_hwnd_printwindow
from joy.capture.frame_ops import (
    apply_crop,
    average_hash,
    content_fingerprint,
    hamming,
    is_mostly_black,
    resize_max_side,
    to_jpeg_bytes,
)
from joy.capture.screen_region import capture_hwnd_region
from joy.capture.windows import (
    WindowInfo,
    ensure_wechat_hwnd,
    is_minimized,
    is_window_unoccluded,
    restore_window,
    sync_window_title,
)
from joy.core.config import CaptureConfig

log = logging.getLogger("joy.capture")


class CaptureBackend(str, Enum):
    AUTO = "auto"
    PRINTWINDOW = "printwindow"
    REGION = "region"


@dataclass
class CapturedFrame:
    rgb: object  # np.ndarray
    hwnd: int
    title: str
    backend: str
    frame_hash: str
    fingerprint: str
    changed: bool
    jpeg: bytes | None = None


@dataclass
class GrabAttempt:
    frame: CapturedFrame | None
    window: WindowInfo | None
    reason: str = ""
    minimized: bool = False
    holding: bool = False  # 无新帧：最小化/遮挡等，应清空或冻结预览


class FrameGrabber:
    def __init__(self, config: CaptureConfig) -> None:
        self.config = config
        self.backend = CaptureBackend.AUTO
        self._last_hash: str | None = None
        self._preferred: CaptureBackend | None = None

    def reset_diff(self) -> None:
        self._last_hash = None

    def seed_hash(self, fhash: str | None) -> None:
        """把指定哈希记为差分基准（武装自动模式用）。"""
        self._last_hash = (fhash or "").strip() or None

    def grab(
        self,
        window: WindowInfo,
        *,
        want_jpeg: bool = False,
        force: bool = False,
        auto_restore: bool = False,
        allow_region: bool = True,
        crop: bool = True,
    ) -> CapturedFrame | None:
        return self.grab_with_reason(
            window,
            want_jpeg=want_jpeg,
            force=force,
            auto_restore=auto_restore,
            allow_region=allow_region,
            crop=crop,
        ).frame

    def grab_with_reason(
        self,
        window: WindowInfo,
        *,
        want_jpeg: bool = False,
        force: bool = False,
        auto_restore: bool = False,
        allow_region: bool = True,
        crop: bool = True,
        resize: bool = True,
    ) -> GrabAttempt:
        # 锁定用户选择的 hwnd，只同步标题，绝不换绑托盘窗
        live = sync_window_title(window)

        import win32gui

        if not win32gui.IsWindow(live.hwnd):
            return GrabAttempt(
                None,
                live,
                "窗口句柄已失效，请点「刷新」重新选择微信",
                holding=True,
            )

        bad = ensure_wechat_hwnd(live)
        if bad:
            return GrabAttempt(None, live, bad, holding=True)

        if is_minimized(live.hwnd):
            if auto_restore:
                restore_window(live.hwnd)
                time.sleep(0.25)
            if is_minimized(live.hwnd):
                return GrabAttempt(
                    None,
                    live,
                    f"已暂停采集：{live.title} 最小化中（点「还原窗口」后继续）",
                    minimized=True,
                    holding=True,
                )

        unoccluded = is_window_unoccluded(live.hwnd)
        rgb, backend = self._grab_raw(
            live.hwnd,
            allow_region=allow_region and unoccluded,
        )
        if rgb is None or getattr(rgb, "size", 0) == 0:
            if not unoccluded:
                return GrabAttempt(
                    None,
                    live,
                    "微信被其它窗口挡住（请勿叠在 Joy 预览上），把微信露出来或点「还原窗口」",
                    holding=True,
                )
            return GrabAttempt(None, live, f"截取失败：{live.title}", holding=True)

        # 区域截屏再校验一次：若此刻已被挡住，丢弃（防套娃）
        if backend.startswith("region") and not is_window_unoccluded(live.hwnd):
            return GrabAttempt(
                None,
                live,
                "区域截屏时窗口被遮挡，已丢弃（避免采到 Joy 自身）",
                holding=True,
            )

        if crop:
            rgb = apply_crop(rgb, self.config.crop)
            if getattr(rgb, "size", 0) == 0:
                return GrabAttempt(None, live, "裁剪后画面为空", holding=True)

        if resize:
            rgb = resize_max_side(rgb, self.config.max_image_side)
        fhash = average_hash(rgb)
        changed = True
        if not force and self._last_hash is not None:
            if hamming(self._last_hash, fhash) <= 2:
                changed = False
        self._last_hash = fhash

        jpeg = None
        if want_jpeg:
            jpeg = to_jpeg_bytes(rgb, self.config.jpeg_quality)

        frame = CapturedFrame(
            rgb=rgb,
            hwnd=live.hwnd,
            title=live.title,
            backend=backend,
            frame_hash=fhash,
            fingerprint=content_fingerprint(rgb),
            changed=changed,
            jpeg=jpeg,
        )
        return GrabAttempt(frame, live, "")

    def _grab_raw(
        self, hwnd: int, *, allow_region: bool
    ) -> tuple[object | None, str]:
        order = self._backend_order(allow_region=allow_region)
        black_fallback: tuple[object, str] | None = None
        for backend in order:
            if backend == CaptureBackend.PRINTWINDOW:
                bgr = capture_hwnd_printwindow(hwnd)
                if bgr is None:
                    continue
                rgb = bgr_to_rgb(bgr)
                if is_mostly_black(rgb):
                    # 不把全黑当成功；也不因此去采被挡住的屏幕区域
                    black_fallback = (rgb, "printwindow-black")
                    continue
                self._preferred = CaptureBackend.PRINTWINDOW
                return rgb, "printwindow"
            if backend == CaptureBackend.REGION:
                if not allow_region:
                    continue
                rgb = capture_hwnd_region(hwnd)
                if rgb is None:
                    continue
                if is_mostly_black(rgb):
                    black_fallback = (rgb, "region-black")
                    continue
                self._preferred = CaptureBackend.REGION
                return rgb, "region"
        # 全黑：仅在允许区域且 PrintWindow 也黑时，不再返回黑帧污染预览
        if black_fallback is not None and allow_region:
            return None, "black"
        if black_fallback is not None:
            return None, "black"
        return None, "none"

    def _backend_order(self, *, allow_region: bool) -> list[CaptureBackend]:
        if self.backend == CaptureBackend.PRINTWINDOW:
            return [CaptureBackend.PRINTWINDOW]
        if self.backend == CaptureBackend.REGION:
            return [CaptureBackend.REGION] if allow_region else [CaptureBackend.PRINTWINDOW]
        # 预览默认优先 PrintWindow；区域截屏仅在未遮挡时作为补充
        if self._preferred == CaptureBackend.REGION and allow_region:
            return [CaptureBackend.REGION, CaptureBackend.PRINTWINDOW]
        if allow_region:
            return [CaptureBackend.PRINTWINDOW, CaptureBackend.REGION]
        return [CaptureBackend.PRINTWINDOW]
