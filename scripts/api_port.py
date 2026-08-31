#!/usr/bin/env python3
"""输出与 API 主入口一致的监听端口。"""

from __future__ import annotations

import os
import sys
from pathlib import Path

from dotenv import dotenv_values


def main() -> int:
    env_file = Path(sys.argv[1]) if len(sys.argv) > 1 else Path(".env")
    merged = {
        **{
            k: str(v)
            for k, v in dotenv_values(env_file, interpolate=False).items()
            if v is not None
        },
        **os.environ,
    }
    value = merged.get("SWARM_PORT") or merged.get("SWARM_API_PORT") or "8420"
    try:
        port = int(value)
    except (TypeError, ValueError) as exc:
        raise SystemExit(f"invalid API port: {value!r}") from exc
    if not 1 <= port <= 65535:
        raise SystemExit(f"invalid API port: {port}")
    sys.stdout.write(str(port))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
