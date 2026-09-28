"""微信 UI 布局视觉探测（与业务识图分流）。"""

from __future__ import annotations

import io
import logging
import time
from typing import Any

from joy.core.config import JoyConfig
from joy.core.wechat_layout import (
    PointRatio,
    VertSpan,
    WeChatLayout,
    make_fingerprint,
    put_cached,
    validate_layout,
)
from joy.providers.resolve import resolve_vision
from joy.providers.vision import jpeg_to_data_url
from joy.providers.vision_parse import extract_json_object
from joy.util.logging import redact

log = logging.getLogger("joy.layout_probe")

LAYOUT_SYSTEM = (
    "你是桌面 UI 结构分析器。用户给出微信 PC 主窗客户区截图。"
    "必须根据截图真实像素估算相对坐标。"
    "只输出一个 JSON 对象，不要 Markdown，不要解释，不要推理过程，禁止照抄示例数值。"
)

LAYOUT_USER = """识别微信聊天主界面各区域在整图中的相对位置（相对整图宽高，取值 0~1）。

必须字段：
- compose_area: 底部输入区整体（表情/文件工具条 + 多行输入 + 发送一带）{top,bottom}
- compose_input_center: 文字输入框中心 {x,y}

可选：
- message_area: 中间消息列表 {top,bottom}

约束：
- 请目测截图：compose_area.top 通常在 0.78~0.90，bottom≈1.0；不要用固定模板值
- compose 高度约占整图 12%~28%（多数约 15%~22%）
- message_area 在 compose 上方且 bottom 接近 compose.top
- 禁止输出与任何教程/示例完全相同的数字

只输出 JSON，字段名必须是 compose_area / compose_input_center（及可选 message_area）。
"""

# 旧 prompt 示例值；模型常原样返回，视为无效
_ECHO_COMPOSE_TOP = 0.72
_ECHO_INPUT = (0.62, 0.86)


def _span_from(obj: Any) -> VertSpan | None:
    if not isinstance(obj, dict):
        return None
    try:
        return VertSpan(top=float(obj["top"]), bottom=float(obj["bottom"]))
    except Exception:
        return None


def _point_from(obj: Any) -> PointRatio | None:
    if not isinstance(obj, dict):
        return None
    try:
        return PointRatio(x=float(obj["x"]), y=float(obj["y"]))
    except Exception:
        return None


def layout_from_dict(
    data: dict[str, Any],
    *,
    fingerprint: str,
    client_w: int,
    client_h: int,
    dpr: float,
    model: str,
) -> WeChatLayout:
    compose = _span_from(data.get("compose_area") or data.get("compose"))
    if compose is None:
        raise ValueError("missing compose_area")
    # 轻微钳制
    compose = VertSpan(
        top=max(0.0, min(0.95, compose.top)),
        bottom=max(0.85, min(1.0, compose.bottom)),
    )
    msg = _span_from(data.get("message_area") or data.get("chat_area"))
    center = _point_from(data.get("compose_input_center") or data.get("input_center"))

    # 拒收「照抄旧示例」——两次校准都是 0.72/0.62/0.86，完全没量图
    if (
        abs(compose.top - _ECHO_COMPOSE_TOP) < 0.002
        and center is not None
        and abs(center.x - _ECHO_INPUT[0]) < 0.002
        and abs(center.y - _ECHO_INPUT[1]) < 0.002
    ):
        raise ValueError("模型照抄示例坐标，未识别真实布局")

    layout = WeChatLayout(
        fingerprint=fingerprint,
        client_w=int(client_w),
        client_h=int(client_h),
        dpr=float(dpr),
        message_area=msg,
        compose_area=compose,
        compose_input_center=center,
        model=model or "",
        updated_at=time.time(),
    )
    err = validate_layout(layout)
    if err:
        raise ValueError(err)
    return layout


def _shrink_layout_jpeg(jpeg: bytes, *, max_side: int = 768, quality: int = 55) -> bytes:
    """布局只需结构，压小图省 token，也减少网关截断 data URL。"""
    if not jpeg:
        return jpeg
    try:
        from PIL import Image

        im = Image.open(io.BytesIO(jpeg))
        im = im.convert("RGB")
        w, h = im.size
        side = max(w, h)
        if side > max_side:
            scale = max_side / float(side)
            im = im.resize(
                (max(1, int(w * scale)), max(1, int(h * scale))),
                Image.Resampling.BILINEAR,
            )
        buf = io.BytesIO()
        im.save(buf, format="JPEG", quality=quality, optimize=True)
        out = buf.getvalue()
        if len(out) < len(jpeg):
            return out
    except Exception as exc:
        log.debug("layout jpeg shrink skipped: %s", exc)
    return jpeg


def _is_rate_limit(exc: BaseException) -> bool:
    s = str(exc).lower()
    return "429" in s or "rate" in s or "tpm" in s or "rpm" in s


def _is_unsupported_effort(exc: BaseException) -> bool:
    if _is_rate_limit(exc):
        return False
    s = str(exc).lower()
    keys = (
        "reasoning_effort",
        "extra_body",
        "unexpected keyword",
        "unknown parameter",
        "invalid parameter",
        "unsupported",
    )
    return any(k in s for k in keys)


def _layout_message_text(msg: Any) -> tuple[str, str]:
    """布局探测优先 content；reasoning 仅当其中含 JSON 对象时才用。"""
    from joy.providers.message_text import content_to_text

    if msg is None:
        return "", "empty"
    content = content_to_text(getattr(msg, "content", None)).strip()
    if content and "{" in content:
        return content, "content"
    if content:
        return content, "content"
    for attr in ("reasoning", "reasoning_content"):
        alt = getattr(msg, attr, None)
        if isinstance(alt, str) and alt.strip() and "{" in alt:
            # 若有平衡 JSON 才采纳，避免把思考过程当答案
            try:
                extract_json_object(alt)
                return alt.strip(), attr
            except Exception:
                continue
    return (content or ""), "empty" if not content else "content"


def probe_wechat_layout(
    cfg: JoyConfig,
    jpeg: bytes,
    *,
    client_w: int,
    client_h: int,
    dpr: float,
    save: bool = True,
    rgb: Any = None,
    prefer_pixel: bool = True,
) -> WeChatLayout:
    """整窗客户区 → 布局结构。默认像素扫描优先，失败再走视觉模型。"""
    if not jpeg and rgb is None:
        raise ValueError("截图为空")
    if client_w < 200 or client_h < 200:
        raise ValueError("客户区过小")

    fp = make_fingerprint(client_w, client_h, dpr)

    # —— 主路径：像素找分割线（准、免费、不限流）——
    if prefer_pixel:
        try:
            from joy.capture.layout_pixel import (
                detect_compose_from_jpeg,
                detect_compose_ratios,
            )

            data = None
            if rgb is not None:
                data = detect_compose_ratios(rgb)
            if data is None and jpeg:
                data = detect_compose_from_jpeg(jpeg)
            if data is not None:
                layout = layout_from_dict(
                    data,
                    fingerprint=fp,
                    client_w=client_w,
                    client_h=client_h,
                    dpr=dpr,
                    model="pixel",
                )
                log.info(
                    "layout_probe pixel ok fp=%s compose=%.3f..%.3f",
                    fp,
                    layout.compose_area.top,
                    layout.compose_area.bottom,
                )
                if save:
                    try:
                        put_cached(layout)
                    except Exception as exc:
                        log.warning("layout cache save failed: %s", exc)
                return layout
        except Exception as exc:
            log.info("layout_probe pixel failed, fallback LLM: %s", redact(str(exc)[:120]))

    if not jpeg:
        raise ValueError("像素布局失败且无 JPEG 可回退模型")

    # —— 回退：视觉模型 ——
    jpeg_small = _shrink_layout_jpeg(jpeg)
    prov = resolve_vision(cfg)
    client = prov.client(timeout=90.0)
    data_url = jpeg_to_data_url(jpeg_small)
    blob = f"{prov.preset or ''} {prov.model or ''}".lower()
    try_effort = "sensenova" in blob or "deepseek-v4" in blob or "deepseek-flash" in blob

    messages = [
        {"role": "system", "content": LAYOUT_SYSTEM},
        {
            "role": "user",
            "content": [
                {"type": "text", "text": LAYOUT_USER},
                {
                    "type": "image_url",
                    "image_url": {"url": data_url, "detail": "low"},
                },
            ],
        },
    ]
    kwargs: dict[str, Any] = {
        "model": prov.model,
        "max_tokens": 256,
        "temperature": 0,
        "messages": messages,
    }
    if try_effort:
        kwargs["extra_body"] = {"reasoning_effort": "none"}

    try:
        resp = client.chat.completions.create(**kwargs)
    except Exception as exc:
        if "extra_body" in kwargs and _is_unsupported_effort(exc):
            log.info("layout_probe effort unsupported: %s", redact(str(exc)[:100]))
            kwargs.pop("extra_body", None)
            resp = client.chat.completions.create(**kwargs)
        elif _is_rate_limit(exc):
            raise ValueError("识图接口限流，请稍后再校准") from exc
        else:
            raise

    choice = resp.choices[0] if resp.choices else None
    raw, src = _layout_message_text(getattr(choice, "message", None) if choice else None)
    if src not in ("content", "empty"):
        log.info("layout_probe text from message.%s", src)
    if not (raw or "").strip() or "{" not in raw:
        raise ValueError(
            "模型未返回布局 JSON（content 为空或只有思考）。可稍后点「重新校准微信布局」"
        )

    data = extract_json_object(raw)
    layout = layout_from_dict(
        data,
        fingerprint=fp,
        client_w=client_w,
        client_h=client_h,
        dpr=dpr,
        model=prov.model,
    )
    log.info(
        "layout_probe llm ok fp=%s compose=%.3f..%.3f model=%s jpeg=%s",
        fp,
        layout.compose_area.top,
        layout.compose_area.bottom,
        prov.model,
        len(jpeg_small),
    )
    if save:
        try:
            put_cached(layout)
        except Exception as exc:
            log.warning("layout cache save failed: %s", exc)
    return layout
