from joy.capture.windows import (
    WindowInfo,
    find_wechat_windows,
    is_capture_candidate,
    is_wechat_satellite_title,
)


def test_exclude_tray_class() -> None:
    assert not is_capture_candidate(1, "WxTrayIconMessageWindow", "微信")
    assert not is_capture_candidate(1, "SomeTrayTip", "提示")
    assert not is_capture_candidate(1, "Tooltip", "x")


def test_satellite_titles() -> None:
    assert is_wechat_satellite_title("")
    assert is_wechat_satellite_title("小微")
    assert is_wechat_satellite_title("小微 AI 测试版")
    assert is_wechat_satellite_title("朋友圈")
    assert not is_wechat_satellite_title("微信")
    assert not is_wechat_satellite_title("刷单工具打王者")


def test_capture_rejects_xiaowei_and_empty() -> None:
    assert not is_capture_candidate(1, "Qt5152QWindowIcon", "")
    assert not is_capture_candidate(1, "Qt5152QWindowIcon", "小微")


def test_find_wechat_drops_satellites() -> None:
    windows = [
        WindowInfo(1, "小微", "Qt5152QWindowIcon", 1, "Weixin.exe"),
        WindowInfo(2, "", "Qt5152QWindowIcon", 1, "Weixin.exe"),
        WindowInfo(3, "刷单工具打王者", "Qt5152QWindowIcon", 1, "Weixin.exe"),
        WindowInfo(4, "朋友圈", "Qt5152QWindowIcon", 1, "Weixin.exe"),
    ]
    # find_wechat_windows(list) 会调 is_minimized(hwnd)；测试用假 hwnd 会失败/True
    # 因此只验证标题过滤逻辑：_accept 路径在无真实窗时 iconic 分支仍看标题
    from joy.capture import windows as wmod

    kept = []
    for win in windows:
        if wmod._is_wechat_process(win.exe_name, win.title, win.class_name) and wmod._accept_wechat_hwnd(
            win.hwnd,
            win.title,
            win.class_name,
            iconic=True,  # 跳过尺寸检查
        ):
            kept.append(win)
    assert [w.hwnd for w in kept] == [3]
