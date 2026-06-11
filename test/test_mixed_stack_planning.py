#!/usr/bin/env python3
"""#4 混编项目编排：按技术栈拆分后，每子任务正确推断语言+模板+沙箱隔离。"""

from __future__ import annotations

import importlib.util
from pathlib import Path

_bs = Path(__file__).resolve().parent / "swarm_bootstrap.py"
_spec = importlib.util.spec_from_file_location("swarm_bootstrap", _bs)
_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_mod)


def test_harness_prefers_produced_language_over_readable():
    """混编子任务：写 Java 文件、只读 Vue 文件 → 应推断 java(产出语言)，非 node。"""
    from swarm.brain.nodes import _infer_harness
    from swarm.types import FileScope

    # 后端子任务：写 .java，readable 含前端 .vue/.ts 作上下文
    scope = FileScope(
        writable=["src/main/java/AuthController.java"],
        readable=["frontend/src/Login.vue", "frontend/src/api.ts"],
    )
    h = _infer_harness("实现登录后端接口", scope)
    assert h.language == "java", f"应按产出文件推断 java，实际 {h.language}"
    print("  ✅ 混编子任务按产出语言(writable)推断，不被 readable 干扰")


def test_per_stack_subtasks_get_distinct_languages():
    """混编项目按栈拆分后，前端/后端/脚本子任务各自语言正确。"""
    from swarm.brain.nodes import _infer_harness
    from swarm.types import FileScope

    cases = [
        (FileScope(create_files=["web/src/Login.vue"]), "node"),
        (FileScope(writable=["api/src/main/java/Auth.java"]), "java"),
        (FileScope(create_files=["scripts/migrate.py"]), "python"),
        (FileScope(writable=["cmd/server/main.go"]), "go"),
    ]
    for scope, expected in cases:
        h = _infer_harness("混编子任务", scope)
        assert h.language == expected, f"{scope.writable or scope.create_files} → {h.language}，期望 {expected}"
    print("  ✅ 按栈拆分的子任务各自语言推断正确(node/java/python/go)")


def test_each_stack_maps_to_its_template():
    """每个技术栈子任务映射到各自语言的沙箱模板(沙箱隔离)。"""
    from swarm.config.settings import SandboxConfig

    c = SandboxConfig()
    # 前端 node、后端 java 用不同镜像 → 沙箱工具链隔离
    assert c.template_for_language("node") != c.template_for_language("java")
    assert c.template_for_language("python") != c.template_for_language("go")
    print("  ✅ 各栈映射到不同沙箱模板(前端/后端工具链隔离)")


def test_mixed_scope_dominant_when_no_produced_ext():
    """produced 无后缀(纯目录)时，回退到 produced+readable 合并判断不崩。"""
    from swarm.brain.nodes import _infer_harness
    from swarm.types import FileScope

    scope = FileScope(writable=[], readable=["a.py", "b.py"])
    h = _infer_harness("改点东西", scope)
    # 不崩、能给出语言或兜底
    assert h is not None
    print("  ✅ produced 无后缀时优雅回退(不崩)")


def test_dominant_language_ignores_stray_other_lang_file():
    """[回归] 大量 Java 中夹带 1 个 .js 不应被误判为 node(真实 RuoYi e2e 暴露的 bug)。"""
    from swarm.brain.nodes import _infer_harness
    from swarm.types import FileScope

    files = [f"src/Foo{i}.java" for i in range(87)] + ["static/highlight.min.js"]
    h = _infer_harness("给 StringUtils 加 isMobile 方法", FileScope(writable=files))
    assert h.language == "java", f"主导语言应为 java，实际 {h.language}"
    print("  ✅ [回归] 主导语言判定：87 java + 1 js → java(不被夹带文件误判)")


def test_simple_plan_scope_not_over_inflated():
    """[回归] SIMPLE 路径不再把所有检索候选塞进 writable(RuoYi e2e 暴露:88→1)。

    描述点了类名 StringUtils → writable 应精确命中 StringUtils.java，
    其余检索文件作 readable 上下文。
    """
    from swarm.brain.nodes import _build_simple_plan

    desc = "在 ruoyi-common 模块给字符串工具类 StringUtils 增加 isMobile 方法"
    affected = ["ruoyi-common/src/main/java/com/ruoyi/common/utils/StringUtils.java"] + [
        f"ruoyi-admin/src/x/Foo{i}.java" for i in range(87)
    ]
    plan = _build_simple_plan(desc, affected)
    sc = plan.subtasks[0].scope
    assert len(sc.writable) == 1, f"writable 应精确为 1，实际 {len(sc.writable)}"
    assert "StringUtils.java" in sc.writable[0]
    assert len(sc.readable) == 88, "全部候选应作 readable 上下文"
    print("  ✅ [回归] SIMPLE scope 精准化：88 候选 → writable 仅 StringUtils.java")


def test_simple_plan_no_match_uses_allow_any():
    """[回归] 描述未命中任何候选文件名 → writable 空 + allow_any(让 worker 自定位)。"""
    from swarm.brain.nodes import _build_simple_plan

    plan = _build_simple_plan("优化一下性能", ["a/Foo.java", "b/Bar.java"])
    sc = plan.subtasks[0].scope
    assert len(sc.writable) == 0 and sc.allow_any, "无命中应 writable 空 + allow_any"
    print("  ✅ [回归] 无文件名命中 → allow_any 兜底(不盲圈整个候选集)")


def main() -> int:
    print("=== test_mixed_stack_planning ===")
    failed = 0
    for fn in (
        test_harness_prefers_produced_language_over_readable,
        test_per_stack_subtasks_get_distinct_languages,
        test_each_stack_maps_to_its_template,
        test_mixed_scope_dominant_when_no_produced_ext,
        test_dominant_language_ignores_stray_other_lang_file,
        test_simple_plan_scope_not_over_inflated,
        test_simple_plan_no_match_uses_allow_any,
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
