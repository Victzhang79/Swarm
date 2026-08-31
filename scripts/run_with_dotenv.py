#!/usr/bin/env python3
"""用 python-dotenv 加载项目 .env 后 exec 目标进程，不经 shell 求值。"""

from __future__ import annotations

import argparse
import os
from pathlib import Path

from dotenv import load_dotenv


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--env-file", default=".env")
    parser.add_argument("--setsid", action="store_true")
    parser.add_argument("command", nargs=argparse.REMAINDER)
    args = parser.parse_args()
    command = list(args.command)
    if command and command[0] == "--":
        command = command[1:]
    if not command:
        parser.error("缺少要执行的命令")

    load_dotenv(Path(args.env_file), override=False, interpolate=False)
    os.environ.setdefault("PYTHONUNBUFFERED", "1")
    if args.setsid:
        os.setsid()
    os.execvpe(command[0], command, os.environ)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
