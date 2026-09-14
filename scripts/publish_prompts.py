#!/usr/bin/env python3
"""仓库内便捷入口：等价于 `jobcopilot publish`（实现见 jobcopilot/publish.py）。"""
from __future__ import annotations

import sys

from jobcopilot.publish import main

if __name__ == "__main__":
    sys.exit(main())
