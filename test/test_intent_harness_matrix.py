#!/usr/bin/env python3
"""W0 地基：TaskIntent 分类 + 5 语言 harness 工具链矩阵 单测。"""

from __future__ import annotations

import importlib.util
from pathlib import Path

_bs = Path(__file__).resolve().parent / "swarm_bootstrap.py"
_spec = importlib.util.spec_from_file_location("swarm_bootstrap", _bs)
_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_mod)


# ── TaskIntent 数据模型 + 向后兼容 ──

def test_subtask_intent_defaults_modify():
    """不传 intent 的 SubTask 默认 MODIFY（向后兼容旧 plan）。"""
    from swarm.types import FileScope, SubTask, TaskIntent

    st = SubTask(id="t1", description="改个 bug", scope=FileScope())
    assert st.intent == TaskIntent.MODIFY
    print("  ✅ SubTask.intent 默认 MODIFY（向后兼容）")


def test_taskplan_parses_intent_from_llm_json():
    """LLM 输出含 intent 字段时，TaskPlan(**result) 自动解析。"""
    from swarm.types import TaskIntent, TaskPlan

    plan = TaskPlan(
        **{
            "subtasks": [
                {"id": "st-1", "description": "审计", "intent": "audit",
                 "scope": {"writable": [], "readable": []}},
            ],
            "parallel_groups": [["st-1"]],
        }
    )
    assert plan.subtasks[0].intent == TaskIntent.AUDIT
    print("  ✅ TaskPlan 从 LLM JSON 解析 intent")


# ── 意图启发式推断 ──

def test_infer_intent_heuristics():
    from swarm.brain.nodes import _infer_intent
    from swarm.types import TaskIntent

    cases = [
        ("对项目做一次安全审计，找出漏洞", TaskIntent.AUDIT),
        ("这个接口报错了，帮我排错修复", TaskIntent.DEBUG),
        ("重构 user 模块，拆分成多个文件", TaskIntent.REFACTOR),
        ("写一个推箱子游戏", TaskIntent.CREATE),
        ("给登录函数加一个参数", TaskIntent.MODIFY),
    ]
    for desc, expected in cases:
        got = _infer_intent(desc)
        assert got == expected, f"{desc!r} → {got} (期望 {expected})"
    # greenfield 标志强制 CREATE
    assert _infer_intent("随便", greenfield=True) == TaskIntent.CREATE
    print("  ✅ _infer_intent 五类意图启发式正确")


# ── 5 语言 harness 工具链矩阵 ──

def test_harness_matrix_all_languages():
    """5 语言 harness 必须齐备 build/test/lint/sast + 白名单。"""
    from swarm.brain.nodes import _infer_harness
    from swarm.types import FileScope

    matrix = {
        "python": "a.py",
        "node": "a.ts",
        "go": "a.go",
        "rust": "a.rs",
        "java": "A.java",
    }
    for lang, fname in matrix.items():
        h = _infer_harness("实现功能", FileScope(writable=[fname]))
        assert h.language == lang, f"{fname} → {h.language}，期望 {lang}"
        assert h.build_command, f"{lang} 缺 build_command"
        # S1(task 34fab09e)：test_command 默认为空，不强制跑测试。
        assert h.test_command == "", f"{lang} 默认不应带 test_command"
        assert h.lint_command, f"{lang} 缺 lint_command"
        assert h.sast_command, f"{lang} 缺 sast_command"
        assert h.extra_whitelist, f"{lang} 缺 extra_whitelist"
    print("  ✅ 5 语言 harness 矩阵齐备(build/test/lint/sast/whitelist)")


def test_harness_fallback_has_secret_scan():
    """未识别语言的兜底 harness 仍放行跨语言密钥扫描。"""
    from swarm.brain.nodes import _infer_harness
    from swarm.types import FileScope

    h = _infer_harness("做点什么", FileScope())
    assert any("gitleaks" in w or "trufflehog" in w for w in h.extra_whitelist)
    print("  ✅ 兜底 harness 含密钥扫描白名单")


def test_language_to_template_mapping():
    """#5: 语言→预建沙箱模板 ID 映射；未知语言回退默认模板。"""
    from swarm.config.settings import SandboxConfig

    c = SandboxConfig()
    # 5 语言各映射到非空模板
    for lang in ("python", "node", "java", "go", "rust"):
        tpl = c.template_for_language(lang)
        assert tpl and tpl.startswith("tpl-"), f"{lang} 未映射到模板: {tpl!r}"
    # 5 个模板互不相同
    tpls = {c.template_for_language(l) for l in ("python", "node", "java", "go", "rust")}
    assert len(tpls) == 5, f"模板应互不相同，实际 {tpls}"
    # 未知语言回退默认
    assert c.template_for_language("ruby") == c.default_template
    assert c.template_for_language("") == c.default_template
    print("  ✅ #5 语言→模板映射正确(5 语言各异，未知回退默认)")


def main() -> int:
    print("=== test_intent_harness_matrix ===")
    failed = 0
    for fn in (
        test_subtask_intent_defaults_modify,
        test_taskplan_parses_intent_from_llm_json,
        test_infer_intent_heuristics,
        test_harness_matrix_all_languages,
        test_harness_fallback_has_secret_scan,
        test_language_to_template_mapping,
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
