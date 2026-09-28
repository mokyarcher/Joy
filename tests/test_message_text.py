from types import SimpleNamespace

from joy.providers.message_text import message_output_text


def test_prefers_content() -> None:
    msg = SimpleNamespace(content='{"a":1}', reasoning="thinking...")
    text, src = message_output_text(msg)
    assert src == "content"
    assert text.startswith("{")


def test_falls_back_to_reasoning() -> None:
    msg = SimpleNamespace(
        content="",
        reasoning='{"title":"张三","messages":[]}',
    )
    text, src = message_output_text(msg)
    assert src == "reasoning"
    assert "张三" in text


def test_model_dump_reasoning() -> None:
    class Msg:
        content = None
        reasoning = None

        def model_dump(self) -> dict:
            return {"role": "assistant", "reasoning": '{"title":"A","messages":[]}'}

    text, src = message_output_text(Msg())
    assert src == "reasoning"
    assert "title" in text
