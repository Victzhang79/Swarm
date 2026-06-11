#!/usr/bin/env python3
"""W2 接入：AUDIT 意图编排分支 _run_security_audit 集成测试。"""

from __future__ import annotations

import asyncio
import importlib.util
import os
import tempfile
from pathlib import Path

_bs = Path(__file__).resolve().parent / "swarm_bootstrap.py"
_spec = importlib.util.spec_from_file_location("swarm_bootstrap", _bs)
_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_mod)


def _proj_with_secret() -> str:
    d = tempfile.mkdtemp(prefix="swarm_audit_")
    # 真实字面量密钥（非拼接），触发内置正则
    with open(os.path.join(d, "app.py"), "w") as f:
        f.write('OPENAI_KEY = "sk-' + "a1b2c3d4" * 5 + '"\n')
    return d


def test_audit_branch_blocks_on_critical():
    """阻断模式(critical)：检出高危密钥 → 不产 diff、出报告、l1_passed=False。"""
    from swarm.brain.nodes import _run_security_audit
    from swarm.types import FileScope, SubTask, TaskHarness, TaskIntent

    d = _proj_with_secret()
    st = SubTask(
        id="audit-1", description="安全审计", intent=TaskIntent.AUDIT,
        scope=FileScope(readable=["app.py"]), harness=TaskHarness(language="python"),
    )
    out = asyncio.run(_run_security_audit(st, d, task_id="t1"))
    assert out.diff == "", "AUDIT 不应产 diff"
    assert len(out.audit_findings) >= 1, "应检出密钥"
    assert out.l1_passed is False, "critical 阻断模式下高危发现应 l1_passed=False"
    assert out.l1_details.get("should_block") is True
    print("  ✅ AUDIT 阻断模式: 检出高危 → 不产diff + 出报告 + 阻断交付")


def test_audit_branch_no_path_skips_safely():
    """无项目路径时安全审计跳过，不误杀(l1_passed=True)。"""
    from swarm.brain.nodes import _run_security_audit
    from swarm.types import FileScope, SubTask, TaskHarness, TaskIntent

    st = SubTask(
        id="audit-2", description="审计", intent=TaskIntent.AUDIT,
        scope=FileScope(), harness=TaskHarness(language="python"),
    )
    out = asyncio.run(_run_security_audit(st, None, task_id="t2"))
    assert out.diff == ""
    assert out.l1_passed is True
    assert out.l1_details.get("skipped") == "no_project_path"
    print("  ✅ AUDIT 无路径优雅跳过(不误杀)")


def test_dispatch_routes_audit_intent():
    """_dispatch_to_worker 对 AUDIT 意图走审计分支(不进 WorkerExecutor)。"""
    from swarm.brain.nodes import _dispatch_to_worker
    from swarm.types import FileScope, SubTask, TaskHarness, TaskIntent

    d = _proj_with_secret()
    st = SubTask(
        id="audit-3", description="对项目做安全审计", intent=TaskIntent.AUDIT,
        scope=FileScope(readable=["app.py"]), harness=TaskHarness(language="python"),
    )
    # 不传 project_id，project_path 为 None → 走审计分支并安全跳过(验证路由本身)
    out = asyncio.run(_dispatch_to_worker(st, {}, task_id="t3"))
    assert out.l1_details.get("mode") == "audit", "AUDIT 意图未走审计分支"
    print("  ✅ _dispatch_to_worker 正确路由 AUDIT 意图到审计分支")


def main() -> int:
    print("=== test_audit_orchestration ===")
    failed = 0
    for fn in (
        test_audit_branch_blocks_on_critical,
        test_audit_branch_no_path_skips_safely,
        test_dispatch_routes_audit_intent,
    ):
        try:
            fn()
        except Exception as exc:
            failed += 1
            print(f"  ❌ {fn.__name__}: {exc}")
            import traceback

            traceback.print_exc()
    if failed:
        print(f"\n{failed} failed")
        return 1
    print("\nAll passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
