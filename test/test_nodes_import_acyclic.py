"""A6：nodes ↔ dispatch/verify 循环依赖已破——防回归 import 冒烟。

原 dispatch.py/verify.py 顶层 `from swarm.brain import nodes` 与 nodes/__init__ 顶层
`from ...dispatch/verify import ...` 构成 eager 环，靠"只在调用时访问 nodes.X"侥幸成立，
重构即 ImportError。A6 改为函数内惰性导入破环。这里在【全新解释器】里先导入子模块，验证
不因导入顺序炸。子进程隔离，保证不吃当前进程已缓存的 sys.modules。
"""
from __future__ import annotations

import subprocess
import sys


def _fresh_import(stmt: str) -> subprocess.CompletedProcess:
    return subprocess.run([sys.executable, "-c", stmt], capture_output=True, text=True, timeout=60)


# 注：`import swarm.brain.nodes.dispatch as d` 会命中 __init__ 里同名 dispatch 函数（属性遮蔽
# 子模块，Python 既有行为），故用 importlib.import_module 取真正的模块对象。
def test_import_verify_first():
    r = _fresh_import(
        "import importlib; v = importlib.import_module('swarm.brain.nodes.verify'); "
        "assert hasattr(v, 'verify_l2'); print('ok')"
    )
    assert r.returncode == 0, f"verify 先导入应无 ImportError:\n{r.stderr}"
    assert "ok" in r.stdout


def test_import_dispatch_first():
    r = _fresh_import(
        "import importlib; d = importlib.import_module('swarm.brain.nodes.dispatch'); "
        "assert hasattr(d, 'dispatch'); print('ok')"
    )
    assert r.returncode == 0, f"dispatch 先导入应无 ImportError:\n{r.stderr}"
    assert "ok" in r.stdout


def test_dispatch_verify_have_no_module_level_nodes_binding():
    # 顶层不再绑定 nodes（惰性导入在函数内）→ 破 eager 环的结构保证
    r = _fresh_import(
        "import importlib; "
        "d = importlib.import_module('swarm.brain.nodes.dispatch'); "
        "v = importlib.import_module('swarm.brain.nodes.verify'); "
        "assert 'nodes' not in vars(d), 'dispatch 顶层不应再绑定 nodes'; "
        "assert 'nodes' not in vars(v), 'verify 顶层不应再绑定 nodes'; print('ok')"
    )
    assert r.returncode == 0, r.stderr
    assert "ok" in r.stdout
