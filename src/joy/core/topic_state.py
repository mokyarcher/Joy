"""Topic State：按联系人维护当前话题 / 事实 / 已讨论点。

对应 docs/微信个人Agent智能回复优化方案.md 第二层。
本地启发式更新（不额外打 LLM），起草时注入，避免每次像「第一次听说」。
"""

from __future__ import annotations

import json
import logging
import re
import time
from pathlib import Path

from pydantic import BaseModel, Field

from joy.core.contact_memory import default_memory_dir, normalize_title, _safe_filename
from joy.core.models import ChatMessage, ScreenContext
from joy.providers.reply_quality import intent_tags, norm_text, text_similarity

log = logging.getLogger("joy.topic_state")

_MAX_FACTS = 12
_MAX_DISCUSSED = 10
_FACT_SIM = 0.55
_TOPIC_KEYWORDS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("事故灾害", ("塌", "水库", "泥石", "爆炸", "车祸", "地震", "火灾", "冲下去", "放炮")),
    ("游戏", ("王者", "apex", "上分", "排位", "匹配", "段位")),
    ("日常闲聊", ("吃饭", "麻辣烫", "猫", "钓鱼", "上班", "下班")),
)


class TopicState(BaseModel):
    title: str = ""
    topic: str = ""
    topic_status: str = "idle"  # idle | active | cooling
    facts: list[str] = Field(default_factory=list)
    latest_development: str = ""
    already_discussed: list[str] = Field(default_factory=list)
    mood: str = ""  # 围观|玩梗|认真|闲聊…
    updated_at: float = Field(default_factory=time.time)


def topic_path(title: str, *, root: Path | None = None) -> Path:
    base = root or default_memory_dir()
    return base / f"{_safe_filename(title)}.topic.json"


def load_topic(title: str, *, root: Path | None = None) -> TopicState | None:
    from joy.core.contact_memory import resolve_storage_title

    t = resolve_storage_title(title, root=root) or normalize_title(title)
    if not t:
        return None
    p = topic_path(t, root=root)
    if not p.is_file():
        # 变体桶上的 topic
        from joy.core.contact_memory import related_contact_titles

        for stem in related_contact_titles(t, root=root):
            alt = topic_path(stem, root=root)
            if alt.is_file():
                p = alt
                t = stem
                break
        else:
            return None
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
        st = TopicState.model_validate(data)
        st.title = t
        return st
    except Exception as exc:
        log.warning("topic load failed %s: %s", p.name, exc)
        return None


def save_topic(state: TopicState, *, root: Path | None = None) -> None:
    from joy.core.contact_memory import resolve_storage_title

    t = resolve_storage_title(state.title, root=root) or normalize_title(state.title)
    if not t:
        return
    state.title = t
    state.updated_at = time.time()
    base = root or default_memory_dir()
    base.mkdir(parents=True, exist_ok=True)
    p = topic_path(t, root=root)
    p.write_text(
        state.model_dump_json(indent=2, ensure_ascii=False),
        encoding="utf-8",
    )


def clear_topic(title: str, *, root: Path | None = None) -> bool:
    """删除该联系人 Topic State（含 OCR 变体桶）。任一文件删掉即 True。"""
    from joy.core.contact_memory import related_contact_titles

    related = related_contact_titles(title, root=root)
    removed = False
    for stem in related:
        p = topic_path(stem, root=root)
        try:
            if p.is_file():
                p.unlink()
                removed = True
                log.info("topic cleared title=%s", stem[:40])
        except OSError as exc:
            log.warning("topic clear failed %s: %s", p.name, exc)
            raise
    return removed


def _topic_scores(texts: list[str]) -> dict[str, int]:
    blob = norm_text("".join(texts[-8:]))
    return {
        name: sum(1 for k in kws if k in blob) for name, kws in _TOPIC_KEYWORDS
    }


def _guess_topic_label(texts: list[str]) -> str:
    scores = _topic_scores(texts)
    best = max(scores, key=scores.get) if scores else "闲聊"
    if scores.get(best, 0) <= 0:
        return texts[-1][:16] if texts else "闲聊"
    return best


def _detect_topic_switch(st: TopicState, other_texts: list[str]) -> str | None:
    """已知类目之间跳转（水库→猫）时返回新类目，否则 None。"""
    if not other_texts or (not st.facts and not st.already_discussed):
        return None
    recent = other_texts[-5:]
    scores = _topic_scores(recent)
    best = max(scores, key=scores.get)
    best_n = scores.get(best, 0)
    if best_n < 1:
        return None
    known = {name for name, _ in _TOPIC_KEYWORDS}
    old = (st.topic or "").strip()
    if old not in known or best not in known:
        return None
    if old == best:
        return None
    # 旧类目在近文里已无命中，新类目有命中 → 清状态
    if scores.get(old, 0) == 0 and best_n >= 1:
        return best
    # 两边都有一点命中但新类目明显更强
    if best_n >= scores.get(old, 0) + 2:
        return best
    return None


def _guess_mood(texts: list[str]) -> str:
    blob = norm_text("".join(texts[-6:]))
    if any(k in blob for k in ("卧槽", "吓人", "塌", "惨", "冲下去")):
        return "围观惊讶"
    if any(k in blob for k in ("哈哈", "表情", "梗", "太强")):
        return "玩梗"
    if "?" in "".join(texts[-4:]) or "？" in "".join(texts[-4:]):
        return "认真讨论"
    return "闲聊"


def _add_unique(items: list[str], text: str, *, sim: float, cap: int) -> bool:
    t = (text or "").strip()
    if len(t) < 2:
        return False
    for old in items:
        if text_similarity(t, old) >= sim or norm_text(t) == norm_text(old):
            return False
    items.append(t)
    while len(items) > cap:
        items.pop(0)
    return True


def _fact_from_other(text: str) -> str | None:
    t = (text or "").strip()
    if len(t) < 2:
        return None
    # 纯感叹/附和不进事实库
    if re.fullmatch(r"[哈呵嗯啊哦卧槽]+", norm_text(t)):
        return None
    if len(norm_text(t)) <= 2:
        return None
    return t[:80]


def update_topic_from_context(
    ctx: ScreenContext,
    *,
    title_hint: str = "",
    root: Path | None = None,
) -> TopicState:
    """用当前上下文增量更新话题状态并落盘。"""
    from joy.core.contact_memory import resolve_storage_title

    title = resolve_storage_title(
        normalize_title(ctx.title) or normalize_title(title_hint), root=root
    )
    st = load_topic(title, root=root) if title else None
    if st is None:
        st = TopicState(title=title or "")
    else:
        st.title = title or st.title

    msgs = list(ctx.messages or [])[-20:]
    other_texts = [(m.text or "").strip() for m in msgs if m.side == "other" and (m.text or "").strip()]
    me_texts = [(m.text or "").strip() for m in msgs if m.side == "me" and (m.text or "").strip()]

    if other_texts:
        switched = _detect_topic_switch(st, other_texts)
        if switched:
            log.info(
                "topic switch title=%s %s → %s (reset facts/discussed)",
                title,
                st.topic or "?",
                switched,
            )
            st.facts = []
            st.already_discussed = []
            st.latest_development = ""
            st.topic = switched
            st.mood = _guess_mood(other_texts)
            st.topic_status = "active"
        else:
            st.topic = _guess_topic_label(other_texts)
            st.mood = _guess_mood(other_texts)
            st.topic_status = "active"
        new_bits: list[str] = []
        for t in other_texts[-8:]:
            fact = _fact_from_other(t)
            if fact and _add_unique(st.facts, fact, sim=_FACT_SIM, cap=_MAX_FACTS):
                new_bits.append(fact)
        if new_bits:
            st.latest_development = new_bits[-1]

    # 我已表达过的意图 → already_discussed
    tag_names = {
        "惊吓": "表达惊吓/吓人",
        "安全关心": "关心人员安全/注意安全",
        "第一次反应": "第一次反应式提问",
        "空洞附和": "空洞附和",
    }
    for t in me_texts[-8:]:
        for tag in intent_tags(t):
            label = tag_names.get(tag, tag)
            _add_unique(st.already_discussed, label, sim=0.8, cap=_MAX_DISCUSSED)

    if title:
        save_topic(st, root=root)
        log.info(
            "topic update title=%s topic=%s facts=%s discussed=%s latest=%s",
            title,
            st.topic,
            len(st.facts),
            len(st.already_discussed),
            (st.latest_development or "")[:24],
        )
    return st


def topic_prompt_block(state: TopicState | None) -> str:
    if state is None or not (state.facts or state.topic):
        return "话题状态：（尚无）"
    facts = "\n".join(f"- {f}" for f in state.facts[-8:]) or "- （无）"
    discussed = "、".join(state.already_discussed[-6:]) or "（无）"
    return (
        f"当前话题：{state.topic or '未知'}（{state.topic_status}，气氛:{state.mood or '—'}）\n"
        f"已知事实（勿再当新闻第一次播报）：\n{facts}\n"
        f"最新进展：{state.latest_development or '—'}\n"
        f"我已讨论过：{discussed}\n"
        "规则：旧事实不要再「第一次听说」；只接 latest_development / 对方最新一条里的新信息。"
    )


_EVENT_TOKENS = (
    "水库",
    "塌方",
    "塌了",
    "放炮",
    "施工",
    "冲下去",
    "泥石",
    "爆炸",
    "车祸",
    "地震",
    "火灾",
    "王者",
    "apex",
    "上分",
    "猫",
    "钓鱼",
    "吃饭",
)


def _event_token_set(text: str) -> set[str]:
    t = norm_text(text)
    return {k for k in _EVENT_TOKENS if k in t}


def is_same_topic_same_fact(text: str, facts: list[str]) -> bool:
    """规则版语义去重：高相似或共享事件词换皮。"""
    t = (text or "").strip()
    if not t or not facts:
        return False
    nt = norm_text(t)
    et = _event_token_set(t)
    for f in facts:
        if not f:
            continue
        if norm_text(f) == nt:
            return True
        sim = text_similarity(t, f)
        if sim >= 0.78:
            return True
        shared = et & _event_token_set(f)
        if len(shared) >= 2 and (sim >= 0.28 or len(nt) <= 20):
            return True
        if len(shared) >= 1 and sim >= 0.55 and len(nt) <= 24:
            return True
    return False


def _semantic_dup_in_window(ctx: ScreenContext, latest: str) -> bool:
    earlier: list[str] = []
    saw_latest = False
    for m in reversed(ctx.messages or []):
        if m.side != "other":
            continue
        body = (m.text or "").strip()
        if not body:
            continue
        if not saw_latest:
            if body == latest:
                saw_latest = True
            continue
        earlier.append(body)
        if len(earlier) >= 6:
            break
    return is_same_topic_same_fact(latest, earlier)


def latest_other_novelty(
    ctx: ScreenContext, state: TopicState | None
) -> tuple[float, str]:
    """对方最新一条相对已知事实的新意 0~1；附原因。"""
    latest = ""
    for m in reversed(ctx.messages or []):
        if m.side == "other" and (m.text or "").strip():
            latest = (m.text or "").strip()
            break
    if not latest:
        return 0.0, "no_other"
    if state is None or not state.facts:
        if _semantic_dup_in_window(ctx, latest):
            return 0.15, "same_topic_same_fact"
        return 1.0, "cold_start"
    if is_same_topic_same_fact(latest, state.facts):
        return 0.12, "same_topic_same_fact"
    best = max((text_similarity(latest, f) for f in state.facts), default=0.0)
    novelty = max(0.0, 1.0 - best)
    if best >= 0.62:
        return novelty, "near_dup_fact"
    if best >= 0.42:
        return novelty, "related_old"
    return novelty, "new_fact"


def _mentions_me(text: str, me_names: list[str] | None = None) -> bool:
    t = text or ""
    if "@" not in t and "＠" not in t:
        return False
    names = [n.strip() for n in (me_names or []) if (n or "").strip()]
    if not names:
        # 任意 @ 视为点名（群里常见），优先值得回
        return True
    for n in names:
        if f"@{n}" in t or f"＠{n}" in t:
            return True
    return False


def _distinct_other_reactors(msgs: list[ChatMessage], *, window: int = 8) -> set[str]:
    """近窗里对事件做过惊吓/附和反应的对方人数（按昵称）。"""
    names: set[str] = set()
    for m in msgs[-window:]:
        if m.side != "other":
            continue
        body = (m.text or "").strip()
        if not body:
            continue
        tags = intent_tags(body)
        nt = norm_text(body)
        if tags & {"惊吓", "空洞附和", "第一次反应", "安全关心"}:
            names.add((m.name or "").strip() or f"anon:{nt[:6]}")
        elif re.fullmatch(r"[哈呵嗯啊哦卧槽我靠我去]+", nt) or len(nt) <= 4:
            names.add((m.name or "").strip() or f"anon:{nt[:6]}")
    return names


def local_reply_policy(
    ctx: ScreenContext,
    state: TopicState | None,
    *,
    me_names: list[str] | None = None,
) -> tuple[bool, str]:
    """
    本地 Reply Policy：默认偏「可不回」。
    返回 (should_reply, reason)。
    """
    msgs = list(ctx.messages or [])
    if not msgs:
        return False, "empty"
    last = msgs[-1]
    if last.side == "me":
        return False, "last_from_me"
    text = (last.text or "").strip()
    if not text:
        return False, "empty_text"

    # @我 / 点名 → 优先回（压过感叹跳过）
    if _mentions_me(text, me_names):
        return True, "mentioned"

    # 纯感叹 / 表情占位
    nt = norm_text(text)
    if re.fullmatch(r"[哈呵嗯啊哦卧槽]+", nt) or nt in {"[动画表情]", "[表情]", "[图片]"}:
        return False, "filler"

    novelty, nov_reason = latest_other_novelty(ctx, state)
    # 同一事实换皮刷屏 → 不回
    if nov_reason == "same_topic_same_fact":
        return False, "same_topic_same_fact"

    me_tags: set[str] = set()
    for m in msgs:
        if m.side == "me":
            me_tags |= intent_tags(m.text or "")
    # 持久 Self Memory 里的意图也算「我已表达」
    title = normalize_title(ctx.title) if ctx.title else ""
    if title:
        try:
            from joy.core.self_replies import self_intent_tags

            me_tags |= self_intent_tags(title)
        except Exception:
            pass

    # 多人已回：近窗已有 ≥2 个对方做过同类反应，最新仍是短惊吓/附和/跟风问 → 可不硬接
    reactors = _distinct_other_reactors(msgs, window=8)
    if len(reactors) >= 2 and len(nt) <= 16:
        tags = intent_tags(text)
        # 纯情绪叠层（无新事实线索）
        if tags & {"惊吓", "空洞附和"} and not any(
            k in text
            for k in ("伤", "死", "救", "放炮", "施工", "原因", "不是", "水库", "塌了", "塌方")
        ):
            return False, "others_already_replied"
        # 跟风确认问句且非明显新事实
        if (
            ("？" in text or "?" in text)
            and any(k in text for k in ("又", "吗", "吧", "呢"))
            and nov_reason != "new_fact"
        ):
            return False, "others_already_replied"

    # 旧事实近重复 + 我已惊吓或关心安全 → 不硬接
    if nov_reason == "near_dup_fact" and (
        me_tags & {"惊吓", "安全关心", "第一次反应"}
    ):
        return False, "dup_fact_already_reacted"

    # 相关旧话题且新意低 + 已讨论过惊吓/安全
    if novelty < 0.45 and state and any(
        "安全" in d or "惊吓" in d for d in state.already_discussed
    ):
        return False, "low_novelty"

    # 冷启动 + 多人已连环感叹，最新只是跟风复述问句 → 可不硬接
    other_texts = [
        (m.text or "").strip()
        for m in msgs
        if m.side == "other" and (m.text or "").strip()
    ]
    shock_n = sum(
        1
        for t in other_texts
        if any(k in t for k in ("卧槽", "我靠", "我去", "吓人", "腿软"))
    )
    if (
        nov_reason == "cold_start"
        and shock_n >= 2
        and ("？" in text or "?" in text)
        and any(k in text for k in ("又", "吗", "吧"))
    ):
        return False, "cold_chorus_rhetorical"

    # 问句 / 新事实 → 更值得回
    if "？" in text or "?" in text:
        return True, "question"
    if novelty >= 0.45:
        return True, "new_or_delta"
    # 群里短附和：可不回
    if len(nt) <= 6:
        return False, "short_ack"
    return True, "default_ok"


ReplyTier = str  # none | micro | normal | long


def decide_reply_tier(
    ctx: ScreenContext,
    state: TopicState | None,
    *,
    policy_reason: str = "",
    me_names: list[str] | None = None,
) -> ReplyTier:
    """Reply 分档：none / micro / normal / long。"""
    msgs = list(ctx.messages or [])
    if not msgs:
        return "none"
    last = msgs[-1]
    if last.side != "other":
        return "none"
    text = (last.text or "").strip()
    if not text:
        return "none"

    if policy_reason in {
        "filler",
        "others_already_replied",
        "cold_chorus_rhetorical",
        "dup_fact_already_reacted",
        "low_novelty",
        "short_ack",
        "same_topic_same_fact",
        "empty",
        "last_from_me",
        "empty_text",
    }:
        return "none"

    if _mentions_me(text, me_names):
        if len(norm_text(text)) >= 28 or text.count("？") + text.count("?") >= 2:
            return "long"
        return "normal"

    novelty, nov_reason = latest_other_novelty(ctx, state)
    if nov_reason in {"same_topic_same_fact", "near_dup_fact"}:
        return "none"

    nt = norm_text(text)
    if len(nt) <= 6 or re.fullmatch(r"[哈呵嗯啊哦卧槽我靠我去离谱]+", nt):
        return "micro"
    if intent_tags(text) <= {"惊吓", "空洞附和"} and len(nt) <= 12:
        return "micro"

    if ("？" in text or "?" in text) and len(nt) >= 18:
        return "long"
    if any(
        k in text for k in ("怎么", "为什么", "为啥", "如何", "帮我", "能不能", "可不可以")
    ):
        return "long"

    if nov_reason == "new_fact" or novelty >= 0.45:
        return "normal"
    if "？" in text or "?" in text:
        return "normal"
    return "micro"


def reply_tier_prompt(tier: ReplyTier) -> str:
    if tier == "micro":
        return (
            "【回复档位：极短】最多 6～10 个字，情绪词即可（如：卧槽 / 离谱 / 我去）。"
            "禁止解释、禁止提问、禁止两句。"
        )
    if tier == "long":
        return (
            "【回复档位：可略长】可以 2～3 句，把问题或观点说清；"
            "仍要像微信口语，不要写成说明文。"
        )
    if tier == "normal":
        return "【回复档位：普通】1～2 句自然口语，接住最新信息点即可。"
    return "【回复档位：不回复】should_reply_now=false，candidates=[]。"
