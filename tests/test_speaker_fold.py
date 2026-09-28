"""OCR 把群昵称当成消息正文时的折叠。"""

from joy.core.message_noise import fold_speaker_name_messages, looks_like_group_nickname
from joy.core.models import ChatMessage


def test_nickname_looks():
    assert looks_like_group_nickname("陈相")
    assert looks_like_group_nickname("汤桓宇")
    assert looks_like_group_nickname("洒家肉山大魔王")
    assert looks_like_group_nickname("LongCat小助手-布丁@美团")
    assert not looks_like_group_nickname("娘的")
    assert not looks_like_group_nickname("热了我五个月")
    assert not looks_like_group_nickname("说是建学校，是则是卖石头")
    assert not looks_like_group_nickname("这500w太耐用了，用了1个亿了都还有220w")
    # 纯文字陈述绝不能当昵称（否则会被折成 name + [表情]）
    assert not looks_like_group_nickname("这是专门给增加的一趟啊")
    assert not looks_like_group_nickname("智能动车组不错的")
    assert not looks_like_group_nickname("有静音车厢")
    assert not looks_like_group_nickname("@Moky")
    assert not looks_like_group_nickname("卧槽")


def test_fold_plain_text_group_no_forced_emoji():
    """无表情的群聊正文，不能整页变成 [表情]。"""
    raw = [
        ChatMessage(side="other", text="陈相"),
        ChatMessage(side="other", text="卧槽"),
        ChatMessage(side="other", text="这是专门给增加的一趟啊"),
        ChatMessage(side="other", text="@Moky"),
        ChatMessage(side="other", text="汤桓宇"),
        ChatMessage(side="other", text="智能动车组不错的"),
        ChatMessage(side="other", text="汤桓宇"),
        ChatMessage(side="other", text="有静音车厢"),
    ]
    out = fold_speaker_name_messages(raw)
    texts = [m.text for m in out]
    assert "[表情]" not in texts, texts
    assert "卧槽" in texts
    assert "这是专门给增加的一趟啊" in texts
    assert "@Moky" in texts
    assert "智能动车组不错的" in texts
    assert "有静音车厢" in texts
    assert out[0].name == "陈相"
    assert any(m.name == "汤桓宇" for m in out)


def test_fold_chen_xiang_thread():
    raw = [
        ChatMessage(side="other", text="陈相"),
        ChatMessage(side="other", text="今年从五月开始过夏天，一个到10月"),
        ChatMessage(side="other", text="陈相"),
        ChatMessage(side="other", text="娘的"),
        ChatMessage(side="other", text="陈相"),
        ChatMessage(side="other", text="热了我五个月"),
        ChatMessage(side="other", text="汤桓宇"),
        ChatMessage(side="other", text="同AI·土地平整项目滑坡险情具体成因是什么？"),
        ChatMessage(side="other", text="陈相"),
        ChatMessage(side="other", text="说是建学校，是则是卖石头"),
    ]
    out = fold_speaker_name_messages(raw)
    texts = [m.text for m in out]
    assert "陈相" not in texts
    assert "汤桓宇" not in texts
    assert texts[0].startswith("今年从五月")
    assert out[0].name == "陈相"
    assert out[1].text == "娘的" and out[1].name == "陈相"
    assert out[2].text == "热了我五个月" and out[2].name == "陈相"
    assert out[3].name == "汤桓宇"
    assert out[-1].text.startswith("说是建学校") and out[-1].name == "陈相"


def test_fold_long_nicks_longcat_thread():
    raw = [
        ChatMessage(side="other", text="洒家肉山大魔王"),
        ChatMessage(side="other", text="这500w太耐用了，用了1个亿了都还有220w"),
        ChatMessage(side="other", text="LongCat小助手-布丁@美团"),
        ChatMessage(side="other", text="@所有人我们在Opencode的免费活动开始了，为期两周，"),
        ChatMessage(side="other", text="欢迎大家体验～有任何使用问题也欢迎大家反馈"),
        ChatMessage(side="other", text="一帆风顺"),
    ]
    out = fold_speaker_name_messages(raw)
    assert out[0].name == "洒家肉山大魔王"
    assert "500w" in out[0].text
    assert out[1].name == "LongCat小助手-布丁@美团"
    assert out[1].text.startswith("@所有人")
    assert out[2].name == "LongCat小助手-布丁@美团"
    assert out[-1].name == "一帆风顺" and out[-1].text == "[表情]"
    assert all(m.text not in {"洒家肉山大魔王", "一帆风顺"} for m in out)


def test_orphan_yifan_nickname_becomes_emoji():
    """纯表情气泡：只 OCR 到昵称「一帆风顺」→ 不能当正文。"""
    from joy.core.message_noise import scrub_context_noise
    from joy.core.models import ScreenContext
    from joy.providers.reply_quality import echoes_peer_nickname, filter_candidates
    from joy.core.models import Candidate

    raw = [
        ChatMessage(side="other", text="这500w太耐用了，用了1个亿都还有220w", name="酒家肉山大魔王"),
        ChatMessage(side="other", text="一帆风顺"),
    ]
    out = fold_speaker_name_messages(raw)
    assert out[-1].name == "一帆风顺"
    assert out[-1].text == "[表情]"
    assert all(m.text != "一帆风顺" for m in out)

    ctx = scrub_context_noise(
        ScreenContext(title="群", messages=raw, source="ocr_local")
    )
    bad = [
        Candidate(text="一帆风顺，这波稳了", score=0.9, recommended=True),
        Candidate(text="6", score=0.5),
    ]
    kept = filter_candidates(bad, ctx)
    assert all(not (c.text or "").startswith("一帆风顺") for c in kept)
    assert echoes_peer_nickname("一帆风顺，这波稳", ctx)
