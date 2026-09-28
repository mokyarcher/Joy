"""python -m joy 入口。"""

from __future__ import annotations

import sys


def main() -> int:
    from joy.app.bootstrap import run_app

    return run_app()


if __name__ == "__main__":
    raise SystemExit(main())
