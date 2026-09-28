"""微信标准小表情库：官方代码 + PNG 资源 + Vision 约束 + 别名归一化。

权威数据源优先：
  docs/Joy_WeChat_Emoji_DirectCrop_109_v2  （白气泡直裁，真实聊天气泡层）
  同步后：src/joy/assets/wechat_emoji/official/
"""

from __future__ import annotations

import json
import logging
import re
from functools import lru_cache
from pathlib import Path

log = logging.getLogger("joy.wechat_emoji")

# 无 map 文件时的最小兜底（应极少走到）
_FALLBACK_CODES: tuple[str, ...] = (
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
    "奸笑",
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
    "嘴唇",
    "爱心",
    "心碎",
    "拥抱",
    "强",
)

# 给 Vision 的极短视觉提示（难认的才写）
_VISUAL_HINTS: dict[str, str] = {
    "得意": "墨镜酷脸",
    "流泪": "闭眼两条泪",
    "闭嘴": "拉链嘴",
    "睡": "zzz",
    "大哭": "大滩泪",
    "囧": "囧字脸",
    "吐": "吐/病绿",
    "偷笑": "手捂嘴笑",
    "悠闲": "绿军帽",
    "骷髅": "骷髅头",
    "敲打": "锤头冒星",
    "衰": "黑脸衰",
    "旺柴": "柴犬头",
    "666": "墨镜比666手势",
    "捂脸": "手捂脸",
    "吃瓜": "啃西瓜",
    "社会社会": "抱拳作揖",
    "好的": "圆脸点头OK",
    "OK": "手势OK圈",
    "让我看看": "指缝偷看",
    "裂开": "脸裂开",
    "破涕为笑": "边笑边泪",
    "可怜": "超大水汪汪眼",
    "强": "竖大拇指",
    "弱": "倒竖拇指",
    "爱心": "粉心",
    "心碎": "碎心",
    "發": "麻将發",
    "福": "福字",
    "红包": "红包",
    "跳跳": "小人跳",
    "发抖": "抖线",
    "转圈": "转圈",
}

_ALIASES: dict[str, str] = {
    "笑": "微笑",
    "微笑脸": "微笑",
    "笑哭": "破涕为笑",
    "哭笑": "破涕为笑",
    "喜极而泣": "破涕为笑",
    "泪笑": "破涕为笑",
    "哈哈": "呲牙",
    "大笑": "呲牙",
    "嘿嘿": "坏笑",
    "可怜巴巴": "可怜",
    "水汪汪": "可怜",
    "狗头": "旺柴",
    "doge": "旺柴",
    "Doge": "旺柴",
    "柴犬": "旺柴",
    "面无表情": "无语",
    "沉默": "无语",
    "emm": "Emm",
    "666手势": "666",
    "六六六": "666",
    "牛": "666",
    "牛逼": "666",
    "真棒": "强",
    "点赞": "强",
    "赞": "强",
    "大拇指": "强",
    "拇指": "强",
    "ok": "OK",
    "okay": "OK",
    "脸红红": "脸红",
    "口罩": "生病",
    "军帽": "悠闲",
    "士兵": "悠闲",
    "吃瓜群众": "吃瓜",
    "偷看": "让我看看",
    "裂了": "裂开",
    "抱拳作揖": "抱拳",
    "作揖": "社会社会",
    "爱你": "爱心",
    "心": "爱心",
    "红心": "爱心",
    "比心": "爱心",
    "心碎了": "心碎",
    "kiss": "亲亲",
    "亲一口": "亲亲",
    "再见手": "再见",
    "挥手": "再见",
    "动画表情": "表情",
}

_BRACKET_RE = re.compile(r"\[([^\[\]]{1,16})\]")


def assets_dir() -> Path:
    return Path(__file__).resolve().parents[1] / "assets" / "wechat_emoji"


def official_dir() -> Path:
    d = assets_dir() / "official"
    d.mkdir(parents=True, exist_ok=True)
    return d


def package_dir() -> Path:
    """用户投放的完整包：白气泡直裁优先，旧透明包作后备。"""
    joy_root = Path(__file__).resolve().parents[3]
    direct = joy_root / "docs" / "Joy_WeChat_Emoji_DirectCrop_109_v2"
    if (direct / "emoji_map.json").is_file():
        return direct
    return joy_root / "docs" / "JoyWeChatEmojiNamed_v1"


def package_dirs() -> tuple[Path, ...]:
    joy_root = Path(__file__).resolve().parents[3]
    out: list[Path] = []
    for name in (
        "Joy_WeChat_Emoji_DirectCrop_109_v2",
        "JoyWeChatEmojiNamed_v1",
    ):
        p = joy_root / "docs" / name
        if (p / "emoji_map.json").is_file():
            out.append(p)
    return tuple(out)


@lru_cache(maxsize=1)
def load_emoji_entries() -> tuple[dict[str, str], ...]:
    """返回 ({name, tag, filename, index}, ...)。"""
    candidates = [d / "emoji_map.json" for d in package_dirs()]
    candidates.append(official_dir() / "emoji_map.json")
    for path in candidates:
        if not path.is_file():
            continue
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
            out: list[dict[str, str]] = []
            for item in raw:
                name = str(item.get("name") or "").strip()
                filename = str(item.get("filename") or "").strip()
                tag_s = str(item.get("tag") or "").strip() or f"[{name}]"
                if not name or not filename:
                    continue
                out.append(
                    {
                        "name": name,
                        "tag": tag_s,
                        "filename": filename,
                        "index": str(item.get("index") or ""),
                    }
                )
            if out:
                log.info("emoji map loaded %s entries from %s", len(out), path)
                return tuple(out)
        except Exception as exc:
            log.warning("emoji map load failed %s: %s", path, exc)
    return tuple(
        {
            "name": c,
            "tag": f"[{c}]",
            "filename": f"{c}.png",
            "index": str(i + 1),
        }
        for i, c in enumerate(_FALLBACK_CODES)
    )


@lru_cache(maxsize=1)
def emoji_codes() -> tuple[str, ...]:
    return tuple(e["name"] for e in load_emoji_entries())


# 兼容：from joy.core.wechat_emoji import WECHAT_EMOJI_CODES
# 注意：首次 import 时若尚未 sync，仍可读 docs 包或 fallback
WECHAT_EMOJI_CODES: tuple[str, ...] = ()  # filled at end of module


def tag(code: str) -> str:
    return f"[{code}]"


def is_known_code(name: str) -> bool:
    return (name or "").strip() in set(emoji_codes())


def resolve_code(raw: str) -> str | None:
    s = (raw or "").strip()
    if not s:
        return None
    codes = set(emoji_codes())
    if s in codes:
        return s
    if s in _ALIASES:
        mapped = _ALIASES[s]
        if mapped == "表情":
            return None
        return mapped if mapped in codes else None
    for prefix in ("微信", "表情", "黄脸"):
        if s.startswith(prefix):
            return resolve_code(s[len(prefix) :])
    return None


def normalize_emoji_text(text: str) -> str:
    if not text:
        return text

    def _repl(m: re.Match[str]) -> str:
        inner = m.group(1).strip()
        code = resolve_code(inner)
        if code:
            return tag(code)
        if inner in {"表情", "动画表情", "贴纸"}:
            return "[表情]"
        return m.group(0)

    return _BRACKET_RE.sub(_repl, text)


def tile_path(code: str) -> Path | None:
    """优先 official/（已同步的直裁白气泡），再 docs 包，再 tiles/。"""
    safe = re.sub(r'[<>:"/\\|?*]', "_", code)
    bases = (official_dir(), *package_dirs(), assets_dir() / "tiles")
    for base in bases:
        p = base / f"{safe}.png"
        if p.is_file():
            return p
    # map 里的 filename
    for e in load_emoji_entries():
        if e["name"] == code:
            for base in (official_dir(), *package_dirs()):
                p = base / e["filename"]
                if p.is_file():
                    return p
    return None


def official_coverage() -> tuple[int, int]:
    total = len(emoji_codes())
    n = sum(1 for c in emoji_codes() if tile_path(c) is not None)
    return n, total


@lru_cache(maxsize=1)
def vision_emoji_prompt_block() -> str:
    """Vision 不负责微信包点名（避免乱贴 [嘿哈]）；点名交给本地 Matcher。"""
    have, total = official_coverage()
    return (
        "表情规则（重要）：\n"
        f"- 微信内置小表情的具体标签由本地 Matcher 识别（库 {have}/{total}），"
        "你不要猜、不要从记忆里填 [嘿哈]/[捂脸]/[666] 等。\n"
        "- 气泡里是系统 Unicode 表情（如 😂🤔）或看不清时：text 只用 \"[表情]\"，"
        "禁止写成 \"[图片]\"，禁止编造微信标签。\n"
        "- 真正的照片/截图才用 \"[图片]\"；小黄脸/贴纸不是图片。\n"
        "- 若用户消息里是可见汉字，必须原样抄字，不要用表情标签代替文字。\n"
        "- 只有当你在画面上清楚看到微信输入码样式的方括号原文时，才可照抄该原文。"
    )


def sync_official_from_package() -> tuple[int, Path]:
    """把当前优先包（白气泡 DirectCrop）同步进 assets/official。"""
    import shutil

    src = package_dir()
    dst = official_dir()
    if not (src / "emoji_map.json").is_file():
        raise FileNotFoundError(f"emoji package missing: {src}")
    n = 0
    shutil.copy2(src / "emoji_map.json", dst / "emoji_map.json")
    for e in json.loads((src / "emoji_map.json").read_text(encoding="utf-8")):
        fn = e.get("filename")
        if not fn:
            continue
        sp = src / fn
        if sp.is_file():
            shutil.copy2(sp, dst / fn)
            n += 1
    for sheet_name in (
        "direct_crop_contact_sheet.png",
        "named_contact_sheet.png",
    ):
        sheet = src / sheet_name
        if sheet.is_file():
            shutil.copy2(sheet, dst / sheet_name)
    # 特征缓存失效，下次 Matcher 重建
    feat = dst / "emoji_features.json"
    if feat.is_file():
        feat.unlink()
    load_emoji_entries.cache_clear()
    emoji_codes.cache_clear()
    vision_emoji_prompt_block.cache_clear()
    log.info("synced official emoji package: %s png ← %s → %s", n, src.name, dst)
    return n, dst


# 模块加载时绑定（sync 后需 reload 或再取 emoji_codes()）
WECHAT_EMOJI_CODES = emoji_codes()
