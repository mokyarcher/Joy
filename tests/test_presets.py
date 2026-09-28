from joy.providers.presets import VISION_PRESETS, resolve_endpoint


def test_vision_presets_include_domestic() -> None:
    for pid in ("sensenova", "kimi", "qwen_vl", "zhipu", "siliconflow", "custom"):
        assert pid in VISION_PRESETS


def test_sensenova_endpoint() -> None:
    from joy.providers.presets import TEXT_PRESETS

    url, model = resolve_endpoint(VISION_PRESETS, "sensenova", "", "")
    assert url == "https://token.sensenova.cn/v1"
    assert model == "sensenova-6.8-flash-lite"
    assert "sensenova" in TEXT_PRESETS


def test_resolve_endpoint_override() -> None:
    url, model = resolve_endpoint(VISION_PRESETS, "kimi", "", "")
    assert "moonshot" in url
    assert "vision" in model

    url2, model2 = resolve_endpoint(
        VISION_PRESETS, "custom", "https://example.com/v1", "my-vl"
    )
    assert url2 == "https://example.com/v1"
    assert model2 == "my-vl"


def test_redact_hides_key() -> None:
    from joy.util.logging import redact

    s = redact("Authorization: Bearer sk-secret-abc api_key=sk-xyz")
    assert "sk-secret" not in s
    assert "sk-xyz" not in s
    assert "<redacted>" in s
