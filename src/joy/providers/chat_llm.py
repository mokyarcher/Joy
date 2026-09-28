"""聊天副驾：判断 + 起草（OpenAI 兼容纯文本）。"""

from __future__ import annotations

import logging

from joy.core.chat_presets import relation_prompt, style_prompt
from joy.core.config import JoyConfig
from joy.core.models import Analysis, Candidate, ScreenContext
from joy.providers.api_retry import call_with_retry, is_rate_limit
from joy.providers.message_text import message_output_text
from joy.providers.reply_quality import (
    cold_start_block,
    draft_transcript,
    filter_candidates,
    latest_anchor_block,
    self_memory_block,
    topic_brief_block,
)
from joy.providers.resolve import resolve_analyze, resolve_draft
from joy.providers.vision_parse import extract_json_object
from joy.util.logging import redact

log = logging.getLogger("joy.chat_llm")

_REPLY_RULES = (
    "你正在模拟真实微信里的一个普通成员，不是客服、不是新闻播报、不是总结助手。\n"
    "硬性规则：\n"
    "1. 默认可以不回复：若最新只是附和/感叹/重复旧信息，设 should_reply_now=false，candidates 可为空数组。\n"
    "2. 禁止重新总结已经聊过的事，禁止表现得像第一次听到旧话题。\n"
    "3. 禁止重复「我最近已经说过」里的观点、情绪、问题（换皮复读也不行）。\n"
    "4. 只接最新产生的信息点；没有自然接话点就不要硬接。\n"
    "5. 禁止模板句：注意安全、希望大家平安、我理解你的感受、综上、总结一下等。\n"
    "6. 口语短句，像真人微信；不要解释、不要排比、不要书面腔。\n"
    "7. 错别字极少；不要为了口语故意写错字。\n"
    "8. 不要提及转账/红包/验证码/不明链接；不要编造对方没说过的承诺。\n"
    "9. 发送权在用户，你只写候选内容。\n"
    "10. 若提供了「话题状态」，必须遵守：已知事实不要再当新闻；优先接最新进展。\n"
    "11. 【必须接住】块里的最新原文与信息词必须点名接住；禁止抽象成「人为原因/咋整/怎么办」。\n"
    "12. 对方已纠正的事实（如「不是天灾」）禁止再写回去；有放炮/施工就点名，不要升格成天灾。\n"
    "13. 禁止硬玩谐音梗（塌方≠方了）；禁止编造聊天未出现的声音/画面细节。\n"
    "14. 冷启动/可见历史很短时：别人已连环卧槽就不要再叠惊吓；接不住就 should_reply_now=false。\n"
    "15. 对方昵称/微信名只是发言人标签，绝不是聊天正文；禁止把昵称写进回复开头或当话题（如「一帆风顺，…」）。\n"
    "16. 最新若是纯表情/[表情]/贴纸，可简短附和或 should_reply_now=false；不要复读发送者名字。\n"
)


def _sensenova_extra(prov_preset: str, prov_model: str) -> dict:
    """SenseNova 上的推理/思考模型：尽量关掉长思考，否则 completion 全耗在 reasoning。"""
    blob = f"{prov_preset} {prov_model}".lower()
    if "sensenova" not in blob and "deepseek-v4" not in blob and "deepseek-flash" not in blob:
        return {}
    return {"extra_body": {"reasoning_effort": "none"}}


def _create(
    client, *, model: str, max_tokens: int, temperature: float, messages: list, extra: dict
):
    def _once():
        kwargs = {
            "model": model,
            "max_tokens": max_tokens,
            "temperature": temperature,
            "messages": messages,
            **extra,
        }
        try:
            return client.chat.completions.create(**kwargs)
        except Exception as exc:
            if "extra_body" not in kwargs or is_rate_limit(exc):
                raise
            log.info(
                "llm reasoning_effort rejected, fallback: %s", redact(str(exc)[:100])
            )
            kwargs.pop("extra_body", None)
            return client.chat.completions.create(**kwargs)

    return call_with_retry(_once, retries=2, label="llm")


def _resp_text(resp: object) -> str:
    choice = resp.choices[0] if getattr(resp, "choices", None) else None  # type: ignore[attr-defined]
    msg = getattr(choice, "message", None) if choice else None
    text, src = message_output_text(msg)
    if src not in ("content", "empty"):
        log.info("llm text from message.%s (content empty)", src)
    return text


def _context_pack(
    ctx: ScreenContext, *, topic_block: str = "", tier_block: str = ""
) -> str:
    parts: list[str] = []
    # 最新句硬锚定置顶，避免长上下文把模型冲成空话复读机
    parts.append(latest_anchor_block(ctx))
    cold = cold_start_block(ctx)
    if cold:
        parts.append(cold)
    tb = (topic_block or "").strip()
    if tb:
        parts.append(tb)
    rb = (tier_block or "").strip()
    if rb:
        parts.append(rb)
    parts.append(self_memory_block(ctx))
    parts.append(topic_brief_block(ctx))
    parts.append(f"最近原文（短窗）：\n{draft_transcript(ctx, limit=12)}")
    return "\n\n".join(parts)


def _parse_analysis(data: dict) -> Analysis:
    risk = int(data.get("risk") or 0)
    risk = max(0, min(9, risk))
    conf = data.get("confidence")
    try:
        conf_f = float(conf) if conf is not None else None
    except (TypeError, ValueError):
        conf_f = None
    return Analysis(
        intent=str(data.get("intent") or "").strip(),
        need=str(data.get("need") or "").strip(),
        suggested_action=str(data.get("suggested_action") or "").strip(),
        risk=risk,
        should_reply_now=bool(data.get("should_reply_now", True)),
        confidence=conf_f,
        degraded=False,
    )


def _parse_candidates(data: object, raw: str) -> list[Candidate]:
    items: object
    if isinstance(data, list):
        items = data
    elif isinstance(data, dict):
        items = data.get("candidates") or data.get("replies") or []
    else:
        items = []
    if not isinstance(items, list):
        raise ValueError("candidates 必须是数组")
    out: list[Candidate] = []
    for item in items:
        if isinstance(item, str):
            text, score = item.strip(), None
        elif isinstance(item, dict):
            text = str(item.get("text") or item.get("reply") or "").strip()
            try:
                score = float(item["score"]) if item.get("score") is not None else None
            except (TypeError, ValueError):
                score = None
        else:
            continue
        if text and text.upper() not in {"NO_REPLY", "NOREPLY", "无", "不回", "跳过"}:
            out.append(Candidate(text=text, score=score, recommended=False))
    if not out:
        if isinstance(data, dict) and data.get("should_reply_now") is False:
            return []
        raise ValueError(f"未解析到候选：{redact(raw[:160])}")
    out.sort(key=lambda c: c.score if c.score is not None else -1.0, reverse=True)
    out[0].recommended = True
    return out[:3]


def analyze_chat(
    cfg: JoyConfig, ctx: ScreenContext, *, topic_block: str = "", tier_block: str = ""
) -> Analysis:
    prov = resolve_analyze(cfg)
    client = prov.client(timeout=90.0)
    relation = relation_prompt(cfg.chat.relation)
    style = style_prompt(cfg.chat.style)
    system = (
        "你是聊天意图分析助手。先判断「现在是否自然需要回复」，再给简短意图。\n"
        + _REPLY_RULES
        + "只输出 JSON，不要 Markdown。"
        'schema: {"intent":"string","need":"string","suggested_action":"string",'
        '"risk":0,"should_reply_now":true,"confidence":0.0}'
        "risk 为 0-9 整数分；不要建议转账/红包/支付。"
    )
    user = (
        f"关系：{relation}\n"
        f"我的说话风格：{style}\n"
        f"会话标题：{ctx.title or '（未知）'}\n"
        f"{_context_pack(ctx, topic_block=topic_block, tier_block=tier_block)}\n\n"
        "请输出 JSON。"
    )
    resp = _create(
        client,
        model=prov.model,
        max_tokens=600,
        temperature=0.2,
        messages=[
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
        extra=_sensenova_extra(prov.preset, prov.model),
    )
    raw = _resp_text(resp)
    data = extract_json_object(raw)
    return _parse_analysis(data)


def draft_replies(
    cfg: JoyConfig,
    ctx: ScreenContext,
    analysis: Analysis,
    *,
    topic_block: str = "",
    tier_block: str = "",
) -> list[Candidate]:
    if not analysis.should_reply_now:
        log.info("draft skip: should_reply_now=false")
        return []
    prov = resolve_draft(cfg)
    client = prov.client(timeout=90.0)
    relation = relation_prompt(cfg.chat.relation)
    style = style_prompt(cfg.chat.style)
    cheat = (
        f"对方意图：{analysis.intent}\n"
        f"对方需要：{analysis.need}\n"
        f"建议动作：{analysis.suggested_action}\n"
        f"风险：{analysis.risk}/9\n"
        f"是否立刻回：{analysis.should_reply_now}"
    )
    system = (
        "你是聊天回复起草助手。根据对话与判断小抄写口语候选。\n"
        + _REPLY_RULES
        + '只输出 JSON：{"candidates":[{"text":"...","score":0.0}, ...]}。'
        "不要长篇思考。score 为 0-1。最多 3 条；若不该回则 candidates=[]。"
    )
    user = (
        f"关系：{relation}\n"
        f"说话风格：{style}\n"
        f"会话：{ctx.title or ''}\n"
        f"判断小抄：\n{cheat}\n\n"
        f"{_context_pack(ctx, topic_block=topic_block, tier_block=tier_block)}\n\n"
        "请输出候选 JSON（务必避开「我最近已经说过」与已讨论点；严格遵守回复档位）。"
    )
    resp = _create(
        client,
        model=prov.model,
        max_tokens=800,
        temperature=0.75,
        messages=[
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
        extra=_sensenova_extra(prov.preset, prov.model),
    )
    choice = resp.choices[0] if resp.choices else None
    raw = _resp_text(resp)
    if not raw.strip():
        reason = getattr(choice, "finish_reason", None) if choice else None
        raise ValueError(
            f"模型返回为空" + (f"（finish_reason={reason}）" if reason else "")
        )
    try:
        data = extract_json_object(raw)
    except ValueError:
        import json
        import re

        m = re.search(r"\[[\s\S]*\]", raw)
        if not m:
            raise
        data = {"candidates": json.loads(m.group(0))}
    cands = _parse_candidates(data, raw)
    return filter_candidates(cands, ctx)


def analyze_and_draft(
    cfg: JoyConfig,
    ctx: ScreenContext,
    *,
    topic_block: str = "",
    tier_block: str = "",
) -> tuple[Analysis, list[Candidate]]:
    """
    一轮文本调用同时完成判断 + 候选（少一次 RTT）。
    使用起草模型；失败由上层回退到分步调用。
    """
    prov = resolve_draft(cfg)
    client = prov.client(timeout=90.0)
    relation = relation_prompt(cfg.chat.relation)
    style = style_prompt(cfg.chat.style)
    system = (
        "你是聊天副驾。先判断是否该回，再给口语候选。\n"
        + _REPLY_RULES
        + "只输出一个 JSON 对象，不要 Markdown，不要长篇思考。"
        "schema: {"
        '"intent":"string","need":"string","suggested_action":"string",'
        '"risk":0,"should_reply_now":true,"confidence":0.0,'
        '"candidates":[{"text":"...","score":0.0}]'
        "}。"
        "risk 为 0-9；score 为 0-1。should_reply_now=false 时 candidates 必须为 []。"
    )
    user = (
        f"关系：{relation}\n"
        f"说话风格：{style}\n"
        f"会话：{ctx.title or '（未知）'}\n"
        f"{_context_pack(ctx, topic_block=topic_block, tier_block=tier_block)}\n\n"
        "请输出上述 JSON（严格遵守回复档位）。"
    )
    resp = _create(
        client,
        model=prov.model,
        max_tokens=900,
        temperature=0.6,
        messages=[
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
        extra=_sensenova_extra(prov.preset, prov.model),
    )
    choice = resp.choices[0] if resp.choices else None
    raw = _resp_text(resp)
    if not raw.strip():
        reason = getattr(choice, "finish_reason", None) if choice else None
        raise ValueError(
            f"模型返回为空" + (f"（finish_reason={reason}）" if reason else "")
        )
    data = extract_json_object(raw)
    analysis = _parse_analysis(data)
    if not analysis.should_reply_now:
        log.info("merged draft: should_reply_now=false → no candidates")
        return analysis, []
    try:
        candidates = _parse_candidates(data, raw)
    except ValueError:
        candidates = []
    return analysis, filter_candidates(candidates, ctx)
