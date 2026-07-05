"""god-file 簇D：AUDIT 节点从 nodes/__init__ 拆出后的可寻址契约（行为）。

守约束：①经 __init__ re-export 保 swarm.brain.nodes._run_security_audit 可寻址；
②nodes/audit 不反向依赖 nodes/__init__（自包含、无环）。
"""
from __future__ import annotations

import subprocess
import sys


def test_reexport_identity():
    import swarm.brain.nodes as n
    import swarm.brain.nodes.audit_node as a

    assert n._run_security_audit is a._run_security_audit


def test_audit_importable_standalone():
    r = subprocess.run(
        [sys.executable, "-c",
         "import importlib; m = importlib.import_module('swarm.brain.nodes.audit_node'); "
         "assert hasattr(m, '_run_security_audit'); print('ok')"],
        capture_output=True, text=True, timeout=60,
    )
    assert r.returncode == 0, r.stderr
    assert "ok" in r.stdout


def test_no_project_path_skips_safe():
    # 无 project_path → 安全跳过、l1_passed=True（不误杀），不产 diff
    import asyncio

    from swarm.brain.nodes.audit_node import _run_security_audit
    from swarm.types import FileScope, SubTask

    st = SubTask(id="s1", description="audit", scope=FileScope(readable=["a.py"]))
    out = asyncio.run(_run_security_audit(st, None, task_id="t"))
    assert out.l1_passed is True
    assert out.diff == ""
    assert out.l1_details.get("skipped") == "no_project_path"


def test_nodes_audit_is_the_audit_function_not_submodule():
    """round27 E2E 实测回归锁：子模块 brain/nodes/audit.py（round24 D 拆分）import 时
    Python 会 setattr 父包 nodes.audit=<module>，覆写 __init__ 早先 `from swarm.audit
    import audit` 绑定的【函数】→ __init__ 里 6 处 audit(...) 全体 TypeError
    ('module' object is not callable)，任何真实 dispatch 秒炸（单测没覆盖这些调用点，
    v0.9.12 起潜伏到 round27 E2E 才暴露）。治类：子模块改名 audit_node.py 消除同名遮蔽。"""
    import swarm.audit as audit_pkg
    import swarm.brain.nodes as n

    assert callable(n.audit), "nodes.audit 必须是可调用的 audit 函数，不是被遮蔽后的子模块"
    assert n.audit is audit_pkg.audit
