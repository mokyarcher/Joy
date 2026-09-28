"""采集工作线程：锁定所选窗口，最小化时降频等待。"""

from __future__ import annotations

import logging
import time

from PySide6.QtCore import QMutex, QMutexLocker, QThread, Signal

from joy.capture.grabber import FrameGrabber
from joy.capture.windows import WindowInfo
from joy.core.config import CaptureConfig

log = logging.getLogger("joy.capture.worker")


class CaptureWorker(QThread):
    frame_ready = Signal(object)
    status = Signal(str)
    error = Signal(str)

    def __init__(self, config: CaptureConfig, parent=None) -> None:
        super().__init__(parent)
        self._config = config
        self._grabber = FrameGrabber(config)
        self._mutex = QMutex()
        self._window: WindowInfo | None = None
        self._paused = bool(config.paused)
        self._running = False
        self._seed_baseline = False

    def set_window(self, window: WindowInfo | None) -> None:
        with QMutexLocker(self._mutex):
            self._window = window
            self._grabber.reset_diff()

    def set_paused(self, paused: bool) -> None:
        with QMutexLocker(self._mutex):
            self._paused = paused

    def request_baseline_seed(self) -> None:
        """下一帧强制截取并记为差分基准，且不标 changed（开自动/进副驾用）。"""
        with QMutexLocker(self._mutex):
            self._seed_baseline = True

    def update_config(self, config: CaptureConfig) -> None:
        with QMutexLocker(self._mutex):
            self._config = config
            self._grabber.config = config

    def stop(self) -> None:
        self._running = False
        self.wait(3000)

    def run(self) -> None:
        self._running = True
        self.status.emit("采集线程已启动（锁定所选窗口）")
        while self._running:
            started = time.perf_counter()
            with QMutexLocker(self._mutex):
                window = self._window
                paused = self._paused
                interval_ms = max(300, int(self._config.poll_interval_ms))
                seed = bool(self._seed_baseline)
                if seed:
                    self._seed_baseline = False

            if paused:
                self.status.emit("已暂停")
                self.msleep(interval_ms)
                continue
            if window is None:
                self.status.emit("未选择窗口")
                self.msleep(interval_ms)
                continue

            try:
                # 连续预览：不自动还原；区域截屏仅在未遮挡时启用（防采到 Joy 自身）
                # seed：强制截一帧写入哈希基准，不把「相对空基准」的首帧当变化
                attempt = self._grabber.grab_with_reason(
                    window,
                    want_jpeg=False,
                    force=bool(seed),
                    auto_restore=False,
                    allow_region=True,
                    # 预览用原分辨率，避免先缩到 960 再放大导致全屏发糊
                    resize=False,
                )
            except Exception as exc:
                log.exception("grab failed")
                self.error.emit(str(exc))
                if seed:
                    with QMutexLocker(self._mutex):
                        self._seed_baseline = True
                self.msleep(interval_ms)
                continue

            frame = attempt.frame
            if frame is None:
                self.status.emit(attempt.reason or f"截取失败：{window.title}")
                if seed:
                    with QMutexLocker(self._mutex):
                        self._seed_baseline = True
                if attempt.holding:
                    self.frame_ready.emit(None)
                wait = 2000 if attempt.minimized or attempt.holding else interval_ms
                self.msleep(wait)
                continue

            if seed:
                # 基准帧：哈希已写入 grabber；对外不触发自动分析
                frame.changed = False
                log.info(
                    "capture baseline seeded hash=%s…",
                    (frame.frame_hash or "")[:12],
                )

            # 未变化时不必刷 UI 状态太勤，但仍推送帧保持预览活着
            self.frame_ready.emit(frame)

            elapsed = (time.perf_counter() - started) * 1000
            # 画面未变时稍降采样
            base = interval_ms if frame.changed else max(interval_ms, 1200)
            sleep_ms = max(1, base - int(elapsed))
            self.msleep(sleep_ms)

        self.status.emit("采集线程已停止")
