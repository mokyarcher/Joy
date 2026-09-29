"""文件日志保留策略测试。"""

from __future__ import annotations

import time
from pathlib import Path

from joy.util.logging import prune_log_files


def test_prune_removes_older_than_retention(tmp_path: Path) -> None:
    old = tmp_path / "joy.log.2026-01-01_00"
    new = tmp_path / "joy.log"
    old.write_text("old\n", encoding="utf-8")
    new.write_text("new\n", encoding="utf-8")
    # 把旧文件 mtime 拨到 48 小时前
    old_ts = time.time() - 48 * 3600
    new_ts = time.time()
    import os

    os.utime(old, (old_ts, old_ts))
    os.utime(new, (new_ts, new_ts))

    removed = prune_log_files(tmp_path, retention_hours=24, max_total_bytes=10_000_000)
    assert old in removed
    assert not old.exists()
    assert new.exists()


def test_prune_enforces_total_size_cap(tmp_path: Path) -> None:
    import os

    now = time.time()
    paths: list[Path] = []
    for i in range(5):
        p = tmp_path / f"joy.log.{i}"
        p.write_bytes(b"x" * 1000)
        # 越新 mtime 越大
        ts = now - (4 - i) * 60
        os.utime(p, (ts, ts))
        paths.append(p)

    # 只够留下最新 2 个（各 1000 字节）
    removed = prune_log_files(tmp_path, retention_hours=24, max_total_bytes=2000)
    assert len(removed) == 3
    living = sorted(tmp_path.glob("joy.log*"), key=lambda p: p.stat().st_mtime)
    assert len(living) == 2
    assert living[-1].name == "joy.log.4"
