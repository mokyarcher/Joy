"""厂商预设：视觉（识图）与文本（判断/起草）。"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class ProviderPreset:
    id: str
    name: str
    base_url: str
    default_model: str
    kind: str  # "vision" | "text" | "both"
    notes: str = ""
    # 下拉可选模型；空则下拉可编辑自填
    models: tuple[str, ...] = ()


_SENSENOVA_MODELS = (
    "deepseek-v4-flash",
    "sensenova-6.8-flash-lite",
    "deepseek-flash",
    "glm-5.2",
    "kimi-k3",
    "deepseek-v4-pro",
    # Free 套餐通常 403，仍列出方便以后开通
    "deepseek-v4.1-flash",
)


# 视觉 / 识图：国内主流 + 自定义。DeepSeek 官方不在此表（纯文本）。
VISION_PRESETS: dict[str, ProviderPreset] = {
    "sensenova": ProviderPreset(
        id="sensenova",
        name="商汤 SenseNova",
        base_url="https://token.sensenova.cn/v1",
        default_model="deepseek-v4-flash",
        kind="both",
        notes=(
            "OpenAI 兼容；Free 推荐 deepseek-v4-flash / sensenova-6.8-flash-lite。"
            "deepseek-v4.1-flash 当前 Token Plan 常 403。"
        ),
        models=_SENSENOVA_MODELS,
    ),
    "kimi": ProviderPreset(
        id="kimi",
        name="Kimi / Moonshot",
        base_url="https://api.moonshot.cn/v1",
        default_model="moonshot-v1-8k-vision-preview",
        kind="vision",
        notes="官方视觉接口以 Base64 为主，不依赖外链图片 URL。",
        models=(
            "moonshot-v1-8k-vision-preview",
            "moonshot-v1-32k-vision-preview",
        ),
    ),
    "qwen_vl": ProviderPreset(
        id="qwen_vl",
        name="通义千问 VL",
        base_url="https://dashscope.aliyuncs.com/compatible-mode/v1",
        default_model="qwen-vl-plus",
        kind="vision",
        models=("qwen-vl-plus", "qwen-vl-max", "qwen3-vl-plus"),
    ),
    "zhipu": ProviderPreset(
        id="zhipu",
        name="智谱 GLM-V",
        base_url="https://open.bigmodel.cn/api/paas/v4",
        default_model="glm-4v-flash",
        kind="vision",
        notes="性价比高，适合作为默认识图候选。",
        models=("glm-4v-flash", "glm-4v", "glm-4.1v-thinking-flash"),
    ),
    "siliconflow": ProviderPreset(
        id="siliconflow",
        name="硅基流动",
        base_url="https://api.siliconflow.cn/v1",
        default_model="",
        kind="vision",
        notes="在控制台选择带视觉能力的模型 id。",
    ),
    "doubao": ProviderPreset(
        id="doubao",
        name="豆包 / 火山方舟",
        base_url="https://ark.cn-beijing.volces.com/api/v3",
        default_model="",
        kind="vision",
        notes="Base URL / 模型以方舟控制台为准，可改自定义。",
    ),
    "openrouter": ProviderPreset(
        id="openrouter",
        name="OpenRouter",
        base_url="https://openrouter.ai/api/v1",
        default_model="google/gemini-2.0-flash-lite-001",
        kind="both",
        models=(
            "google/gemini-2.0-flash-lite-001",
            "google/gemini-2.0-flash-001",
            "openai/gpt-4o-mini",
        ),
    ),
    "openai": ProviderPreset(
        id="openai",
        name="OpenAI",
        base_url="https://api.openai.com/v1",
        default_model="gpt-4o-mini",
        kind="both",
        models=("gpt-4o-mini", "gpt-4o", "gpt-4.1-mini"),
    ),
    "custom": ProviderPreset(
        id="custom",
        name="自定义 OpenAI 兼容",
        base_url="",
        default_model="",
        kind="both",
        notes="任意中转 / OneAPI / vLLM 等，自填 Base URL 与模型。",
    ),
}


# 判断 / 起草：纯文本。DeepSeek 推荐用于起草。
TEXT_PRESETS: dict[str, ProviderPreset] = {
    "deepseek": ProviderPreset(
        id="deepseek",
        name="DeepSeek 官网",
        base_url="https://api.deepseek.com",
        default_model="deepseek-chat",
        kind="text",
        notes="官方 API；识图请用视觉预设。",
        models=("deepseek-chat", "deepseek-reasoner", "deepseek-flash"),
    ),
    "sensenova": ProviderPreset(
        id="sensenova",
        name="商汤 SenseNova",
        base_url="https://token.sensenova.cn/v1",
        default_model="deepseek-v4-flash",
        kind="both",
        notes=(
            "默认 deepseek-v4-flash（Free 可用）；"
            "deepseek-v4.1-flash 当前套餐常 403。"
        ),
        models=_SENSENOVA_MODELS,
    ),
    "kimi": ProviderPreset(
        id="kimi",
        name="Kimi / Moonshot",
        base_url="https://api.moonshot.cn/v1",
        default_model="moonshot-v1-8k-preview",
        kind="text",
        models=("moonshot-v1-8k-preview", "moonshot-v1-32k-preview", "moonshot-v1-128k-preview"),
    ),
    "qwen": ProviderPreset(
        id="qwen",
        name="通义千问",
        base_url="https://dashscope.aliyuncs.com/compatible-mode/v1",
        default_model="qwen-plus",
        kind="text",
        models=("qwen-plus", "qwen-turbo", "qwen-max"),
    ),
    "zhipu": ProviderPreset(
        id="zhipu",
        name="智谱 GLM",
        base_url="https://open.bigmodel.cn/api/paas/v4",
        default_model="glm-4-flash",
        kind="text",
        models=("glm-4-flash", "glm-4-air", "glm-4-plus"),
    ),
    "siliconflow": ProviderPreset(
        id="siliconflow",
        name="硅基流动",
        base_url="https://api.siliconflow.cn/v1",
        default_model="",
        kind="text",
    ),
    "openrouter": ProviderPreset(
        id="openrouter",
        name="OpenRouter",
        base_url="https://openrouter.ai/api/v1",
        default_model="deepseek/deepseek-chat",
        kind="text",
        models=(
            "deepseek/deepseek-chat",
            "openai/gpt-4o-mini",
            "google/gemini-2.0-flash-001",
        ),
    ),
    "openai": ProviderPreset(
        id="openai",
        name="OpenAI",
        base_url="https://api.openai.com/v1",
        default_model="gpt-4o-mini",
        kind="text",
        models=("gpt-4o-mini", "gpt-4o", "gpt-4.1-mini"),
    ),
    "custom": ProviderPreset(
        id="custom",
        name="自定义 OpenAI 兼容",
        base_url="",
        default_model="",
        kind="text",
    ),
}


def resolve_endpoint(
    presets: dict[str, ProviderPreset],
    preset_id: str,
    base_url: str = "",
    model: str = "",
) -> tuple[str, str]:
    """合并预设与用户覆盖，返回 (base_url, model)。"""
    preset = presets.get(preset_id) or presets["custom"]
    url = (base_url or preset.base_url).rstrip("/")
    mid = model or preset.default_model
    return url, mid
