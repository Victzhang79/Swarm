"""pytest 全局 — 加载 swarm_bootstrap。"""

from __future__ import annotations

import importlib.util
import os
from pathlib import Path

# 单元测试默认关闭 RBAC（匿名 admin 放行），避免大量 401。
# 认证相关测试（test_auth_login / test_rbac）直接调用 auth 模块或公开端点，不受影响。
os.environ.setdefault("SWARM_RBAC_ENABLED", "false")

_path = Path(__file__).parent / "swarm_bootstrap.py"
_spec = importlib.util.spec_from_file_location("swarm_bootstrap", _path)
assert _spec and _spec.loader
_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_mod)
