"""微信表情库。"""

from joy.core.wechat_emoji import (
    normalize_emoji_text,
    resolve_code,
    tag,
    vision_emoji_prompt_block,
)


def test_catalog_has_user_list():
    from joy.core.wechat_emoji import emoji_codes, tag

    codes = emoji_codes()
    assert len(codes) >= 100
    for name in ("微笑", "可怜", "666", "旺柴", "破涕为笑", "强", "猪头", "红包"):
        assert name in codes
        assert tag(name) == f"[{name}]"


def test_normalize_aliases():
    assert normalize_emoji_text("[笑哭]") == "[破涕为笑]"
    assert normalize_emoji_text("[doge]") == "[旺柴]"
    assert normalize_emoji_text("[点赞]") == "[强]"
    assert normalize_emoji_text("[666]") == "[666]"
    assert normalize_emoji_text("哈哈[捂脸]行") == "哈哈[捂脸]行"
    assert resolve_code("六六六") == "666"


def test_vision_block_defers_to_matcher():
    block = vision_emoji_prompt_block()
    assert "Matcher" in block
    assert "109/109" in block or "库" in block
    assert "不要猜" in block
