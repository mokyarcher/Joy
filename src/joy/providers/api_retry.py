"""OpenAI 兼容调用：短暂失败 / 限流时有限次退避重试。"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable
from typing import TypeVar

from joy.util.logging import redact

log = logging.getLogger("joy.api_retry")
T = TypeVar("T")


def is_rate_limit(exc: BaseException) -> bool:
    s = str(exc).lower()
    return (
        "429" in s
        or "rate limit" in s
        or "rate_limit" in s
        or "tpm" in s
        or "rpm" in s
        or "quota_exceeded" in s
        or "exhausted" in s
    )


def is_retryable(exc: BaseException) -> bool:
    """可重试：限流、超时、连接抖动、网关 5xx。鉴权/参数错误不重试。"""
    s = str(exc).lower()
    if any(
        k in s
        for k in (
            "401",
            "403",
            "invalid api key",
            "authentication",
            "permission",
            "invalid_api_key",
        )
    ):
        return False
    if is_rate_limit(exc):
        return True
    keys = (
        "timeout",
        "timed out",
        "connection",
        "temporarily",
        "unavailable",
        "502",
        "503",
        "504",
        "524",
        "reset",
        "broken pipe",
        "ssl",
        "server error",
        "internal server",
        "cloudflare",
    )
    return any(k in s for k in keys)


def call_with_retry(
    fn: Callable[[], T],
    *,
    retries: int = 2,
    base_delay_sec: float = 1.2,
    label: str = "api",
) -> T:
    """
    最多 retries 次重试（合计 retries+1 次尝试）。
    429 / rpm 会多等一会再打，避免立刻连打加重限流。
    """
    last: BaseException | None = None
    attempts = max(0, int(retries)) + 1
    for i in range(attempts):
        try:
            return fn()
        except Exception as exc:
            last = exc
            if i + 1 >= attempts or not is_retryable(exc):
                raise
            if is_rate_limit(exc):
                delay = min(12.0, max(2.5, base_delay_sec * (2**i)) + float(i))
            else:
                delay = min(6.0, base_delay_sec * (2**i))
            log.warning(
                "%s retry %s/%s in %.1fs: %s",
                label,
                i + 1,
                attempts - 1,
                delay,
                redact(str(exc)[:160]),
            )
            time.sleep(delay)
    assert last is not None
    raise last
