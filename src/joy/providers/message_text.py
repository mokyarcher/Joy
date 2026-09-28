"""从 OpenAI 兼容 message 取出可用正文（含 SenseNova reasoning）。"""

from __future__ import annotations

from typing import Any


def content_to_text(content: Any) -> str:
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for p in content:
            if isinstance(p, str):
                parts.append(p)
            elif isinstance(p, dict):
                if p.get("type") == "text" or "text" in p:
                    parts.append(str(p.get("text") or ""))
            else:
                t = getattr(p, "text", None)
                if t:
                    parts.append(str(t))
        return "".join(parts)
    return str(content)


def message_output_text(msg: Any) -> tuple[str, str]:
    """
    返回 (text, source)。
    SenseNova 推理模型常把正文放在 message.reasoning，content 为空。
    """
    if msg is None:
        return "", "empty"
    content = content_to_text(getattr(msg, "content", None)).strip()
    if content:
        return content, "content"
    for attr in ("reasoning", "reasoning_content"):
        alt = getattr(msg, attr, None)
        if isinstance(alt, str) and alt.strip():
            return alt.strip(), attr
    if hasattr(msg, "model_dump"):
        try:
            dumped = msg.model_dump()
        except Exception:
            dumped = {}
        for attr in ("reasoning", "reasoning_content"):
            alt = dumped.get(attr) if isinstance(dumped, dict) else None
            if isinstance(alt, str) and alt.strip():
                return alt.strip(), attr
    return "", "empty"
