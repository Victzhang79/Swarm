"""god-file 簇D：AUDIT 节点从 nodes/__init__ 拆出后的可寻址契约（行为）。

守约束：①经 __init__ re-export 保 swarm.brain.nodes._run_security_audit 可寻址；
②nodes/audit 不反向依赖 nodes/__init__（自包含、无环）。
"""
from __future__ import annotations

import subprocess
import sys


def test_reexport_identity():
    import swarm.brain.nodes as n
    import swarm.brain.nodes.audit as a

    assert n._run_security_audit is a._run_security_audit


def test_audit_importable_standalone():
    r = subprocess.run(
        [sys.executable, "-c",
         "import importlib; m = importlib.import_module('swarm.brain.nodes.audit'); "
         "assert hasattr(m, '_run_security_audit'); print('ok')"],
        capture_output=True, text=True, timeout=60,
    )
    assert r.returncode == 0, r.stderr
    assert "ok" in r.stdout


def test_no_project_path_skips_safe():
    # 无 project_path → 安全跳过、l1_passed=True（不误杀），不产 diff
    import asyncio

    from swarm.brain.nodes.audit import _run_security_audit
    from swarm.types import FileScope, SubTask

    st = SubTask(id="s1", description="audit", scope=FileScope(readable=["a.py"]))
    out = asyncio.run(_run_security_audit(st, None, task_id="t"))
    assert out.l1_passed is True
    assert out.diff == ""
    assert out.l1_details.get("skipped") == "no_project_path"
