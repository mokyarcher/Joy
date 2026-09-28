"""编排引擎：采集 → 识图 → 判断/起草（优先合并一轮 LLM）。"""

from __future__ import annotations

import logging
import re
import time
from collections.abc import Callable

from joy.capture.grabber import CapturedFrame, FrameGrabber
from joy.capture.windows import WindowInfo, pick_default_window
from joy.core.config import JoyConfig
from joy.core.models import Analysis, AnalyzeResult, ScreenContext
from joy.providers.chat_llm import analyze_and_draft, analyze_chat, draft_replies
from joy.providers.vision import VisionResult, parse_chat_image
from joy.util.logging import redact

log = logging.getLogger("joy.engine")

ProgressCb = Callable[[str, int], None]

# 画面指纹 → 识图结果（跨 AnalyzeWorker 复用，跳过未变帧的视觉 API）
_vision_cache: dict[str, ScreenContext] = {}
_VISION_CACHE_MAX = 12

# 自动模式：上次已处理过的「对方消息」签名；无新对方内容则不起草
_last_other_sig: str = ""


def clear_vision_cache() -> None:
    _vision_cache.clear()


def _cache_put(fp: str, ctx: ScreenContext) -> None:
    if not fp:
        return
    _vision_cache[fp] = ctx.model_copy(deep=True)
    while len(_vision_cache) > _VISION_CACHE_MAX:
        _vision_cache.pop(next(iter(_vision_cache)), None)


def _cache_get(fp: str) -> ScreenContext | None:
    if not fp:
        return None
    hit = _vision_cache.get(fp)
    return hit.model_copy(deep=True) if hit is not None else None


def _norm_msg_text(text: str) -> str:
    """弱化识图抖动：空白、常见标点差异不触发「对方新话」。"""
    t = (text or "").strip().lower()
    t = re.sub(r"\s+", "", t)
    t = re.sub(r"[，。！？、,.!?;；:：\"'“”‘’…·\-—_~～]+", "", t)
    return t


def _other_messages_sig(ctx: ScreenContext) -> str:
    """对方气泡签名（最近若干条），用于判断是否有「对面新话」。"""
    parts: list[str] = []
    for m in ctx.messages:
        if m.side != "other":
            continue
        name = _norm_msg_text(m.name or "")
        text = _norm_msg_text(m.text or "")
        if not text:
            continue
        parts.append(f"{name}:{text}")
    return "\n".join(parts[-6:])


def _should_skip_auto_draft(ctx: ScreenContext) -> tuple[bool, str]:
    """
    自动/智驾：只在对方有新内容、且最新气泡是对方时才起草。
    自己发出消息（或智驾刚发出）导致的画面变化 → 必须跳过，否则会「自己跟自己聊」。
    """
    global _last_other_sig
    sig = _other_messages_sig(ctx)
    last_side = ctx.messages[-1].side if ctx.messages else None

    # 1) 最新一条是自己：绝不起草（比对方签名更优先，防 OCR 抖动误判「对方变了」）
    if last_side == "me":
        _last_other_sig = sig
        return True, "last_from_me"

    # 2) 对方内容相对上次无实质变化
    if sig == _last_other_sig:
        return True, "self_or_no_new_other"

    # 3) 冷启动只有自己气泡、还没有对方签名时，上面 last_from_me 已覆盖
    return False, ""


def _remember_other_sig(ctx: ScreenContext) -> None:
    global _last_other_sig
    _last_other_sig = _other_messages_sig(ctx)

class Engine:
    def __init__(self, config: JoyConfig) -> None:
        self.config = config
        self.grabber = FrameGrabber(config.capture)

    def grab_once(self, window: WindowInfo | None = None) -> CapturedFrame | None:
        win = window or pick_default_window()
        if win is None:
            log.warning("no window to grab")
            return None
        # 表情匹配要原图像素；识图 JPEG 仍按 max_image_side 缩小以上传
        attempt = self.grabber.grab_with_reason(
            win,
            want_jpeg=False,
            force=True,
            auto_restore=True,
            resize=False,
        )
        if attempt.frame is None:
            raise RuntimeError(attempt.reason or "截取失败")
        frame = attempt.frame
        from joy.capture.frame_ops import resize_max_side, to_jpeg_bytes

        rgb = frame.rgb
        side = int(getattr(self.config.capture, "max_image_side", 960) or 960)
        quality = int(getattr(self.config.capture, "jpeg_quality", 72) or 72)
        small = resize_max_side(rgb, side) if rgb is not None else None
        jpeg = to_jpeg_bytes(small, quality) if small is not None else None
        try:
            h, w = int(rgb.shape[0]), int(rgb.shape[1])  # type: ignore[union-attr]
            sh, sw = (int(small.shape[0]), int(small.shape[1])) if small is not None else (0, 0)
            log.info(
                "grab full=%sx%s vision_jpeg=%sx%s q=%s",
                w,
                h,
                sw,
                sh,
                quality,
            )
        except Exception:
            pass
        return CapturedFrame(
            rgb=rgb,
            hwnd=frame.hwnd,
            title=frame.title,
            backend=frame.backend,
            frame_hash=frame.frame_hash,
            fingerprint=frame.fingerprint,
            changed=frame.changed,
            jpeg=jpeg,
        )

    def vision_from_frame(
        self, frame: CapturedFrame, *, emoji_hint: str = ""
    ) -> VisionResult:
        jpeg = frame.jpeg
        if not jpeg:
            from joy.capture.frame_ops import to_jpeg_bytes

            jpeg = to_jpeg_bytes(frame.rgb, self.config.capture.jpeg_quality)
        result = parse_chat_image(self.config, jpeg, emoji_hint=emoji_hint)
        if not result.context.title and frame.title:
            result.context.title = frame.title
        return result

    def analyze_once(
        self,
        window: WindowInfo | None = None,
        *,
        on_progress: ProgressCb | None = None,
        auto: bool = False,
        title_hint: str = "",
    ) -> AnalyzeResult:
        def _p(msg: str, pct: int) -> None:
            if on_progress:
                on_progress(msg, max(0, min(100, pct)))

        t0 = time.perf_counter()
        _p("准备截屏…", 8)
        frame = self.grab_once(window)
        if frame is None:
            raise RuntimeError("截取失败：窗口不可用或已最小化")
        t_grab = time.perf_counter()

        fp = (frame.fingerprint or frame.frame_hash or "").strip()
        cached = _cache_get(fp) if auto else None
        vision_ms_note = "cache"
        emoji_hits = []
        crop = self.config.capture.crop
        left = float(getattr(crop, "left", 0.28) or 0.28)
        emoji_hint = ""
        try:
            from joy.capture.emoji_match import (
                format_emoji_match_hint,
                match_emojis_in_frame,
            )

            if frame.rgb is not None and cached is None:
                _p("本地匹配小表情…", 18)
                emoji_hits = match_emojis_in_frame(
                    frame.rgb, contact_left_ratio=max(0.22, left)
                )
                emoji_hint = format_emoji_match_hint(emoji_hits)
        except Exception as exc:
            log.warning("emoji_match failed: %s", redact(str(exc)))
            emoji_hits = []
            emoji_hint = ""

        if cached is not None:
            _p("画面未变，跳过识图…", 42)
            ctx = cached
            if not ctx.title and frame.title:
                ctx.title = frame.title
            log.info("vision cache hit fp=%s…", fp[:16])
        else:
            from joy.core.message_router import route_understand

            prefer_ocr = bool(
                getattr(self.config.chat, "prefer_ocr_text_pipeline", True)
            )
            _p("路由理解路径…", 22)
            decision = route_understand(
                frame_rgb=frame.rgb,
                title_hint=title_hint or "",
                cached=None,
                prefer_ocr=prefer_ocr,
                contact_left_ratio=max(0.22, left),
            )
            if decision.path == "ocr_text" and decision.context is not None:
                ctx = decision.context
                if not ctx.title and (title_hint or frame.title):
                    ctx = ctx.model_copy(
                        update={"title": (title_hint or frame.title or "")}
                    )
                if auto:
                    _cache_put(fp, ctx)
                vision_ms_note = "ocr"
                log.info(
                    "router path=ocr_text reason=%s msgs=%s ocr=%.0fms",
                    decision.reason,
                    len(ctx.messages),
                    decision.ocr_ms,
                )
            else:
                _p("识图中，识别对话气泡…", 28)
                vision = self.vision_from_frame(frame, emoji_hint=emoji_hint)
                ctx = vision.context
                if auto:
                    _cache_put(fp, ctx)
                vision_ms_note = "api"
                log.info("router path=vision reason=%s", decision.reason)
        t_vision = time.perf_counter()

        # 本帧上下文（自动跳过只看这一帧，避免记忆尾部干扰）
        # 微信居中时间条（10:53）绝不能当对方消息
        from joy.core.message_noise import scrub_context_noise

        ctx = scrub_context_noise(ctx)
        # 无论有无命中都要 reconcile：清掉 VL 乱贴的 [图片][嘿哈]
        try:
            from joy.capture.emoji_match import apply_emoji_matches_to_context

            ctx = apply_emoji_matches_to_context(ctx, emoji_hits or [])
        except Exception as exc:
            log.warning("emoji_match apply failed: %s", redact(str(exc)))
        frame_ctx = ctx
        # 顶栏小字 VL 易把 moky/noky 读串：有 title_hint 时优先稳住记忆桶
        from joy.core.contact_memory import prefer_contact_title

        raw_vision_title = (frame_ctx.title or "").strip()
        title_for_mem = prefer_contact_title(raw_vision_title, title_hint or "")
        if title_for_mem and title_for_mem != raw_vision_title:
            frame_ctx = frame_ctx.model_copy(update={"title": title_for_mem})
        elif title_for_mem and not raw_vision_title:
            frame_ctx = frame_ctx.model_copy(update={"title": title_for_mem})

        n = max(1, int(self.config.chat.context_messages))
        if len(frame_ctx.messages) > n:
            frame_ctx = frame_ctx.model_copy(
                update={"messages": frame_ctx.messages[-n:]}
            )

        analysis: Analysis
        candidates: list = []
        draft_err = ""
        degraded = False
        skipped_reason: str | None = None

        # 自动模式：只解析「对面新话」；自己发出的消息引起变化 → 不起草
        if auto:
            skip, reason = _should_skip_auto_draft(frame_ctx)
            if skip:
                skipped_reason = reason
                last_side = frame_ctx.messages[-1].side if frame_ctx.messages else "?"
                _p("对面无新消息，跳过起草…", 90)
                log.info(
                    "auto skip draft reason=%s last_side=%s other_sig_len=%s",
                    reason,
                    last_side,
                    len(_other_messages_sig(frame_ctx)),
                )
                # 跳过起草仍写入记忆（自己发的也要记）
                if bool(getattr(self.config.chat, "memory_enabled", True)):
                    try:
                        from joy.core.contact_memory import enrich_context_from_memory

                        enrich_context_from_memory(
                            frame_ctx,
                            title_hint=title_for_mem,
                            prompt_max=int(
                                getattr(self.config.chat, "memory_prompt_max", 30) or 30
                            ),
                            keep_max=int(
                                getattr(self.config.chat, "memory_keep_max", 400) or 400
                            ),
                            enabled=True,
                        )
                    except Exception as exc:
                        log.warning("memory ingest on skip failed: %s", redact(str(exc)))
                analysis = Analysis(
                    intent="（自动跳过：最新是自己发的消息）"
                    if reason == "last_from_me"
                    else "（自动跳过：无对面新话）",
                    need="",
                    suggested_action="等对方再发再回",
                    risk=0,
                    should_reply_now=False,
                    confidence=1.0,
                    degraded=False,
                )
                t_llm = time.perf_counter()
                grab_ms = (t_grab - t0) * 1000
                vision_ms = (t_vision - t_grab) * 1000
                llm_ms = (t_llm - t_vision) * 1000
                total_ms = (t_llm - t0) * 1000
                log.info(
                    "timing grab=%.0fms vision=%.0fms(%s) llm=%.0fms total=%.0fms cand=0 skip=%s",
                    grab_ms,
                    vision_ms,
                    vision_ms_note,
                    llm_ms,
                    total_ms,
                    reason,
                )
                return AnalyzeResult(
                    context=frame_ctx,
                    analysis=analysis,
                    candidates=[],
                    skipped_reason=skipped_reason,
                    grab_ms=grab_ms,
                    vision_ms=vision_ms,
                    vision_source=vision_ms_note,
                    llm_ms=llm_ms,
                    total_ms=total_ms,
                )

        # 起草用：合并进联系人记忆，再用尾部填满上下文
        ctx = frame_ctx
        if bool(getattr(self.config.chat, "memory_enabled", True)):
            try:
                from joy.core.contact_memory import enrich_context_from_memory

                prompt_max = int(
                    getattr(self.config.chat, "memory_prompt_max", 30) or 30
                )
                # 与旧配置兼容：至少用 context_messages
                prompt_max = max(prompt_max, n)
                ctx = enrich_context_from_memory(
                    frame_ctx,
                    title_hint=title_for_mem,
                    prompt_max=prompt_max,
                    keep_max=int(
                        getattr(self.config.chat, "memory_keep_max", 400) or 400
                    ),
                    enabled=True,
                )
                log.info(
                    "memory prompt title=%s msgs=%s (frame=%s)",
                    ctx.title or "?",
                    len(ctx.messages),
                    len(frame_ctx.messages),
                )
            except Exception as exc:
                log.warning("memory enrich failed: %s", redact(str(exc)))
                ctx = frame_ctx

        # Topic State + 本地 Reply Policy（文档 P2/P3/P4）
        # 先用「更新前」状态做新意判定，再写入新事实（避免最新句与自己比重）
        topic_block = ""
        tier_block = ""
        tier = "normal"
        try:
            from joy.core.contact_memory import normalize_title
            from joy.core.topic_state import (
                decide_reply_tier,
                load_topic,
                local_reply_policy,
                reply_tier_prompt,
                topic_prompt_block,
                update_topic_from_context,
            )

            title_for_topic = normalize_title(ctx.title) or normalize_title(title_for_mem)
            prev_topic = load_topic(title_for_topic) if title_for_topic else None
            pol_reason = ""
            if auto:
                allow, pol_reason = local_reply_policy(frame_ctx, prev_topic)
                log.info(
                    "reply_policy allow=%s reason=%s topic=%s",
                    allow,
                    pol_reason,
                    (prev_topic.topic if prev_topic else "") or "?",
                )
                if not allow:
                    # 仍更新话题/关系记忆，但不打 LLM
                    try:
                        update_topic_from_context(ctx, title_hint=title_for_mem)
                    except Exception:
                        pass
                    try:
                        from joy.core.relation_memory import update_relation_from_context

                        update_relation_from_context(ctx, title_hint=title_for_mem)
                    except Exception:
                        pass
                    skipped_reason = f"policy:{pol_reason}"
                    _p("本地策略：此刻不必回…", 90)
                    analysis = Analysis(
                        intent=f"（本地策略跳过：{pol_reason}）",
                        need="",
                        suggested_action="等有新信息再回",
                        risk=0,
                        should_reply_now=False,
                        confidence=1.0,
                        degraded=False,
                    )
                    _remember_other_sig(frame_ctx)
                    t_llm = time.perf_counter()
                    grab_ms = (t_grab - t0) * 1000
                    vision_ms = (t_vision - t_grab) * 1000
                    llm_ms = (t_llm - t_vision) * 1000
                    total_ms = (t_llm - t0) * 1000
                    log.info(
                        "timing grab=%.0fms vision=%.0fms(%s) llm=%.0fms total=%.0fms cand=0 skip=%s",
                        grab_ms,
                        vision_ms,
                        vision_ms_note,
                        llm_ms,
                        total_ms,
                        skipped_reason,
                    )
                    return AnalyzeResult(
                        context=frame_ctx,
                        analysis=analysis,
                        candidates=[],
                        skipped_reason=skipped_reason,
                        grab_ms=grab_ms,
                        vision_ms=vision_ms,
                        vision_source=vision_ms_note,
                        llm_ms=llm_ms,
                        total_ms=total_ms,
                    )
            topic_st = update_topic_from_context(ctx, title_hint=title_for_mem)
            topic_block = topic_prompt_block(topic_st)
            # 关系画像 + 风格画像（中期）
            try:
                from joy.core.relation_memory import (
                    relation_prompt_block,
                    update_relation_from_context,
                )

                rel = update_relation_from_context(ctx, title_hint=title_for_mem)
                rb = relation_prompt_block(rel)
                if rb:
                    topic_block = f"{topic_block}\n\n{rb}".strip()
            except Exception as exc:
                log.warning("relation memory failed: %s", redact(str(exc)))
            try:
                from joy.core.style_profile import (
                    refresh_style_profile,
                    style_prompt_block,
                )

                style_prof = refresh_style_profile(title_for_mem or ctx.title or "")
                sb = style_prompt_block(style_prof)
                if sb:
                    topic_block = f"{topic_block}\n\n{sb}".strip()
            except Exception as exc:
                log.warning("style profile failed: %s", redact(str(exc)))
            tier = decide_reply_tier(
                frame_ctx, prev_topic or topic_st, policy_reason=pol_reason
            )
            if not auto and tier == "none":
                tier = "normal"
            tier_block = reply_tier_prompt(tier)
            log.info("reply_tier=%s auto=%s", tier, auto)
        except Exception as exc:
            log.warning("topic/policy failed: %s", redact(str(exc)))
            tier = "normal"
            tier_block = ""

        _p("判断并起草候选…", 62)
        try:
            analysis, candidates = analyze_and_draft(
                self.config,
                ctx,
                topic_block=topic_block,
                tier_block=tier_block,
            )
        except Exception as exc:
            log.warning("merged llm failed, fallback 2-step: %s", redact(str(exc)))
            degraded = True
            _p("分步判断中…", 68)
            try:
                analysis = analyze_chat(
                    self.config, ctx, topic_block=topic_block, tier_block=tier_block
                )
            except Exception as exc2:
                log.warning("analyze failed, degrade: %s", redact(str(exc2)))
                analysis = Analysis(
                    intent="（判断失败，盲起草）",
                    need="",
                    suggested_action="请人工判断后再发",
                    risk=0,
                    should_reply_now=False,
                    confidence=None,
                    degraded=True,
                )
            _p("起草候选回复…", 82)
            try:
                candidates = draft_replies(
                    self.config,
                    ctx,
                    analysis,
                    topic_block=topic_block,
                    tier_block=tier_block,
                )
                if candidates:
                    draft_err = ""
            except Exception as exc3:
                log.warning("draft failed: %s", redact(str(exc3)))
                draft_err = redact(str(exc3))
                try:
                    _p("起草重试中…", 88)
                    candidates = draft_replies(
                        self.config,
                        ctx,
                        analysis,
                        topic_block=topic_block,
                        tier_block=tier_block,
                    )
                    if candidates:
                        draft_err = ""
                except Exception as exc4:
                    draft_err = redact(str(exc4))

        if not candidates and not draft_err and analysis.should_reply_now:
            try:
                _p("补起草中…", 88)
                candidates = draft_replies(
                    self.config,
                    ctx,
                    analysis,
                    topic_block=topic_block,
                    tier_block=tier_block,
                )
            except Exception as exc:
                draft_err = redact(str(exc))
                degraded = True

        if degraded:
            analysis.degraded = True

        # 模型判定不必回 / 本地防复读后无候选
        if not analysis.should_reply_now and not candidates:
            skipped_reason = "no_reply"
            log.info("skip draft: should_reply_now=false")
        elif candidates:
            from joy.providers.reply_quality import filter_candidates

            before = len(candidates)
            candidates = filter_candidates(candidates, ctx)
            try:
                from joy.core.style_profile import load_style_profile, rerank_by_style

                sp = load_style_profile(title_for_mem or ctx.title or "")
                candidates = rerank_by_style(candidates, sp)
            except Exception as exc:
                log.warning("style rerank failed: %s", redact(str(exc)))
            if tier == "micro":
                tight = [c for c in candidates if len((c.text or "").strip()) <= 14]
                if tight:
                    candidates = tight
                else:
                    # 档位极短但模型写长了：截断第一条到 10 字内口头感
                    for c in candidates[:1]:
                        t = (c.text or "").strip()
                        if len(t) > 14:
                            c.text = t[:10]
            if before and not candidates:
                log.info("all candidates dropped by local dedup")
            elif before != len(candidates):
                log.info("cand filter %s → %s", before, len(candidates))

        # 无论手动/自动，成功起草后记住对方签名，供下次自动判断
        # 必须用本帧签名，不能用记忆尾部（否则永远对不上）
        _remember_other_sig(frame_ctx)

        t_llm = time.perf_counter()
        grab_ms = (t_grab - t0) * 1000
        vision_ms = (t_vision - t_grab) * 1000
        llm_ms = (t_llm - t_vision) * 1000
        total_ms = (t_llm - t0) * 1000
        log.info(
            "timing grab=%.0fms vision=%.0fms(%s) llm=%.0fms total=%.0fms cand=%s",
            grab_ms,
            vision_ms,
            vision_ms_note,
            llm_ms,
            total_ms,
            len(candidates),
        )

        _p("整理结果…", 96)
        # 解析详情只展示本帧识图；记忆尾部仅用于起草，不塞进 UI
        return AnalyzeResult(
            context=frame_ctx,
            analysis=analysis,
            candidates=candidates,
            draft_error=draft_err or None,
            skipped_reason=skipped_reason,
            grab_ms=grab_ms,
            vision_ms=vision_ms,
            vision_source=vision_ms_note,
            llm_ms=llm_ms,
            total_ms=total_ms,
        )

    def format_detail_text(self, result: AnalyzeResult) -> str:
        """识图对话 + 判断小抄（中间过程，默认折叠展示）。"""
        ctx = result.context
        a = result.analysis
        lines = [
            f"会话：{ctx.title or '(未识别)'}",
            f"来源：{ctx.source}",
            "",
            "—— 对话 ——",
        ]
        if not ctx.messages:
            lines.append("（未识别到消息气泡）")
        for m in ctx.messages:
            who = {"me": "我", "other": "对方", "system": "系统"}.get(m.side, m.side)
            if m.name:
                who = f"{who}/{m.name}"
            lines.append(f"[{who}] {m.text}")

        lines.extend(
            [
                "",
                "—— 判断 ——",
                f"意图：{a.intent or '—'}",
                f"对方需要：{a.need or '—'}",
                f"建议动作：{a.suggested_action or '—'}",
                f"风险：{a.risk}/9",
                f"是否立刻回：{'是' if a.should_reply_now else '否'}",
            ]
        )
        if a.degraded:
            lines.append("（部分步骤降级）")
        if result.skipped_reason:
            tip = {
                "last_from_me": "已跳过起草：最新气泡是自己发的",
                "self_or_no_new_other": "已跳过起草：对方没有新消息",
                "self_send": "已跳过起草：冷启动且最新是自己",
                "no_reply": "已跳过起草：此刻不必硬接话",
            }.get(result.skipped_reason, "")
            if not tip and result.skipped_reason:
                if str(result.skipped_reason).startswith("policy:"):
                    tip = f"已跳过起草：本地策略（{result.skipped_reason[7:]}）"
                else:
                    tip = f"已跳过起草：{result.skipped_reason}"
            if tip:
                lines.append(tip)
        if result.draft_error and not result.candidates:
            lines.extend(["", "—— 起草失败 ——", result.draft_error])
        return "\n".join(lines)

    def format_context_text(self, result: AnalyzeResult) -> str:
        """完整文本（详情 + 候选），兼容调试。"""
        lines = [self.format_detail_text(result), "", "—— 候选回复 ——"]
        if not result.candidates:
            if result.draft_error:
                lines.append(f"（起草失败：{result.draft_error}）")
            else:
                lines.append("（无候选）")
        for i, c in enumerate(result.candidates, 1):
            tag = "【推荐】" if c.recommended else f"{i}."
            score = f" ({c.score:.0%})" if c.score is not None else ""
            lines.append(f"{tag}{score} {c.text}")
        return "\n".join(lines)

    def format_detail_summary(self, result: AnalyzeResult) -> str:
        ctx = result.context
        a = result.analysis
        title = ctx.title or "未命名会话"
        n = len(ctx.messages)
        base = f"{title} · 识别 {n} 条 · 风险 {a.risk}/9"
        if result.draft_error and not result.candidates:
            return f"{base} · 起草失败"
        if not result.candidates:
            return f"{base} · 无候选"
        return f"{base} · 候选 {len(result.candidates)}"
