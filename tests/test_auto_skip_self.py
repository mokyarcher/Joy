from joy.core.engine import _other_messages_sig, _should_skip_auto_draft
from joy.core.models import ChatMessage, ScreenContext
import joy.core.engine as eng


def _ctx(*pairs: tuple[str, str]) -> ScreenContext:
    msgs = [ChatMessage(side=s, text=t) for s, t in pairs]  # type: ignore[arg-type]
    return ScreenContext(title="t", messages=msgs)


def test_other_sig_ignores_me() -> None:
    a = _ctx(("other", "抽频率"), ("me", "我怕饵料抽完"))
    b = _ctx(("other", "抽频率"), ("me", "我怕饵料抽完"), ("me", "再加一句"))
    assert _other_messages_sig(a) == _other_messages_sig(b)


def test_auto_skip_when_only_me_changed(monkeypatch) -> None:
    eng._last_other_sig = _other_messages_sig(
        _ctx(("other", "抽频率"), ("me", "旧回复"))
    )
    ctx = _ctx(("other", "抽频率"), ("me", "我怕饵料抽完"))
    skip, reason = _should_skip_auto_draft(ctx)
    assert skip
    # 最新是自己 → last_from_me（优先于「对方签名未变」）
    assert reason == "last_from_me"


def test_auto_draft_when_other_new(monkeypatch) -> None:
    eng._last_other_sig = _other_messages_sig(_ctx(("other", "抽频率")))
    ctx = _ctx(("other", "抽频率"), ("me", "嗯"), ("other", "那你慢点抽"))
    skip, reason = _should_skip_auto_draft(ctx)
    assert not skip
    assert reason == ""


def test_cold_start_last_is_me_skips() -> None:
    eng._last_other_sig = ""
    ctx = _ctx(("other", "抽频率"), ("me", "我怕饵料抽完"))
    skip, reason = _should_skip_auto_draft(ctx)
    assert skip
    assert reason == "last_from_me"
    assert eng._last_other_sig == _other_messages_sig(ctx)
