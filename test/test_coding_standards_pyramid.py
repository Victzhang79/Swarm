#!/usr/bin/env python3
"""开发规范执行金字塔 L0(自动格式化) + L2(分级规范注入) 单测。"""

from __future__ import annotations

import importlib.util
import os
import tempfile
from pathlib import Path

_bs = Path(__file__).resolve().parent / "swarm_bootstrap.py"
_spec = importlib.util.spec_from_file_location("swarm_bootstrap", _bs)
_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_mod)


# ── L2 分级编码规范注入 ──

def test_coding_standards_core_rules_always_present():
    """所有档位都注入跨语言核心铁律(含禁止硬编码密钥)。"""
    from swarm.types import FileScope, SubTask, SubTaskDifficulty, TaskHarness
    from swarm.worker.coding_standards import build_coding_standards_section

    st = SubTask(
        id="t1", description="x", difficulty=SubTaskDifficulty.TRIVIAL,
        scope=FileScope(), harness=TaskHarness(language="python"),
    )
    section = build_coding_standards_section(st)
    assert "硬编码" in section, "核心铁律缺'禁止硬编码密钥'"
    assert "Scope" in section
    print("  ✅ L2 核心铁律恒在(含禁止硬编码密钥)")


def test_coding_standards_small_model_terse():
    """小模型(trivial/medium)不注入语言细则——避免长指令淹没。"""
    from swarm.types import FileScope, SubTask, SubTaskDifficulty, TaskHarness
    from swarm.worker.coding_standards import build_coding_standards_section

    st = SubTask(
        id="t2", description="x", difficulty=SubTaskDifficulty.MEDIUM,
        scope=FileScope(), harness=TaskHarness(language="python"),
    )
    section = build_coding_standards_section(st)
    assert "细则" not in section, "小模型不应注入语言细则"
    assert "自动处理" in section, "小模型应说明格式/lint 由系统兜底"
    print("  ✅ L2 小模型极简(仅核心铁律 + 工具兜底说明)")


def test_coding_standards_large_model_has_lang_rules():
    """大模型(complex)注入语言细则。"""
    from swarm.types import FileScope, SubTask, SubTaskDifficulty, TaskHarness
    from swarm.worker.coding_standards import build_coding_standards_section

    for lang, kw in [("python", "类型注解"), ("go", "err"), ("rust", "unwrap"),
                     ("node", "TypeScript"), ("java", "try-with-resources")]:
        st = SubTask(
            id=f"c-{lang}", description="架构重构", difficulty=SubTaskDifficulty.COMPLEX,
            scope=FileScope(), harness=TaskHarness(language=lang),
        )
        section = build_coding_standards_section(st)
        assert "细则" in section, f"{lang} 大模型应有语言细则"
        assert kw in section, f"{lang} 细则缺关键内容 {kw!r}"
    print("  ✅ L2 大模型注入 5 语言细则")


def test_coding_standards_injected_in_worker_prompt():
    """编码规范段确实进入 Worker 完整 prompt。"""
    from swarm.types import FileScope, SubTask, SubTaskDifficulty, TaskHarness
    from swarm.worker.prompts import build_worker_prompt

    st = SubTask(
        id="t3", description="实现功能", difficulty=SubTaskDifficulty.COMPLEX,
        scope=FileScope(writable=["a.py"]), harness=TaskHarness(language="python"),
    )
    prompt = build_worker_prompt(st)
    assert "编码规范" in prompt, "Worker prompt 未包含编码规范段"
    print("  ✅ L2 编码规范段进入 Worker prompt")


# ── L0 自动格式化 ──

def test_format_gate_formats_python_if_tool_available():
    """格式化器可用时格式化 Python；不可用则优雅 skip(不崩)。"""
    from swarm.worker.format_gate import _which, format_files

    d = tempfile.mkdtemp(prefix="swarm_fmt_")
    fp = os.path.join(d, "messy.py")
    with open(fp, "w") as f:
        f.write("x=1\ny  =  2\n")  # 风格不规范
    result = format_files(d, ["messy.py"], timeout=30)
    assert result["status"] in ("ok", "partial", "skipped")
    if _which("ruff") or _which("black"):
        assert "messy.py" in result["formatted"], "ruff/black 可用却没格式化"
        # 格式化后应规范化（至少不崩、内容仍是合法 python）
        content = open(fp).read()
        assert "x = 1" in content or "x=1" in content
        print("  ✅ L0 Python 自动格式化生效")
    else:
        print("  ✅ L0 无格式化器时优雅 skip")


def test_format_gate_graceful_skip_unknown_lang():
    """未知语言/无格式化器一律优雅 skip，不抛异常。"""
    from swarm.worker.format_gate import format_files

    d = tempfile.mkdtemp(prefix="swarm_fmt_")
    with open(os.path.join(d, "a.go"), "w") as f:
        f.write("package main\n")
    with open(os.path.join(d, "x.unknown"), "w") as f:
        f.write("whatever\n")
    result = format_files(d, ["a.go", "x.unknown"], timeout=20)
    # 不崩、返回合法结构即可
    assert "status" in result and "formatted" in result and "skipped" in result
    print("  ✅ L0 未知语言/缺工具优雅 skip(不崩)")


def main() -> int:
    print("=== test_coding_standards_pyramid ===")
    failed = 0
    for fn in (
        test_coding_standards_core_rules_always_present,
        test_coding_standards_small_model_terse,
        test_coding_standards_large_model_has_lang_rules,
        test_coding_standards_injected_in_worker_prompt,
        test_format_gate_formats_python_if_tool_available,
        test_format_gate_graceful_skip_unknown_lang,
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
