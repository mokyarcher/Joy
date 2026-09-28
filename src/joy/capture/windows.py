"""顶层窗口枚举与微信主窗匹配（排除托盘气泡等附属窗）。"""

from __future__ import annotations

from dataclasses import dataclass

import win32con
import win32gui
import win32process

# 微信附属窗：不能当采集目标
_EXCLUDE_CLASS_SUBSTR = (
    "tray",
    "tooltip",
    "shadow",
    "tip",
    "notify",
    "ime",
)
_EXCLUDE_CLASS_EXACT = {
    "wxtrayiconmessagewindow",
    "traynotifywnd",
    "shell_traywnd",
}
_WECHAT_EXE = {"weixin.exe", "wechat.exe", "wechatapp.exe"}
_WECHAT_TITLE_HINTS = ("微信", "WeChat", "Weixin")
# 主窗类名启发式（微信 4.x Qt）
_WECHAT_MAIN_CLASS_HINTS = ("qt5", "qt6", "wechatmain", "weixin")


def hwnd_dpi_scale(hwnd: int) -> float | None:
    """窗口 DPI 缩放（96→1.0）。失败返回 None，由调用方回退到 Qt。"""
    try:
        import ctypes

        if hwnd and win32gui.IsWindow(hwnd):
            dpi = int(ctypes.windll.user32.GetDpiForWindow(int(hwnd)))
            if dpi >= 96:
                return float(dpi) / 96.0
    except Exception:
        pass
    return None



@dataclass(frozen=True)
class WindowInfo:
    hwnd: int
    title: str
    class_name: str
    pid: int
    exe_name: str = ""

    @property
    def label(self) -> str:
        title = self.title or "(无标题)"
        exe = self.exe_name or "?"
        return f"{title}  [{exe}]  hwnd={self.hwnd}"


def _exe_name(pid: int) -> str:
    try:
        import win32api

        handle = win32api.OpenProcess(
            win32con.PROCESS_QUERY_LIMITED_INFORMATION, False, pid
        )
        try:
            path = win32process.GetModuleFileNameEx(handle, 0)
            return path.rsplit("\\", 1)[-1]
        finally:
            win32api.CloseHandle(handle)
    except Exception:
        return ""


def _window_size(hwnd: int) -> tuple[int, int]:
    try:
        left, top, right, bottom = win32gui.GetWindowRect(hwnd)
        return max(0, right - left), max(0, bottom - top)
    except Exception:
        return 0, 0


def is_capture_candidate(hwnd: int, class_name: str, title: str) -> bool:
    """过滤托盘气泡、极小窗、无标题工具窗。"""
    cls = (class_name or "").lower()
    if cls in _EXCLUDE_CLASS_EXACT:
        return False
    if any(s in cls for s in _EXCLUDE_CLASS_SUBSTR):
        return False
    if "wxtrayicon" in cls:
        return False
    w, h = _window_size(hwnd)
    if w < 200 or h < 200:
        return False
    if not (title or "").strip():
        return False
    return True


def list_top_windows(*, visible_only: bool = True) -> list[WindowInfo]:
    result: list[WindowInfo] = []

    def _cb(hwnd: int, _: object) -> bool:
        if visible_only and not win32gui.IsWindowVisible(hwnd):
            return True
        title = win32gui.GetWindowText(hwnd) or ""
        try:
            class_name = win32gui.GetClassName(hwnd)
        except Exception:
            class_name = ""
        if not is_capture_candidate(hwnd, class_name, title):
            return True
        _, pid = win32process.GetWindowThreadProcessId(hwnd)
        result.append(
            WindowInfo(
                hwnd=int(hwnd),
                title=title,
                class_name=class_name,
                pid=int(pid),
                exe_name=_exe_name(int(pid)),
            )
        )
        return True

    win32gui.EnumWindows(_cb, None)
    result.sort(key=lambda w: w.title.lower())
    return result


def _is_wechat_process(exe: str, title: str, class_name: str = "") -> bool:
    """严格匹配：进程名或标题含微信；不用 Qt 类名（会误伤其它 Qt 应用）。"""
    exe_l = (exe or "").lower()
    if exe_l in _WECHAT_EXE:
        return True
    return any(h in (title or "") for h in _WECHAT_TITLE_HINTS)


def _wechat_score(w: WindowInfo) -> tuple[int, int, int]:
    """越高越像主聊天窗。"""
    cls = w.class_name.lower()
    title = w.title
    ww, hh = _window_size(w.hwnd)
    area = ww * hh
    score = 0
    if any(h in cls for h in _WECHAT_MAIN_CLASS_HINTS):
        score += 50
    if title in ("微信", "WeChat", "Weixin") or title.startswith("微信"):
        score += 30
    if w.exe_name.lower() in _WECHAT_EXE:
        score += 20
    if "tray" in cls or "messagewindow" in cls:
        score -= 100
    return (score, area, ww)


def find_wechat_windows(windows: list[WindowInfo] | None = None) -> list[WindowInfo]:
    """只返回微信相关可采集窗（含最小化主窗）；不含其它应用。"""
    if windows is not None:
        matched = [w for w in windows if _is_wechat_process(w.exe_name, w.title, w.class_name)]
        matched = [w for w in matched if is_capture_candidate(w.hwnd, w.class_name, w.title)
                   or (w.exe_name.lower() in _WECHAT_EXE)]
        matched.sort(key=_wechat_score, reverse=True)
        return matched

    matched: list[WindowInfo] = []

    def _cb(hwnd: int, _: object) -> bool:
        # 含最小化：IsWindowVisible 对 iconic 仍可能为 True；再放宽尺寸给微信进程
        title = win32gui.GetWindowText(hwnd) or ""
        try:
            class_name = win32gui.GetClassName(hwnd)
        except Exception:
            class_name = ""
        _, pid = win32process.GetWindowThreadProcessId(hwnd)
        exe = _exe_name(int(pid))
        if not _is_wechat_process(exe, title, class_name):
            return True
        cls = (class_name or "").lower()
        if cls in _EXCLUDE_CLASS_EXACT or any(s in cls for s in _EXCLUDE_CLASS_SUBSTR):
            return True
        if "wxtrayicon" in cls:
            return True
        # 微信主窗：允许最小化（尺寸可能异常）；仍排除极小工具窗
        ww, hh = _window_size(hwnd)
        iconic = False
        try:
            iconic = bool(win32gui.IsIconic(hwnd))
        except Exception:
            pass
        if not iconic and (ww < 200 or hh < 200):
            return True
        if not (title or "").strip() and not iconic:
            return True
        matched.append(
            WindowInfo(
                hwnd=int(hwnd),
                title=title or "微信",
                class_name=class_name,
                pid=int(pid),
                exe_name=exe,
            )
        )
        return True

    win32gui.EnumWindows(_cb, None)
    matched.sort(key=_wechat_score, reverse=True)
    return matched


def pick_default_window(windows: list[WindowInfo] | None = None) -> WindowInfo | None:
    """只选微信主窗；找不到则返回 None（不再回退到其它应用窗口）。"""
    wechat = (
        find_wechat_windows(windows)
        if windows is not None
        else find_wechat_windows()
    )
    for w in wechat:
        if not is_minimized(w.hwnd):
            return w
    return wechat[0] if wechat else None


def _dwm_visible_rect(hwnd: int) -> tuple[int, int, int, int] | None:
    """DWM 可见外框（不含 Win10/11 不可见 resize 边），物理像素。"""
    try:
        import ctypes
        from ctypes import wintypes

        class RECT(ctypes.Structure):
            _fields_ = (
                ("left", wintypes.LONG),
                ("top", wintypes.LONG),
                ("right", wintypes.LONG),
                ("bottom", wintypes.LONG),
            )

        # DWMWA_EXTENDED_FRAME_BOUNDS = 9
        rect = RECT()
        hr = ctypes.windll.dwmapi.DwmGetWindowAttribute(
            wintypes.HWND(int(hwnd)),
            9,
            ctypes.byref(rect),
            ctypes.sizeof(rect),
        )
        if hr != 0:
            return None
        return int(rect.left), int(rect.top), int(rect.right), int(rect.bottom)
    except Exception:
        return None


def window_screen_rect(hwnd: int) -> tuple[int, int, int, int] | None:
    """窗口外框屏幕坐标 (left, top, right, bottom)，物理像素。最小化/无效时返回 None。

    优先用 DWM 可见外框：GetWindowRect 在 Win10/11 会多出一层不可见边，
    按它贴边会在微信与侧窗之间留出明显黑缝。
    """
    try:
        if not win32gui.IsWindow(hwnd):
            return None
        if win32gui.IsIconic(hwnd):
            return None
        bounds = _dwm_visible_rect(hwnd)
        if bounds is None:
            left, top, right, bottom = win32gui.GetWindowRect(hwnd)
            bounds = int(left), int(top), int(right), int(bottom)
        left, top, right, bottom = bounds
        if left < -10000 or top < -10000:
            return None
        w, h = right - left, bottom - top
        # 过小或离谱尺寸不能用于贴边（最小化残留矩形等）
        if w < 200 or h < 200:
            return None
        return int(left), int(top), int(right), int(bottom)
    except Exception:
        return None


def visible_frame_gap_phys(hwnd_a: int, hwnd_b: int) -> int | None:
    """两窗可见外框水平间隙（b.left - a.right），物理像素；失败返回 None。"""
    a = _dwm_visible_rect(hwnd_a) or window_screen_rect(hwnd_a)
    b = _dwm_visible_rect(hwnd_b) or window_screen_rect(hwnd_b)
    if a is None or b is None:
        return None
    return int(b[0] - a[2])


def dx_to_flush_beside(
    anchor_hwnd: int,
    panel_hwnd: int,
    *,
    gap: int = 0,
) -> int:
    """
    计算 panel 需水平移动的物理像素，使与 anchor 可见外框贴齐。
    panel 在右侧 → 调 panel.left；在左侧 → 调 panel 使 right 贴齐。
    """
    dx, _dy, _dh = flush_deltas_phys(anchor_hwnd, panel_hwnd, gap=gap)
    return dx


def flush_deltas_phys(
    anchor_hwnd: int,
    panel_hwnd: int,
    *,
    gap: int = 0,
) -> tuple[int, int, int]:
    """
    相对 anchor 可见外框，panel 需调整的 (dx, dy, d_h)，均为物理像素。
    d_h > 0 表示 panel 可见高度偏高，应缩小。
    """
    a = _dwm_visible_rect(anchor_hwnd)
    b = _dwm_visible_rect(panel_hwnd)
    if a is None or b is None:
        return 0, 0, 0
    al, at, ar, ab = a
    bl, bt, br, bb = b
    acx = (al + ar) / 2.0
    bcx = (bl + br) / 2.0
    if bcx >= acx:
        dx = int(ar + gap - bl)
    else:
        dx = int(al - gap - br)
    dy = int(at - bt)
    d_h = int((bb - bt) - (ab - at))
    return dx, dy, d_h


def compute_dock_beside(
    anchor_hwnd: int,
    panel_w: int,
    panel_h: int | None = None,
    *,
    gap: int = 0,
    work: tuple[int, int, int, int] | None = None,
    min_w: int = 280,
) -> tuple[int, int, int, int] | None:
    """
    贴锚点窗右侧（不够则左侧）；高度默认与锚点同高。
    坐标与 work 同单位（调用方保证物理或逻辑一致）。
    返回 (x, y, w, h)。
    """
    rect = window_screen_rect(anchor_hwnd)
    if rect is None:
        return None
    al, at, ar, ab = rect
    ah = ab - at
    aw = ar - al
    if ah < 200 or aw < 200:
        return None
    if work is None:
        work = (min(0, al), min(0, at), ar + panel_w + 120, ab + 80)
    wl, wt, wr, wb = work

    # 高度：严格对齐微信外框，再钳进工作区
    h = int(panel_h if panel_h is not None else ah)
    h = max(240, min(h, max(240, wb - wt)))
    y = at
    if y + h > wb:
        y = max(wt, wb - h)
    if y < wt:
        y = wt

    space_right = wr - (ar + gap)
    space_left = (al - gap) - wl
    pref_w = max(min_w, int(panel_w))

    if space_right >= min_w:
        w = min(pref_w, space_right)
        x = ar + gap
    elif space_left >= min_w:
        w = min(pref_w, space_left)
        x = al - gap - w
    elif space_right >= 160:
        w = space_right
        x = ar + gap
    elif space_left >= 160:
        w = space_left
        x = al - gap - w
    else:
        # 实在没边可贴：叠在锚点右侧内侧一点，仍用锚点高度
        w = min(pref_w, max(160, wr - wl))
        x = max(wl, min(ar + gap, wr - w))

    # 禁止盖住微信：右侧贴边时 x 不得小于 ar
    if x < ar and x + w > al:
        x = ar + max(0, gap)

    return int(x), int(y), int(w), int(h)


def client_rect_screen(hwnd: int) -> tuple[int, int, int, int] | None:
    """返回客户区在屏幕坐标下的 (left, top, right, bottom)。"""
    try:
        if not win32gui.IsWindow(hwnd):
            return None
        left, top, right, bottom = win32gui.GetClientRect(hwnd)
        width, height = right - left, bottom - top
        if width <= 0 or height <= 0:
            return None
        sx, sy = win32gui.ClientToScreen(hwnd, (0, 0))
        if sx < -10000 or sy < -10000:
            return None
        return sx, sy, sx + width, sy + height
    except Exception:
        return None


def is_minimized(hwnd: int) -> bool:
    try:
        if not win32gui.IsWindow(hwnd):
            return True
        if win32gui.IsIconic(hwnd):
            return True
        try:
            left, top, right, bottom = win32gui.GetWindowRect(hwnd)
            if left < -10000 or top < -10000:
                return True
            if (right - left) < 80 or (bottom - top) < 80:
                return True
        except Exception:
            pass
        return False
    except Exception:
        return True


def is_hwnd_foreground(hwnd: int) -> bool:
    """hwnd / 其子窗 / 同进程顶层窗 是否为前台（任务栏点回微信时为 True）。"""
    try:
        if not hwnd or not win32gui.IsWindow(hwnd):
            return False
        fg = win32gui.GetForegroundWindow()
        if not fg:
            return False
        if fg == hwnd:
            return True
        # 微信内部子控件有时会拿到焦点
        try:
            if win32gui.GetAncestor(fg, win32con.GA_ROOT) == hwnd:
                return True
        except Exception:
            pass
        try:
            if win32gui.GetParent(fg) == hwnd:
                return True
        except Exception:
            pass
        # 微信多顶层窗：前台属于同一进程也算「微信在前」
        try:
            _tid_t, pid_t = win32process.GetWindowThreadProcessId(hwnd)
            _tid_f, pid_f = win32process.GetWindowThreadProcessId(fg)
            if pid_t and pid_t == pid_f:
                return True
        except Exception:
            pass
        return False
    except Exception:
        return False


def restore_window(hwnd: int) -> bool:
    """若最小化则还原；成功返回 True。"""
    try:
        if not win32gui.IsWindow(hwnd):
            return False
        if win32gui.IsIconic(hwnd):
            win32gui.ShowWindow(hwnd, win32con.SW_RESTORE)
        else:
            try:
                left, top, _, _ = win32gui.GetWindowRect(hwnd)
                if left < -10000 or top < -10000:
                    win32gui.ShowWindow(hwnd, win32con.SW_RESTORE)
            except Exception:
                win32gui.ShowWindow(hwnd, win32con.SW_RESTORE)
        win32gui.ShowWindow(hwnd, win32con.SW_SHOW)
        return not is_minimized(hwnd)
    except Exception:
        return False


def sync_window_title(window: WindowInfo) -> WindowInfo:
    """只更新同一 hwnd 的标题，绝不换绑到其它窗。"""
    try:
        if not win32gui.IsWindow(window.hwnd):
            return window
        title = win32gui.GetWindowText(window.hwnd) or window.title
        if title == window.title:
            return window
        return WindowInfo(
            hwnd=window.hwnd,
            title=title,
            class_name=window.class_name,
            pid=window.pid,
            exe_name=window.exe_name,
        )
    except Exception:
        return window


def _root_hwnd(hwnd: int) -> int:
    try:
        root = win32gui.GetAncestor(hwnd, win32con.GA_ROOT)
        return int(root) if root else int(hwnd)
    except Exception:
        return int(hwnd)


def point_belongs_to_hwnd(hwnd: int, x: int, y: int) -> bool:
    """屏幕坐标 (x,y) 处顶层/祖先是否属于目标 hwnd。"""
    try:
        at = win32gui.WindowFromPoint((int(x), int(y)))
    except Exception:
        return False
    if not at:
        return False
    target = _root_hwnd(hwnd)
    cur = int(at)
    for _ in range(40):
        if cur == int(hwnd) or _root_hwnd(cur) == target:
            return True
        try:
            parent = win32gui.GetParent(cur)
        except Exception:
            break
        if not parent or parent == cur:
            break
        cur = int(parent)
    return False


def is_window_unoccluded(hwnd: int) -> bool:
    """客户区抽样点仍属于该窗（未被 Joy 等其它窗挡住）。最小化视为遮挡。"""
    if is_minimized(hwnd):
        return False
    rect = client_rect_screen(hwnd)
    if rect is None:
        return False
    left, top, right, bottom = rect
    w, h = right - left, bottom - top
    if w < 80 or h < 80:
        return False
    samples = (
        (left + w // 2, top + h // 2),
        (left + w // 4, top + h // 4),
        (left + 3 * w // 4, top + 3 * h // 4),
        (left + w // 2, top + h // 5),
    )
    hits = sum(1 for x, y in samples if point_belongs_to_hwnd(hwnd, x, y))
    return hits >= 2


def ensure_wechat_hwnd(window: WindowInfo) -> str | None:
    """确认 hwnd 仍是微信；异常时返回中文原因，OK 返回 None。"""
    try:
        if not win32gui.IsWindow(window.hwnd):
            return "窗口句柄已失效，请点「刷新」"
        _, pid = win32process.GetWindowThreadProcessId(window.hwnd)
        exe = _exe_name(int(pid))
        title = win32gui.GetWindowText(window.hwnd) or window.title or ""
        if not _is_wechat_process(exe, title):
            return f"目标已不是微信（{exe or title or '?'}），请点「刷新」"
        return None
    except Exception:
        return "无法校验窗口，请点「刷新」"
