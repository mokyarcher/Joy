"""识图 Prompt 与 JSON 解析。"""

from __future__ import annotations

import json
import re
from typing import Any

from joy.core.models import ChatMessage, ScreenContext

# 短、硬：减少废话与截断；不要放可照抄的示例句子（模型会直接抄「在吗/在」）
# 微表情细则由 wechat_emoji.vision_emoji_prompt_block() 动态拼入
_VISION_SYSTEM_HEAD = """从聊天截图提取结构化数据。只输出一个 JSON 对象，禁止 Markdown、禁止解释、不要长篇思考。

硬性要求：
- 只提取截图里实际能看见的气泡文字，禁止编造、禁止补全、禁止用常见闲聊话术凑数
- 截图里没有的字绝不能出现在 JSON 里

字段：
- title: 顶部会话名，看不清用 ""
- messages: 最近可见气泡，最多 8 条，时间从上到下
  - side: "me"(右/绿) | "other"(左/白) | "system"
  - name: 群聊发言人，私聊 null
  - text: 原文；图片用"[图片]"，语音用"[语音]"，红包转账用"[红包或转账]"
"""

_VISION_SYSTEM_TAIL = """
忽略（绝不要放进 messages.text）：
- 聊天区正中间的时间灰字（如 10:53、昨天 21:30）——系统时间条
- 群聊气泡上方的发言人昵称——只能放进 name 字段
- 底部弹出的「表情选择面板」（最近使用/所有表情网格）——那是选表情 UI，不是聊天内容
- 输入框、广告、未读分割线

输出形状（尖括号仅为占位说明，不要原样输出）：
{"title":"<会话名>","messages":[{"side":"other","name":null,"text":"<可见原文>"},{"side":"me","name":null,"text":"<可见原文>"}]}
"""


def build_vision_system() -> str:
    from joy.core.wechat_emoji import vision_emoji_prompt_block

    return (
        _VISION_SYSTEM_HEAD.strip()
        + "\n\n"
        + vision_emoji_prompt_block()
        + "\n"
        + _VISION_SYSTEM_TAIL.strip()
    )


# 兼容旧引用
VISION_SYSTEM = build_vision_system()

VISION_USER = "根据这张截图输出 JSON，文字必须与画面一致："

# 旧 prompt 示例；模型常原样返回
_ECHO_MESSAGE_TEXTS = frozenset({"在吗", "在"})


_FENCE_RE = re.compile(r"```(?:json)?\s*([\s\S]*?)\s*```", re.IGNORECASE)
_SMART_QUOTES = str.maketrans(
    {
        "\u201c": '"',
        "\u201d": '"',
        "\u2018": "'",
        "\u2019": "'",
        "\ufeff": "",
        "\u200b": "",
    }
)


def _strip_noise(text: str) -> str:
    raw = (text or "").translate(_SMART_QUOTES).strip()
    if not raw:
        return ""
    m = _FENCE_RE.search(raw)
    if m:
        raw = m.group(1).strip()
    # 去掉常见前缀废话
    for prefix in ("json", "JSON", "输出：", "结果："):
        if raw.startswith(prefix):
            raw = raw[len(prefix) :].lstrip(" \n:：")
    return raw.strip()


def _balanced_object(text: str) -> str | None:
    """取第一个平衡的 {...}，避免贪婪正则截错。"""
    start = text.find("{")
    if start < 0:
        return None
    depth = 0
    in_str = False
    escape = False
    for i in range(start, len(text)):
        ch = text[i]
        if in_str:
            if escape:
                escape = False
            elif ch == "\\":
                escape = True
            elif ch == '"':
                in_str = False
            continue
        if ch == '"':
            in_str = True
        elif ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return text[start : i + 1]
    # 截断：返回未闭合片段，交给 repair
    if depth > 0:
        return text[start:]
    return None


def _fix_trailing_commas(s: str) -> str:
    return re.sub(r",(\s*[}\]])", r"\1", s)


def _repair_truncated(s: str) -> str:
    """尽力闭合被 max_tokens 截断的 JSON。"""
    s = s.rstrip()
    if not s.startswith("{"):
        return s
    # 若停在开字符串中间，先合上引号
    in_str = False
    escape = False
    for ch in s:
        if in_str:
            if escape:
                escape = False
            elif ch == "\\":
                escape = True
            elif ch == '"':
                in_str = False
        elif ch == '"':
            in_str = True
    if in_str:
        s += '"'

    stack: list[str] = []
    in_str = False
    escape = False
    for ch in s:
        if in_str:
            if escape:
                escape = False
            elif ch == "\\":
                escape = True
            elif ch == '"':
                in_str = False
            continue
        if ch == '"':
            in_str = True
        elif ch in "{[":
            stack.append("}" if ch == "{" else "]")
        elif ch in "}]" and stack and stack[-1] == ch:
            stack.pop()
    # 去掉末尾悬空逗号再闭合
    s = re.sub(r",\s*$", "", s)
    while stack:
        s += stack.pop()
    return s


def _loads_candidate(s: str) -> dict[str, Any]:
    candidates = [s, _fix_trailing_commas(s), _repair_truncated(_fix_trailing_commas(s))]
    last_err: Exception | None = None
    for cand in candidates:
        try:
            data = json.loads(cand)
        except json.JSONDecodeError as exc:
            last_err = exc
            continue
        if isinstance(data, dict):
            return data
        last_err = ValueError("JSON 根必须是对象")
    raise ValueError(f"无法解析 JSON：{(s or '')[:200]}") from last_err


def extract_json_object(text: str) -> dict[str, Any]:
    raw = _strip_noise(text)
    if not raw:
        raise ValueError("模型返回为空")
    try:
        data = json.loads(raw)
        if isinstance(data, dict):
            return data
    except json.JSONDecodeError:
        pass

    obj = _balanced_object(raw)
    if obj is None:
        raise ValueError(f"无法解析 JSON：{raw[:200]}")
    return _loads_candidate(obj)


def screen_context_from_dict(
    data: dict[str, Any],
    *,
    app_id: str = "wechat_win",
) -> ScreenContext:
    title = str(data.get("title") or "").strip()
    messages_raw = data.get("messages") or []
    messages: list[ChatMessage] = []
    if not isinstance(messages_raw, list):
        raise ValueError("messages 必须是数组")
    for item in messages_raw:
        if not isinstance(item, dict):
            continue
        side = str(item.get("side") or "other").strip().lower()
        if side not in ("me", "other", "system"):
            side = "other"
        text = str(item.get("text") or "").strip()
        if not text:
            continue
        from joy.core.message_noise import is_ui_timestamp
        from joy.core.wechat_emoji import normalize_emoji_text

        # 模型偶发把居中「10:53」标成 other —— 硬滤
        if is_ui_timestamp(text):
            continue
        text = normalize_emoji_text(text)
        name = item.get("name")
        name_s = str(name).strip() if name else None
        messages.append(ChatMessage(side=side, text=text, name=name_s or None))  # type: ignore[arg-type]
    # 硬上限，与 prompt 一致
    if len(messages) > 8:
        messages = messages[-8:]
    from joy.core.message_noise import fold_speaker_name_messages, drop_ui_timestamps

    messages = fold_speaker_name_messages(drop_ui_timestamps(messages))
    return ScreenContext(
        app_id=app_id,
        title=title,
        messages=messages,
        source="vision_api",
    )


def looks_like_prompt_echo(ctx: ScreenContext) -> bool:
    """检测是否照抄了旧示例「在吗/在」之类，而非读图。"""
    texts = [m.text.strip() for m in ctx.messages if (m.text or "").strip()]
    if not texts:
        return False
    if len(texts) <= 2 and set(texts) <= _ECHO_MESSAGE_TEXTS:
        return True
    # 仅两条且恰好是 在吗 + 在
    if texts == ["在吗", "在"] or texts == ["在", "在吗"]:
        return True
    return False
