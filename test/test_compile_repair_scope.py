"""治本 RUN16(st-20 死循环)回归：编译失败根因在 scope 外 → 重试前加宽 scope。

st-20 的 AppAuthInterceptor 用了 spring-webmvc(HandlerInterceptor/ModelAndView),但 ruoyi-alarm
模块 pom 只依赖 ruoyi-common(无 spring-webmvc)→ 编译失败。修复在 pom,但 st-20 scope 改不到 pom
→ 永远编不过 → 死循环。治本：重试前把【模块 pom + 编译错误点名的项目文件】纳入 writable scope。
"""

from __future__ import annotations

from swarm.brain.nodes import _widen_scope_for_compile_repair
from swarm.types import FileScope, SubTask, TaskPlan


def _plan(st):
    return TaskPlan(subtasks=[st])


def test_widen_adds_module_pom_on_missing_dependency():
    """缺依赖类(package does not exist)：报错只点症状文件、不点 pom → 无条件补模块 pom。"""
    st = SubTask(id="st-20", description="鉴权拦截器",
                 scope=FileScope(create_files=[
                     "ruoyi-alarm/src/main/java/com/ruoyi/alarm/filter/AppAuthInterceptor.java"]))
    details = {"l1_2_1_build_ok": False,
               "build_output": "[ERROR] package org.springframework.web.servlet does not exist"}
    added = _widen_scope_for_compile_repair(_plan(st), "st-20", details)
    assert "ruoyi-alarm/pom.xml" in added, f"应补模块 pom,实得 {added}"
    assert "ruoyi-alarm/pom.xml" in st.scope.writable


def test_widen_adds_named_upstream_file():
    """上游签名不符类：编译错误点名了 scope 外的项目文件 → 纳入,让重试能改上游。"""
    st = SubTask(id="st-3", description="过滤器",
                 scope=FileScope(create_files=[
                     "ruoyi-alarm/src/main/java/com/ruoyi/alarm/filter/AppAuthFilter.java"]))
    details = {"l1_2_1_build_ok": False, "build_output": (
        "[ERROR] /workspace/ruoyi-alarm/src/main/java/com/ruoyi/alarm/controller/"
        "AlarmAppController.java:[55,46] cannot find symbol\n  symbol: method selectAppList")}
    added = _widen_scope_for_compile_repair(_plan(st), "st-3", details)
    assert any(f.endswith("controller/AlarmAppController.java") for f in added), \
        f"应纳入报错点名的上游文件,实得 {added}"
    assert any("AlarmAppController.java" in f for f in st.scope.writable)


def test_widen_noop_when_compile_passed():
    """非编译失败(只是 L1 自检/测试问题)不加宽,零行为差。"""
    st = SubTask(id="st-1", description="x",
                 scope=FileScope(create_files=["ruoyi-alarm/src/main/java/com/ruoyi/alarm/X.java"]))
    assert _widen_scope_for_compile_repair(_plan(st), "st-1", {"l1_2_1_build_ok": True}) == []
    assert _widen_scope_for_compile_repair(_plan(st), "st-1", {}) == []


def test_widen_idempotent_no_duplicate():
    """已在 scope 的文件不重复添加。"""
    st = SubTask(id="st-20", description="x", scope=FileScope(
        create_files=["ruoyi-alarm/src/main/java/com/ruoyi/alarm/X.java"],
        writable=["ruoyi-alarm/pom.xml"]))
    details = {"l1_2_1_build_ok": False, "build_output": "package x.y.z does not exist"}
    added = _widen_scope_for_compile_repair(_plan(st), "st-20", details)
    assert "ruoyi-alarm/pom.xml" not in added, "已在 scope 不应重复加"
    assert st.scope.writable.count("ruoyi-alarm/pom.xml") == 1


if __name__ == "__main__":
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for fn in fns:
        fn(); print(f"  ✅ {fn.__name__}")
    print(f"\n=== 编译修复加宽 scope: {len(fns)}/{len(fns)} passed ===")
