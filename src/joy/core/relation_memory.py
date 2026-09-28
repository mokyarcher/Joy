"""按会话的人物关系粗画像：对方说话习惯 / 互动期待。

对应 docs/微信个人Agent智能回复优化方案.md §六（细粒度的轻量落地）。
"""

from __future__ import annotations

import json
import logging
import time
from pathlib import Path

from pydantic import BaseModel, Field

from joy.core.contact_memory import (
    _safe_filename,
    default_memory_dir,
    normalize_title,
    related_contact_titles,
    resolve_storage_title,
)
from joy.core.models import ScreenContext
from joy.providers.reply_quality import intent_tags, norm_text

log = logging.getLogger("joy.relation_memory")

_MAX_PEOPLE = 12


class PersonProfile(BaseModel):
    name: str
    msg_count: int = 0
    avg_len: float = 0.0
    question_rate: float = 0.0
    likes_joking: bool = False
    short_style: bool = True
    frequent_tags: list[str] = Field(default_factory=list)


class RelationMemory(BaseModel):
    title: str = ""
    people: dict[str, PersonProfile] = Field(default_factory=dict)
    updated_at: float = Field(default_factory=time.time)


def relation_path(title: str, *, root: Path | None = None) -> Path:
    base = root or default_memory_dir()
    return base / f"{_safe_filename(title)}.relation.json"


def load_relation(title: str, *, root: Path | None = None) -> RelationMemory | None:
    t = resolve_storage_title(title, root=root) or normalize_title(title)
    if not t:
        return None
    p = relation_path(t, root=root)
    if not p.is_file():
        for stem in related_contact_titles(t, root=root):
            alt = relation_path(stem, root=root)
            if alt.is_file():
                p = alt
                t = stem
                break
        else:
            return None
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
        st = RelationMemory.model_validate(data)
        st.title = t
        return st
    except Exception as exc:
        log.warning("relation load failed %s: %s", p.name, exc)
        return None


def save_relation(mem: RelationMemory, *, root: Path | None = None) -> None:
    t = resolve_storage_title(mem.title, root=root) or normalize_title(mem.title)
    if not t:
        return
    mem.title = t
    mem.updated_at = time.time()
    base = root or default_memory_dir()
    base.mkdir(parents=True, exist_ok=True)
    relation_path(t, root=root).write_text(
        mem.model_dump_json(indent=2, ensure_ascii=False),
        encoding="utf-8",
    )


def clear_relation(title: str, *, root: Path | None = None) -> bool:
    removed = False
    for stem in related_contact_titles(title, root=root):
        p = relation_path(stem, root=root)
        try:
            if p.is_file():
                p.unlink()
                removed = True
        except OSError as exc:
            log.warning("relation clear failed %s: %s", p.name, exc)
            raise
    return removed


def _upsert_person(mem: RelationMemory, name: str, text: str) -> None:
    who = (name or "").strip() or "对方"
    body = (text or "").strip()
    if not body:
        return
    L = len(norm_text(body))
    is_q = ("？" in body or "?" in body)
    joking = any(k in body for k in ("哈哈", "笑死", "离谱", "绝了"))
    tags = sorted(intent_tags(body))
    p = mem.people.get(who)
    if p is None:
        mem.people[who] = PersonProfile(
            name=who,
            msg_count=1,
            avg_len=float(L),
            question_rate=1.0 if is_q else 0.0,
            likes_joking=joking,
            short_style=L <= 12,
            frequent_tags=tags[:4],
        )
        return
    n = p.msg_count
    p.avg_len = (p.avg_len * n + L) / (n + 1)
    p.question_rate = (p.question_rate * n + (1.0 if is_q else 0.0)) / (n + 1)
    p.likes_joking = p.likes_joking or joking
    p.short_style = p.avg_len <= 14
    # 合并标签（简单保留）
    merged = list(dict.fromkeys((p.frequent_tags or []) + tags))[:6]
    p.frequent_tags = merged
    p.msg_count = n + 1


def update_relation_from_context(
    ctx: ScreenContext,
    *,
    title_hint: str = "",
    root: Path | None = None,
) -> RelationMemory:
    title = resolve_storage_title(ctx.title or title_hint, root=root) or normalize_title(
        ctx.title or title_hint
    )
    mem = load_relation(title, root=root) if title else None
    if mem is None:
        mem = RelationMemory(title=title or "")
    else:
        mem.title = title or mem.title

    for m in ctx.messages or []:
        if m.side != "other":
            continue
        _upsert_person(mem, m.name or "对方", m.text or "")

    # 人数封顶：保留发言最多的
    if len(mem.people) > _MAX_PEOPLE:
        top = sorted(mem.people.values(), key=lambda p: p.msg_count, reverse=True)[
            :_MAX_PEOPLE
        ]
        mem.people = {p.name: p for p in top}

    if title:
        save_relation(mem, root=root)
        log.info(
            "relation update title=%s people=%s",
            title[:32],
            len(mem.people),
        )
    return mem


def relation_prompt_block(mem: RelationMemory | None, *, limit: int = 5) -> str:
    if mem is None or not mem.people:
        return "会话人物：（尚无细画像）"
    people = sorted(mem.people.values(), key=lambda p: p.msg_count, reverse=True)[
        :limit
    ]
    lines: list[str] = []
    for p in people:
        bits = [
            f"均长≈{p.avg_len:.0f}字",
            "短句" if p.short_style else "偏长",
            "爱开玩笑" if p.likes_joking else "较少玩笑",
            f"提问率{p.question_rate:.0%}",
        ]
        if p.frequent_tags:
            bits.append("标签:" + "/".join(p.frequent_tags[:3]))
        lines.append(f"- {p.name}：{'，'.join(bits)}")
    return (
        "会话里对方说话习惯（接话时对齐，勿过度客套）：\n"
        + "\n".join(lines)
    )
