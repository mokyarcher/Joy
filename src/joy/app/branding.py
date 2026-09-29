"""Joy 品牌图标：程序绘制，无外部设计稿依赖。"""

from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import QPointF, QRectF, Qt
from PySide6.QtGui import QColor, QIcon, QImage, QPainter, QPen, QPixmap


# 微信绿，与副驾 UI 一致
JOY_GREEN = QColor("#07C160")
JOY_GREEN_DARK = QColor("#06AD56")


def _paint_joy_mark(p: QPainter, size: int) -> None:
    """圆角绿底 + 白「看屏侧窗」剪影（微信窗 + 右侧副驾条）。

    绿底尽量铺满画布（接近微信图标的视觉大小），仅留极少抗锯齿边。
    """
    p.setRenderHint(QPainter.RenderHint.Antialiasing, True)
    s = float(size)
    # 原先 8% 留白会让任务栏上看起来明显小于微信；压到约 1.5%
    m = max(1.0, s * 0.015)
    tile = QRectF(m, m, s - 2 * m, s - 2 * m)
    tw = tile.width()
    th = tile.height()
    radius = tw * 0.22

    # 底：圆角方块（任务栏比纯圆更像正式 App）
    p.setPen(Qt.PenStyle.NoPen)
    p.setBrush(JOY_GREEN)
    p.drawRoundedRect(tile, radius, radius)

    # 轻微内阴影感：内圈略深描边
    rim = QPen(JOY_GREEN_DARK, max(1.0, tw * 0.02))
    p.setPen(rim)
    p.setBrush(Qt.BrushStyle.NoBrush)
    inset = tw * 0.015
    p.drawRoundedRect(
        tile.adjusted(inset, inset, -inset, -inset),
        radius * 0.92,
        radius * 0.92,
    )

    # 白色内容区：左侧聊天窗 + 右侧细侧窗（相对绿底比例）
    ink = QColor("#FFFFFF")
    stroke = max(1.2, tw * 0.048)
    pen = QPen(
        ink,
        stroke,
        Qt.PenStyle.SolidLine,
        Qt.PenCapStyle.RoundCap,
        Qt.PenJoinStyle.RoundJoin,
    )
    p.setPen(pen)
    p.setBrush(Qt.BrushStyle.NoBrush)

    left = tile.left() + tw * 0.17
    top = tile.top() + th * 0.19
    win_w = tw * 0.46
    win_h = th * 0.54
    win_r = tw * 0.065
    chat = QRectF(left, top, win_w, win_h)
    p.drawRoundedRect(chat, win_r, win_r)

    # 聊天窗内两行消息线
    line_pen = QPen(
        ink, max(1.0, tw * 0.038), Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap
    )
    p.setPen(line_pen)
    y1 = top + win_h * 0.32
    y2 = top + win_h * 0.52
    x0 = left + win_w * 0.18
    p.drawLine(QPointF(x0, y1), QPointF(left + win_w * 0.72, y1))
    p.drawLine(QPointF(x0, y2), QPointF(left + win_w * 0.55, y2))

    # 右侧副驾侧窗条
    p.setPen(pen)
    dock_x = left + win_w + tw * 0.055
    dock = QRectF(dock_x, top + th * 0.04, tw * 0.145, win_h - th * 0.04)
    p.drawRoundedRect(dock, tw * 0.04, tw * 0.04)

    # 侧窗中间小圆点（「在线 / 驾驶」）
    p.setPen(Qt.PenStyle.NoPen)
    p.setBrush(ink)
    dot_r = tw * 0.036
    p.drawEllipse(
        QPointF(dock.center().x(), dock.top() + dock.height() * 0.35),
        dot_r,
        dot_r,
    )


def render_joy_pixmap(size: int = 256) -> QPixmap:
    pm = QPixmap(size, size)
    pm.fill(Qt.GlobalColor.transparent)
    p = QPainter(pm)
    _paint_joy_mark(p, size)
    p.end()
    return pm


def make_app_icon() -> QIcon:
    """多尺寸图标，供窗口 / 托盘 / 任务栏使用。"""
    icon = QIcon()
    for sz in (16, 20, 24, 32, 40, 48, 64, 128, 256):
        icon.addPixmap(render_joy_pixmap(sz))
    return icon


def asset_icon_path() -> Path:
    return Path(__file__).resolve().parent.parent / "assets" / "app" / "joy.ico"


def load_app_icon() -> QIcon:
    """优先磁盘 .ico（打包友好）；没有则现场绘制。"""
    ico = asset_icon_path()
    if ico.is_file():
        icon = QIcon(str(ico))
        if not icon.isNull():
            # 再补绘制尺寸，避免某些环境只读到单档
            painted = make_app_icon()
            for sz in (16, 24, 32, 48, 64, 128, 256):
                icon.addPixmap(painted.pixmap(sz, sz))
            return icon
    return make_app_icon()


def _pixmap_to_pil(pm: QPixmap):
    from PIL import Image

    if hasattr(Image, "fromqpixmap"):
        img = Image.fromqpixmap(pm)
        return img.convert("RGBA") if img.mode != "RGBA" else img
    qimg = pm.toImage().convertToFormat(QImage.Format.Format_RGBA8888)
    w, h = qimg.width(), qimg.height()
    bpl = int(qimg.bytesPerLine())
    raw = bytes(qimg.constBits())[: bpl * h]
    if bpl == w * 4:
        buf = raw
    else:
        rows = [raw[y * bpl : y * bpl + w * 4] for y in range(h)]
        buf = b"".join(rows)
    return Image.frombytes("RGBA", (w, h), buf)


def write_icon_files(out_dir: Path | None = None) -> tuple[Path, Path]:
    """生成 joy.png + joy.ico，供 PyInstaller / 资源提交。"""
    out = out_dir or (Path(__file__).resolve().parent.parent / "assets" / "app")
    out.mkdir(parents=True, exist_ok=True)
    png_path = out / "joy.png"
    ico_path = out / "joy.ico"

    big = render_joy_pixmap(256)
    big.save(str(png_path), "PNG")

    # Windows 资源 / 资源管理器更吃 BMP 档 ICO；纯 PNG 压缩 ICO 常显示成默认 Python 盘符
    # 16～128 用 BMP；256 仍可进容器（Pillow 对超大档会按平台处理）
    sizes = (256, 128, 64, 48, 32, 24, 16)
    frames = [_pixmap_to_pil(render_joy_pixmap(s)).convert("RGBA") for s in sizes]
    frames[0].save(
        str(ico_path),
        format="ICO",
        append_images=frames[1:],
        sizes=[(s, s) for s in sizes],
        bitmap_format="bmp",
    )
    return png_path, ico_path


def apply_process_branding(app) -> None:
    """Windows 任务栏独立分组 + 全局窗口图标。"""
    try:
        import ctypes

        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(  # type: ignore[attr-defined]
            "Joy.WeChatCopilot.1"
        )
    except Exception:
        pass
    app.setWindowIcon(load_app_icon())
