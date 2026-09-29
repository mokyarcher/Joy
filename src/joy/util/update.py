"""客户端热更新：检查清单 → 下载校验 → 退出后替换 exe。"""

from __future__ import annotations

import hashlib
import json
import logging
import re
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Callable
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

log = logging.getLogger("joy.update")

DEFAULT_UPDATE_URL = "https://down.sharex.my/api/latest.json"
_Progress = Callable[[int, int], None]  # received, total(-1 if unknown)


@dataclass(frozen=True)
class UpdateInfo:
    version: str
    url: str
    sha256: str = ""
    changelog: str = ""
    force: bool = False
    min_version: str = ""
    size_mb: float | None = None


def current_version() -> str:
    from joy import __version__

    return str(__version__ or "0.0.0").strip()


def is_frozen() -> bool:
    return bool(getattr(sys, "frozen", False))


def current_exe_path() -> Path | None:
    """打包后返回 Joy.exe 路径；源码运行返回 None。"""
    if not is_frozen():
        return None
    try:
        return Path(sys.executable).resolve()
    except Exception:
        return None


def parse_version(text: str) -> tuple[int, ...]:
    nums = [int(x) for x in re.findall(r"\d+", (text or "").strip())]
    return tuple(nums) if nums else (0,)


def version_newer(remote: str, local: str) -> bool:
    return parse_version(remote) > parse_version(local)


def version_older_than(local: str, min_version: str) -> bool:
    if not (min_version or "").strip():
        return False
    return parse_version(local) < parse_version(min_version)


def _absolute_url(url: str, base: str) -> str:
    u = (url or "").strip()
    if not u:
        return ""
    if u.startswith("http://") or u.startswith("https://"):
        return u
    # 相对路径：挂到清单同域
    from urllib.parse import urljoin

    return urljoin(base, u)


def fetch_update_info(update_url: str = DEFAULT_UPDATE_URL, *, timeout: float = 12.0) -> UpdateInfo:
    req = Request(
        update_url,
        headers={"User-Agent": f"Joy/{current_version()}", "Accept": "application/json"},
        method="GET",
    )
    with urlopen(req, timeout=timeout) as resp:  # noqa: S310 — 固定更新域名
        raw = resp.read()
    data = json.loads(raw.decode("utf-8"))
    if not isinstance(data, dict):
        raise ValueError("更新清单格式无效")
    ver = str(data.get("version") or "").strip()
    url = _absolute_url(str(data.get("url") or ""), update_url)
    if not ver or not url:
        raise ValueError("更新清单缺少 version/url")
    size = data.get("size_mb")
    try:
        size_mb = float(size) if size is not None else None
    except (TypeError, ValueError):
        size_mb = None
    return UpdateInfo(
        version=ver,
        url=url,
        sha256=str(data.get("sha256") or "").strip().lower(),
        changelog=str(data.get("changelog") or "").strip(),
        force=bool(data.get("force")),
        min_version=str(data.get("min_version") or "").strip(),
        size_mb=size_mb,
    )


def check_for_update(
    *,
    update_url: str = DEFAULT_UPDATE_URL,
    local_version: str | None = None,
) -> UpdateInfo | None:
    """有新版本返回 UpdateInfo，否则 None。"""
    local = (local_version or current_version()).strip()
    info = fetch_update_info(update_url)
    if version_newer(info.version, local):
        return info
    return None


def update_dir() -> Path:
    p = Path.home() / ".joy" / "updates"
    p.mkdir(parents=True, exist_ok=True)
    return p


def sha256_file(path: Path, chunk: int = 1024 * 1024) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        while True:
            b = f.read(chunk)
            if not b:
                break
            h.update(b)
    return h.hexdigest()


def download_update(
    info: UpdateInfo,
    *,
    dest: Path | None = None,
    progress: _Progress | None = None,
    timeout: float = 60.0,
) -> Path:
    """下载到 ~/.joy/updates/Joy-{ver}.exe 并校验 sha256（若清单提供）。"""
    out = dest or (update_dir() / f"Joy-{info.version}.exe")
    tmp = out.with_suffix(out.suffix + ".part")
    req = Request(
        info.url,
        headers={"User-Agent": f"Joy/{current_version()}"},
        method="GET",
    )
    try:
        with urlopen(req, timeout=timeout) as resp:  # noqa: S310
            total = -1
            try:
                total = int(resp.headers.get("Content-Length") or -1)
            except ValueError:
                total = -1
            received = 0
            with tmp.open("wb") as f:
                while True:
                    block = resp.read(256 * 1024)
                    if not block:
                        break
                    f.write(block)
                    received += len(block)
                    if progress:
                        progress(received, total)
    except HTTPError as exc:
        raise RuntimeError(f"下载失败 HTTP {exc.code}") from exc
    except URLError as exc:
        raise RuntimeError(f"下载失败：{exc.reason}") from exc

    if info.sha256 and info.sha256 != "replace_after_upload":
        digest = sha256_file(tmp)
        if digest.lower() != info.sha256.lower():
            try:
                tmp.unlink(missing_ok=True)
            except OSError:
                pass
            raise RuntimeError(
                f"校验失败：期望 {info.sha256[:12]}… 实际 {digest[:12]}…"
            )
    tmp.replace(out)
    return out


def _write_replace_script(target_exe: Path, new_exe: Path) -> Path:
    """生成 bat：等旧进程退出 → 覆盖 → 启动新 exe。"""
    script = update_dir() / "apply_update.bat"
    # 用短路径安全的引号；timeout 等待进程名
    name = target_exe.name
    lines = [
        "@echo off",
        "setlocal",
        f'set "TARGET={target_exe}"',
        f'set "NEW={new_exe}"',
        f'set "NAME={name}"',
        "echo [Joy] waiting for process exit...",
        ":wait",
        "timeout /t 1 /nobreak >nul",
        'tasklist /FI "IMAGENAME eq %NAME%" 2>nul | find /I "%NAME%" >nul',
        "if not errorlevel 1 goto wait",
        "timeout /t 1 /nobreak >nul",
        'copy /Y "%NEW%" "%TARGET%" >nul',
        "if errorlevel 1 (",
        "  echo [Joy] copy failed",
        "  pause",
        "  exit /b 1",
        ")",
        'start "" "%TARGET%"',
        'del "%NEW%" >nul 2>&1',
        'del "%~f0" >nul 2>&1',
        "endlocal",
    ]
    script.write_text("\r\n".join(lines) + "\r\n", encoding="ascii")
    return script


def launch_apply_and_exit(new_exe: Path, *, target_exe: Path | None = None) -> None:
    """启动替换脚本并请求退出当前应用。仅打包 exe 可用。"""
    target = target_exe or current_exe_path()
    if target is None:
        raise RuntimeError("当前不是打包运行，无法自动替换")
    if not new_exe.is_file():
        raise RuntimeError(f"新包不存在：{new_exe}")
    script = _write_replace_script(target, new_exe)
    # 独立进程，不继承控制台
    creation = 0
    if hasattr(subprocess, "CREATE_NEW_CONSOLE"):
        creation = subprocess.CREATE_NEW_CONSOLE  # type: ignore[attr-defined]
    elif hasattr(subprocess, "DETACHED_PROCESS"):
        creation = subprocess.DETACHED_PROCESS  # type: ignore[attr-defined]
    subprocess.Popen(  # noqa: S603
        ["cmd.exe", "/c", str(script)],
        cwd=str(script.parent),
        creationflags=creation,
        close_fds=True,
    )
    log.info("update apply script launched target=%s new=%s", target, new_exe)

    # 退出 Qt
    try:
        from PySide6.QtWidgets import QApplication

        app = QApplication.instance()
        if app is not None:
            app.quit()
            return
    except Exception:
        pass
    raise SystemExit(0)


def needs_force(info: UpdateInfo, local_version: str | None = None) -> bool:
    local = (local_version or current_version()).strip()
    if info.force:
        return True
    return version_older_than(local, info.min_version)
