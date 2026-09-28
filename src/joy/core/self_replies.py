"""独立「我的回复库」：按联系人持久化近期自己发过的话。

对应 docs/微信个人Agent智能回复优化方案.md Self Memory：
不依赖当前识图短窗，防复读在换窗/清屏后仍有效。
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
from joy.providers.reply_quality import intent_tags, norm_text, text_similarity

log = logging.getLogger("joy.self_replies")

_MAX_REPLIES = 40
_SIM_DEDUP = 0.72


class SelfReplyStore(BaseModel):
    title: str = ""
    replies: list[str] = Field(default_factory=list)
    updated_at: float = Field(default_factory=time.time)


def self_path(title: str, *, root: Path | None = None) -> Path:
    base = root or default_memory_dir()
    return base / f"{_safe_filename(title)}.self.json"


def load_self_store(title: str, *, root: Path | None = None) -> SelfReplyStore | None:
    t = resolve_storage_title(title, root=root) or normalize_title(title)
    if not t:
        return None
    p = self_path(t, root=root)
    if not p.is_file():
        for stem in related_contact_titles(t, root=root):
            alt = self_path(stem, root=root)
            if alt.is_file():
                p = alt
                t = stem
                break
        else:
            return None
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
        st = SelfReplyStore.model_validate(data)
        st.title = t
        return st
    except Exception as exc:
        log.warning("self store load failed %s: %s", p.name, exc)
        return None


def save_self_store(store: SelfReplyStore, *, root: Path | None = None) -> None:
    t = resolve_storage_title(store.title, root=root) or normalize_title(store.title)
    if not t:
        return
    store.title = t
    store.updated_at = time.time()
    base = root or default_memory_dir()
    base.mkdir(parents=True, exist_ok=True)
    self_path(t, root=root).write_text(
        store.model_dump_json(indent=2, ensure_ascii=False),
        encoding="utf-8",
    )


def clear_self_replies(title: str, *, root: Path | None = None) -> bool:
    related = related_contact_titles(title, root=root)
    removed = False
    for stem in related:
        p = self_path(stem, root=root)
        try:
            if p.is_file():
                p.unlink()
                removed = True
                log.info("self replies cleared title=%s", stem[:40])
        except OSError as exc:
            log.warning("self clear failed %s: %s", p.name, exc)
            raise
    return removed


def recent_self_replies(title: str, *, limit: int = 12, root: Path | None = None) -> list[str]:
    st = load_self_store(title, root=root)
    if st is None or not st.replies:
        return []
    n = max(1, int(limit))
    return list(st.replies[-n:])


def append_self_replies(
    title: str,
    texts: list[str],
    *,
    root: Path | None = None,
    keep_max: int = _MAX_REPLIES,
) -> int:
    """追加「我」的发言；返回净增条数。"""
    t = resolve_storage_title(title, root=root) or normalize_title(title)
    if not t:
        return 0
    incoming = [(x or "").strip() for x in texts if (x or "").strip()]
    if not incoming:
        return 0
    st = load_self_store(t, root=root) or SelfReplyStore(title=t)
    added = 0
    for text in incoming:
        if any(
            text_similarity(text, old) >= _SIM_DEDUP or norm_text(text) == norm_text(old)
            for old in st.replies[-20:]
        ):
            continue
        st.replies.append(text[:120])
        added += 1
    cap = max(8, int(keep_max))
    if len(st.replies) > cap:
        st.replies = st.replies[-cap:]
    if added:
        save_self_store(st, root=root)
        log.info("self replies +%s → %s title=%s", added, len(st.replies), t[:40])
    return added


def self_intent_tags(title: str, *, root: Path | None = None) -> set[str]:
    tags: set[str] = set()
    for t in recent_self_replies(title, limit=16, root=root):
        tags |= intent_tags(t)
    return tags
