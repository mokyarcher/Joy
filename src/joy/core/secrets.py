"""API Key 持久化到当前用户环境变量（注册表 HKCU\\Environment），不进 config.json。"""

from __future__ import annotations

import logging
import os
import sys

log = logging.getLogger("joy.secrets")


def load_user_env(name: str) -> str:
    if not name:
        return ""
    existing = os.environ.get(name, "").strip()
    if existing:
        return existing
    if sys.platform != "win32":
        return ""
    try:
        import winreg

        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, "Environment") as key:
            value, _ = winreg.QueryValueEx(key, name)
            if isinstance(value, str) and value.strip():
                os.environ[name] = value.strip()
                return value.strip()
    except OSError:
        return ""
    return ""


def _notify_env_changed() -> None:
    """通知其它进程。必须用超时版，SendMessage 广播会卡死 UI。"""
    if sys.platform != "win32":
        return
    try:
        import ctypes

        HWND_BROADCAST = 0xFFFF
        WM_SETTINGCHANGE = 0x001A
        SMTO_ABORTIFHUNG = 0x0002
        result = ctypes.c_size_t(0)
        ctypes.windll.user32.SendMessageTimeoutW(
            HWND_BROADCAST,
            WM_SETTINGCHANGE,
            0,
            "Environment",
            SMTO_ABORTIFHUNG,
            200,  # ms
            ctypes.byref(result),
        )
    except Exception as exc:
        log.debug("env notify skipped: %s", exc)


def save_user_env(name: str, value: str, *, notify: bool = True) -> None:
    if not name:
        return
    value = (value or "").strip()
    if value:
        os.environ[name] = value
    else:
        os.environ.pop(name, None)

    if sys.platform != "win32":
        return
    try:
        import winreg

        with winreg.OpenKey(
            winreg.HKEY_CURRENT_USER,
            "Environment",
            0,
            winreg.KEY_SET_VALUE,
        ) as key:
            if value:
                winreg.SetValueEx(key, name, 0, winreg.REG_SZ, value)
            else:
                try:
                    winreg.DeleteValue(key, name)
                except OSError:
                    pass
        if notify:
            _notify_env_changed()
    except OSError as exc:
        log.warning("persist env failed: %s", exc)


def save_user_env_batch(pairs: dict[str, str]) -> None:
    """批量写入，只广播一次，避免三次同步通知拖慢保存。"""
    for name, value in pairs.items():
        save_user_env(name, value, notify=False)
    if pairs:
        _notify_env_changed()


def hydrate_provider_keys(api_key_env_names: list[str]) -> None:
    for name in api_key_env_names:
        load_user_env(name)
