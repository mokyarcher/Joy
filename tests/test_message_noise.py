"""微信 UI 时间条不得当成聊天消息。"""

from joy.core.message_noise import (
    drop_ui_timestamps,
    is_ui_timestamp,
    scrub_context_timestamps,
)
from joy.core.models import ChatMessage, ScreenContext


def test_clock_variants():
    assert is_ui_timestamp("10:53")
    assert is_ui_timestamp("9:05")
    assert is_ui_timestamp("10：53")  # 全角冒号
    assert is_ui_timestamp("１０:５３")  # 全角数字
    assert is_ui_timestamp("昨天 10:53")
    assert is_ui_timestamp("上午 9:05")
    assert is_ui_timestamp("2026年9月27日 10:53")
    assert is_ui_timestamp("9月27日")


def test_real_messages_not_time():
    assert not is_ui_timestamp("说是建学校，是则是卖石头")
    assert not is_ui_timestamp("😂")
    assert not is_ui_timestamp("去非洲了吗")
    assert not is_ui_timestamp("10点半见")  # 口语不是时间条
    assert not is_ui_timestamp("53")


def test_drop_from_context():
    ctx = ScreenContext(
        title="群",
        messages=[
            ChatMessage(side="other", text="说是建学校，是则是卖石头", name="陈相"),
            ChatMessage(side="other", text="10:53"),
            ChatMessage(side="other", text="😂", name="陈相"),
        ],
    )
    scrubbed = scrub_context_timestamps(ctx)
    texts = [m.text for m in scrubbed.messages]
    assert texts == ["说是建学校，是则是卖石头", "😂"]
    assert drop_ui_timestamps(ctx.messages)[0].text.startswith("说是")
