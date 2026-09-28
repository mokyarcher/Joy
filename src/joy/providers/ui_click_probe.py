"""视觉定位微信 UI 点击点（多选流程等）。"""

from __future__ import annotations

import logging
from typing import Any

from joy.core.config import JoyConfig
from joy.providers.layout_probe import _is_rate_limit, _is_unsupported_effort, _shrink_layout_jpeg
from joy.providers.message_text import message_output_text
from joy.providers.resolve import resolve_vision
from joy.providers.vision import jpeg_to_data_url
from joy.providers.vision_parse import extract_json_object
from joy.util.logging import redact

log = logging.getLogger("joy.ui_click_probe")

_SYSTEM = (
    "你是桌面 UI 点击定位器。根据微信 PC 截图，给出控件中心点的相对坐标。"
    "坐标相对整张图宽高，x/y 均在 0~1。"
    "只输出一个 JSON 对象，不要 Markdown，不要解释。"
)


def _user_prompt(targets: list[str], *, hint: str = "") -> str:
    names = "、".join(f"「{t}」" for t in targets)
    extra = f"\n补充说明：{hint}" if hint else ""
    return f"""在截图中定位下列控件的中心点{extra}：

目标：{names}

规则：
- 「最后一条消息气泡」：消息列表里最靠下那条气泡的**彩色气泡本体正中**（绿/白气泡块中心），不要点气泡下方空白缝、不要点头像、不要点输入框
- 「倒数第二条消息气泡」：倒数第二条气泡的**彩色气泡本体正中**（在最后一条正上方；必须落在气泡颜色块内部，严禁落在两条气泡之间的白色空隙）
- 「多选」：右键菜单里的「多选」文字项中心（仅当菜单已弹出时）；在「收藏」正下方，勿与收藏混淆
- 「选择到这里」：浮层按钮，常带「↓」或「选择到这里」文字
- 「复制」：多选模式底栏工具条上的「复制」（两个重叠方形图标旁的文字），绝不要点「删除」

输出 JSON：
{{"points": {{"倒数第二条消息气泡": {{"x":0.0,"y":0.0}}, "最后一条消息气泡": {{"x":0.0,"y":0.0}}}}}}

找不到的键请省略。x、y 必须是根据截图像素估算的真实值。若同时给出倒数第二条与最后一条，必须满足：倒数第二条.y < 最后一条.y（至少高一截）。
"""


def probe_ui_click_points(
    cfg: JoyConfig,
    jpeg: bytes,
    targets: list[str],
    *,
    hint: str = "",
) -> dict[str, tuple[float, float]]:
    """
    返回 {目标名: (x_ratio, y_ratio)}。
    找不到的目标不会出现在字典里。
    """
    names = [t.strip() for t in targets if (t or "").strip()]
    if not names:
        return {}
    if not jpeg:
        raise ValueError("无截图")

    jpeg_small = _shrink_layout_jpeg(jpeg, max_side=1600, quality=78)
    prov = resolve_vision(cfg)
    client = prov.client(timeout=60.0)
    data_url = jpeg_to_data_url(jpeg_small)
    blob = f"{prov.preset or ''} {prov.model or ''}".lower()
    try_effort = "sensenova" in blob or "deepseek-v4" in blob or "deepseek-flash" in blob

    messages = [
        {"role": "system", "content": _SYSTEM},
        {
            "role": "user",
            "content": [
                {"type": "text", "text": _user_prompt(names, hint=hint)},
                {
                    "type": "image_url",
                    # high：长菜单底部「多选」等小字需要更大有效取样
                    "image_url": {"url": data_url, "detail": "high"},
                },
            ],
        },
    ]
    kwargs: dict[str, Any] = {
        "model": prov.model,
        "max_tokens": 220,
        "temperature": 0,
        "messages": messages,
    }
    if try_effort:
        kwargs["extra_body"] = {"reasoning_effort": "none"}

    try:
        resp = client.chat.completions.create(**kwargs)
    except Exception as exc:
        if "extra_body" in kwargs and _is_unsupported_effort(exc):
            kwargs.pop("extra_body", None)
            resp = client.chat.completions.create(**kwargs)
        elif _is_rate_limit(exc):
            raise ValueError("识图限流，定位点击失败，请稍后再试") from exc
        else:
            raise

    choice = resp.choices[0] if resp.choices else None
    raw, _src = message_output_text(getattr(choice, "message", None) if choice else None)
    if not (raw or "").strip():
        raise ValueError("视觉定位返回空")

    data = extract_json_object(raw)
    pts_obj = data.get("points") if isinstance(data, dict) else None
    if not isinstance(pts_obj, dict):
        pts_obj = data if isinstance(data, dict) else {}

    out: dict[str, tuple[float, float]] = {}
    for key in names:
        node = pts_obj.get(key)
        if not isinstance(node, dict):
            # 容错：模型可能用短名
            for k, v in pts_obj.items():
                if key in str(k) or str(k) in key:
                    node = v
                    break
        if not isinstance(node, dict):
            continue
        try:
            x = float(node["x"])
            y = float(node["y"])
        except Exception:
            continue
        if 0.0 <= x <= 1.0 and 0.0 <= y <= 1.0:
            out[key] = (x, y)

    log.info(
        "ui_click_probe targets=%s found=%s",
        names,
        list(out.keys()),
    )
    if not out:
        raise ValueError(f"未定位到任何目标：{names}；raw={redact(raw[:120])}")
    return out
