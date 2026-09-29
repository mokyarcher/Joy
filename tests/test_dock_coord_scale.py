"""贴边坐标缩放：避免 Win32 半尺寸 / 二次 /dpr。"""

from joy.util.dpi_win import dock_coord_scale, resolve_dock_anchor


def test_resolve_inflates_half_height() -> None:
    # 日志复现：raw_h=380 dpr=2 → 应放大到约 760，而不是锁死 380 或缩成 190
    wr = (100, 100, 500, 480)  # 400x380
    rect, scale, why = resolve_dock_anchor(
        wr=wr, dpr=2.0, screen_logical_h=720
    )
    assert why == "inflate"
    assert scale == 1.0
    assert rect[3] - rect[1] == 760


def test_resolve_keeps_physical_when_tall() -> None:
    wr = (0, 0, 1200, 1370)
    rect, scale, why = resolve_dock_anchor(
        wr=wr, dpr=2.0, screen_logical_h=720
    )
    assert why == "physical"
    assert scale == 2.0
    assert rect[3] - rect[1] == 1370


def test_dock_coord_scale_compat() -> None:
    # inflate 路径 scale 仍为 1
    assert dock_coord_scale(win32_h=380, dpr=2.0, screen_logical_h=720) == 1.0
    assert dock_coord_scale(win32_h=1370, dpr=2.0, screen_logical_h=720) == 2.0
    assert dock_coord_scale(win32_h=680, dpr=1.0) == 1.0
