"""应用启动：优先 GUI，无 PySide6 时回退 CLI 提示。"""

from __future__ import annotations

import logging
import sys

from joy.util.logging import setup_logging

log = logging.getLogger("joy.app")


def run_app() -> int:
    setup_logging()
    try:
        from PySide6.QtWidgets import QApplication
    except ImportError:
        print(
            "未安装 UI 依赖。请执行: pip install -e \".[ui]\"\n"
            "或先用 CLI 查看预设: python -c \"from joy.providers.presets import VISION_PRESETS; "
            "print(list(VISION_PRESETS))\"",
            file=sys.stderr,
        )
        return 1

    from joy.app.settings_window import SettingsWindow
    from joy.core.config import JoyConfig, default_config_path, get_api_key
    from joy.core.secrets import hydrate_provider_keys, load_user_env, save_user_env
    from PySide6.QtWidgets import QApplication, QMessageBox

    hydrate_provider_keys(
        [
            "JOY_VISION_API_KEY",
            "JOY_LLM_API_KEY",
            "JOY_DRAFT_API_KEY",
            "SENSENOVA_API_KEY",
        ]
    )

    cfg, cfg_warn = JoyConfig.try_load()
    if not cfg.providers.vision.api_key_env:
        cfg.providers.vision.api_key_env = "JOY_VISION_API_KEY"

    if not get_api_key(cfg.providers.vision):
        sn = load_user_env("SENSENOVA_API_KEY")
        if sn:
            save_user_env(cfg.providers.vision.api_key_env, sn)

    app = QApplication(sys.argv)
    app.setApplicationName("Joy")
    app.setOrganizationName("Joy")

    win = SettingsWindow(cfg)
    win.show()
    if cfg_warn:
        QMessageBox.warning(win, "配置已重置", cfg_warn)

    # 已配置 Key 且开启开关 → 自动打开副驾
    if cfg.ui.auto_open_preview and get_api_key(cfg.providers.vision):
        from PySide6.QtCore import QTimer

        QTimer.singleShot(120, win._open_preview)

    log.info("Joy 已启动 · 配置路径 %s", default_config_path())
    return app.exec()
