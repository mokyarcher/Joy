"""把用户提供的官方表情 PNG 导入 official/，并报告覆盖率。

用法:
  python scripts/import_wechat_emoji_official.py <源目录>
  python scripts/import_wechat_emoji_official.py   # 只检查 official/ 缺哪些
"""

from __future__ import annotations

import re
import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from joy.core.wechat_emoji import WECHAT_EMOJI_CODES, assets_dir, resolve_code


def official_dir() -> Path:
    d = assets_dir() / "official"
    d.mkdir(parents=True, exist_ok=True)
    return d


def stem_to_code(stem: str) -> str | None:
    s = (stem or "").strip()
    # [微笑] / 微笑 / emoji_微笑
    m = re.fullmatch(r"\[(.+)\]", s)
    if m:
        s = m.group(1)
    if s.endswith(".png"):
        s = s[:-4]
    code = resolve_code(s) or (s if s in WECHAT_EMOJI_CODES else None)
    return code


def coverage() -> tuple[list[str], list[str]]:
    have: list[str] = []
    missing: list[str] = []
    od = official_dir()
    for code in WECHAT_EMOJI_CODES:
        p = od / f"{code}.png"
        if p.is_file() and p.stat().st_size > 0:
            have.append(code)
        else:
            missing.append(code)
    return have, missing


def import_from(src: Path) -> tuple[int, list[str]]:
    """返回 (成功拷贝数, 无法识别的文件名)。"""
    od = official_dir()
    ok = 0
    unknown: list[str] = []
    for p in sorted(src.rglob("*")):
        if not p.is_file():
            continue
        if p.suffix.lower() not in {".png", ".webp", ".jpg", ".jpeg"}:
            continue
        code = stem_to_code(p.stem)
        if not code:
            unknown.append(p.name)
            continue
        dst = od / f"{code}.png"
        if p.suffix.lower() == ".png":
            shutil.copy2(p, dst)
        else:
            # 转成 png
            from PIL import Image

            Image.open(p).convert("RGBA").save(dst)
        ok += 1
    return ok, unknown


def main(argv: list[str]) -> int:
    if argv:
        src = Path(argv[0])
        if not src.is_dir():
            print(f"not a directory: {src}")
            return 2
        n, unknown = import_from(src)
        print(f"imported {n} files → {official_dir()}")
        if unknown:
            print(f"unrecognized ({len(unknown)}):")
            for name in unknown[:30]:
                print(f"  - {name}")
            if len(unknown) > 30:
                print(f"  … +{len(unknown) - 30} more")

    have, missing = coverage()
    total = len(WECHAT_EMOJI_CODES)
    print(f"coverage: {len(have)}/{total}")
    if missing:
        print("missing:")
        for c in missing:
            print(f"  - {c}.png")
    else:
        print("official library complete.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
