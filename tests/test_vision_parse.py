from joy.providers.vision_parse import extract_json_object, screen_context_from_dict


def test_extract_json_plain() -> None:
    data = extract_json_object(
        '{"title":"张三","messages":[{"side":"other","text":"在吗"},{"side":"me","text":"在"}]}'
    )
    ctx = screen_context_from_dict(data)
    assert ctx.title == "张三"
    assert len(ctx.messages) == 2
    assert ctx.messages[0].side == "other"
    assert ctx.messages[1].side == "me"


def test_extract_json_fenced() -> None:
    raw = """```json
{"title":"群聊","messages":[{"side":"other","name":"李四","text":"你好啊，在忙吗？"}]}
```"""
    data = extract_json_object(raw)
    ctx = screen_context_from_dict(data)
    assert ctx.title == "群聊"
    assert ctx.messages[0].name == "李四"
    assert "在忙吗" in ctx.messages[0].text


def test_extract_json_with_prefix_noise() -> None:
    raw = '好的，结果如下：\n{"title":"A","messages":[{"side":"me","text":"hi"}]}'
    data = extract_json_object(raw)
    assert data["title"] == "A"


def test_extract_json_trailing_comma() -> None:
    raw = '{"title":"A","messages":[{"side":"me","text":"hi"},],}'
    data = extract_json_object(raw)
    assert data["title"] == "A"
    assert len(data["messages"]) == 1


def test_extract_json_smart_quotes() -> None:
    raw = '{"title":"张三","messages":[{"side":"other","text":"在吗"}]}'
    # 弯引号包裹 key（常见脏输出）
    dirty = raw.replace('"', "\u201c", 1).replace('"', "\u201d", 1)
    # 上面只换了前两个，改用整段弯引号更贴近真实
    dirty = (
        "{\u201ctitle\u201d:\u201c张三\u201d,"
        "\u201cmessages\u201d:[{\u201cside\u201d:\u201cother\u201d,"
        "\u201ctext\u201d:\u201c在吗\u201d}]}"
    )
    data = extract_json_object(dirty)
    assert data["title"] == "张三"


def test_extract_json_truncated_repaired() -> None:
    # 模拟 max_tokens 截断：缺闭合
    raw = '{"title":"群","messages":[{"side":"other","text":"很长的一句话没写完'
    data = extract_json_object(raw)
    assert data["title"] == "群"
    assert data["messages"][0]["side"] == "other"


def test_extract_json_truncated_mid_next_message() -> None:
    """第 4 条写到一半（图片描述常见）→ 丢掉半截后补 [图片]，避免漏图。"""
    raw = (
        '{"title":"群","messages":['
        '{"side":"other","name":"陈相","text":"15块钱"},'
        '{"side":"other","name":"陈相","text":"剪头发"},'
        '{"side":"other","name":"陈相","text":"不亚于剃了个光头"},'
        '{"'
    )
    data = extract_json_object(raw)
    assert data["title"] == "群"
    assert len(data["messages"]) == 4
    assert data["messages"][2]["text"] == "不亚于剃了个光头"
    assert data["messages"][3]["text"] == "[图片]"
    assert data["messages"][3]["name"] == "陈相"

    raw2 = (
        '{"title":"群","messages":['
        '{"side":"other","name":"陈相","text":"15块钱"},'
        '{"side":"other","name":"陈相","text":"剪头发"},'
        '{"side":"other","name":"陈相","text":"不亚于剃了个光头"},'
        '{"side":"other","name":"name"'
    )
    data2 = extract_json_object(raw2)
    assert len(data2["messages"]) == 4
    assert data2["messages"][-1]["text"] == "[图片]"
    assert data2["messages"][0]["text"] == "15块钱"


def test_skip_empty_text() -> None:
    data = {
        "title": "A",
        "messages": [
            {"side": "other", "text": ""},
            {"side": "me", "text": "hi"},
            {"side": "weird", "text": "x"},
        ],
    }
    ctx = screen_context_from_dict(data)
    assert len(ctx.messages) == 2
    assert ctx.messages[1].side == "other"


def test_messages_hard_cap_8() -> None:
    # 用带标点的完整句子，避免被 fold_speaker 当成群昵称折掉
    msgs = [{"side": "other", "text": f"这是第{i}条消息。"} for i in range(12)]
    ctx = screen_context_from_dict({"title": "T", "messages": msgs})
    assert len(ctx.messages) == 8
    assert ctx.messages[0].text == "这是第4条消息。"


def test_build_vision_system_image_rules() -> None:
    from joy.providers.vision_parse import build_vision_system

    plain = build_vision_system(describe_images=False)
    rich = build_vision_system(describe_images=True)
    assert "[图片]" in plain
    assert "禁止漏图" in plain or "必须" in plain
    assert "≤12字" in rich or "12字" in rich
    assert "[图片:" in rich
    assert plain != rich

