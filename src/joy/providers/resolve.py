"""解析 Provider 槽位 → OpenAI 兼容客户端。"""

from __future__ import annotations

from dataclasses import dataclass

from openai import OpenAI

from joy.core.config import JoyConfig, ProviderSlotConfig, get_api_key
from joy.providers.presets import TEXT_PRESETS, VISION_PRESETS, resolve_endpoint


@dataclass(frozen=True)
class ResolvedProvider:
    base_url: str
    model: str
    api_key: str
    preset: str

    def client(self, *, timeout: float = 90.0) -> OpenAI:
        if not self.api_key:
            raise ValueError("缺少 API Key")
        if not self.base_url:
            raise ValueError("缺少 Base URL")
        if not self.model:
            raise ValueError("缺少模型 id")
        return OpenAI(api_key=self.api_key, base_url=self.base_url, timeout=timeout)


def _inherit_key(cfg: JoyConfig, slot_name: str) -> str:
    """起草 ← 判断 ← 视觉。"""
    order = {
        "vision": ["vision"],
        "analyze": ["analyze", "vision"],
        "draft": ["draft", "analyze", "vision"],
    }[slot_name]
    for name in order:
        slot: ProviderSlotConfig = getattr(cfg.providers, name)
        key = get_api_key(slot)
        if key:
            return key
    return ""


def resolve_vision(cfg: JoyConfig) -> ResolvedProvider:
    slot = cfg.providers.vision
    url, model = resolve_endpoint(VISION_PRESETS, slot.preset, slot.base_url, slot.model)
    return ResolvedProvider(
        base_url=url,
        model=model,
        api_key=_inherit_key(cfg, "vision"),
        preset=slot.preset,
    )


def resolve_analyze(cfg: JoyConfig) -> ResolvedProvider:
    slot = cfg.providers.analyze
    url, model = resolve_endpoint(TEXT_PRESETS, slot.preset, slot.base_url, slot.model)
    return ResolvedProvider(
        base_url=url,
        model=model,
        api_key=_inherit_key(cfg, "analyze"),
        preset=slot.preset,
    )


def resolve_draft(cfg: JoyConfig) -> ResolvedProvider:
    slot = cfg.providers.draft
    url, model = resolve_endpoint(TEXT_PRESETS, slot.preset, slot.base_url, slot.model)
    return ResolvedProvider(
        base_url=url,
        model=model,
        api_key=_inherit_key(cfg, "draft"),
        preset=slot.preset,
    )


def ping_text_slot(cfg: JoyConfig, slot_name: str) -> str:
    """判断 / 起草槽位的最小连通测试（纯文本）。"""
    if slot_name == "analyze":
        prov = resolve_analyze(cfg)
    elif slot_name == "draft":
        prov = resolve_draft(cfg)
    else:
        raise ValueError(f"不支持的文本槽位: {slot_name}")
    client = prov.client(timeout=60.0)
    resp = client.chat.completions.create(
        model=prov.model,
        max_tokens=32,
        messages=[{"role": "user", "content": "只回复：ok"}],
    )
    choice = resp.choices[0] if resp.choices else None
    text = ""
    if choice is not None and choice.message is not None:
        text = (choice.message.content or "").strip()
    return text


def ping_provider_slot(cfg: JoyConfig, slot_name: str) -> str:
    """按槽位连通测试：识图走 vision，判断/起草走文本。"""
    if slot_name == "vision":
        from joy.providers.vision import ping_vision

        return ping_vision(cfg)
    return ping_text_slot(cfg, slot_name)
