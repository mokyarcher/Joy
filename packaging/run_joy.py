"""PyInstaller 入口（保证以 joy 包方式启动）。"""

from __future__ import annotations

import sys


def main() -> int:
    from joy.__main__ import main as joy_main

    return joy_main()


if __name__ == "__main__":
    raise SystemExit(main())
