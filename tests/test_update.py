"""热更新版本比较与清单解析测试。"""

from __future__ import annotations

import json
from pathlib import Path

from joy.util.update import (
    parse_version,
    version_newer,
    version_older_than,
    _absolute_url,
    fetch_update_info,
)


def test_parse_version() -> None:
    assert parse_version("0.1.0") == (0, 1, 0)
    assert parse_version("v1.2.3") == (1, 2, 3)
    assert parse_version("") == (0,)


def test_version_newer() -> None:
    assert version_newer("0.1.1", "0.1.0")
    assert not version_newer("0.1.0", "0.1.0")
    assert not version_newer("0.1.0", "0.2.0")


def test_version_older_than_min() -> None:
    assert version_older_than("0.1.0", "0.2.0")
    assert not version_older_than("0.2.0", "0.2.0")


def test_absolute_url() -> None:
    base = "https://down.sharex.my/api/latest.json"
    assert _absolute_url("/releases/Joy-0.1.0.exe", base).endswith(
        "/releases/Joy-0.1.0.exe"
    )
    assert _absolute_url("https://x/a.exe", base) == "https://x/a.exe"


def test_fetch_update_info_from_file(tmp_path: Path, monkeypatch) -> None:
    data = {
        "version": "0.2.0",
        "url": "/releases/Joy-0.2.0.exe",
        "sha256": "abc",
        "changelog": "test",
        "force": False,
        "min_version": "0.1.0",
        "size_mb": 10,
    }
    p = tmp_path / "latest.json"
    p.write_text(json.dumps(data), encoding="utf-8")

    # file:// URL
    url = p.as_uri()
    info = fetch_update_info(url)
    assert info.version == "0.2.0"
    assert info.url.endswith("/releases/Joy-0.2.0.exe")
    assert info.sha256 == "abc"
