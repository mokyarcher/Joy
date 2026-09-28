from joy.core.models import Analysis, ChatMessage, ScreenContext
from joy.providers.chat_llm import _transcript


def test_transcript() -> None:
    ctx = ScreenContext(
        title="测",
        messages=[
            ChatMessage(side="other", text="在吗", name="甲"),
            ChatMessage(side="me", text="在"),
        ],
    )
    t = _transcript(ctx)
    assert "对方(甲): 在吗" in t
    assert "我: 在" in t


def test_analysis_model_bounds() -> None:
    a = Analysis(intent="闲聊", risk=9, should_reply_now=True)
    assert a.risk == 9
