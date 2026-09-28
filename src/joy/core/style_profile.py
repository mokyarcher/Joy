"""真实风格画像：从「我的回复库」统计长度/开场白/幽默，供 prompt + 候选重排。

对应 docs/微信个人Agent智能回复优化方案.md §十六 / 十七。
"""

from __future__ import annotations

import json
import logging
import re
import time
from collections import Counter
from pathlib import Path

from pydantic import BaseModel, Field

from joy.core.contact_memory import (
    _safe_filename,
    default_memory_dir,
    normalize_title,
    related_contact_titles,
    resolve_storage_title,
)
from joy.core.models import Candidate
from joy.core.self_replies import recent_self_replies
from joy.providers.reply_quality import norm_text

log = logging.getLogger("joy.style_profile")

_HUMOR = ("哈哈", "笑死", "离谱", "绝了", "绷不住", "笑抽", "太逗", "哈哈哈")
_OPENER_RE = re.compile(r"^[\u4e00-\u9fffA-Za-z卧槽我靠我去不是]{1,6}")


class StyleProfile(BaseModel):
    title: str = ""
    sample_n: int = 0
    avg_reply_length: float = 0.0
    median_reply_length: float = 0.0
    short_ratio: float = 0.0  # <=8 字占比
    humor_frequency: float = 0.0
    question_frequency: float = 0.0
    common_openers: list[str] = Field(default_factory=list)
    sample_replies: list[str] = Field(default_factory=list)
    updated_at: float = Field(default_factory=time.time)


def style_path(title: str, *, root: Path | None = None) -> Path:
    base = root or default_memory_dir()
    return base / f"{_safe_filename(title)}.style.json"


def load_style_profile(title: str, *, root: Path | None = None) -> StyleProfile | None:
    t = resolve_storage_title(title, root=root) or normalize_title(title)
    if not t:
        return None
    p = style_path(t, root=root)
    if not p.is_file():
        for stem in related_contact_titles(t, root=root):
            alt = style_path(stem, root=root)
            if alt.is_file():
                p = alt
                t = stem
                break
        else:
            return None
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
        st = StyleProfile.model_validate(data)
        st.title = t
        return st
    except Exception as exc:
        log.warning("style load failed %s: %s", p.name, exc)
        return None


def save_style_profile(profile: StyleProfile, *, root: Path | None = None) -> None:
    t = resolve_storage_title(profile.title, root=root) or normalize_title(profile.title)
    if not t:
        return
    profile.title = t
    profile.updated_at = time.time()
    base = root or default_memory_dir()
    base.mkdir(parents=True, exist_ok=True)
    style_path(t, root=root).write_text(
        profile.model_dump_json(indent=2, ensure_ascii=False),
        encoding="utf-8",
    )


def clear_style_profile(title: str, *, root: Path | None = None) -> bool:
    removed = False
    for stem in related_contact_titles(title, root=root):
        p = style_path(stem, root=root)
        try:
            if p.is_file():
                p.unlink()
                removed = True
        except OSError as exc:
            log.warning("style clear failed %s: %s", p.name, exc)
            raise
    return removed


def _median(vals: list[int]) -> float:
    if not vals:
        return 0.0
    s = sorted(vals)
    n = len(s)
    mid = n // 2
    if n % 2:
        return float(s[mid])
    return (s[mid - 1] + s[mid]) / 2.0


def build_style_profile(
    title: str,
    *,
    root: Path | None = None,
    limit: int = 40,
) -> StyleProfile:
    replies = recent_self_replies(title, limit=limit, root=root)
    t = resolve_storage_title(title, root=root) or normalize_title(title)
    if not replies:
        return StyleProfile(title=t or "")
    lengths = [len(norm_text(r)) for r in replies]
    humor_n = sum(1 for r in replies if any(h in r for h in _HUMOR))
    q_n = sum(1 for r in replies if ("？" in r or "?" in r))
    openers: Counter[str] = Counter()
    for r in replies:
        m = _OPENER_RE.match((r or "").strip())
        if m:
            op = m.group(0)
            if 1 < len(op) <= 4:
                openers[op] += 1
    short_n = sum(1 for L in lengths if L <= 8)
    n = len(replies)
    return StyleProfile(
        title=t or "",
        sample_n=n,
        avg_reply_length=sum(lengths) / n,
        median_reply_length=_median(lengths),
        short_ratio=short_n / n,
        humor_frequency=humor_n / n,
        question_frequency=q_n / n,
        common_openers=[w for w, _ in openers.most_common(5)],
        sample_replies=replies[-5:],
    )


def refresh_style_profile(title: str, *, root: Path | None = None) -> StyleProfile:
    prof = build_style_profile(title, root=root)
    if prof.sample_n > 0:
        save_style_profile(prof, root=root)
        log.info(
            "style refresh title=%s n=%s avg=%.1f short=%.2f humor=%.2f",
            (title or "")[:32],
            prof.sample_n,
            prof.avg_reply_length,
            prof.short_ratio,
            prof.humor_frequency,
        )
    return prof


def style_prompt_block(profile: StyleProfile | None) -> str:
    if profile is None or profile.sample_n < 2:
        return "我的真实风格：（样本不足，先跟设置里的说话风格）"
    openers = "、".join(profile.common_openers) if profile.common_openers else "（无）"
    samples = "\n".join(f"- {s}" for s in profile.sample_replies[-4:]) or "- （无）"
    humor = (
        "高"
        if profile.humor_frequency >= 0.35
        else ("中" if profile.humor_frequency >= 0.15 else "低")
    )
    return (
        "我的真实说话习惯（从历史抽出，优先模仿）：\n"
        f"- 平均字数约 {profile.avg_reply_length:.0f}（中位 {profile.median_reply_length:.0f}）\n"
        f"- 短句占比 {profile.short_ratio:.0%}；幽默频率 {humor}；提问频率 {profile.question_frequency:.0%}\n"
        f"- 常见开场：{openers}\n"
        f"真实样本：\n{samples}\n"
        "候选回复的长度与口吻应贴近上述习惯，不要写成客服长文。"
    )


def style_score(text: str, profile: StyleProfile | None) -> float:
    """越像本人越高（0~1），无画像时返回 0.5。"""
    if profile is None or profile.sample_n < 2:
        return 0.5
    t = (text or "").strip()
    if not t:
        return 0.0
    L = len(norm_text(t))
    target = profile.median_reply_length or profile.avg_reply_length or 10.0
    # 长度贴近中位
    len_score = max(0.0, 1.0 - abs(L - target) / max(8.0, target * 1.5))
    # 短句习惯：本人短句多时，长候选扣分
    if profile.short_ratio >= 0.45 and L > 22:
        len_score *= 0.55
    if profile.short_ratio >= 0.45 and L <= 12:
        len_score = min(1.0, len_score + 0.15)
    # 开场白命中
    opener_hit = 0.0
    for op in profile.common_openers:
        if t.startswith(op):
            opener_hit = 0.2
            break
    # 幽默匹配
    has_humor = any(h in t for h in _HUMOR)
    humor_score = 0.0
    if profile.humor_frequency >= 0.25 and has_humor:
        humor_score = 0.15
    elif profile.humor_frequency < 0.1 and has_humor and L > 16:
        humor_score = -0.1
    return max(0.0, min(1.0, 0.55 * len_score + 0.25 + opener_hit + humor_score))


def rerank_by_style(
    candidates: list[Candidate],
    profile: StyleProfile | None,
) -> list[Candidate]:
    if not candidates or profile is None or profile.sample_n < 2:
        return candidates
    scored: list[tuple[float, Candidate]] = []
    for c in candidates:
        base = float(c.score) if c.score is not None else 0.5
        s = style_score(c.text or "", profile)
        scored.append((0.45 * base + 0.55 * s, c))
    scored.sort(key=lambda x: x[0], reverse=True)
    out = [c for _, c in scored]
    for i, c in enumerate(out):
        c.recommended = i == 0
    log.info(
        "style rerank scores=%s",
        [round(s, 2) for s, _ in scored],
    )
    return out
