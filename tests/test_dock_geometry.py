from joy.capture.windows import compute_dock_beside


def test_compute_dock_prefers_right() -> None:
    import joy.capture.windows as w

    def fake_rect(_hwnd: int):
        return (100, 50, 700, 650)  # 600x600 wechat

    old = w.window_screen_rect
    w.window_screen_rect = fake_rect  # type: ignore[assignment]
    try:
        geo = compute_dock_beside(
            1, 360, None, gap=4, work=(0, 0, 1600, 900), min_w=280
        )
        assert geo is not None
        x, y, ww, hh = geo
        assert x == 700 + 4
        assert y == 50
        assert ww == 360
        assert hh == 600  # 与微信同高
    finally:
        w.window_screen_rect = old  # type: ignore[assignment]


def test_compute_dock_shrinks_when_right_tight() -> None:
    import joy.capture.windows as w

    def fake_rect(_hwnd: int):
        # 右侧只剩 300px 工作区
        return (100, 40, 900, 640)

    old = w.window_screen_rect
    w.window_screen_rect = fake_rect  # type: ignore[assignment]
    try:
        geo = compute_dock_beside(
            1, 360, None, gap=4, work=(0, 0, 1200, 900), min_w=280
        )
        assert geo is not None
        x, y, ww, hh = geo
        assert x == 900 + 4
        assert ww == 1200 - (900 + 4)  # 缩到剩余空间，不甩到屏幕右缘
        assert hh == 600
        assert y == 40
    finally:
        w.window_screen_rect = old  # type: ignore[assignment]


def test_compute_dock_falls_back_left() -> None:
    import joy.capture.windows as w

    def fake_rect(_hwnd: int):
        return (1000, 40, 1500, 700)

    old = w.window_screen_rect
    w.window_screen_rect = fake_rect  # type: ignore[assignment]
    try:
        geo = compute_dock_beside(
            1, 360, None, gap=4, work=(0, 0, 1600, 900), min_w=280
        )
        assert geo is not None
        x, y, ww, hh = geo
        assert x == 1000 - 4 - 360
        assert y == 40
        assert ww == 360
        assert hh == 660
    finally:
        w.window_screen_rect = old  # type: ignore[assignment]
