"""动态指引步骤与配置字段。"""

from __future__ import annotations

from joy.app.coach_tour import default_dock_steps, default_normal_steps
from joy.core.config import UiConfig


def test_default_steps_non_empty() -> None:
    dock = default_dock_steps()
    normal = default_normal_steps()
    assert len(dock) >= 5
    assert len(normal) >= 4
    assert any("collapse_btn" in s.target_ids for s in dock)
    assert not any("collapse_btn" in s.target_ids for s in normal)
    assert "当前会话" in dock[1].body
    assert dock[0].title
    assert dock[-1].target_ids


def test_ui_config_has_coach_flag() -> None:
    ui = UiConfig()
    assert ui.coach_tour_done is False
    ui.coach_tour_done = True
    assert ui.coach_tour_done is True
