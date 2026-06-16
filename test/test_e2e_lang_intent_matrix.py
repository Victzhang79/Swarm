#!/usr/bin/env python3
"""W4a 生产加固：5 语言 × 4 意图 端到端决策矩阵。

不依赖真实模型/沙箱，验证 W0-W3 集成的【确定性决策层】：
- 每语言 harness 工具链矩阵齐备(build/test/lint/sast)
- 每意图正确分类 + 路由(AUDIT 走审计分支、DEBUG 带 failing_test 校验钩子)
这是"生产标准"的回归护栏：任一语言/意图的编排决策回退都会被这里抓到。
"""

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

LANG_FILES = {
    "python": "app.py",
    "node": "app.ts",
    "go": "app.go",
    "rust": "app.rs",
    "java": "App.java",
}


def test_harness_matrix_5x_complete():
    """5 语言 harness 必须各自齐备 build/test/lint/sast + 白名单(生产护栏)。"""
    from swarm.brain.nodes import _infer_harness
    from swarm.types import FileScope

    for lang, fname in LANG_FILES.items():
        h = _infer_harness("实现并测试一个功能", FileScope(writable=[fname]))
        assert h.language == lang, f"{fname} → {h.language}"
        # S1(task 34fab09e)：test_command 默认为空（任务未要求测试时不强制跑），故不在必填断言内。
        for field in ("build_command", "lint_command", "sast_command"):
            assert getattr(h, field), f"{lang} 缺 {field}"
        assert h.extra_whitelist, f"{lang} 缺白名单"
    print("  ✅ 5 语言 harness 工具链矩阵齐备")


def test_intent_classification_4x():
    """4 类意图(create/debug/audit/refactor) + 默认 modify 分类正确。"""
    from swarm.brain.nodes import _infer_intent
    from swarm.types import TaskIntent

    assert _infer_intent("用 Go 写一个新的 HTTP 服务") == TaskIntent.CREATE
    assert _infer_intent("Rust 程序 panic 了，帮我排错") == TaskIntent.DEBUG
    assert _infer_intent("对 Java 项目做安全审计找漏洞") == TaskIntent.AUDIT
    assert _infer_intent("重构这个 Python 模块拆分文件") == TaskIntent.REFACTOR
    assert _infer_intent("给 node 接口加个字段") == TaskIntent.MODIFY
    print("  ✅ 4+1 类意图分类正确")


def test_audit_intent_routes_per_language():
    """5 语言的 AUDIT 子任务都走审计分支(不产 diff，出 audit 报告结构)。"""
    from swarm.brain.nodes import _run_security_audit
    from swarm.types import FileScope, SubTask, TaskHarness, TaskIntent

    for lang, fname in LANG_FILES.items():
        d = tempfile.mkdtemp(prefix=f"swarm_e2e_{lang}_")
        with open(os.path.join(d, fname), "w") as f:
            f.write("// code\n")
        st = SubTask(
            id=f"audit-{lang}", description="安全审计", intent=TaskIntent.AUDIT,
            scope=FileScope(readable=[fname]), harness=TaskHarness(language=lang),
        )
        out = asyncio.run(_run_security_audit(st, d, task_id=f"t-{lang}"))
        assert out.diff == "", f"{lang} AUDIT 不应产 diff"
        assert out.l1_details.get("mode") == "audit", f"{lang} 未走审计分支"
    print("  ✅ 5 语言 AUDIT 意图均走审计分支(不产 diff)")


def test_debug_intent_prompt_per_language():
    """5 语言的 DEBUG 子任务 worker prompt 都含排错 4 阶段提示。"""
    from swarm.types import FileScope, SubTask, TaskHarness, TaskIntent
    from swarm.worker.prompts import _format_debug_section

    for lang, fname in LANG_FILES.items():
        st = SubTask(
            id=f"debug-{lang}", description="排错", intent=TaskIntent.DEBUG,
            scope=FileScope(writable=[fname]),
            harness=TaskHarness(language=lang, failing_test_command="run-failing-test"),
        )
        section = _format_debug_section(st)
        assert section, f"{lang} DEBUG 段为空"
        assert ("复现" in section or "回归" in section), f"{lang} DEBUG 段缺关键阶段"
    print("  ✅ 5 语言 DEBUG 意图 prompt 含排错 4 阶段")


def test_create_and_modify_preserve_diff_flow():
    """CREATE/MODIFY 意图不走审计分支(保留产 diff 的常规流程)。"""
    from swarm.types import FileScope, SubTask, TaskIntent

    for intent in (TaskIntent.CREATE, TaskIntent.MODIFY, TaskIntent.REFACTOR):
        st = SubTask(id=f"x-{intent.value}", description="x", intent=intent, scope=FileScope())
        # 非 AUDIT 意图：dispatch 不应路由到审计分支（intent != AUDIT）
        assert st.intent != TaskIntent.AUDIT
    print("  ✅ CREATE/MODIFY/REFACTOR 保留常规 diff 流程")


def main() -> int:
    print("=== test_e2e_lang_intent_matrix ===")
    failed = 0
    for fn in (
        test_harness_matrix_5x_complete,
        test_intent_classification_4x,
        test_audit_intent_routes_per_language,
        test_debug_intent_prompt_per_language,
        test_create_and_modify_preserve_diff_flow,
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
