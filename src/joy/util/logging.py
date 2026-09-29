"""日志辅助：脱敏 + 文件落盘（打包后无控制台时用于排错）。

文件日志策略：
- 目录：~/.joy/logs/
- 按小时滚动，最多保留约 24 个历史文件（≈最近 24 小时）
- 单文件超过上限强制滚动；目录总大小超限时删最旧文件
"""

from __future__ import annotations

import logging
import re
import sys
import time
from logging.handlers import TimedRotatingFileHandler
from pathlib import Path

_KEY_RE = re.compile(
    r"(?i)(api[_-]?key|authorization)\s*[:=]\s*['\"]?([^\s'\"]+)"
)
_BEARER_RE = re.compile(r"(?i)Bearer\s+[A-Za-z0-9._\-]+")
_SK_RE = re.compile(r"(?i)\b(sk-[A-Za-z0-9._\-]{4,})")
_DATA_URL_RE = re.compile(r"data:image/[a-zA-Z0-9+.-]+;base64,[A-Za-z0-9+/=\s]+")

# 保留窗口与体积上限（时间覆盖 + 防无限膨胀）
LOG_RETENTION_HOURS = 24
LOG_BACKUP_COUNT = 24
LOG_MAX_BYTES_PER_FILE = 2 * 1024 * 1024  # 2 MB / 段
LOG_MAX_TOTAL_BYTES = 10 * 1024 * 1024  # 目录合计约 10 MB


def redact(text: str) -> str:
    if not text:
        return text
    out = _BEARER_RE.sub("Bearer <redacted>", text)
    out = _KEY_RE.sub(r"\1=<redacted>", out)
    out = _SK_RE.sub("sk-<redacted>", out)
    out = _DATA_URL_RE.sub("data:image/...;base64,<redacted>", out)
    return out


class RedactingFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        if isinstance(record.msg, str):
            record.msg = redact(record.msg)
        if record.args:
            if isinstance(record.args, dict):
                record.args = {k: redact(str(v)) for k, v in record.args.items()}
            else:
                record.args = tuple(
                    redact(str(a)) if isinstance(a, str) else a for a in record.args
                )
        return True


def default_log_dir() -> Path:
    return Path.home() / ".joy" / "logs"


def default_log_path() -> Path:
    return default_log_dir() / "joy.log"


def prune_log_files(
    log_dir: Path | None = None,
    *,
    retention_hours: int = LOG_RETENTION_HOURS,
    max_total_bytes: int = LOG_MAX_TOTAL_BYTES,
) -> list[Path]:
    """删掉超过保留时长或撑破总容量的日志文件；返回被删路径。"""
    root = log_dir or default_log_dir()
    if not root.is_dir():
        return []
    cutoff = time.time() - max(1, int(retention_hours)) * 3600
    files: list[tuple[float, int, Path]] = []
    for p in root.glob("joy.log*"):
        if not p.is_file():
            continue
        try:
            st = p.stat()
        except OSError:
            continue
        files.append((st.st_mtime, st.st_size, p))

    removed: list[Path] = []
    keep: list[tuple[float, int, Path]] = []
    for mtime, size, p in files:
        if mtime < cutoff:
            try:
                p.unlink(missing_ok=True)
                removed.append(p)
            except OSError:
                pass
        else:
            keep.append((mtime, size, p))

    # 总大小：保留较新的，删最旧的
    keep.sort(key=lambda t: t[0], reverse=True)
    total = 0
    for mtime, size, p in keep:
        if total + size <= max_total_bytes:
            total += size
            continue
        try:
            p.unlink(missing_ok=True)
            removed.append(p)
        except OSError:
            pass
    return removed


class BoundedTimedRotatingFileHandler(TimedRotatingFileHandler):
    """按小时滚动，同时单文件超大时提前滚动。"""

    def __init__(self, *args, maxBytes: int = LOG_MAX_BYTES_PER_FILE, **kwargs):
        super().__init__(*args, **kwargs)
        self.maxBytes = max(0, int(maxBytes))

    def shouldRollover(self, record: logging.LogRecord) -> int:
        if super().shouldRollover(record):
            return 1
        if self.maxBytes <= 0 or self.stream is None:
            return 0
        try:
            self.stream.seek(0, 2)
            if self.stream.tell() >= self.maxBytes:
                return 1
        except OSError:
            return 0
        return 0

    def doRollover(self) -> None:
        super().doRollover()
        try:
            prune_log_files(Path(self.baseFilename).parent)
        except OSError:
            pass


def _want_console() -> bool:
    if getattr(sys, "frozen", False):
        # 打包无控制台；可用环境变量强制开（调试用）
        return bool(os_environ_truthy("JOY_LOG_CONSOLE"))
    return True


def os_environ_truthy(name: str) -> bool:
    import os

    return os.environ.get(name, "").strip().lower() in {"1", "true", "yes", "on"}


def setup_logging(level: int = logging.INFO) -> Path:
    """配置 joy 日志：文件必开；开发态额外打控制台。返回当前日志文件路径。"""
    root = logging.getLogger("joy")
    log_path = default_log_path()
    if root.handlers:
        return log_path

    root.setLevel(level)
    fmt = logging.Formatter("%(asctime)s [%(levelname)s] %(name)s: %(message)s")
    redactor = RedactingFilter()

    log_dir = default_log_dir()
    try:
        log_dir.mkdir(parents=True, exist_ok=True)
        prune_log_files(log_dir)
        file_handler: logging.Handler = BoundedTimedRotatingFileHandler(
            filename=str(log_path),
            when="H",
            interval=1,
            backupCount=LOG_BACKUP_COUNT,
            encoding="utf-8",
            delay=True,
            maxBytes=LOG_MAX_BYTES_PER_FILE,
        )
        file_handler.setFormatter(fmt)
        file_handler.addFilter(redactor)
        root.addHandler(file_handler)
    except OSError:
        # 磁盘不可写时至少保证不崩
        pass

    if _want_console():
        stream = logging.StreamHandler()
        stream.setFormatter(fmt)
        stream.addFilter(redactor)
        root.addHandler(stream)

    _install_excepthook()
    return log_path


def _install_excepthook() -> None:
    """未捕获异常写入日志，方便 exe 反馈。"""
    if getattr(sys, "_joy_excepthook_installed", False):
        return

    prev = sys.excepthook

    def _hook(exc_type, exc, tb) -> None:  # type: ignore[no-untyped-def]
        try:
            logging.getLogger("joy").error(
                "未捕获异常",
                exc_info=(exc_type, exc, tb),
            )
        except Exception:
            pass
        try:
            prev(exc_type, exc, tb)
        except Exception:
            pass

    sys.excepthook = _hook
    sys._joy_excepthook_installed = True  # type: ignore[attr-defined]

    if hasattr(sys, "unraisablehook"):
        prev_u = sys.unraisablehook

        def _unraisable(args) -> None:  # type: ignore[no-untyped-def]
            try:
                logging.getLogger("joy").error(
                    "未处理异常（unraisable）: %s",
                    getattr(args, "errormsg", None) or args,
                    exc_info=(
                        args.exc_type,
                        args.exc_value,
                        args.exc_traceback,
                    ),
                )
            except Exception:
                pass
            try:
                prev_u(args)
            except Exception:
                pass

        sys.unraisablehook = _unraisable  # type: ignore[assignment]
