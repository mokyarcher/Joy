"""回复质量：Self Memory 摘要 + 本地防复读 / 模板过滤。

对应 docs/微信个人Agent智能回复优化方案.md 的 P0：
  - 我最近说过什么
  - 候选与「我」近年发言去重
  - 砍掉客服式模板句
"""

from __future__ import annotations

import logging
import re
from collections.abc import Iterable

from joy.core.models import Candidate, ChatMessage, ScreenContext

log = logging.getLogger("joy.reply_quality")

# 意图粗指纹：同一簇里我刚说过，再出同类就当复读
_INTENT_CLUSTERS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("惊吓", ("吓人", "可怕", "卧槽", "太猛", "惨", "震惊", "吓人啊")),
    ("安全关心", ("注意安全", "伤到", "受伤", "没事吧", "有没有人", "平安", "希望大家")),
    ("第一次反应", ("啥情况", "怎么回事", "什么情况", "这是啥", "看着有点")),
    ("空洞附和", ("太强了", "牛逼", "厉害", "哈哈哈", "确实", "是啊", "对啊")),
)

_TEMPLATE_BAD = re.compile(
    r"(注意安全|希望大家|保重身体|愿.*平安|总结一下|综上|作为AI|我理解你的感受)"
)
# 空壳接话：没咬住具体信息，只会问「咋整」/抽象成「人为原因」
_HOLLOW_REPLY = re.compile(
    r"(咋整|咋办|怎么办|怎么整|怎么说|如何是好|怎么看|有什么看法|你觉得呢|说说看|"
    r"人为原因|人为的|天灾人祸)"
)


def norm_text(text: str) -> str:
    s = (text or "").strip().lower()
    s = re.sub(r"\s+", "", s)
    s = re.sub(r"[，。！？、；：…—\-_,.!?;:\"'“”‘’（）()【】\[\]]+", "", s)
    return s


def _bigrams(s: str) -> set[str]:
    if len(s) < 2:
        return {s} if s else set()
    return {s[i : i + 2] for i in range(len(s) - 1)}


def text_similarity(a: str, b: str) -> float:
    """字符 bigram Jaccard；0~1。"""
    aa, bb = _bigrams(norm_text(a)), _bigrams(norm_text(b))
    if not aa or not bb:
        return 1.0 if norm_text(a) == norm_text(b) and norm_text(a) else 0.0
    inter = len(aa & bb)
    union = len(aa | bb)
    return inter / union if union else 0.0


def intent_tags(text: str) -> set[str]:
    t = norm_text(text)
    hits: set[str] = set()
    for name, kws in _INTENT_CLUSTERS:
        if any(k in t for k in kws):
            hits.add(name)
    return hits


def recent_me_texts(ctx: ScreenContext, *, limit: int = 8) -> list[str]:
    out: list[str] = []
    for m in reversed(ctx.messages or []):
        if m.side != "me":
            continue
        t = (m.text or "").strip()
        if not t:
            continue
        out.append(t)
        if len(out) >= limit:
            break
    out.reverse()
    return out


def recent_other_texts(ctx: ScreenContext, *, limit: int = 6) -> list[str]:
    out: list[str] = []
    for m in reversed(ctx.messages or []):
        if m.side != "other":
            continue
        t = (m.text or "").strip()
        if not t:
            continue
        who = (m.name or "").strip()
        out.append(f"{who}: {t}" if who else t)
        if len(out) >= limit:
            break
    out.reverse()
    return out


def self_memory_block(
    ctx: ScreenContext,
    *,
    limit: int = 8,
    title_hint: str = "",
) -> str:
    """合并：独立回复库 + 当前窗「我」的发言。"""
    title = (ctx.title or title_hint or "").strip()
    stored: list[str] = []
    if title:
        try:
            from joy.core.self_replies import recent_self_replies

            stored = recent_self_replies(title, limit=max(limit, 10))
        except Exception as exc:
            log.warning("self store read failed: %s", exc)
    window = recent_me_texts(ctx, limit=limit)
    # 库在前、窗口在后；去重
    mine: list[str] = []
    seen: set[str] = set()
    for t in stored + window:
        k = norm_text(t)
        if not k or k in seen:
            continue
        seen.add(k)
        mine.append(t)
    mine = mine[-max(limit, 10) :]
    if not mine:
        return "（本轮可见范围内我还没发言；持久回复库也空）"
    lines = [f"- {t}" for t in mine]
    tags: set[str] = set()
    for t in mine:
        tags |= intent_tags(t)
    tag_line = "、".join(sorted(tags)) if tags else "（无）"
    return (
        "我最近已经说过（禁止再换皮复读同观点/同情绪/同问题）：\n"
        + "\n".join(lines)
        + f"\n已表达过的意图簇：{tag_line}"
    )


def latest_other_message(ctx: ScreenContext) -> str:
    for m in reversed(ctx.messages or []):
        if m.side == "other" and (m.text or "").strip():
            return (m.text or "").strip()
    return ""


_ANCHOR_STOP = frozenset(
    {
        "这个",
        "那个",
        "什么",
        "怎么",
        "不是",
        "一个",
        "我们",
        "你们",
        "他们",
        "已经",
        "还是",
        "就是",
        "然后",
        "因为",
        "所以",
        "可以",
        "没有",
        "一下",
        "起来",
        "过来",
        "过去",
        "应该",
        "附近",
        "山上",
    }
)


def _cn_ngrams(text: str, *, n: int = 2) -> list[str]:
    """连续汉字滑窗 n-gram，避免整句只抽成一个超长 token。"""
    chars = re.findall(r"[\u4e00-\u9fff]", text or "")
    if len(chars) < n:
        return []
    return ["".join(chars[i : i + n]) for i in range(len(chars) - n + 1)]


def anchor_keys(text: str, *, limit: int = 12) -> list[str]:
    """从一句里抽出可点名的信息词（优先 2~3 字实词）。"""
    raw = (text or "").strip()
    if not raw:
        return []
    keys: list[str] = []
    seen: set[str] = set()
    # 英文/数字整词
    for m in re.findall(r"[A-Za-z0-9]{2,}", raw):
        if m.lower() not in seen:
            seen.add(m.lower())
            keys.append(m)
    # 2-gram + 3-gram，过滤停用
    for gram in _cn_ngrams(raw, n=2) + _cn_ngrams(raw, n=3):
        if gram in _ANCHOR_STOP or gram in seen:
            continue
        seen.add(gram)
        keys.append(gram)
        if len(keys) >= limit:
            break
    return keys[:limit]


_EVENT_LEXICON = (
    "放炮",
    "施工",
    "水库",
    "震垮",
    "塌方",
    "山上",
    "不是天灾",
    "天灾",
    "泥石",
    "冲下去",
)


def latest_anchor_block(ctx: ScreenContext) -> str:
    """强制钉死对方最新一句，避免模型抽象成「人为原因啊咋整」。"""
    latest = latest_other_message(ctx)
    if not latest:
        return "【必须接住】对方最新：（无）"
    from joy.core.message_noise import is_light_bubble_body, peer_display_names

    if is_light_bubble_body(latest) or latest.strip() in {"[表情]", "[动画表情]"}:
        names = "、".join(sorted(peer_display_names(ctx))[:4]) or "（未知）"
        return (
            "【必须接住】对方最新是表情/贴纸（可能已识别为 [666]/[偷笑] 等）。\n"
            f"发送者昵称仅供参考：{names}——禁止把昵称写进回复正文。\n"
            "按表情含义轻附和即可，或 should_reply_now=false；不要复读方括号标签本身。"
        )
    keys = [k for k in anchor_keys(latest, limit=12) if len(k) >= 3][:8]
    # 顺带点出近几条里的硬事实，防止只盯最新半句丢了「放炮/施工」
    recent = recent_other_texts(ctx, limit=4)
    fact_hint: list[str] = []
    blob = " ".join(recent)
    for w in _EVENT_LEXICON:
        if w in blob and w not in fact_hint:
            fact_hint.append(w)
    hint = "、".join(keys or fact_hint) if (keys or fact_hint) else latest[:20]
    facts = ("；近期硬事实：" + "、".join(fact_hint)) if fact_hint else ""
    return (
        f"【必须接住】对方最新一条原文：\n「{latest}」\n"
        f"本条里可点名的信息词：{hint}{facts}\n"
        "回复必须直接咬住这些新信息（点名山上/塌方/放炮等），"
        "禁止改写成空洞的「人为原因/咋整/怎么办」，"
        "禁止拿关键词硬玩谐音梗（塌方≠方了）。"
    )


def cold_start_block(ctx: ScreenContext) -> str:
    """可见窗短、我还没发言时，压住 AI 硬接话/玩梗。"""
    if recent_me_texts(ctx, limit=1):
        return ""
    others = [
        (m.text or "").strip()
        for m in (ctx.messages or [])
        if m.side == "other" and (m.text or "").strip()
    ]
    if len(others) > 8:
        return ""
    shock_n = sum(
        1 for t in others if any(k in t for k in ("卧槽", "我靠", "我去", "吓人", "腿软"))
    )
    extra = (
        "别人已连环感叹时不要再叠一层惊吓/玩梗；"
        if shock_n >= 2
        else ""
    )
    return (
        "【冷启动】可见历史很短，且我还没发过言。"
        f"{extra}"
        "禁止硬玩谐音梗；禁止编造聊天里没有的声音/画面细节（如「这动静听着就…」）；"
        "能短接事实就短接，接不住就 should_reply_now=false。"
    )


def topic_brief_block(ctx: ScreenContext, *, other_limit: int = 6) -> str:
    """轻量话题提示：只列对方最近信息，不叫模型重新总结全书。"""
    others = recent_other_texts(ctx, limit=other_limit)
    if not others:
        return "对方最近可见消息：（无）"
    return "对方最近新信息（优先接最新一条，勿整段重讲）：\n" + "\n".join(
        f"- {t}" for t in others
    )


def draft_transcript(ctx: ScreenContext, *, limit: int = 12) -> str:
    """起草用短窗原文，避免 30 条 memory 把模型冲昏。"""
    lines: list[str] = []
    msgs: list[ChatMessage] = list(ctx.messages or [])[-max(4, limit) :]
    for m in msgs:
        who = {"me": "我", "other": "对方", "system": "系统"}.get(m.side, m.side)
        if m.name:
            who = f"{who}({m.name})"
        lines.append(f"{who}: {m.text}")
    return "\n".join(lines) if lines else "(无消息)"


def is_template_bad(text: str) -> bool:
    t = (text or "").strip()
    if not t:
        return True
    if _TEMPLATE_BAD.search(t):
        return True
    if _HOLLOW_REPLY.search(t):
        return True
    # 过长「发言稿」不像微信
    if len(t) > 48 and ("。" in t or "，" in t) and t.count("，") + t.count("。") >= 3:
        return True
    return False


def is_abstract_dodge(text: str, ctx: ScreenContext) -> bool:
    """对方已给具体原因时，候选还用「人为/天灾」抽象糊弄。"""
    blob = " ".join(
        (m.text or "") for m in (ctx.messages or []) if m.side == "other"
    )
    t = text or ""
    if not t:
        return True
    specific = any(w in blob for w in ("放炮", "施工", "炸", "爆破"))
    if specific and re.search(r"(人为|天灾)", t) and not any(
        w in t for w in ("放炮", "施工", "炸", "爆破", "震垮", "水库")
    ):
        return True
    return False


def is_forced_wordplay(text: str, ctx: ScreenContext) -> bool:
    """拿事件词硬拆谐音梗（塌方→方了）或编造不存在的感官细节。"""
    blob = " ".join(
        (m.text or "") for m in (ctx.messages or []) if m.side == "other"
    )
    t = (text or "").strip()
    if not t:
        return True
    # 塌方 → 方了（未点名塌方本身）
    if ("塌方" in blob or "塌了" in blob) and re.search(
        r"(^|[，,。！!？?\s])方了", t
    ):
        if "塌方" not in t and "塌了" not in t:
            return True
    # 编造听觉细节：聊天里没提声音/视频
    if re.search(r"(这动静|听着就|听着都|听着怪|声音好吓人)", t):
        if not any(
            w in blob for w in ("听", "声", "动静", "视频", "语音", "[视频]", "[语音]")
        ):
            return True
    return False


def contradicts_context(text: str, ctx: ScreenContext) -> bool:
    """候选与对方刚纠正的事实冲突（如对方说不是天灾，候选还说天灾）。"""
    blob = " ".join(
        (m.text or "") for m in (ctx.messages or []) if m.side == "other"
    )
    t = text or ""
    if ("不是天灾" in blob or "非天灾" in blob) and ("天灾" in t) and ("不是" not in t):
        return True
    if ("放炮" in blob or "施工" in blob) and ("天灾" in t) and ("不是" not in t):
        return True
    return False


def echoes_peer_nickname(text: str, ctx: ScreenContext) -> bool:
    """候选把对方昵称当正文开头/整句复读（如「一帆风顺，这波稳」）。"""
    from joy.core.message_noise import peer_display_names

    body = (text or "").strip()
    if not body:
        return False
    for name in peer_display_names(ctx):
        if len(name) < 2:
            continue
        if body == name:
            return True
        for sep in ("，", ",", " ", "：", ":"):
            if body.startswith(name + sep):
                return True
        # 「@昵称」允许；裸昵称开头不允许
        if body.startswith(name) and not body.startswith("@" + name):
            # 昵称后紧跟汉字/标点也算（一帆风顺这波）
            rest = body[len(name) :]
            if rest and not rest[0].isalnum():
                return True
            if rest and re.match(r"^[\u4e00-\u9fff]", rest):
                return True
    return False


def misses_latest_anchor(text: str, ctx: ScreenContext) -> bool:
    """最新句有明确实词时，候选完全不沾边 → 视为没接住上下文。"""
    latest = latest_other_message(ctx)
    if not latest:
        return False
    keys = anchor_keys(latest, limit=12)
    if len(keys) < 1:
        return False
    body = text or ""
    # 强锚：3 字及以上 + 事件词；避免「塌方了」末两字「方了」被谐音命中
    strong = [k for k in keys if len(k) >= 3]
    for w in _EVENT_LEXICON:
        if w in latest and w not in strong:
            strong.insert(0, w)
    dangling = {s[-2:] for s in strong if len(s) >= 3}
    weak = [k for k in keys if len(k) == 2 and k not in dangling]
    if any(k in body for k in strong[:10]):
        return False
    if any(k in body for k in weak[:6]):
        return False
    # 近期硬事实也算接住（最新句可能只是结果半句）
    recent_blob = " ".join(recent_other_texts(ctx, limit=4))
    for w in _EVENT_LEXICON:
        if w in recent_blob and w in body:
            return False
    if text_similarity(body, latest) >= 0.18:
        return False
    # 最新句很短时放宽
    if len(norm_text(latest)) <= 4:
        return False
    return True


def looks_like_repeat(
    text: str,
    recent_me: Iterable[str],
    *,
    sim_thr: float = 0.42,
) -> bool:
    tags = intent_tags(text)
    heavy = {"惊吓", "安全关心", "第一次反应"}
    for prev in recent_me:
        if text_similarity(text, prev) >= sim_thr:
            return True
        shared = tags & intent_tags(prev)
        # 同「惊吓/安全/第一次反应」簇：换皮也算复读（水库群典型复读机）
        if shared & heavy:
            return True
        if shared and len(norm_text(text)) <= 24 and text_similarity(text, prev) >= 0.22:
            return True
    return False


def filter_candidates(
    candidates: list[Candidate],
    ctx: ScreenContext,
    *,
    sim_thr: float = 0.42,
) -> list[Candidate]:
    """去掉复读 / 模板 / 彼此雷同的候选，重标 recommended。"""
    mine = recent_me_texts(ctx, limit=10)
    title = (ctx.title or "").strip()
    if title:
        try:
            from joy.core.self_replies import recent_self_replies

            for t in recent_self_replies(title, limit=16):
                if t not in mine:
                    mine.append(t)
        except Exception:
            pass
    kept: list[Candidate] = []
    for c in candidates:
        text = (c.text or "").strip()
        if not text:
            continue
        if is_template_bad(text):
            log.info("cand drop template/hollow: %s", text[:40])
            continue
        if is_abstract_dodge(text, ctx):
            log.info("cand drop abstract: %s", text[:40])
            continue
        if is_forced_wordplay(text, ctx):
            log.info("cand drop wordplay: %s", text[:40])
            continue
        if contradicts_context(text, ctx):
            log.info("cand drop contradict: %s", text[:40])
            continue
        if echoes_peer_nickname(text, ctx):
            log.info("cand drop peer-nickname echo: %s", text[:40])
            continue
        if misses_latest_anchor(text, ctx):
            log.info("cand drop no-anchor: %s", text[:40])
            continue
        if looks_like_repeat(text, mine, sim_thr=sim_thr):
            log.info("cand drop vs self: %s", text[:40])
            continue
        if any(text_similarity(text, k.text) >= 0.55 for k in kept):
            log.info("cand drop vs sibling: %s", text[:40])
            continue
        kept.append(c)
    if not kept and candidates:
        # 全灭时：只回退到「非空话/非玩梗/非矛盾」的一条；没有就空手
        for c in candidates:
            text = (c.text or "").strip()
            if not text:
                continue
            if (
                is_template_bad(text)
                or is_abstract_dodge(text, ctx)
                or is_forced_wordplay(text, ctx)
                or contradicts_context(text, ctx)
                or echoes_peer_nickname(text, ctx)
            ):
                continue
            log.info("cand filter empty → keep non-hollow fallback")
            kept = [c]
            break
        if not kept:
            log.info("cand filter empty → no safe fallback, return []")
    for i, c in enumerate(kept):
        c.recommended = i == 0
    return kept[:3]
