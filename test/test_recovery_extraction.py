"""A7：recovery 簇从 nodes/__init__ 首拆后的可寻址契约（行为）。

守两条硬约束：①可 patch 符号仍以 swarm.brain.nodes.X 可寻址(re-export)；②recovery.py
不反向依赖 nodes/__init__(自包含，防重建 A6 破的环)。
"""
from __future__ import annotations

import subprocess
import sys


def test_reexport_identity():
    import swarm.brain.nodes as n
    import swarm.brain.nodes.recovery as r

    for name in ("_producers_of", "_package_in_baseline", "_blocked_pkg_unrecoverable",
                 "_is_missing_dependency_failure", "_det_of",
                 "_INTERNAL_BLOCKED_KINDS", "_MISSING_DEP_PATTERNS"):
        assert getattr(n, name) is getattr(r, name), f"{name} 未经 __init__ re-export"


def test_recovery_importable_standalone():
    # 全新解释器直接导入 recovery，证明不反向依赖 nodes/__init__（无环）
    r = subprocess.run(
        [sys.executable, "-c",
         "import importlib; m = importlib.import_module('swarm.brain.nodes.recovery'); "
         "assert hasattr(m, '_producers_of'); print('ok')"],
        capture_output=True, text=True, timeout=60,
    )
    assert r.returncode == 0, r.stderr
    assert "ok" in r.stdout


def test_is_missing_dependency_failure_behavior():
    from swarm.brain.nodes.recovery import _is_missing_dependency_failure
    from swarm.types import WorkerOutput

    hit = WorkerOutput(subtask_id="x", diff="", summary="", l1_passed=False,
                       l1_details={"build_output": "error: cannot find symbol: class Foo"})
    assert _is_missing_dependency_failure({"x": hit}, ["x"]) is True
    # 内部未就绪 → 排除，不判缺外部依赖
    internal = WorkerOutput(subtask_id="y", diff="", summary="", l1_passed=False,
                            l1_details={"pipeline_blocked": "internal_pkg_not_built",
                                        "build_output": "cannot find symbol"})
    assert _is_missing_dependency_failure({"y": internal}, ["y"]) is False
