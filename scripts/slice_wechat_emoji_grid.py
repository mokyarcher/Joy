"""从表情面板网格图切出单枚参考图 → assets/wechat_emoji/tiles/。"""

from __future__ import annotations

import shutil
import sys
from pathlib import Path

import numpy as np

# 与 joy.core.wechat_emoji.WECHAT_EMOJI_CODES 前 80 个对齐（8×10）
# 用户提供的网格图行列顺序（与常见微信面板一致）
_GRID_ORDER: tuple[str, ...] = (
    "微笑",
    "撇嘴",
    "色",
    "发呆",
    "得意",
    "流泪",
    "害羞",
    "闭嘴",
    "睡",
    "大哭",
    "尴尬",
    "发怒",
    "调皮",
    "呲牙",
    "惊讶",
    "难过",
    "囧",
    "抓狂",
    "吐",
    "偷笑",
    "愉快",
    "白眼",
    "傲慢",
    "困",
    "惊恐",
    "憨笑",
    "悠闲",
    "咒骂",
    "疑问",
    "嘘",
    "晕",
    "衰",
    "骷髅",
    "敲打",
    "再见",
    "擦汗",
    "抠鼻",
    "鼓掌",
    "坏笑",
    "右哼哼",
    "鄙视",
    "委屈",
    "快哭了",
    "阴险",
    "亲亲",
    "可怜",
    "笑脸",
    "生病",
    "脸红",
    "破涕为笑",
    "恐惧",
    "失望",
    "无语",
    "嘿哈",
    "捂脸",
    "机智",
    "皱眉",
    "耶",
    "吃瓜",
    "加油",
    "汗",
    "天啊",
    "Emm",
    "社会社会",
    "旺柴",
    "好的",
    "打脸",
    "哇",
    "翻白眼",
    "666",
    "让我看看",
    "叹气",
    "苦涩",
    "裂开",
    "奸笑",
    "嘴唇",
    "爱心",
    "心碎",
    "拥抱",
    "强",
)


def _find_content_bbox(gray: np.ndarray) -> tuple[int, int, int, int]:
    """去掉大块浅灰边，返回 (y0,y1,x0,x1)。"""
    # 非近白像素
    mask = gray < 245
    ys, xs = np.where(mask)
    if len(ys) < 10:
        h, w = gray.shape
        return 0, h, 0, w
    y0, y1 = int(ys.min()), int(ys.max()) + 1
    x0, x1 = int(xs.min()), int(xs.max()) + 1
    return y0, y1, x0, x1


def slice_emoji_grid(
    grid_path: Path,
    out_dir: Path,
    *,
    rows: int = 8,
    cols: int = 10,
) -> int:
    from PIL import Image

    img = Image.open(grid_path).convert("RGB")
    arr = np.asarray(img)
    gray = (
        0.299 * arr[:, :, 0] + 0.587 * arr[:, :, 1] + 0.114 * arr[:, :, 2]
    ).astype(np.float32)
    y0, y1, x0, x1 = _find_content_bbox(gray)
    # 略内缩，避开选中框边缘
    pad = 2
    y0, x0 = y0 + pad, x0 + pad
    y1, x1 = max(y0 + 10, y1 - pad), max(x0 + 10, x1 - pad)
    crop = img.crop((x0, y0, x1, y1))
    cw, ch = crop.size
    cell_w = cw / cols
    cell_h = ch / rows

    out_dir.mkdir(parents=True, exist_ok=True)
    n = 0
    for i, code in enumerate(_GRID_ORDER):
        if i >= rows * cols:
            break
        r, c = divmod(i, cols)
        # 再向内缩，去掉格子间隙
        inset = 0.12
        left = int((c + inset) * cell_w)
        upper = int((r + inset) * cell_h)
        right = int((c + 1 - inset) * cell_w)
        lower = int((r + 1 - inset) * cell_h)
        tile = crop.crop((left, upper, right, lower))
        safe = code.replace("/", "_")
        tile.save(out_dir / f"{safe}.png")
        n += 1
    return n


def main(argv: list[str] | None = None) -> int:
    argv = list(argv or sys.argv[1:])
    root = Path(__file__).resolve().parents[1]  # Joy/
    assets = root / "src" / "joy" / "assets" / "wechat_emoji"
    assets.mkdir(parents=True, exist_ok=True)
    grid_dst = assets / "grid.png"

    if argv:
        src = Path(argv[0])
    else:
        # 默认：Cursor 附件里用户刚发的网格图
        cand = Path(
            r"C:\Users\Moky\.cursor\projects\d-GitCode-myproject-d-Joy\assets"
            r"\c__Users_Moky_AppData_Roaming_Cursor_User_workspaceStorage_"
            r"e945c1a610b0e2db16e4173ede11f22c_images_image-dfea4725-6fc5-4863-9e69-01ba01c9af1c.png"
        )
        src = cand if cand.is_file() else None
        if src is None:
            print("usage: slice_wechat_emoji_grid.py <grid.png>")
            return 2

    shutil.copy2(src, grid_dst)
    n = slice_emoji_grid(grid_dst, assets / "tiles")
    print(f"ok: copied grid → {grid_dst}")
    print(f"ok: sliced {n} tiles → {assets / 'tiles'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
