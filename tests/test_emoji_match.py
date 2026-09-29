"""微信表情比对：按方案文档的 pHash→SSIM→综合分。"""

from __future__ import annotations

import numpy as np
from PIL import Image

from joy.capture.emoji_match import (
    WeChatEmojiMatcher,
    apply_emoji_matches_to_context,
    match_emojis_in_frame,
    normalize_emoji_patch,
    _load_template_feats,
)
from joy.core.models import ChatMessage, ScreenContext
from joy.core.wechat_emoji import tile_path
from joy.capture.emoji_match import EmojiHit


def test_templates_loaded():
    feats = _load_template_feats()
    assert len(feats) >= 80
    names = {t.name for t in feats}
    assert "666" in names
    assert "微笑" in names


def test_self_match_high():
    p = tile_path("666")
    assert p is not None
    rgba = np.asarray(Image.open(p).convert("RGBA"))
    rgb = rgba[:, :, :3]
    # 合成灰底
    a = rgba[:, :, 3:4].astype(np.float32) / 255.0
    rgb = (rgb.astype(np.float32) * a + 245 * (1 - a)).astype(np.uint8)
    m = WeChatEmojiMatcher().match(rgb)
    assert m is not None
    assert m.name == "666"
    assert m.score >= 0.50  # 全库 Top1，不卡 medium 门槛


def test_paste_official_on_canvas():
    p = tile_path("捂脸")
    assert p is not None
    emoji = Image.open(p).convert("RGBA").resize((40, 40))
    canvas = Image.new("RGB", (800, 600), (255, 255, 255))
    for y in range(60, 480):
        for x in range(240, 780):
            canvas.putpixel((x, y), (245, 245, 245))
    canvas.paste(emoji, (400, 200), emoji)
    hits = match_emojis_in_frame(np.asarray(canvas), contact_left_ratio=0.28)
    codes = [h.code for h in hits]
    assert "捂脸" in codes or any(h.confidence == "high" for h in hits), codes


def test_apply_only_placeholder():
    ctx = ScreenContext(
        title="群",
        messages=[
            ChatMessage(side="other", text="哈哈哈哈哈"),
            ChatMessage(side="other", text="😂😂😂"),
            ChatMessage(side="other", text="[表情]", name="A"),
        ],
    )
    hits = [
        EmojiHit(
            code="666",
            score=0.95,
            y=0.5,
            x=0.4,
            w=30,
            h=30,
            confidence="high",
            margin=0.2,
        )
    ]
    out = apply_emoji_matches_to_context(ctx, hits)
    assert out.messages[0].text == "哈哈哈哈哈"
    assert out.messages[1].text == "😂😂😂"
    assert out.messages[2].text == "[666]"


def test_reconcile_strips_vl_heha_guess():
    from joy.capture.emoji_match import reconcile_vl_emoji_guesses

    ctx = ScreenContext(
        title="Moky",
        messages=[
            ChatMessage(side="other", text="[图片] [嘿哈]"),
            ChatMessage(side="other", text="[图片][嘿哈]"),
            ChatMessage(side="other", text="[嘿哈]"),
        ],
    )
    # 无命中 → 贴纸前缀剥掉后降为 [表情]
    out = reconcile_vl_emoji_guesses(ctx, hits=[])
    assert all(m.text == "[表情]" for m in out.messages), [m.text for m in out.messages]


def test_reconcile_keeps_real_image_bubbles():
    """识图产出的 [图片]/[图片:简述] 在无 Matcher 命中时必须保留，不能降成 [表情]。"""
    from joy.capture.emoji_match import reconcile_vl_emoji_guesses

    ctx = ScreenContext(
        title="Moky",
        messages=[
            ChatMessage(side="other", text="[图片]"),
            ChatMessage(side="other", text="[图片:一只橘猫趴在窗台]"),
            ChatMessage(side="other", text="[图片：海边日落]"),
            ChatMessage(side="other", text="[嘿哈]"),
        ],
    )
    out = reconcile_vl_emoji_guesses(ctx, hits=[])
    texts = [m.text for m in out.messages]
    assert texts == [
        "[图片]",
        "[图片:一只橘猫趴在窗台]",
        "[图片：海边日落]",
        "[表情]",
    ], texts


def test_apply_keeps_image_when_emoji_hits_exist():
    """同屏既有图片又有表情命中时，不能把 [图片:…] 换成 Matcher 标签。"""
    from joy.capture.emoji_match import apply_emoji_matches_to_context

    ctx = ScreenContext(
        title="Moky",
        messages=[
            ChatMessage(side="other", text="[图片:窗外风景]"),
            ChatMessage(side="other", text="[表情]"),
        ],
    )
    hits = [
        EmojiHit(
            code="嘿哈",
            score=0.95,
            y=0.7,
            x=0.4,
            w=30,
            h=30,
            confidence="high",
            margin=0.2,
        )
    ]
    out = apply_emoji_matches_to_context(ctx, hits)
    texts = [m.text for m in out.messages]
    assert texts[0] == "[图片:窗外风景]", texts
    assert texts[1] == "[嘿哈]", texts


def test_reconcile_orders_hits_not_global_set():
    """只有 1 个 medium 嘿哈时，不能三条都变成嘿哈；多出来的 VL 气泡丢掉。"""
    from joy.capture.emoji_match import reconcile_vl_emoji_guesses

    ctx = ScreenContext(
        title="Moky",
        messages=[
            ChatMessage(side="other", text="[图片] [嘿哈]"),
            ChatMessage(side="other", text="[图片] [嘿哈]"),
            ChatMessage(side="other", text="[图片] [嘿哈]"),
        ],
    )
    hits = [
        EmojiHit(
            code="嘿哈",
            score=0.80,
            y=0.6,
            x=0.4,
            w=30,
            h=30,
            confidence="medium",
            margin=0.14,
        )
    ]
    out = reconcile_vl_emoji_guesses(ctx, hits)
    texts = [m.text for m in out.messages]
    assert texts == ["[嘿哈]"], texts

def test_reconcile_joins_same_row_into_one_message():
    """同一气泡里横向多表情 → 拼成一条 [微笑][睡]…"""
    from joy.capture.emoji_match import reconcile_vl_emoji_guesses

    ctx = ScreenContext(
        title="Moky",
        messages=[
            ChatMessage(side="other", text="[捂脸]"),
            ChatMessage(side="other", text="[微笑]"),
        ],
    )
    hits = [
        EmojiHit(code="捂脸", score=0.8, y=0.2, x=0.3, w=30, h=30, confidence="medium"),
        EmojiHit(code="微笑", score=0.75, y=0.55, x=0.30, w=28, h=28, confidence="medium"),
        EmojiHit(code="睡", score=0.78, y=0.55, x=0.55, w=28, h=28, confidence="medium"),
        EmojiHit(code="大哭", score=0.70, y=0.56, x=0.62, w=28, h=28, confidence="medium"),
    ]
    out = reconcile_vl_emoji_guesses(ctx, hits)
    texts = [m.text for m in out.messages]
    assert texts[0] == "[捂脸]"
    assert texts[1] == "[微笑][睡][大哭]"


def test_paste_emoji_row_detects_multiple():
    """画布上横排粘贴多个官方表情，应检出多于 1 个。"""
    names = ["微笑", "撇嘴", "色", "发呆", "得意"]
    canvas = Image.new("RGB", (900, 600), (255, 255, 255))
    for y in range(60, 480):
        for x in range(240, 880):
            canvas.putpixel((x, y), (245, 245, 245))
    x0 = 300
    for name in names:
        p = tile_path(name)
        assert p is not None, name
        emoji = Image.open(p).convert("RGBA").resize((36, 36))
        canvas.paste(emoji, (x0, 220), emoji)
        x0 += 40
    hits = match_emojis_in_frame(np.asarray(canvas), contact_left_ratio=0.28)
    codes = [h.code for h in hits]
    assert len(hits) >= 3, codes
    from joy.capture.emoji_match import group_hits_by_row

    rows = group_hits_by_row(hits)
    assert len(rows) == 1
    assert len(rows[0]) >= 3


def test_y_merge_ocr_text_with_matcher_rows():
    """OCR 只有文字时，Matcher 行按 y 拼进气泡或插入。"""
    from joy.capture.emoji_match import apply_emoji_matches_to_context

    ctx = ScreenContext(
        title="Moky",
        messages=[
            ChatMessage(side="other", text="睡了么", y_norm=0.20),
            ChatMessage(side="other", text="准备睡了", y_norm=0.35),
            ChatMessage(side="other", text="哈哈哈", y_norm=0.48),
        ],
        source="ocr_local",
    )
    hits = [
        EmojiHit(code="害羞", score=0.9, y=0.36, x=0.40, w=40, h=40, confidence="high", margin=0.2),
        EmojiHit(code="撇嘴", score=0.88, y=0.62, x=0.35, w=40, h=40, confidence="high", margin=0.15),
        EmojiHit(code="撇嘴", score=0.87, y=0.62, x=0.42, w=40, h=40, confidence="high", margin=0.15),
    ]
    out = apply_emoji_matches_to_context(ctx, hits)
    texts = [m.text for m in out.messages]
    assert "准备睡了[害羞]" in texts, texts
    assert "[撇嘴][撇嘴]" in texts, texts
    assert "睡了么" in texts
    assert "哈哈哈" in texts


def test_single_emoji_no_side_cleaver():
    """单个表情两侧不能扫出菜刀之类。"""
    p = tile_path("捂脸")
    assert p is not None
    emoji = Image.open(p).convert("RGBA").resize((40, 40))
    canvas = Image.new("RGB", (800, 600), (255, 255, 255))
    for y in range(60, 480):
        for x in range(240, 780):
            canvas.putpixel((x, y), (245, 245, 245))
    canvas.paste(emoji, (400, 200), emoji)
    hits = match_emojis_in_frame(np.asarray(canvas), contact_left_ratio=0.28)
    codes = [h.code for h in hits]
    assert "菜刀" not in codes, codes
    assert len(hits) == 1, codes
    assert hits[0].code == "捂脸"
