from joy.core.dock_bar import (
    DEFAULT_DOCK_BAR_SLOTS,
    DOCK_BAR_USER_SLOTS,
    normalize_dock_bar_slots,
)
from joy.core.config import UiConfig


def test_normalize_default_length() -> None:
    out = normalize_dock_bar_slots(None)
    assert len(out) == DOCK_BAR_USER_SLOTS == 4
    assert out == list(DEFAULT_DOCK_BAR_SLOTS)


def test_normalize_strips_drive_and_dedupe() -> None:
    out = normalize_dock_bar_slots(
        ["analyze", "drive", "analyze", "clear", "detail", "preview"]
    )
    assert "drive" not in out
    assert out[0] == "analyze"
    assert out[1] == "empty"  # duplicate analyze → empty
    assert out[2] == "clear"
    assert len(out) == 4


def test_ui_config_normalizes_slots() -> None:
    ui = UiConfig(dock_bar_slots=["drive_cycle", "nope", "detail", "drive"])
    assert "drive" not in ui.dock_bar_slots
    assert ui.dock_bar_slots == ["empty", "detail", "empty", "empty"]
