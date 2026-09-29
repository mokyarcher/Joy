"""聊天 UI 噪音：中间时间条、群昵称灰字等，不能当成对方消息正文。"""

from __future__ import annotations

import re
import unicodedata

from joy.core.models import ChatMessage, ScreenContext

# 全角数字 → 半角
_FW_DIGIT = str.maketrans("０１２３４５６７８９", "0123456789")

# 10:53 / 10：53 / 10.53 / 9:05:01
_CLOCK = re.compile(r"^\d{1,2}[:：.]\d{2}([:：.]\d{2})?$")
# 昨天 10:53 / 前天 9:05 / 今天 12:00
_REL_DAY = re.compile(
    r"^(昨天|前天|今天|星期[一二三四五六日天]|周[一二三四五六日天])\s*"
    r"\d{1,2}[:：.]\d{2}([:：.]\d{2})?$"
)
# 上午 10:53 / 下午3:20
_AMPM = re.compile(r"^(上午|下午)\s*\d{1,2}[:：.]\d{2}([:：.]\d{2})?$")
# 2026年9月27日 10:53 / 2026年09月27日
_CN_DATE = re.compile(
    r"^\d{4}年\d{1,2}月\d{1,2}日(\s+\d{1,2}[:：.]\d{2}([:：.]\d{2})?)?$"
)
# 仅日期灰条：9月27日 / 09月27日
_CN_MD = re.compile(r"^\d{1,2}月\d{1,2}日$")
# OCR 偶发：整串只含时间相关字符且像时钟
_LOOSE_CLOCK_CHARS = re.compile(r"^[\d\s:：.\-年月日上午下午昨天前天今天星期周一二三四五六日天]+$")

_NICK_RE = re.compile(r"^[\u4e00-\u9fffA-Za-z0-9_·•．.\-@]{2,20}$")
_EMOJI_OR_MEDIA = re.compile(
    r"^\[(动画表情|表情|图片|视频|语音|文件|名片|位置|链接|聊天记录|小程序|音乐|表情包)\]$"
)
# 短句/语气词：不要当成群昵称
_NOT_NICK = frozenset(
    {
        "嗯",
        "哦",
        "啊",
        "哈",
        "嗯嗯",
        "好的",
        "收到",
        "在吗",
        "在的",
        "可以",
        "不行",
        "不是",
        "是的",
        "确实",
        "哈哈",
        "哈哈哈",
        "笑死",
        "绝了",
        "服了",
        "卧槽",
        "我靠",
        "我去",
        "娘的",
        "妈的",
        "我靠",
        "草",
        "靠",
        "666",
        "6",
        "1",
        "ok",
        "OK",
        "嗯呐",
        "好吧",
        "行吧",
        "得了",
        "同AI",
    }
)


def _normalize_time_text(text: str) -> str:
    t = unicodedata.normalize("NFKC", (text or "").strip())
    t = t.translate(_FW_DIGIT)
    # 去零宽 / 薄空格
    t = re.sub(r"[\u200b\u200c\u200d\ufeff\u00a0]", "", t)
    t = re.sub(r"\s+", " ", t).strip()
    return t


def is_ui_timestamp(text: str) -> bool:
    """微信消息区居中灰字时间条（如 10:53），不是聊天内容。"""
    t = _normalize_time_text(text)
    if not t or len(t) > 28:
        return False
    if _CLOCK.match(t) or _REL_DAY.match(t) or _AMPM.match(t):
        return True
    if _CN_DATE.match(t) or _CN_MD.match(t):
        return True
    # 宽松：很短且全是时间字符，并含有时钟分隔
    if len(t) <= 16 and _LOOSE_CLOCK_CHARS.match(t):
        if re.search(r"\d{1,2}[:：.]\d{2}", t) or re.search(r"\d{4}年", t):
            return True
        if re.fullmatch(r"(昨天|前天|今天|上午|下午)", t):
            return True
    return False


def looks_like_group_nickname(text: str) -> bool:
    """像群聊气泡上方昵称，而非一句正经话。

    微信群昵称常到 10+ 字（酒家肉山大魔王、LongCat小助手-布丁@美团），
    不能只认短名；但也绝不能把「这是专门给…」这类正文当成昵称。
    """
    t = (text or "").strip()
    if not t or len(t) > 20 or len(t) < 2:
        return False
    if is_ui_timestamp(t):
        return False
    if t in _NOT_NICK:
        return False
    # @提及是气泡正文，不是发言人灰字昵称
    if t.startswith("@"):
        return False
    # 金额/数量短句（「15块钱」「3块」「100元」）是聊天正文
    if re.search(
        r"\d+\s*(块钱|块|元|毛|角|万|亿|倍|斤|米|公里|分钟|小时|天|岁|个)",
        t,
    ):
        return False
    if re.fullmatch(r"\d+[块元万亿]?", t):
        return False
    # 常见动宾短聊（「剪头发」「吃饭了」已由虚词拦截；无虚词也要拦）
    if re.fullmatch(
        r"(剪|吃|喝|买|看|打|玩|睡|走|来|去|回|开|关|洗|刷|穿|脱|拿|放|发|收|"
        r"写|读|听|唱|画|拍|录|传|下|上|点|选|换|修|装|卸|扫|拖|煮|炒|蒸)"
        r"[\u4e00-\u9fff]{1,4}",
        t,
    ):
        return False
    # 纯笑声/语气延长不是昵称（「哈哈哈哈哈」曾被误挂成 name）
    if re.fullmatch(r"[哈呵嘿嘻啊哦嗯欸诶]+", t):
        return False
    if re.fullmatch(r"(哈哈|呵呵|嘿嘿|嘻嘻|啊啊|哦哦|嗯嗯){1,6}", t):
        return False
    # 冒号/波浪号多半是置顶公告或正文
    if any(c in t for c in "。！？!?，,；;：:、…～~"):
        return False
    # 句子虚词 → 正文（如「热了我五个月」「太耐用了」）
    if re.search(r"[了吗呢吧着过]", t):
        return False
    if re.search(r"(太|都还有|用了|欢迎|开始了|为期)", t):
        return False
    # 常见陈述/评价骨架 → 正文（群测：火车静音车厢那串）
    if re.search(
        r"(这是|那是|不是|什么|怎么|为啥|有没有|不错|专门|增加|需要|应该|已经|"
        r"还是|就是|觉得|感觉|好像|卧槽|我靠|牛逼|可以啊)",
        t,
    ):
        return False
    # 「有静音车厢」等：有/没/在 开头的短陈述
    if re.match(r"^[有没在]", t) and len(t) >= 4:
        return False
    # 带「的」且偏长，更像短语/句子而非昵称
    if "的" in t and len(t) >= 6:
        return False
    cjk_n = len(re.findall(r"[\u4e00-\u9fff]", t))
    if cjk_n >= 17:
        return False
    if not re.search(r"[\u4e00-\u9fffA-Za-z0-9]", t):
        return False
    return bool(_NICK_RE.match(t))


def _emit_orphan_speaker(pending: str, *, confirmed: set[str]) -> ChatMessage:
    """昵称行后无正文。

    仅当该串已确认为发言人时，才补 [表情]（真·贴纸漏识）；
    否则保留原文，避免把「15块钱」等短聊幻觉成表情。
    """
    p = (pending or "").strip()
    if p in confirmed:
        return ChatMessage(side="other", text="[表情]", name=p)
    return ChatMessage(side="other", text=p)


def is_light_bubble_body(text: str) -> bool:
    """表情/贴纸/占位，可作为昵称后面的「正文」。"""
    t = (text or "").strip()
    if not t:
        return False
    if t in _NOT_NICK:
        return True
    if _EMOJI_OR_MEDIA.match(t):
        return True
    # 微信微表情名：[666][偷笑] 或连续多个
    if re.fullmatch(r"(?:\[[^\[\]\n]{1,12}\]){1,6}", t):
        return True
    # 纯 emoji / 符号（无汉字词）
    if not re.search(r"[\u4e00-\u9fffA-Za-z0-9]{2,}", t) and len(t) <= 8:
        return True
    return False


def fold_speaker_name_messages(messages: list[ChatMessage]) -> list[ChatMessage]:
    """把误当成正文的群昵称折进下一条的 name。

    OCR 常见：`陈相` / `热了我五个月` → 应为 other+name=陈相。
    短句「娘的」保留为正文；已识别过的昵称再次单独出现也折进去。
    已确认发言人单独一行（贴纸漏识）→ name=昵称 text=[表情]；
    未确认的短串保留原文，禁止幻觉 [表情]。
    """
    msgs = list(messages or [])
    if not msgs:
        return []

    # 仅「已在 name 字段出现」或「后面确实挂了正文」的才算确认发言人
    confirmed: set[str] = set()
    for m in msgs:
        if m.name and looks_like_group_nickname(m.name):
            confirmed.add(m.name.strip())

    known: set[str] = set(confirmed)
    # Pass1：昵称行 + 后续正文/表情 → 学昵称
    for i, m in enumerate(msgs):
        if m.side != "other":
            continue
        t = (m.text or "").strip()
        if not looks_like_group_nickname(t):
            continue
        for j in range(i + 1, min(i + 5, len(msgs))):
            nxt = msgs[j]
            if nxt.side != "other":
                break
            nt = (nxt.text or "").strip()
            if nt in known or (looks_like_group_nickname(nt) and nt not in _NOT_NICK):
                continue
            if (
                len(nt) >= 6
                or nt in _NOT_NICK
                or is_light_bubble_body(nt)
                or nt.startswith("@")
            ):
                known.add(t)
                confirmed.add(t)
            break

    out: list[ChatMessage] = []
    pending: str | None = None
    pending_used = False  # 该昵称后是否已挂上至少一条正文
    for m in msgs:
        t = (m.text or "").strip()
        # 已知昵称，或形态像昵称的新发言人行（避免被上一人 pending 吞成正文）
        if m.side == "other" and t and (
            t in known or looks_like_group_nickname(t)
        ):
            if pending and not pending_used:
                out.append(_emit_orphan_speaker(pending, confirmed=confirmed))
            pending = t
            pending_used = False
            known.add(t)
            continue
        if m.side == "other" and pending:
            existing_name = (m.name or "").strip()
            # 下一条已有 name 且与 pending 不同：pending 更像漏掉的正文，勿吞掉
            if (
                not pending_used
                and existing_name
                and existing_name != pending
            ):
                out.append(ChatMessage(side="other", text=pending))
                out.append(m)
                pending = None
                pending_used = False
                continue
            # 同一气泡被 OCR 拆多行：连续正文共用 pending，直到下一个昵称
            out.append(m.model_copy(update={"name": m.name or pending}))
            pending_used = True
            continue
        if m.side != "other":
            if pending and not pending_used:
                out.append(_emit_orphan_speaker(pending, confirmed=confirmed))
            pending = None
            pending_used = False
        out.append(m)

    # 尾部昵称后无正文
    if pending and not pending_used:
        out.append(_emit_orphan_speaker(pending, confirmed=confirmed))

    return repair_orphan_nickname_bodies(out, confirmed=confirmed)


def repair_orphan_nickname_bodies(
    messages: list[ChatMessage],
    *,
    confirmed: set[str] | None = None,
) -> list[ChatMessage]:
    """正文居然整句是群昵称/ID：确认发言人 → name+[表情]；否则保留原文。"""
    conf = confirmed or set()
    out: list[ChatMessage] = []
    for m in messages or []:
        t = (m.text or "").strip()
        if m.side == "other" and t:
            # vision 偶发 name 与 text 同为昵称（真贴纸）
            if m.name and t == (m.name or "").strip():
                out.append(m.model_copy(update={"text": "[表情]"}))
                continue
            # 无 name、整句却是已确认发言人
            if not m.name and t in conf:
                out.append(ChatMessage(side="other", text="[表情]", name=t))
                continue
            # 无 name、仅形态像昵称：保留原文（短聊不能幻觉成表情）
        out.append(m)
    return out


def drop_ui_timestamps(messages: list[ChatMessage]) -> list[ChatMessage]:
    """去掉时间条；误标成 other/me 的也删，system 时间同样丢弃。"""
    out: list[ChatMessage] = []
    for m in messages or []:
        if is_ui_timestamp(m.text or ""):
            continue
        out.append(m)
    return out


def scrub_context_noise(ctx: ScreenContext) -> ScreenContext:
    """去掉时间条，并把群昵称折进 name。"""
    msgs = list(ctx.messages or [])
    cleaned = fold_speaker_name_messages(drop_ui_timestamps(msgs))
    if cleaned == msgs:
        return ctx
    return ctx.model_copy(update={"messages": cleaned})


def scrub_context_timestamps(ctx: ScreenContext) -> ScreenContext:
    """兼容旧名 → scrub_context_noise。"""
    return scrub_context_noise(ctx)


def peer_display_names(ctx: ScreenContext) -> set[str]:
    """会话里出现过的对方昵称（禁止当正文/候选开头复读）。"""
    names: set[str] = set()
    for m in ctx.messages or []:
        if m.side != "other":
            continue
        if m.name and len(m.name.strip()) >= 2:
            names.add(m.name.strip())
        t = (m.text or "").strip()
        if looks_like_group_nickname(t):
            names.add(t)
    return names
