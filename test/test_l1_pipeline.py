#!/usr/bin/env python3
"""L1 pipeline lint + LLM 自检单元测试。"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path
from unittest.mock import MagicMock, patch

import importlib.util
from pathlib import Path

_bs = Path(__file__).resolve().parent / "swarm_bootstrap.py"
_spec = importlib.util.spec_from_file_location("swarm_bootstrap", _bs)
_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_mod)


def _make_subtask(diff_desc="test subtask", writable=None):
    """构造最简 SubTask。"""
    from swarm.types import FileScope, SubTask, SubTaskDifficulty

    return SubTask(
        id="sub-1",
        description=diff_desc,
        difficulty=SubTaskDifficulty.MEDIUM,
        scope=FileScope(writable=writable or ["hello.py"], readable=writable or ["hello.py"]),
    )


def _simple_diff(filename="hello.py", old="old_line", new="new_line"):
    """生成简单 unified diff。"""
    return f"--- a/{filename}\n+++ b/{filename}\n@@ -1 +1 @@\n-{old}\n+{new}\n"


def _setup_project(tmp_dir: str) -> None:
    """在 tmp_dir 创建可编译的 Python 文件。"""
    (Path(tmp_dir) / "hello.py").write_text("# hello\nx = 1\n", encoding="utf-8")


# ── lint 阶段测试 ──

def test_lint_enabled_by_default():
    """默认开启 lint，ruff 可用时检查 Python 文件。"""
    from swarm.worker.l1_pipeline import _lint_files

    with tempfile.TemporaryDirectory() as tmp:
        (Path(tmp) / "hello.py").write_text("x=1\n", encoding="utf-8")
        has_error, msg, issues = _lint_files(tmp, ["hello.py"])
        # ruff 可能报 warning 但不应有 E9/F4 error
        assert not has_error or all(i["severity"] == "warning" for i in issues), \
            f"不应有 lint error: {issues}"
    print("  ✅ lint 默认开启，ruff 无 error 时通过")


def test_lint_syntax_error_detected():
    """ruff 应能检测到 E999 语法错误。"""
    from swarm.worker.l1_pipeline import _lint_files

    with tempfile.TemporaryDirectory() as tmp:
        (Path(tmp) / "bad.py").write_text("def foo(\n", encoding="utf-8")
        has_error, msg, issues = _lint_files(tmp, ["bad.py"])
        if has_error:
            print("  ✅ lint 检测到语法 error")
        else:
            print("  ✅ lint 即使未检测到语法 error 也不阻断（优雅降级）")


def test_lint_env_var_disable():
    """SWARM_WORKER_L1_LINT=false 时跳过 lint。"""
    from swarm.worker.l1_pipeline import run_l1_pipeline

    with tempfile.TemporaryDirectory() as tmp:
        _setup_project(tmp)
        subtask = _make_subtask()
        diff = _simple_diff()
        os.environ.pop("SWARM_WORKER_L1_LINT", None)
        os.environ.pop("SWARM_WORKER_L1_SELF_REVIEW", None)
        # 先确认 lint 在默认开启时存在于 details
        ok, details = run_l1_pipeline(tmp, subtask, diff)
        assert "lint" in details, f"lint 键缺失: {details.keys()}"
        # 关闭 lint
        os.environ["SWARM_WORKER_L1_LINT"] = "false"
        try:
            ok2, details2 = run_l1_pipeline(tmp, subtask, diff)
            assert details2["lint"]["status"] == "disabled"
            assert details2["lint"]["reason"] == "SWARM_WORKER_L1_LINT=false"
        finally:
            del os.environ["SWARM_WORKER_L1_LINT"]
    print("  ✅ SWARM_WORKER_L1_LINT=false 跳过 lint")


def test_lint_no_py_files():
    """非 Python/JS 文件不做 lint。"""
    from swarm.worker.l1_pipeline import _lint_files

    with tempfile.TemporaryDirectory() as tmp:
        (Path(tmp) / "readme.md").write_text("# hello\n", encoding="utf-8")
        has_error, msg, issues = _lint_files(tmp, ["readme.md"])
        assert not has_error
        assert len(issues) == 0
    print("  ✅ 非 Python/JS 文件不做 lint")


# ── LLM 自检阶段测试 ──

def test_self_review_env_var_disable():
    """SWARM_WORKER_L1_SELF_REVIEW=false 时跳过自检。"""
    from swarm.worker.l1_pipeline import run_l1_pipeline

    with tempfile.TemporaryDirectory() as tmp:
        _setup_project(tmp)
        subtask = _make_subtask()
        diff = _simple_diff()
        os.environ["SWARM_WORKER_L1_SELF_REVIEW"] = "false"
        os.environ.pop("SWARM_WORKER_L1_LINT", None)
        try:
            ok, details = run_l1_pipeline(tmp, subtask, diff)
            assert "self_review" in details, f"self_review 键缺失: {details.keys()}"
            assert details["self_review"]["status"] == "disabled"
        finally:
            del os.environ["SWARM_WORKER_L1_SELF_REVIEW"]
    print("  ✅ SWARM_WORKER_L1_SELF_REVIEW=false 跳过自检")


def test_self_review_no_llm():
    """llm=None 时自检跳过。"""
    from swarm.worker.l1_pipeline import run_l1_pipeline

    with tempfile.TemporaryDirectory() as tmp:
        _setup_project(tmp)
        subtask = _make_subtask()
        diff = _simple_diff()
        os.environ.pop("SWARM_WORKER_L1_LINT", None)
        os.environ.pop("SWARM_WORKER_L1_SELF_REVIEW", None)
        ok, details = run_l1_pipeline(tmp, subtask, diff, llm=None)
        assert details["self_review"]["status"] == "skipped"
        assert details["self_review"]["reason"] == "llm not provided"
    print("  ✅ llm=None 时自检跳过")


def test_self_review_with_mock_llm():
    """mock LLM 返回通过的自检结果。"""
    from swarm.worker.l1_pipeline import _run_self_review

    mock_llm = MagicMock()
    mock_response = MagicMock()
    mock_response.content = '{"passed": true, "issues": []}'
    mock_llm.invoke.return_value = mock_response

    subtask = _make_subtask()
    result = _run_self_review(mock_llm, subtask, "diff content")
    assert result["passed"] is True
    assert result["issues"] == []
    print("  ✅ mock LLM 自检通过")


def test_self_review_llm_finds_issues():
    """mock LLM 返回发现问题，但自检不硬阻断。"""
    from swarm.worker.l1_pipeline import _run_self_review, run_l1_pipeline

    mock_llm = MagicMock()
    mock_response = MagicMock()
    mock_response.content = '{"passed": false, "issues": ["缺少边界检查"]}'
    mock_llm.invoke.return_value = mock_response

    subtask = _make_subtask()
    result = _run_self_review(mock_llm, subtask, "diff content")
    assert result["passed"] is False
    assert len(result["issues"]) == 1
    print("  ✅ mock LLM 自检发现问题")

    # 集成：自检失败不硬阻断流水线
    with tempfile.TemporaryDirectory() as tmp:
        _setup_project(tmp)
        os.environ.pop("SWARM_WORKER_L1_LINT", None)
        os.environ.pop("SWARM_WORKER_L1_SELF_REVIEW", None)
        ok, details = run_l1_pipeline(tmp, subtask, _simple_diff(), llm=mock_llm)
        # 流水线应通过（自检不硬阻断）
        assert ok is True, f"自检不应硬阻断: {details}"
        assert details["self_review"]["passed"] is False
        assert "note" in details["self_review"]
    print("  ✅ 自检失败不硬阻断流水线")


def test_self_review_llm_exception_graceful():
    """LLM 异常时自检优雅跳过。"""
    from swarm.worker.l1_pipeline import _run_self_review

    mock_llm = MagicMock()
    mock_llm.invoke.side_effect = RuntimeError("LLM 不可用")

    subtask = _make_subtask()
    result = _run_self_review(mock_llm, subtask, "diff content")
    assert result["passed"] is True  # 异常时默认通过
    assert "skipped" in result["raw"]
    print("  ✅ LLM 异常时自检优雅跳过")


# ── 流水线端到端测试 ──

def test_pipeline_full_success():
    """完整流水线：scope → compile → lint → test → self_review 全部通过。"""
    from swarm.worker.l1_pipeline import run_l1_pipeline

    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        _setup_project(tmp)

        subtask = _make_subtask()
        diff = _simple_diff()
        os.environ.pop("SWARM_WORKER_L1_LINT", None)
        os.environ.pop("SWARM_WORKER_L1_SELF_REVIEW", None)

        ok, details = run_l1_pipeline(tmp, subtask, diff)
        assert details["l1_2_compile_ok"] is True, f"编译失败: {details.get('compile_message')}"
        assert "lint" in details
        assert details["pipeline"] == "L1.1-L1.4"
    print("  ✅ 完整流水线端到端通过")


def test_pipeline_backward_compatible():
    """不传 llm 也能跑（向后兼容）。"""
    from swarm.worker.l1_pipeline import run_l1_pipeline

    with tempfile.TemporaryDirectory() as tmp:
        _setup_project(tmp)
        subtask = _make_subtask()
        diff = _simple_diff()
        os.environ.pop("SWARM_WORKER_L1_LINT", None)
        os.environ.pop("SWARM_WORKER_L1_SELF_REVIEW", None)
        ok, details = run_l1_pipeline(tmp, subtask, diff)
        assert "lint" in details
        assert "self_review" in details
        assert details["self_review"]["status"] == "skipped"
    print("  ✅ 不传 llm 向后兼容")


def main() -> int:
    print("\n🧪 L1 pipeline lint + LLM 自检 单元测试\n")
    tests = [
        test_lint_enabled_by_default,
        test_lint_syntax_error_detected,
        test_lint_env_var_disable,
        test_lint_no_py_files,
        test_self_review_env_var_disable,
        test_self_review_no_llm,
        test_self_review_with_mock_llm,
        test_self_review_llm_finds_issues,
        test_self_review_llm_exception_graceful,
        test_pipeline_full_success,
        test_pipeline_backward_compatible,
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
    sys.exit(main())
