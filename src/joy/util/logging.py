"""日志辅助：脱敏，避免 Key / 长 Base64 进日志。"""

from __future__ import annotations

import logging
import re

_KEY_RE = re.compile(
    r"(?i)(api[_-]?key|authorization)\s*[:=]\s*['\"]?([^\s'\"]+)"
)
_BEARER_RE = re.compile(r"(?i)Bearer\s+[A-Za-z0-9._\-]+")
_SK_RE = re.compile(r"(?i)\b(sk-[A-Za-z0-9._\-]{4,})")
_DATA_URL_RE = re.compile(r"data:image/[a-zA-Z0-9+.-]+;base64,[A-Za-z0-9+/=\s]+")


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


def setup_logging(level: int = logging.INFO) -> None:
    root = logging.getLogger("joy")
    if root.handlers:
        return
    root.setLevel(level)
    handler = logging.StreamHandler()
    handler.setFormatter(
        logging.Formatter("%(asctime)s [%(levelname)s] %(name)s: %(message)s")
    )
    handler.addFilter(RedactingFilter())
    root.addHandler(handler)
