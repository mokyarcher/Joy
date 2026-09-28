"""联系人记忆：合并去重与导入解析。"""

from __future__ import annotations

from pathlib import Path

from joy.core.contact_memory import (
    MemoryRecord,
    enrich_context_from_memory,
    import_transcript,
    merge_records,
    parse_import_text,
)
from joy.core.models import ChatMessage, ScreenContext


def test_merge_overlap_appends_only_new() -> None:
    a = [
        MemoryRecord(side="other", text="你好"),
        MemoryRecord(side="me", text="嗨"),
        MemoryRecord(side="other", text="周五有空吗"),
    ]
    b = [
        MemoryRecord(side="me", text="嗨"),
        MemoryRecord(side="other", text="周五有空吗"),
        MemoryRecord(side="me", text="有啊"),
    ]
    m = merge_records(a, b)
    assert [r.text for r in m] == ["你好", "嗨", "周五有空吗", "有啊"]


def test_parse_import_speakers() -> None:
    raw = "对方: 周五有空吗\n我: 有啊\nMoky: 那吃饭\n"
    msgs = parse_import_text(raw)
    assert len(msgs) == 3
    assert msgs[0].side == "other" and "周五" in msgs[0].text
    assert msgs[1].side == "me"
    assert msgs[2].side == "other" and msgs[2].name == "Moky"


def test_parse_wechat_private_moky() -> None:
    raw = """Moky
2026年09月25日 22:18
我靠喂 。。你在干嘛啊

moky
2026年09月25日 22:25
哈哈刚在摸鱼，被你抓到了

Moky
2026年09月25日 22:26
你胆子挺肥啊

moky
2026年09月25日 22:26
哈哈被你发现了，摸鱼一时爽，被抓更爽
"""
    from joy.core.contact_memory import parse_import_text

    msgs = parse_import_text(raw, default_other_name="Moky")
    assert len(msgs) == 4
    assert msgs[0].side == "other" and "干嘛" in msgs[0].text
    assert msgs[1].side == "me" and "摸鱼" in msgs[1].text
    assert msgs[2].side == "other"
    assert msgs[3].side == "me"


def test_parse_wechat_group_needs_me_name() -> None:
    raw = """红师傅神机妙算
2026年09月25日 21:57
哈哈哈

Moky
2026年09月25日 21:59
@昵称太麻烦了

Moky
2026年09月25日 21:59
[动画表情]

昵称太麻烦了
2026年09月25日 22:01
发了

Moky
2026年09月25日 22:04
[图片] 微信图片_20260925220404_380.dat
"""
    msgs = parse_import_text(
        raw, default_other_name="某群", me_names="昵称太麻烦了"
    )
    assert [m.side for m in msgs] == ["other", "other", "me"]
    assert msgs[0].name == "红师傅神机妙算"
    assert msgs[1].name == "Moky"
    assert "发了" in msgs[2].text


def test_enrich_and_import_roundtrip(tmp_path: Path) -> None:
    title = "测谊联系人"
    root = tmp_path
    ctx = ScreenContext(
        title=title,
        messages=[
            ChatMessage(side="other", text="嗨"),
            ChatMessage(side="me", text="在的"),
        ],
    )
    out = enrich_context_from_memory(
        ctx, prompt_max=30, keep_max=100, root=root, enabled=True
    )
    assert len(out.messages) >= 2

    parsed, added = import_transcript(
        title,
        "对方: 嗨\n我: 在的\n对方: 晚上吃啥\n",
        root=root,
        keep_max=100,
    )
    assert parsed == 3
    assert added >= 1
    out2 = enrich_context_from_memory(
        ScreenContext(title=title, messages=[ChatMessage(side="other", text="晚上吃啥")]),
        prompt_max=30,
        keep_max=100,
        root=root,
        enabled=True,
    )
    texts = [m.text for m in out2.messages]
    assert "晚上吃啥" in texts
    assert "在的" in texts
