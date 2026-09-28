from joy.core.chat_presets import (
    DEFAULT_STYLE,
    normalize_relation,
    normalize_style,
    style_prompt,
)
from joy.core.config import ChatConfig, JoyConfig


def test_normalize_known_and_legacy() -> None:
    assert normalize_relation("同事") == "同事"
    assert normalize_relation("职场同事往来") == "同事"
    assert normalize_relation("") == "朋友"
    assert normalize_style("") == DEFAULT_STYLE
    assert normalize_style("正式礼貌") == "正式礼貌"
    assert "正式" in style_prompt("正式礼貌")


def test_chat_config_coerces_legacy_style() -> None:
    cfg = ChatConfig(relation="朋友", style="简洁、口语、少用网络梗")
    assert cfg.style == DEFAULT_STYLE
    full = JoyConfig.model_validate({"chat": {"style": ""}})
    assert full.chat.style == DEFAULT_STYLE
