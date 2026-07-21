"""DR-PM66-C4(#111) 红测试（对抗双复核整改后）：内容断言扫描路径不得越出【本子任务声明 scope
＋本模块区域】。

★复核整改要点★：正确不变量不是"⊆ writable 文件"（过严）——
  · 整模块目录级基线断言（lombok 禁令，挂脚手架、只 owns pom.xml、故意扫全模块）→ 保留；
  · 针对 readable 契约文件的验证断言 → 保留；
  · 只有【外模块】scope 泄漏才剔除。
round66 st-32 的真根（负断言未锚定 import）由 #102 治，本闸只堵跨模块责任错配。
"""
from __future__ import annotations

from swarm.brain.contract_utils import sanitize_verify_scope
from swarm.types import FileScope, SubTask, TaskHarness, TaskPlan

_MOD = "ruoyi-alarm"
_MINE = f"{_MOD}/src/main/java/com/ruoyi/alarm/engine/AlarmEngineStateManager.java"
_IFACE = f"{_MOD}/src/main/java/com/ruoyi/alarm/task/service/IAlarmTaskService.java"
_FOREIGN = "ruoyi-admin/src/main/java/com/ruoyi/web/controller/Foo.java"


def _st(sid, *, create=None, writable=None, readable=None, vcs=None):
    return SubTask(
        id=sid, description="d",
        scope=FileScope(writable=writable or [], create_files=create or [], readable=readable or []),
        harness=TaskHarness(language="java", verify_commands=vcs or []),
    )


def _vcs(plan):
    return plan.subtasks[0].harness.verify_commands


def test_111_module_baseline_gate_preserved():
    # 脚手架子任务只 owns pom.xml，负断言故意扫全模块（lombok 禁令）→ 必须原样保留（复核 HIGH）。
    st = _st("st-1", create=[f"{_MOD}/pom.xml"],
             vcs=[f"! grep -rq 'lombok' {_MOD}/"])
    plan = TaskPlan(subtasks=[st])
    sanitize_verify_scope(plan)
    assert _vcs(plan) == [f"! grep -rq 'lombok' {_MOD}/"], "整模块基线断言被误收窄/删除"


def test_111_readable_contract_file_scan_preserved():
    # 只 readable 一个契约文件、grep 它坐实依赖提供某方法 → 必须保留（复核 HIGH：readable 不得误删）。
    st = _st("st-c", create=["ruoyi-admin/src/main/java/com/ruoyi/web/C.java"],
             readable=[_IFACE], vcs=[f"grep -q 'selectAlarmTaskById' {_IFACE}"])
    plan = TaskPlan(subtasks=[st])
    sanitize_verify_scope(plan)
    assert _vcs(plan) == [f"grep -q 'selectAlarmTaskById' {_IFACE}"], "readable 契约验证断言被误删"


def test_111_own_module_dir_scan_preserved():
    # st-32 型：扫自己模块 engine/ 目录 → 保留（真根由 #102 锚定 import 治，非本闸收窄）。
    st = _st("st-32", create=[_MINE],
             vcs=["! grep -rE 'import lombok|javax\\.' ruoyi-alarm/src/main/java/com/ruoyi/alarm/engine/"])
    plan = TaskPlan(subtasks=[st])
    sanitize_verify_scope(plan)
    assert _vcs(plan)[0].endswith("engine/"), "本模块目录断言被误收窄"


def test_111_foreign_module_scan_dropped():
    # 子任务 owns ruoyi-alarm 文件，却扫 ruoyi-admin 外模块 → 整条剔除（外模块泄漏）。
    st = _st("st-x", create=[_MINE], vcs=[f"! grep -rn 'lombok' {_FOREIGN}"])
    plan = TaskPlan(subtasks=[st])
    summary = sanitize_verify_scope(plan)
    assert _vcs(plan) == [], "外模块 scope 泄漏未被剔除"
    assert "st-x" in summary and summary["st-x"]["dropped"]


def test_111_mixed_foreign_and_own_narrowed():
    st = _st("st-m", create=[_MINE],
             vcs=[f"! grep -rn 'lombok' {_MOD}/ {_FOREIGN}"])
    plan = TaskPlan(subtasks=[st])
    sanitize_verify_scope(plan)
    out = _vcs(plan)[0]
    assert f"{_MOD}/" in out and _FOREIGN not in out, f"外模块参数未剔除/本模块误删: {out}"


def test_111_build_command_untouched():
    st = _st("st-b", create=[_MINE], vcs=["mvn -pl ruoyi-alarm -am -q compile"])
    plan = TaskPlan(subtasks=[st])
    sanitize_verify_scope(plan)
    assert _vcs(plan) == ["mvn -pl ruoyi-alarm -am -q compile"], "构建命令被误动"
