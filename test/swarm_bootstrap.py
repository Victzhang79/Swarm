"""测试前 bootstrap：注册 `swarm` 包指向项目根，不污染 sys.path（避免 types.py 遮蔽标准库）。"""

from __future__ import annotations

import sys
import types as _stdlib_types
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def install() -> Path:
    if "swarm" not in sys.modules:
        pkg = _stdlib_types.ModuleType("swarm")
        pkg.__path__ = [str(ROOT)]
        sys.modules["swarm"] = pkg
    return ROOT


install()
