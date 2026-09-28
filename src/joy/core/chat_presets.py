"""对话关系 / 说话风格：固定预设，避免自由文本导致表现漂移。"""

from __future__ import annotations

# (id, 界面文案, 写入提示词的说明)
RELATION_PRESETS: list[tuple[str, str, str]] = [
    ("朋友", "朋友", "朋友；语气自然对等，不必过度客套"),
    ("同事", "同事", "同事；偏职场，礼貌克制，少谈私事"),
    ("家人", "家人", "家人；亲近随和，可关心近况"),
    ("恋人", "恋人", "恋人；亲密但不油腻，避免越界玩笑"),
    ("客户", "客户", "客户/商务；专业礼貌，答复清晰可靠"),
    ("同学", "同学", "同学；轻松随意，可带一点玩笑"),
]

STYLE_PRESETS: list[tuple[str, str, str]] = [
    (
        "简洁口语",
        "简洁口语",
        "简洁口语：一两句说完，少用网络梗与表情堆砌，像真人微信",
    ),
    (
        "正式礼貌",
        "正式礼貌",
        "正式礼貌：用词克制、完整，适合职场与不熟的人",
    ),
    (
        "轻松幽默",
        "轻松幽默",
        "轻松幽默：自然带一点轻松感，但不油腻、不玩梗过头",
    ),
    (
        "温柔体贴",
        "温柔体贴",
        "温柔体贴：语气柔和、少催促，先回应情绪再谈事",
    ),
    (
        "干脆直接",
        "干脆直接",
        "干脆直接：不绕弯、少客套，把结论先说清楚",
    ),
]

DEFAULT_RELATION = "朋友"
DEFAULT_STYLE = "简洁口语"


def _ids(presets: list[tuple[str, str, str]]) -> set[str]:
    return {p[0] for p in presets}


def normalize_relation(value: str | None) -> str:
    raw = (value or "").strip()
    if raw in _ids(RELATION_PRESETS):
        return raw
    for pid, label, _ in RELATION_PRESETS:
        if pid in raw or label in raw:
            return pid
    return DEFAULT_RELATION


def normalize_style(value: str | None) -> str:
    raw = (value or "").strip()
    if not raw:
        return DEFAULT_STYLE
    if raw in _ids(STYLE_PRESETS):
        return raw
    for pid, label, prompt in STYLE_PRESETS:
        if raw in (pid, label, prompt) or pid in raw or label in raw:
            return pid
    return DEFAULT_STYLE


def relation_prompt(value: str | None) -> str:
    pid = normalize_relation(value)
    for i, label, prompt in RELATION_PRESETS:
        if i == pid:
            return prompt
    return RELATION_PRESETS[0][2]


def style_prompt(value: str | None) -> str:
    pid = normalize_style(value)
    for i, label, prompt in STYLE_PRESETS:
        if i == pid:
            return prompt
    return STYLE_PRESETS[0][2]
