"""ROADMAP §7.1 约定的入口名：转调 ``tools.lifelike_eval.snapshot``。

    python -m tools.lifelike_snapshot --data-root /AstrBot/data --stdout
"""

from tools.lifelike_eval.snapshot import main

if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
