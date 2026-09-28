from joy.capture.windows import is_capture_candidate


def test_exclude_tray_class() -> None:
    assert not is_capture_candidate(1, "WxTrayIconMessageWindow", "微信")
    assert not is_capture_candidate(1, "SomeTrayTip", "提示")
    assert not is_capture_candidate(1, "Tooltip", "x")
