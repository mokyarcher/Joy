"""按联系人持久化聊天记忆：识图合并 + 导入喂食 + 起草时取尾部。"""

from __future__ import annotations

import json
import logging
import re
import time
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, Field

from joy.core.models import ChatMessage, ScreenContext

log = logging.getLogger("joy.contact_memory")

MemorySource = Literal["vision", "import", "sent"]


class MemoryRecord(BaseModel):
    side: Literal["me", "other", "system"]
    text: str
    name: str | None = None
    ts: float = Field(default_factory=time.time)
    source: MemorySource = "vision"


def default_memory_dir() -> Path:
    return Path.home() / ".joy" / "memory"


def normalize_title(title: str) -> str:
    t = (title or "").strip()
    t = re.sub(r"\s+", " ", t)
    return t


def _mostly_latin_short(name: str) -> bool:
    """短英文/数字昵称：顶栏 VL 极易把 m/n 等读串。"""
    t = normalize_title(name)
    if not t or len(t) > 28:
        return False
    letters = sum(1 for c in t if ("a" <= c.lower() <= "z") or c.isdigit())
    return letters >= max(2, int(len(t) * 0.6))


def _edit_distance(a: str, b: str) -> int:
    if a == b:
        return 0
    if not a:
        return len(b)
    if not b:
        return len(a)
    if abs(len(a) - len(b)) > 2:
        return 99
    prev = list(range(len(b) + 1))
    for i, ca in enumerate(a, 1):
        cur = [i]
        for j, cb in enumerate(b, 1):
            cur.append(
                min(
                    prev[j] + 1,
                    cur[j - 1] + 1,
                    prev[j - 1] + (0 if ca == cb else 1),
                )
            )
        prev = cur
    return prev[-1]


def titles_confusable(a: str, b: str) -> bool:
    """两名是否像 OCR 抖动（moky↔noky）。"""
    aa, bb = normalize_title(a), normalize_title(b)
    if not aa or not bb or aa == bb:
        return False
    if not (_mostly_latin_short(aa) and _mostly_latin_short(bb)):
        return False
    return _edit_distance(aa.lower(), bb.lower()) <= 1


def prefer_contact_title(vision_title: str, sticky_hint: str) -> str:
    """识图标题与已锚定名易混时，保留 sticky，避免换错记忆桶。"""
    v = normalize_title(vision_title)
    h = normalize_title(sticky_hint)
    # 窗体壳名「微信」不是联系人；有锚定名时绝不当记忆桶
    _chrome = {"微信", "wechat", "weixin", "joy"}
    if v and v.casefold() in _chrome:
        v = ""
    if not v:
        return h
    if not h:
        return v
    if titles_confusable(v, h):
        log.info(
            "title stabilize keep=%s reject_vision=%s (ocr confusable)",
            h[:40],
            v[:40],
        )
        return h
    return v


def stabilize_contact_title(new_title: str, current_title: str) -> str:
    """顶栏轮询识名：易混单字母抖动时不换名。"""
    return prefer_contact_title(new_title, current_title)


def _safe_filename(title: str) -> str:
    t = normalize_title(title)
    if not t:
        return "_unknown"
    # Windows 非法字符
    t = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", t)
    t = t.strip(" .") or "_unknown"
    return t[:80]


def canonical_contact_key(title: str) -> str:
    """会话归一化键：抹平 OCR 常见的 &/+、全半角差异，避免同群分裂成多个桶。"""
    t = normalize_title(title).casefold()
    if not t:
        return ""
    repl = (
        ("＆", "&"),
        ("＋", "+"),
        ("—", "-"),
        ("–", "-"),
        ("（", "("),
        ("）", ")"),
    )
    for a, b in repl:
        t = t.replace(a, b)
    t = t.replace("&", "+")
    t = re.sub(r"\s+", "", t)
    return t


def list_stored_contact_titles(*, root: Path | None = None) -> list[str]:
    """磁盘上已有的会话名（来自 *.jsonl / *.topic.json 文件名）。"""
    base = root or default_memory_dir()
    if not base.is_dir():
        return []
    titles: set[str] = set()
    for p in base.iterdir():
        if not p.is_file():
            continue
        name = p.name
        if name.endswith(".jsonl.tmp"):
            continue
        if name.endswith(".topic.json"):
            titles.add(name[: -len(".topic.json")])
        elif name.endswith(".jsonl"):
            titles.add(name[: -len(".jsonl")])
    return sorted(titles)


def related_contact_titles(title: str, *, root: Path | None = None) -> list[str]:
    """同一会话的全部本地桶（含 &/+ 变体、截断前缀）。"""
    title_n = normalize_title(title)
    if not title_n:
        return []
    key = canonical_contact_key(title_n)
    safe = _safe_filename(title_n)
    out: list[str] = []
    seen: set[str] = set()

    def _add(stem: str) -> None:
        s = (stem or "").strip()
        if not s or s in seen:
            return
        seen.add(s)
        out.append(s)

    _add(title_n)
    _add(safe)
    for stem in list_stored_contact_titles(root=root):
        if stem == safe or stem == title_n:
            _add(stem)
            continue
        ck = canonical_contact_key(stem)
        if key and ck == key:
            _add(stem)
            continue
        # 顶栏省略号截断：一方是另一方前缀
        if key and ck and len(key) >= 10 and len(ck) >= 10:
            if key.startswith(ck) or ck.startswith(key):
                _add(stem)
    return out


def resolve_storage_title(title: str, *, root: Path | None = None) -> str:
    """读写记忆时归并到已有同键桶（优先更长全名），避免 &/+ 再拆文件。"""
    title_n = normalize_title(title)
    if not title_n:
        return ""
    key = canonical_contact_key(title_n)
    matches = [
        s
        for s in list_stored_contact_titles(root=root)
        if canonical_contact_key(s) == key
    ]
    if not matches:
        return title_n
    preferred = max(matches, key=lambda s: (len(canonical_contact_key(s)), len(s)))
    return preferred


def memory_path(title: str, *, root: Path | None = None) -> Path:
    base = root or default_memory_dir()
    return base / f"{_safe_filename(title)}.jsonl"


def norm_text(text: str) -> str:
    s = (text or "").strip()
    s = re.sub(r"\s+", "", s)
    return s


def _key(side: str, text: str) -> str:
    return f"{side}|{norm_text(text)}"


def record_key(rec: MemoryRecord) -> str:
    return _key(rec.side, rec.text)


def message_to_record(
    msg: ChatMessage, *, source: MemorySource = "vision"
) -> MemoryRecord | None:
    text = (msg.text or "").strip()
    if not text:
        return None
    side = msg.side if msg.side in ("me", "other", "system") else "other"
    return MemoryRecord(
        side=side,  # type: ignore[arg-type]
        text=text,
        name=(msg.name or None),
        source=source,
    )


def record_to_message(rec: MemoryRecord) -> ChatMessage:
    return ChatMessage(side=rec.side, text=rec.text, name=rec.name)


def load_records(title: str, *, root: Path | None = None) -> list[MemoryRecord]:
    path = memory_path(title, root=root)
    if not path.is_file():
        return []
    out: list[MemoryRecord] = []
    try:
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                out.append(MemoryRecord.model_validate(json.loads(line)))
            except Exception:
                continue
    except OSError as exc:
        log.warning("memory load failed %s: %s", path, exc)
    return out


def save_records(
    title: str,
    records: list[MemoryRecord],
    *,
    root: Path | None = None,
    keep_max: int = 400,
) -> Path:
    path = memory_path(title, root=root)
    path.parent.mkdir(parents=True, exist_ok=True)
    keep = max(20, int(keep_max))
    trimmed = records[-keep:] if len(records) > keep else records
    tmp = path.with_suffix(".jsonl.tmp")
    body = "\n".join(r.model_dump_json() for r in trimmed)
    if body:
        body += "\n"
    tmp.write_text(body, encoding="utf-8")
    tmp.replace(path)
    return path


def clear_memory(title: str, *, root: Path | None = None) -> int:
    """清空当前会话的全部本地上下文。

    含：聊天记忆 jsonl、Topic State，以及同一群因 OCR 产生的 &/+、截断等变体桶。
    返回清除前各桶记忆条数之和。
    """
    related = related_contact_titles(title, root=root)
    if not related:
        return 0
    total = 0
    try:
        from joy.core.topic_state import clear_topic
    except Exception:
        clear_topic = None  # type: ignore[assignment]

    for stem in related:
        before = load_records(stem, root=root)
        total += len(before)
        path = memory_path(stem, root=root)
        try:
            if path.is_file():
                path.unlink()
            tmp = path.with_suffix(".jsonl.tmp")
            if tmp.is_file():
                tmp.unlink()
        except OSError as exc:
            log.warning("memory clear failed %s: %s", path, exc)
            raise

    if clear_topic is not None:
        try:
            clear_topic(title, root=root)
        except Exception as exc:
            log.warning("topic clear alongside memory failed: %s", exc)

    try:
        from joy.core.self_replies import clear_self_replies

        clear_self_replies(title, root=root)
    except Exception as exc:
        log.warning("self replies clear failed: %s", exc)

    try:
        from joy.core.style_profile import clear_style_profile

        clear_style_profile(title, root=root)
    except Exception as exc:
        log.warning("style profile clear failed: %s", exc)

    try:
        from joy.core.relation_memory import clear_relation

        clear_relation(title, root=root)
    except Exception as exc:
        log.warning("relation memory clear failed: %s", exc)

    # 识图帧缓存不按联系人索引，整清以免旧帧分析结果串味
    try:
        from joy.core.engine import clear_vision_cache

        clear_vision_cache()
    except Exception as exc:
        log.warning("vision cache clear failed: %s", exc)

    log.info(
        "memory cleared title=%s related=%s removed=%s",
        normalize_title(title)[:40],
        len(related),
        total,
    )
    return total


def merge_records(
    existing: list[MemoryRecord], incoming: list[MemoryRecord]
) -> list[MemoryRecord]:
    """尾部重叠对齐后追加；无重叠则只追加尾部未见过的新句。"""
    inc = [r for r in incoming if norm_text(r.text)]
    if not inc:
        return list(existing)
    if not existing:
        return list(inc)

    ex_keys = [record_key(r) for r in existing]
    in_keys = [record_key(r) for r in inc]

    max_k = min(len(ex_keys), len(in_keys))
    for k in range(max_k, 0, -1):
        if ex_keys[-k:] == in_keys[:k]:
            return list(existing) + list(inc[k:])

    # 无整段重叠：跳过已在近期出现过的，避免乱序整段重复粘贴
    recent = set(ex_keys[-60:])
    out = list(existing)
    for r, k in zip(inc, in_keys):
        if k in recent:
            continue
        out.append(r)
        recent.add(k)
    return out


def ingest_messages(
    title: str,
    messages: list[ChatMessage],
    *,
    source: MemorySource = "vision",
    root: Path | None = None,
    keep_max: int = 400,
) -> tuple[int, int]:
    """
    把一批消息合并进该联系人记忆。
    返回 (合并后总条数, 本次净增条数)。
    """
    title_n = resolve_storage_title(title, root=root)
    if not title_n:
        return 0, 0
    from joy.core.message_noise import is_ui_timestamp

    incoming = [
        r
        for m in messages
        if not is_ui_timestamp(m.text or "")
        and (m.text or "").strip() not in {"[表情]", "[动画表情]", "[贴纸]"}
        and (r := message_to_record(m, source=source)) is not None
    ]
    if not incoming:
        return len(load_records(title_n, root=root)), 0
    before = load_records(title_n, root=root)
    # 同键其它变体桶并入主桶，再删掉变体，防止砂浆/水库藏在旁路文件
    key = canonical_contact_key(title_n)
    for stem in list_stored_contact_titles(root=root):
        if stem == _safe_filename(title_n) or stem == title_n:
            continue
        if canonical_contact_key(stem) != key:
            continue
        extra = load_records(stem, root=root)
        if extra:
            before = merge_records(before, extra)
        try:
            p = memory_path(stem, root=root)
            if p.is_file():
                p.unlink()
            tmp = p.with_suffix(".jsonl.tmp")
            if tmp.is_file():
                tmp.unlink()
        except OSError:
            pass
    merged = merge_records(before, incoming)
    added = max(0, len(merged) - len(before))
    save_records(title_n, merged, root=root, keep_max=keep_max)
    if added:
        log.info(
            "memory ingest title=%s +%s → %s (source=%s)",
            title_n,
            added,
            len(merged),
            source,
        )
    return len(merged), added


def append_sent(
    title: str,
    text: str,
    *,
    root: Path | None = None,
    keep_max: int = 400,
) -> None:
    t = (text or "").strip()
    if not normalize_title(title) or not t:
        return
    ingest_messages(
        title,
        [ChatMessage(side="me", text=t)],
        source="sent",
        root=root,
        keep_max=keep_max,
    )
    try:
        from joy.core.self_replies import append_self_replies

        append_self_replies(title, [t], root=root)
        try:
            from joy.core.style_profile import refresh_style_profile

            refresh_style_profile(title, root=root)
        except Exception:
            pass
    except Exception as exc:
        log.warning("self replies append sent failed: %s", exc)


def load_tail_messages(
    title: str,
    *,
    limit: int = 30,
    root: Path | None = None,
) -> list[ChatMessage]:
    title_n = resolve_storage_title(title, root=root) or normalize_title(title)
    recs = load_records(title_n, root=root)
    # 变体桶尚未并入时，拼上同键其它文件
    key = canonical_contact_key(title_n)
    if key:
        base_keys = {record_key(r) for r in recs}
        for stem in related_contact_titles(title_n, root=root):
            if stem == title_n or stem == _safe_filename(title_n):
                continue
            if canonical_contact_key(stem) != key:
                continue
            for r in load_records(stem, root=root):
                k = record_key(r)
                if k in base_keys:
                    continue
                base_keys.add(k)
                recs.append(r)
    n = max(1, int(limit))
    return [record_to_message(r) for r in recs[-n:]]


def count_related_memory(title: str, *, root: Path | None = None) -> tuple[int, int]:
    """返回 (记忆条数合计, 有数据的本地桶数)。"""
    related = related_contact_titles(title, root=root)
    total = 0
    buckets = 0
    for stem in related:
        n = len(load_records(stem, root=root))
        topic_exists = False
        try:
            from joy.core.topic_state import topic_path

            topic_exists = topic_path(stem, root=root).is_file()
        except Exception:
            pass
        if n or topic_exists:
            buckets += 1
        total += n
    return total, buckets


def enrich_context_from_memory(
    ctx: ScreenContext,
    *,
    title_hint: str = "",
    prompt_max: int = 30,
    keep_max: int = 400,
    root: Path | None = None,
    enabled: bool = True,
) -> ScreenContext:
    """
    用本帧消息更新记忆，再用记忆尾部替换 ctx.messages（供起草）。
    自动跳过判定应使用本帧原始 ctx，不要用本函数返回值。
    """
    if not enabled:
        return ctx
    title = resolve_storage_title(
        normalize_title(ctx.title) or normalize_title(title_hint), root=root
    )
    if not title:
        return ctx
    if not ctx.title:
        ctx = ctx.model_copy(update={"title": title})
    ingest_messages(
        title,
        list(ctx.messages),
        source="vision",
        root=root,
        keep_max=keep_max,
    )
    # 把本帧识别到的「我」写入独立回复库
    try:
        from joy.core.self_replies import append_self_replies

        me_bits = [
            (m.text or "").strip()
            for m in (ctx.messages or [])
            if m.side == "me" and (m.text or "").strip()
        ]
        if me_bits:
            append_self_replies(title, me_bits, root=root)
    except Exception as exc:
        log.warning("self replies ingest failed: %s", exc)
    tail = load_tail_messages(title, limit=prompt_max, root=root)
    if not tail:
        return ctx
    return ctx.model_copy(update={"messages": tail, "title": title})


_LINE_SPEAKER = re.compile(
    r"^(?P<who>我|对方|别人|好友|Me|Other|ME|OTHER)\s*[:：]\s*(?P<body>.+)$"
)
_LINE_NAMED = re.compile(
    r"^(?P<name>[^:：\d]{1,24})\s*[:：]\s*(?P<body>.+)$"
)
# 微信官方导出：昵称\n2026年09月25日 22:18\n正文…
_WECHAT_TIME = re.compile(
    r"^\d{4}年\d{1,2}月\d{1,2}日\s+\d{1,2}:\d{2}(:\d{2})?$"
)
# 多选复制常见：名字下一行只有短时间 2:16 / 02:16
_WECHAT_TIME_SHORT = re.compile(r"^\d{1,2}:\d{2}(:\d{2})?$")
_SKIP_BODY = re.compile(
    r"^\[(动画表情|表情|图片|视频|语音|文件|名片|位置|链接|聊天记录|小程序|音乐)\]"
)


def _is_wechat_time_line(s: str) -> bool:
    t = (s or "").strip()
    if _WECHAT_TIME.match(t) or _WECHAT_TIME_SHORT.match(t):
        return True
    try:
        from joy.core.message_noise import is_ui_timestamp

        return is_ui_timestamp(t)
    except Exception:
        return False


def _looks_like_wechat_official(lines: list[str]) -> bool:
    """至少两处「非空行 + 下一行是时间」。"""
    hits = 0
    i = 0
    n = len(lines)
    while i < n - 1:
        if lines[i] and _is_wechat_time_line(lines[i + 1] or ""):
            hits += 1
            if hits >= 2:
                return True
            i += 2
            continue
        i += 1
    return False


def _split_me_names(me_names: str | list[str] | None) -> list[str]:
    if me_names is None:
        return []
    if isinstance(me_names, str):
        parts = re.split(r"[,，;；、\s]+", me_names.strip())
        return [p for p in parts if p]
    return [str(p).strip() for p in me_names if str(p).strip()]


def _side_for_wechat_speaker(
    speaker: str,
    *,
    contact_title: str | None,
    me_names: list[str],
) -> tuple[Literal["me", "other"], str | None]:
    """
    私聊：发言者 == 顶栏联系人名 → 对方；其余 → 我（兼容 Moky / moky 大小写不同）。
    群聊：填了「我的昵称」则精确匹配为我；未填则一律对方（带 name）。
    """
    sp = speaker.strip()
    title = (contact_title or "").strip()
    me_set = set(me_names)

    if sp in me_set or sp in ("我", "Me", "ME"):
        return "me", None
    if title and sp == title:
        return "other", sp
    if me_set:
        # 明确声明了我的昵称：其他人全是对方
        return "other", sp
    if title:
        # 私聊默认：不是联系人名字的，算我
        return "me", None
    return "other", sp


def parse_wechat_official(
    raw: str,
    *,
    contact_title: str | None = None,
    me_names: str | list[str] | None = None,
    skip_media: bool = True,
) -> list[ChatMessage]:
    """解析微信「导出聊天记录」文本（好友/群）。"""
    lines = [ln.strip() for ln in (raw or "").replace("\r\n", "\n").split("\n")]
    me_list = _split_me_names(me_names)
    msgs: list[ChatMessage] = []
    i = 0
    n = len(lines)
    while i < n:
        if not lines[i]:
            i += 1
            continue
        # 昵称 + 时间
        if i + 1 < n and _is_wechat_time_line(lines[i + 1] or ""):
            speaker = lines[i]
            i += 2  # skip name + time
            body_lines: list[str] = []
            while i < n and lines[i]:
                # 下一条也是「名+时间」则停
                if i + 1 < n and _is_wechat_time_line(lines[i + 1] or ""):
                    break
                body_lines.append(lines[i])
                i += 1
            text = "\n".join(body_lines).strip()
            if not text:
                continue
            if skip_media and _SKIP_BODY.match(text):
                continue
            # 纯 [图片] xxx.dat 也跳过
            if skip_media and text.startswith("[图片]"):
                continue
            side, name = _side_for_wechat_speaker(
                speaker, contact_title=contact_title, me_names=me_list
            )
            msgs.append(ChatMessage(side=side, text=text, name=name))
            continue
        i += 1
    return msgs


def parse_import_text(
    raw: str,
    *,
    default_other_name: str | None = None,
    me_names: str | list[str] | None = None,
) -> list[ChatMessage]:
    """
    宽松解析人工粘贴的聊天记录。
    优先：微信官方导出（昵称\\n时间\\n正文）
    其次：我:/对方:、名字:、裸行交替
    """
    lines = [ln.strip() for ln in (raw or "").replace("\r\n", "\n").split("\n")]
    if _looks_like_wechat_official(lines):
        return parse_wechat_official(
            raw,
            contact_title=default_other_name,
            me_names=me_names,
            skip_media=True,
        )

    msgs: list[ChatMessage] = []
    pending_who: Literal["me", "other"] | None = None
    pending_name: str | None = None
    buf: list[str] = []

    def _flush() -> None:
        nonlocal pending_who, pending_name, buf
        if pending_who is None:
            buf = []
            return
        text = "\n".join(buf).strip()
        buf = []
        if text:
            msgs.append(
                ChatMessage(side=pending_who, text=text, name=pending_name)
            )
        pending_who = None
        pending_name = None

    for ln in lines:
        if not ln:
            _flush()
            continue
        m = _LINE_SPEAKER.match(ln)
        if m:
            _flush()
            who = m.group("who")
            pending_who = "me" if who.lower() in ("我", "me") else "other"
            pending_name = None
            buf = [m.group("body").strip()]
            continue
        m2 = _LINE_NAMED.match(ln)
        if m2:
            _flush()
            name = m2.group("name").strip()
            if name in ("我", "Me", "ME"):
                pending_who = "me"
                pending_name = None
            else:
                pending_who = "other"
                pending_name = name
            buf = [m2.group("body").strip()]
            continue
        if pending_who is not None:
            buf.append(ln)
            continue
        pending_who = "other" if len(msgs) % 2 == 0 else "me"
        pending_name = default_other_name if pending_who == "other" else None
        buf = [ln]

    _flush()
    return msgs


def import_transcript(
    title: str,
    raw: str,
    *,
    me_names: str | list[str] | None = None,
    root: Path | None = None,
    keep_max: int = 400,
) -> tuple[int, int]:
    """导入文本。返回 (解析条数, 净增条数)。"""
    title_n = normalize_title(title)
    if not title_n:
        raise ValueError("请先指定联系人名称")
    parsed = parse_import_text(
        raw, default_other_name=title_n, me_names=me_names
    )
    if not parsed:
        raise ValueError("未能从文本中解析出消息（可检查是否为微信导出格式）")
    before = len(load_records(title_n, root=root))
    total, _ = ingest_messages(
        title_n,
        parsed,
        source="import",
        root=root,
        keep_max=keep_max,
    )
    return len(parsed), max(0, total - before)
