#!/usr/bin/env python3
"""Harness 驱动的 L1 确定性闸门测试。

验证：
- run_l1_pipeline 优先用 harness.test_command（而非启发式猜测）
- harness.verify_commands 作为硬阻断验收命令真实执行
- verify 命令失败 → L1 不通过（杜绝 LLM 口头自报合格）
- _infer_harness 按语言/scope 推断合理 harness
"""

from __future__ import annotations

import importlib.util
import tempfile
from pathlib import Path

_bs = Path(__file__).resolve().parent / "swarm_bootstrap.py"
_spec = importlib.util.spec_from_file_location("swarm_bootstrap", _bs)
_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_mod)


def _make_subtask(harness, writable):
    from swarm.types import FileScope, SubTask, SubTaskDifficulty
    return SubTask(
        id="st-1",
        description="测试任务",
        difficulty=SubTaskDifficulty.TRIVIAL,
        scope=FileScope(writable=writable),
        harness=harness,
    )


def test_infer_harness_python():
    from swarm.brain.nodes import _build_simple_plan
    plan = _build_simple_plan("用 python 写一个计算器 calc.py")
    h = plan.subtasks[0].harness
    assert h.language == "python"
    assert "py_compile" in h.build_command
    assert "pytest" in h.test_command
    assert any("python" in w for w in h.extra_whitelist)
    print("  ✅ _infer_harness 正确推断 Python harness")


def test_infer_harness_node():
    from swarm.brain.nodes import _build_simple_plan
    plan = _build_simple_plan("写一个 react 组件 Button.tsx")
    h = plan.subtasks[0].harness
    assert h.language == "node"
    print("  ✅ _infer_harness 正确推断 Node harness")


def test_l1_uses_harness_test_command():
    """L1 pipeline 应优先用 harness.test_command。"""
    from swarm.types import TaskHarness
    from swarm.worker.l1_pipeline import run_l1_pipeline

    with tempfile.TemporaryDirectory() as d:
        # 写一个能通过的 python 文件 + 测试
        (Path(d) / "mod.py").write_text("def add(a, b):\n    return a + b\n")
        (Path(d) / "test_mod.py").write_text(
            "from mod import add\n\ndef test_add():\n    assert add(1, 2) == 3\n"
        )
        diff = (
            "--- /dev/null\n+++ b/mod.py\n@@ -0,0 +1,2 @@\n"
            "+def add(a, b):\n+    return a + b\n"
        )
        harness = TaskHarness(
            language="python",
            test_command="python -m pytest -q test_mod.py",
        )
        st = _make_subtask(harness, ["mod.py"])
        ok, details = run_l1_pipeline(d, st, diff, timeout=60)
        assert details.get("test_cmd_source") == "harness", f"应使用 harness 命令, got {details.get('test_cmd_source')}"
        assert "test_mod.py" in (details.get("test_cmd") or "")
        assert ok is True, f"测试应通过, details={details}"
        print("  ✅ L1 优先使用 harness.test_command 且真实执行通过")


def test_l1_verify_commands_hard_gate():
    """harness.verify_commands 失败 → L1 硬阻断（不放过 LLM 自报）。"""
    from swarm.types import TaskHarness
    from swarm.worker.l1_pipeline import run_l1_pipeline

    with tempfile.TemporaryDirectory() as d:
        # 写一个 BUG 文件：add 实现错误
        (Path(d) / "mod.py").write_text("def add(a, b):\n    return a - b  # BUG\n")
        diff = (
            "--- /dev/null\n+++ b/mod.py\n@@ -0,0 +1,2 @@\n"
            "+def add(a, b):\n+    return a - b\n"
        )
        harness = TaskHarness(
            language="python",
            # verify 命令断言 add(1,2)==3，会因 BUG 失败
            verify_commands=['python -c "from mod import add; assert add(1,2)==3"'],
        )
        st = _make_subtask(harness, ["mod.py"])
        ok, details = run_l1_pipeline(d, st, diff, timeout=60)
        assert ok is False, "verify 断言失败应导致 L1 不通过"
        assert details.get("verify_failed"), "应记录失败的 verify 命令"
        print("  ✅ harness.verify_commands 失败硬阻断 L1（拦截不合格产出）")


def test_l1_verify_commands_pass():
    """verify_commands 全通过 → L1 通过。"""
    from swarm.types import TaskHarness
    from swarm.worker.l1_pipeline import run_l1_pipeline

    with tempfile.TemporaryDirectory() as d:
        (Path(d) / "mod.py").write_text("def add(a, b):\n    return a + b\n")
        diff = (
            "--- /dev/null\n+++ b/mod.py\n@@ -0,0 +1,2 @@\n"
            "+def add(a, b):\n+    return a + b\n"
        )
        harness = TaskHarness(
            language="python",
            verify_commands=['python -c "from mod import add; assert add(1,2)==3"'],
        )
        st = _make_subtask(harness, ["mod.py"])
        ok, details = run_l1_pipeline(d, st, diff, timeout=60)
        assert ok is True, f"verify 通过应使 L1 通过, details={details}"
        vr = details.get("verify_commands", [])
        assert vr and all(r["ok"] for r in vr)
        print("  ✅ harness.verify_commands 全通过 → L1 通过")


def test_l1_empty_diff_still_runs_verify():
    """空 diff（如 greenfield 新建文件未进 diff）但有 verify_commands → 仍执行验收。"""
    from swarm.types import TaskHarness
    from swarm.worker.l1_pipeline import run_l1_pipeline

    with tempfile.TemporaryDirectory() as d:
        (Path(d) / "mod.py").write_text("def add(a, b):\n    return a + b\n")
        harness = TaskHarness(
            language="python",
            verify_commands=['python -c "from mod import add; assert add(2,3)==5"'],
        )
        st = _make_subtask(harness, ["mod.py"])
        # 传空 diff
        ok, details = run_l1_pipeline(d, st, "", timeout=60)
        assert ok is True, f"空 diff 但 verify 通过应使 L1 通过, details={details}"
        assert details.get("verify_commands"), "空 diff 也应执行 verify_commands"
        print("  ✅ 空 diff 仍执行 harness.verify_commands（greenfield 也有确定性闸门）")


if __name__ == "__main__":
    test_infer_harness_python()
    test_infer_harness_node()
    test_l1_uses_harness_test_command()
    test_l1_verify_commands_hard_gate()
    test_l1_verify_commands_pass()
    test_l1_empty_diff_still_runs_verify()
    print("\nHarness L1 闸门测试通过。")
