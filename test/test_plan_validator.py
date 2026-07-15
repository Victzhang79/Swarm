#!/usr/bin/env python3
"""P0 — PlanValidator 单元测试"""

from __future__ import annotations

import importlib.util
from pathlib import Path

_bs = Path(__file__).resolve().parent / "swarm_bootstrap.py"
_spec = importlib.util.spec_from_file_location("swarm_bootstrap", _bs)
_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_mod)

from swarm.brain.plan_validator import validate_plan_structure
from swarm.types import FileScope, SubTask, SubTaskDifficulty, SubTaskModality, TaskPlan


def _st(
    sid: str,
    *,
    writable: list[str] | None = None,
    depends_on: list[str] | None = None,
) -> SubTask:
    return SubTask(
        id=sid,
        description=sid,
        difficulty=SubTaskDifficulty.MEDIUM,
        modality=SubTaskModality.TEXT,
        scope=FileScope(writable=writable or [f"{sid}.py"], readable=[]),
        depends_on=depends_on or [],
    )


def test_valid_plan_passes():
    plan = TaskPlan(
        subtasks=[_st("a"), _st("b", depends_on=["a"])],
        parallel_groups=[["a"], ["b"]],
    )
    r = validate_plan_structure(plan)
    assert r.valid, r.issues


def test_cycle_detected():
    plan = TaskPlan(
        subtasks=[
            _st("a", depends_on=["b"]),
            _st("b", depends_on=["a"]),
        ],
        parallel_groups=[["a", "b"]],
    )
    r = validate_plan_structure(plan)
    assert not r.valid
    assert any("循环" in i for i in r.issues)


def test_parallel_writable_conflict():
    plan = TaskPlan(
        subtasks=[
            _st("a", writable=["shared.py"]),
            _st("b", writable=["shared.py"]),
        ],
        parallel_groups=[["a", "b"]],
    )
    r = validate_plan_structure(plan)
    assert not r.valid
    assert any("并行冲突" in i or "同时写" in i for i in r.issues)


def test_max_writable_files():
    # C1(task 34fab09e)：软上限(6)内合法——一个垂直功能跨分层文件（如导出功能的
    # domain/controller/service/impl）不应被判失败。
    plan_ok = TaskPlan(
        subtasks=[_st("a", writable=["f1.py", "f2.py", "f3.py", "f4.py"])],
        parallel_groups=[["a"]],
    )
    r_ok = validate_plan_structure(plan_ok)
    assert r_ok.valid, f"4 个文件（垂直功能）应合法: {r_ok.issues}"

    # 超软上限(6) 但未超硬上限(12)：仅 warning，不阻断
    plan_warn = TaskPlan(
        subtasks=[_st("a", writable=[f"f{i}.py" for i in range(8)])],
        parallel_groups=[["a"]],
    )
    r_warn = validate_plan_structure(plan_warn)
    assert r_warn.valid, "8 个文件应仅告警不阻断"
    assert any("软上限" in w for w in r_warn.warnings)

    # 超硬上限(12)：判失败
    plan_fail = TaskPlan(
        subtasks=[_st("a", writable=[f"f{i}.py" for i in range(15)])],
        parallel_groups=[["a"]],
    )
    r_fail = validate_plan_structure(plan_fail)
    assert not r_fail.valid
    assert any("硬上限" in i for i in r_fail.issues)


def test_unknown_dependency():
    plan = TaskPlan(
        subtasks=[_st("a", depends_on=["missing"])],
        parallel_groups=[["a"]],
    )
    r = validate_plan_structure(plan)
    assert not r.valid
    assert any("未知任务" in i for i in r.issues)


# ── G1 模块 coherence 闸（Task#9 审计①②）──
from swarm.brain.plan_validator import validate_module_coherence


def _cst(sid, create_files, module=None):
    """带 create_files（+可选 contract.module）的子任务。"""
    sc = FileScope(writable=[], readable=[], create_files=create_files)
    st = SubTask(id=sid, description=sid, difficulty=SubTaskDifficulty.MEDIUM,
                 modality=SubTaskModality.TEXT, scope=sc)
    if module:
        st.contract = {"module": module}
    return st


def _plan_with_contract(subtasks, modules):
    p = TaskPlan(subtasks=subtasks,
                 parallel_groups=[[s.id for s in subtasks]])
    p.shared_contract = {"dependencies": [{"module": m} for m in modules]}
    return p


def test_g1_clean_one_to_one_passes():
    """每模块恰好一个物理目录 → 通过（绝不误伤好 plan）。"""
    plan = _plan_with_contract(
        [_cst("a", ["ruoyi-alarm/alarm-core/src/main/java/Core.java"]),
         _cst("b", ["ruoyi-alarm/alarm-api/src/main/java/Api.java"])],
        ["alarm-core", "alarm-api"])
    r = validate_module_coherence(plan)
    assert r.valid, r.issues


def test_g1_greenfield_no_contract_passes():
    """无契约依赖 + 无 file_plan（单模块/greenfield）→ 无适用面，通过。"""
    plan = TaskPlan(subtasks=[_cst("a", ["src/main/java/X.java"])],
                    parallel_groups=[["a"]])
    r = validate_module_coherence(plan)
    assert r.valid, r.issues


def test_g1_module_multi_dir_fails():
    """① 一个模块散落到多个物理目录 → 硬打回（round62 alarm-api 双落点）。"""
    plan = _plan_with_contract(
        [_cst("a", ["alarm-api/src/main/java/A.java"]),
         _cst("b", ["ruoyi-alarm/alarm-api/src/main/java/B.java"])],
        ["alarm-api"])
    r = validate_module_coherence(plan)
    assert not r.valid
    assert any("alarm-api" in i and "多个物理目录" in i for i in r.issues)


def test_g1_same_dir_collision_fails():
    """② 多个模块塌进同一物理目录 → 硬打回（R59-2）。"""
    plan = _plan_with_contract(
        [_cst("a", ["ruoyi-alarm/alarm-core/src/main/java/A.java"], module="mod-a"),
         _cst("b", ["ruoyi-alarm/alarm-core/src/main/java/B.java"], module="mod-b")],
        ["mod-a", "mod-b"])
    # 两个契约模块的证据都指向同一物理目录 ruoyi-alarm/alarm-core
    # （构造：让两个模块名都出现在同一目录段——用 file_plan 更直接）
    plan.shared_contract = {"dependencies": [{"module": "mod-a"}, {"module": "mod-b"}]}
    fp = [{"module": "mod-a", "path": "ruoyi-alarm/shared/src/main/java/A.java"},
          {"module": "mod-b", "path": "ruoyi-alarm/shared/src/main/java/B.java"}]
    r = validate_module_coherence(plan, file_plan=fp)
    assert not r.valid
    assert any("同一物理目录" in i for i in r.issues)


def test_g1_zero_dir_module_warns_not_fails():
    """契约声明模块但计划里无落点 → 仅 warn（离线不区分幻影 vs 棕地基线，防状态依赖假阳）。"""
    plan = _plan_with_contract(
        [_cst("a", ["ruoyi-alarm/alarm-core/src/main/java/A.java"])],
        ["alarm-core", "alarm-ghost"])
    r = validate_module_coherence(plan)
    assert r.valid, r.issues   # 不因 zero-dir 硬失败
    assert any("alarm-ghost" in w for w in r.warnings)


def test_g1_java_package_name_repeat_passes():
    """★双复核 CRITICAL 回归★：模块名作为尾部包名重复出现，绝不误判成【多个物理目录】。

    `ruoyi-alarm/api/src/main/java/com/ruoyi/alarm/api/X.java` 里 `api` 出现两次（模块顶层
    目录 + 尾部包名），旧实现把包名当第二个物理目录 → 确定性打回惯例命名的单模块 plan（比
    round59 更毒）。扫到源码根即停后必须通过。"""
    plan = _plan_with_contract(
        [_cst("a", ["ruoyi-alarm/api/src/main/java/com/ruoyi/alarm/api/AlarmController.java"])],
        ["api"])
    r = validate_module_coherence(plan)
    assert r.valid, r.issues


def test_g1_cross_module_package_dir_passes():
    """两个正确放置的模块，其中一个的包树里恰好含另一个的名字段 → 不得误判歧义。"""
    plan = _plan_with_contract(
        [_cst("a", ["svc/api/src/main/java/com/x/api/A.java"]),
         _cst("b", ["svc/core/src/main/java/com/x/core/api/B.java"])],
        ["api", "core"])
    r = validate_module_coherence(plan)
    assert r.valid, r.issues


def test_g1_non_src_layout_multi_dir_still_caught():
    """非标准源码布局（flat）下同名模块跨两目录仍应被 file_plan 通道抓到（silent-hunter #1）。"""
    plan = TaskPlan(subtasks=[_cst("a", ["svc-a/app.py"]),
                              _cst("b", ["svc-a-legacy/deploy.py"])],
                    parallel_groups=[["a"], ["b"]])
    plan.shared_contract = {"dependencies": [{"module": "svc-a"}]}
    fp = [{"module": "svc-a", "path": "svc-a/app.py"},
          {"module": "svc-a", "path": "svc-a-legacy/deploy.py"}]
    r = validate_module_coherence(plan, file_plan=fp)
    assert not r.valid
    assert any("svc-a" in i and "多个物理目录" in i for i in r.issues)


def test_g1_rejects_round62_cassette():
    """回归：真 round62 plan（cassette 01520400）必被本闸打回。"""
    import json
    from pathlib import Path
    cf = Path(__file__).resolve().parents[1] / "cassettes" / "01520400_final.json"
    if not cf.exists():
        import pytest
        pytest.skip("cassette 不在本机")
    c = json.loads(cf.read_text())
    plan = TaskPlan.model_validate(c["plan"])
    r = validate_module_coherence(plan, file_plan=c.get("file_plan") or [])
    assert not r.valid
    assert any("alarm-api" in i for i in r.issues)


if __name__ == "__main__":
    test_valid_plan_passes()
    test_cycle_detected()
    test_parallel_writable_conflict()
    test_max_writable_files()
    test_unknown_dependency()
    test_g1_clean_one_to_one_passes()
    test_g1_greenfield_no_contract_passes()
    test_g1_module_multi_dir_fails()
    test_g1_same_dir_collision_fails()
    test_g1_zero_dir_module_warns_not_fails()
    test_g1_rejects_round62_cassette()
    print("test_plan_validator: all passed")
