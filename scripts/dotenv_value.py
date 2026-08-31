#!/usr/bin/env python3
"""安全读取单个 dotenv 值，供 shell 做端口/开关判定。"""

from __future__ import annotations

import os
import sys
from pathlib import Path

from dotenv import dotenv_values


def main() -> int:
    if len(sys.argv) not in (2, 3, 4):
        raise SystemExit("usage: dotenv_value.py KEY [DEFAULT] [ENV_FILE]")
    key = sys.argv[1]
    default = sys.argv[2] if len(sys.argv) >= 3 else ""
    env_file = Path(sys.argv[3]) if len(sys.argv) == 4 else Path(".env")
    value = os.environ.get(key)
    if value is None:
        value = dotenv_values(env_file, interpolate=False).get(key)
    sys.stdout.write(str(value if value is not None else default))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
