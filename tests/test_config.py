import json
from pathlib import Path

from joy.core.config import JoyConfig


def test_try_load_ok(tmp_path: Path) -> None:
    p = tmp_path / "config.json"
    p.write_text(json.dumps({"chat": {"relation": "同事"}}), encoding="utf-8")
    cfg, warn = JoyConfig.try_load(p)
    assert warn is None
    assert cfg.chat.relation == "同事"


def test_try_load_corrupt(tmp_path: Path) -> None:
    p = tmp_path / "config.json"
    p.write_text("{not-json", encoding="utf-8")
    cfg, warn = JoyConfig.try_load(p)
    assert warn is not None
    assert cfg.chat.relation == "朋友"
    assert (tmp_path / "config.json.bad").is_file()
