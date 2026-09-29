"""视觉识图客户端：OpenAI 兼容 image_url（Base64）。"""

from __future__ import annotations

import base64
import json
import logging
from dataclasses import dataclass
from typing import Any

from joy.core.config import JoyConfig
from joy.core.models import ScreenContext
from joy.providers.api_retry import call_with_retry, is_rate_limit
from joy.providers.message_text import message_output_text
from joy.providers.resolve import ResolvedProvider, resolve_vision
from joy.providers.vision_parse import (
    VISION_USER,
    build_vision_system,
    extract_json_object,
    looks_like_prompt_echo,
    screen_context_from_dict,
)
from joy.util.logging import redact

log = logging.getLogger("joy.vision")

# 视觉默认不用 response_format=json_object：
# SenseNova 上曾稳定复现 content 空 + finish_reason=length，等于白打一轮 ~20s。
_USE_JSON_MODE = False


@dataclass
class VisionResult:
    context: ScreenContext
    raw_text: str
    model: str
    preset: str


def jpeg_to_data_url(jpeg: bytes) -> str:
    b64 = base64.standard_b64encode(jpeg).decode("ascii")
    return f"data:image/jpeg;base64,{b64}"


def _choice_text(choice: Any) -> tuple[str, str | None]:
    """返回 (text, finish_reason)。"""
    if choice is None:
        return "", None
    reason = getattr(choice, "finish_reason", None)
    text, src = message_output_text(getattr(choice, "message", None))
    if src not in ("content", "empty"):
        log.info("vision text from message.%s (content empty)", src)
    return text, reason


def _usage_brief(resp: Any) -> str:
    u = getattr(resp, "usage", None)
    if u is None:
        return "usage=?"
    return (
        f"prompt={getattr(u, 'prompt_tokens', '?')} "
        f"completion={getattr(u, 'completion_tokens', '?')} "
        f"total={getattr(u, 'total_tokens', '?')}"
    )


def _looks_truncated_json(text: str) -> bool:
    """粗判：像 JSON 但扫尾未闭合（模型早停常见）。"""
    s = (text or "").strip()
    if not s.startswith("{"):
        return False
    try:
        json.loads(s)
        return False
    except Exception:
        return True


def parse_chat_image(
    cfg: JoyConfig,
    jpeg: bytes,
    *,
    provider: ResolvedProvider | None = None,
    retry: bool = True,
    emoji_hint: str = "",
) -> VisionResult:
    if not jpeg:
        raise ValueError("截图为空")
    prov = provider or resolve_vision(cfg)
    client = prov.client(timeout=120.0)
    data_url = jpeg_to_data_url(jpeg)
    describe_images = bool(getattr(cfg.chat, "vision_describe_images", False))
    system_prompt = build_vision_system(describe_images=describe_images)
    # SenseNova / DeepSeek V4：尽量关掉长思考，否则 max_tokens 全耗在 reasoning
    blob = f"{prov.preset or ''} {prov.model or ''}".lower()
    try_effort_none = "sensenova" in blob or "deepseek-v4" in blob or "deepseek-flash" in blob

    def _once(
        extra_hint: str = "",
        *,
        detail: str = "low",
        max_tokens: int = 2048,
        reasoning_effort: str | None = None,
    ) -> tuple[str, str | None, str]:
        merged_hint = "\n".join(x for x in (emoji_hint.strip(), extra_hint.strip()) if x)
        user_text = VISION_USER + (f"\n{merged_hint}" if merged_hint else "")
        image_url: dict[str, Any] = {"url": data_url}
        if detail:
            image_url["detail"] = detail
        messages = [
            {"role": "system", "content": system_prompt},
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": user_text},
                    {"type": "image_url", "image_url": image_url},
                ],
            },
        ]
        kwargs: dict[str, Any] = {
            "model": prov.model,
            "max_tokens": max_tokens,
            "temperature": 0,
            "messages": messages,
        }
        if _USE_JSON_MODE:
            kwargs["response_format"] = {"type": "json_object"}
        if reasoning_effort is not None:
            kwargs["extra_body"] = {"reasoning_effort": reasoning_effort}

        def _call():
            try:
                return client.chat.completions.create(**kwargs)
            except Exception as exc:
                # 仅参数不支持时去掉 effort；429 等交给外层退避重试
                if (
                    reasoning_effort is not None
                    and "extra_body" in kwargs
                    and not is_rate_limit(exc)
                ):
                    log.info(
                        "vision reasoning_effort rejected, retry without: %s",
                        redact(str(exc)[:120]),
                    )
                    kwargs.pop("extra_body", None)
                    return client.chat.completions.create(**kwargs)
                raise

        resp = call_with_retry(_call, retries=2, label="vision")
        choice = resp.choices[0] if resp.choices else None
        text, reason = _choice_text(choice)
        usage = _usage_brief(resp)
        trunc = _looks_truncated_json(text)
        # stop+半截 JSON ≠ 撞 max_tokens；length 才是上限
        log.info(
            "vision reply finish_reason=%s %s chars=%s max_tokens=%s truncated_json=%s",
            reason or "?",
            usage,
            len(text or ""),
            max_tokens,
            trunc,
        )
        if trunc and reason == "stop":
            log.warning(
                "vision early-stop mid JSON (finish_reason=stop, not length) chars=%s",
                len(text or ""),
            )
        elif reason == "length":
            log.warning(
                "vision hit max_tokens (finish_reason=length) chars=%s %s",
                len(text or ""),
                usage,
            )
        if not (text or "").strip():
            msg = getattr(choice, "message", None) if choice else None
            keys = ""
            if msg is not None and hasattr(msg, "model_dump"):
                try:
                    dumped = msg.model_dump()
                    keys = ",".join(sorted(str(k) for k, v in dumped.items() if v))
                except Exception:
                    keys = "?"
            log.warning(
                "vision empty content finish_reason=%s %s msg_keys=%s",
                reason,
                usage,
                keys or "-",
            )
        return text, reason, usage

    def _parse_or_raise(raw: str, reason: str | None) -> dict:
        try:
            return extract_json_object(raw)
        except ValueError as exc:
            raise ValueError(
                f"{exc} (len={len(raw or '')}, finish_reason={reason})"
            ) from exc

    effort = "none" if try_effort_none else None
    # 简述图片时输出更长，给足 token；否则 1024 易在第 4 条截断
    tok = 2048 if describe_images else 1024
    # 默认 detail=auto：low 时 deepseek-v4-flash 更容易忽略画面、照抄 prompt
    raw, reason, usage = _once(
        detail="auto", max_tokens=tok, reasoning_effort=effort
    )
    try:
        data = _parse_or_raise(raw, reason)
        ctx = screen_context_from_dict(data)
        if looks_like_prompt_echo(ctx):
            raise ValueError("识图结果疑似照抄示例（在吗/在），非截图内容")
    except ValueError as first_err:
        if not retry:
            raise
        snippet = redact((raw or "").replace("\n", " ")[:180]) or "<empty>"
        log.warning(
            "vision parse/echo retry: finish_reason=%s %s | %s",
            reason or "?",
            usage,
            snippet
            if snippet != "<empty>"
            else f"<empty> finish_reason={reason}",
        )
        hint = (
            "严禁输出「在吗」「在」等示例话。只抄截图里看得见的字。JSON 必须完整可解析。"
            "若画面有照片/大图气泡，必须单独一条 text 为 [图片] 或 [图片:简述]，禁止漏掉。"
        )
        raw, reason, usage = _once(
            hint,
            detail="high",
            max_tokens=max(tok, 2048),
            reasoning_effort="none" if try_effort_none else None,
        )
        try:
            data = _parse_or_raise(raw, reason)
            ctx = screen_context_from_dict(data)
            if looks_like_prompt_echo(ctx):
                raise ValueError("重试后仍像示例照抄，请换模型或提高截图清晰度")
        except ValueError:
            raise first_err from None

    n_img = sum(
        1
        for m in ctx.messages
        if (m.text or "").strip().startswith("[图片")
    )
    log.info(
        "vision ok preset=%s model=%s title=%s msgs=%s images=%s finish_reason=%s %s chars=%s",
        prov.preset,
        prov.model,
        ctx.title[:40],
        len(ctx.messages),
        n_img,
        reason or "?",
        usage,
        len(raw or ""),
    )
    return VisionResult(context=ctx, raw_text=raw, model=prov.model, preset=prov.preset)


def _is_bogus_contact_title(title: str) -> bool:
    """过滤提示词回声 / 占位词，避免顶栏出现「名称」这类鬼字。"""
    t = (title or "").strip()
    if not t:
        return True
    low = t.lower()
    bad = {
        "名称",
        "会话名",
        "联系人",
        "昵称",
        "群名",
        "标题",
        "title",
        "name",
        "unknown",
        "未识别",
        "未知",
        "微信",
        "wechat",
        "weixin",
        "小微",
        "...",
        "…",
    }
    if t in bad or low in bad:
        return True
    if "小微" in t or "AI 测试版" in t or "AI测试版" in t:
        return True
    # 整段都是 JSON 字段名口吻
    if t in {"title", "Title"} or t.startswith("title="):
        return True
    return False


def identify_chat_contact(
    cfg: JoyConfig, jpeg: bytes, *, fallback_full: bool = True
) -> str:
    """轻量识图：只取会话标题/聊天对象名；空返回时重试并可回退完整识图。"""
    if not jpeg:
        raise ValueError("截图为空")
    prov = resolve_vision(cfg)
    client = prov.client(timeout=60.0)
    data_url = jpeg_to_data_url(jpeg)
    system = (
        "你是微信 PC 截图助手。图里是聊天窗口顶部区域。"
        "请读出标题栏里当前会话的真实显示名（联系人昵称或群名，可含省略号与人数）。"
        '只输出一个 JSON 对象，键名固定为 title，值为读到的原文字符串。'
        "不要翻译、不要编造；看不清则 title 用空字符串。"
        "禁止把字段说明、占位词写进 title。"
        "特别注意：英文/数字标题的首字母极易被裁糊（LongCat 勿读成 ongCat；moky 勿读成 noky）。"
        "请逐字母核对开头 2～3 个拉丁字符是否完整；不确定就留空，禁止猜。"
    )
    blob = f"{prov.preset or ''} {prov.model or ''}".lower()
    try_effort_none = (
        "sensenova" in blob or "deepseek-v4" in blob or "deepseek-flash" in blob
    )

    def _once() -> str:
        kwargs: dict[str, Any] = {
            "model": prov.model,
            "max_tokens": 64,
            "temperature": 0,
            "messages": [
                {"role": "system", "content": system},
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "text",
                            "text": "读出顶部会话显示名，只输出 JSON。",
                        },
                        {"type": "image_url", "image_url": {"url": data_url}},
                    ],
                },
            ],
        }
        if try_effort_none:
            kwargs["extra_body"] = {"reasoning_effort": "none"}

        def _call():
            try:
                return client.chat.completions.create(**kwargs)
            except Exception as exc:
                if "extra_body" in kwargs and not is_rate_limit(exc):
                    kwargs.pop("extra_body", None)
                    return client.chat.completions.create(**kwargs)
                raise

        resp = call_with_retry(_call, retries=2, label="vision.contact")
        choice = resp.choices[0] if resp.choices else None
        text, _ = _choice_text(choice)
        return text

    title = ""
    last_err = ""
    for i in range(2):
        try:
            raw = _once()
            if not raw.strip():
                last_err = "模型返回为空"
                log.warning("contact identify empty attempt=%s", i + 1)
                continue
            data = extract_json_object(raw)
            cand = str(data.get("title") or "").strip()
            if _is_bogus_contact_title(cand):
                last_err = f"无效标题: {cand!r}"
                log.warning("contact identify bogus title=%r attempt=%s", cand, i + 1)
                continue
            title = cand
            break
        except Exception as exc:
            last_err = str(exc)
            log.warning("contact identify attempt=%s failed: %s", i + 1, redact(str(exc)))

    if not title and fallback_full:
        # 回退：走完整识图，只取 title（与分析共用同一能力，更稳）
        try:
            vr = parse_chat_image(cfg, jpeg, provider=prov, retry=True)
            cand = (vr.context.title or "").strip()
            if cand and not _is_bogus_contact_title(cand):
                log.info("contact identify via full vision title=%s", cand[:40])
                return cand
            if cand:
                last_err = f"无效标题: {cand!r}"
        except Exception as exc:
            last_err = redact(str(exc))
            log.warning("contact identify full vision failed: %s", last_err)

    if title:
        # 成功识名很常见；默认 debug，避免侧窗轮询刷屏
        log.debug("contact identify title=%s", title[:40])
        return title
    raise ValueError(last_err or "未能识别会话名")


def ping_vision(cfg: JoyConfig) -> str:
    """最小连通测试：1x1 JPEG + 短问。"""
    from PIL import Image
    import io

    img = Image.new("RGB", (8, 8), color=(200, 200, 200))
    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=50)
    prov = resolve_vision(cfg)
    client = prov.client(timeout=60.0)
    data_url = jpeg_to_data_url(buf.getvalue())
    resp = client.chat.completions.create(
        model=prov.model,
        max_tokens=32,
        messages=[
            {
                "role": "user",
                "content": [
                    {"type": "image_url", "image_url": {"url": data_url}},
                    {"type": "text", "text": "只回复：ok"},
                ],
            }
        ],
    )
    text, _ = _choice_text(resp.choices[0] if resp.choices else None)
    return text.strip()
