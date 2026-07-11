#!/usr/bin/env python3
"""版本双源对账（round42 教训）：swarm.__version__ 与 pyproject.toml 各自为政，
发版只 bump 一处 → /api/health 报旧版 → e2e 版本一致性闸门误判"API 没加载新代码"。
本测试把同步变成 CI 硬约束。"""
from __future__ import annotations

import importlib.util
import tomllib
from pathlib import Path

_bs = Path(__file__).resolve().parent / "swarm_bootstrap.py"
_spec = importlib.util.spec_from_file_location("swarm_bootstrap", _bs)
_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_mod)


def test_dunder_version_matches_pyproject():
    # 读源文件文本而非 import swarm：swarm_bootstrap 的包别名不执行真实 __init__.py
    import re
    pkg_root = Path(__file__).resolve().parents[1]
    py = tomllib.loads((pkg_root / "pyproject.toml").read_text("utf-8"))
    src = (pkg_root / "__init__.py").read_text("utf-8")
    m = re.search(r'^__version__\s*=\s*"([^"]+)"', src, re.M)
    assert m, "swarm/__init__.py 缺 __version__ 定义"
    assert m.group(1) == py["project"]["version"], (
        "swarm/__init__.py:__version__ 与 pyproject.toml:version 漂移——"
        "发版必须两处同步 bump（/api/health 与 e2e 版本闸门消费 __version__）")
