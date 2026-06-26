#!/usr/bin/env python3
"""DEBUG 意图排错闭环单元测试 — 验证 prompt 注入 + L1 闸门逻辑。"""

from __future__ import annotations

import importlib.util
import subprocess
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

_bs = Path(__file__).resolve().parent / "swarm_bootstrap.py"
_spec = importlib.util.spec_from_file_location("swarm_bootstrap", _bs)
_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_mod)

from swarm.types import FileScope, SubTask, SubTaskDifficulty, TaskHarness, TaskIntent


def _make_subtask(
    intent: TaskIntent = TaskIntent.DEBUG,
    failing_test_command: str = "",
    test_command: str = "",
    writable: list[str] | None = None,
) -> SubTask:
    """构造 SubTask，支持指定 intent 和 harness.failing_test_command。"""
    harness = TaskHarness(
        language="python",
        test_command=test_command,
        failing_test_command=failing_test_command,
    )
    return SubTask(
        id="st-debug-1",
        description="Fix the off-by-one bug in calculate_total",
        intent=intent,
        difficulty=SubTaskDifficulty.MEDIUM,
        scope=FileScope(writable=writable or ["calc.py"], readable=writable or ["calc.py"]),
        harness=harness,
    )


# ── _format_debug_section 测试 ──


def test_debug_section_returns_content_for_debug_intent():
    """DEBUG intent + 有 failing_test_command → 返回含'复现'和'回归'的完整提示。"""
    from swarm.worker.prompts import _format_debug_section

    subtask = _make_subtask(
        intent=TaskIntent.DEBUG,
        failing_test_command="python -m pytest test_calc.py::test_off_by_one -q",
    )
    result = _format_debug_section(subtask)

    assert result, "DEBUG intent 应返回非空字符串"
    assert "复现" in result, "DEBUG section 应含'复现'关键词"
    assert "回归" in result, "DEBUG section 应含'回归'关键词"
    assert "pytest test_calc.py::test_off_by_one" in result, (
        "DEBUG section 应包含具体的 failing_test_command"
    )
    print("  ✅ DEBUG intent + failing_test_command → 含复现/回归的完整提示")


def test_debug_section_returns_empty_for_non_debug_intent():
    """非 DEBUG intent → 返回空串。"""
    from swarm.worker.prompts import _format_debug_section

    for intent in (TaskIntent.CREATE, TaskIntent.MODIFY, TaskIntent.AUDIT, TaskIntent.REFACTOR):
        subtask = _make_subtask(intent=intent)
        result = _format_debug_section(subtask)
        assert result == "", f"intent={intent.value} 应返回空串，实际: {result[:80]}"

    print("  ✅ 非 DEBUG intent → 返回空串")


def test_debug_section_graceful_without_failing_test_command():
    """DEBUG intent 但无 failing_test_command → 仍返回通用提示（优雅降级）。"""
    from swarm.worker.prompts import _format_debug_section

    subtask = _make_subtask(intent=TaskIntent.DEBUG, failing_test_command="")
    result = _format_debug_section(subtask)

    assert result, "无 failing_test_command 仍应返回通用 DEBUG 提示"
    assert "复现" in result, "通用提示应含'复现'"
    assert "回归" in result, "通用提示应含'回归'"
    print("  ✅ DEBUG intent 无 failing_test_command → 通用提示（优雅降级）")


# ── build_worker_prompt 集成测试 ──


def _mock_config():
    """构造 mock config 对象，避免读 .env / 配置文件。"""
    return MagicMock(
        worker=MagicMock(
            max_fix_rounds=3, max_iterations=50, max_execution_time=300,
        ),
    )


def test_build_worker_prompt_includes_debug_section_for_debug():
    """完整 prompt 对 DEBUG subtask 包含 debug 段。"""
    from swarm.worker.prompts import build_worker_prompt

    subtask = _make_subtask(
        intent=TaskIntent.DEBUG,
        failing_test_command="python -m pytest test_bug.py -q",
    )
    # get_config 在 build_worker_prompt 内部 via `from swarm.config.settings import get_config`
    with patch("swarm.config.settings.get_config", return_value=_mock_config()):
        prompt = build_worker_prompt(subtask)

    assert "🐛 DEBUG 排错流程" in prompt, "完整 prompt 应含 DEBUG 排错流程段"
    assert "pytest test_bug.py" in prompt, "完整 prompt 应含具体 failing_test_command"
    assert "不复现不许改" in prompt, "完整 prompt 应含核心原则"
    print("  ✅ build_worker_prompt 对 DEBUG subtask 包含 debug 段")


def test_build_worker_prompt_excludes_debug_section_for_modify():
    """完整 prompt 对 MODIFY subtask 不包含 debug 段。"""
    from swarm.worker.prompts import build_worker_prompt

    subtask = _make_subtask(intent=TaskIntent.MODIFY)
    with patch("swarm.config.settings.get_config", return_value=_mock_config()):
        prompt = build_worker_prompt(subtask)

    assert "🐛 DEBUG 排错流程" not in prompt, "MODIFY intent 不应包含 DEBUG 排错流程段"
    print("  ✅ build_worker_prompt 对 MODIFY subtask 不包含 debug 段")


# ── Executor _run_failing_test_gate 测试（mock subprocess） ──


def test_failing_test_gate_passes_when_cmd_succeeds():
    """failing_test_command 修复后通过（exit code 0）→ gate 返回 True。"""
    from swarm.worker.executor import WorkerExecutor

    subtask = _make_subtask(
        intent=TaskIntent.DEBUG,
        failing_test_command="python -m pytest test_bug.py -q",
    )
    executor = WorkerExecutor(subtask=subtask, project_path="/tmp/fake_project")

    mock_proc = MagicMock()
    mock_proc.returncode = 0
    mock_proc.stdout = "1 passed"
    mock_proc.stderr = ""

    with patch("subprocess.run", return_value=mock_proc):
        ok, detail = executor._run_failing_test_gate("python -m pytest test_bug.py -q")

    assert ok is True, f"修复后命令通过应返回 True，实际: {ok}"
    assert "exit_code=0" in detail
    print("  ✅ _run_failing_test_gate: 命令通过 → True")


def test_failing_test_gate_fails_when_cmd_fails():
    """failing_test_command 仍失败（exit code ≠ 0）→ gate 返回 False。"""
    from swarm.worker.executor import WorkerExecutor

    subtask = _make_subtask(
        intent=TaskIntent.DEBUG,
        failing_test_command="python -m pytest test_bug.py -q",
    )
    executor = WorkerExecutor(subtask=subtask, project_path="/tmp/fake_project")

    mock_proc = MagicMock()
    mock_proc.returncode = 1
    mock_proc.stdout = "1 failed"
    mock_proc.stderr = ""

    with patch("subprocess.run", return_value=mock_proc):
        ok, detail = executor._run_failing_test_gate("python -m pytest test_bug.py -q")

    assert ok is False, f"命令仍失败应返回 False，实际: {ok}"
    assert "exit_code=1" in detail
    print("  ✅ _run_failing_test_gate: 命令仍失败 → False")


def test_failing_test_gate_graceful_on_exception():
    """执行环境异常 → 保守判失败返回 False（M1 修复：不把"验证不了"误判为"已修复 PASS"）。"""
    from swarm.worker.executor import WorkerExecutor

    subtask = _make_subtask(
        intent=TaskIntent.DEBUG,
        failing_test_command="python -m pytest test_bug.py -q",
    )
    executor = WorkerExecutor(subtask=subtask, project_path="/tmp/fake_project")

    # TD2606-C2：DEBUG 闸门改走 sandbox-first 的 _run_l1_command；模拟其执行异常 → 保守判失败。
    with patch(
        "swarm.worker.l1_pipeline._run_l1_command",
        side_effect=FileNotFoundError("sandbox destroyed"),
    ):
        ok, detail = executor._run_failing_test_gate("python -m pytest test_bug.py -q")

    assert ok is False, f"异常时应保守判失败返回 False（M1），实际: {ok}"
    assert "execution error" in detail
    print("  ✅ _run_failing_test_gate: 执行异常 → 保守失败 False (M1)")


def test_failing_test_gate_timeout_returns_false():
    """命令超时 → 返回 False。"""
    from swarm.worker.executor import WorkerExecutor

    subtask = _make_subtask(
        intent=TaskIntent.DEBUG,
        failing_test_command="python -m pytest test_bug.py -q",
    )
    executor = WorkerExecutor(subtask=subtask, project_path="/tmp/fake_project")

    with patch(
        "subprocess.run",
        side_effect=subprocess.TimeoutExpired(cmd="pytest", timeout=120),
    ):
        ok, detail = executor._run_failing_test_gate("python -m pytest test_bug.py -q")

    assert ok is False, f"超时应返回 False，实际: {ok}"
    assert "timeout" in detail.lower()
    print("  ✅ _run_failing_test_gate: 超时 → False")


def test_main() -> int:
    print("\n🧪 DEBUG 意图排错闭环 单元测试\n")
    tests = [
        test_debug_section_returns_content_for_debug_intent,
        test_debug_section_returns_empty_for_non_debug_intent,
        test_debug_section_graceful_without_failing_test_command,
        test_build_worker_prompt_includes_debug_section_for_debug,
        test_build_worker_prompt_excludes_debug_section_for_modify,
        test_failing_test_gate_passes_when_cmd_succeeds,
        test_failing_test_gate_fails_when_cmd_fails,
        test_failing_test_gate_graceful_on_exception,
        test_failing_test_gate_timeout_returns_false,
    ]
    passed = failed = 0
    for t in tests:
        try:
            t()
            passed += 1
        except Exception as e:
            print(f"  ❌ {t.__name__}: {e}")
            import traceback
            traceback.print_exc()
            failed += 1
    print(f"\n📊 结果: {passed} 通过, {failed} 失败\n")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(test_main())
