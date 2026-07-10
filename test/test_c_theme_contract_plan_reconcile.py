#!/usr/bin/env python3
"""主题C（round38c）—— 契约↔计划↔执行三面对账。

取证（forensics_C_theme_code.md）：24 契约接口从未入 PLAN 语料、规则5 落空 98 条纯
log 无消费、L2 才第一次对账（8h 后爆缺失 16/24）；writable 幻觉路径（SysUser.java
声明在不存在的位置）；设计了十几张新表全 diff 零 .sql；契约片 3×600s 原样重放后
静默少片。
治本：C1 validate_contract_ownership（D5 前移 PLAN 期，超阈值 D09 打回）+ 规则5
落空升 warn；规则0 writable 存在性 lint（唯一 basename 重定位/无命中转 create_files）；
C3 file_plan DDL 哨兵；C4 contract_failed_modules 机读键+反馈式重试。
"""
from __future__ import annotations

import importlib.util
import subprocess
from pathlib import Path

_bs = Path(__file__).resolve().parent / "swarm_bootstrap.py"
_spec = importlib.util.spec_from_file_location("swarm_bootstrap", _bs)
_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_mod)

from swarm.brain.contract_utils import (  # noqa: E402
    normalize_plan_scopes,
    unclaimed_contract_deps,
)
from swarm.brain.plan_validator import validate_contract_ownership  # noqa: E402
from swarm.types import (  # noqa: E402
    FileScope,
    SubTask,
    SubTaskDifficulty,
    TaskPlan,
)


def _st(sid, desc="", writable=None, create=None, contract=None):
    return SubTask(id=sid, description=desc or f"task {sid}",
                   difficulty=SubTaskDifficulty.MEDIUM,
                   contract=contract or {},
                   scope=FileScope(writable=writable or [], create_files=create or []))


# ── C1：契约符号→owner 对账 ──

def _sc(interfaces):
    return {"interfaces": interfaces}


def test_c1_owned_symbols_pass():
    plan = TaskPlan(subtasks=[
        _st("st-1", desc="实现 IAlarmRobotService 机器人服务"),
        _st("st-2", create=["mod/src/main/java/com/x/ITemplateRenderService.java"]),
    ], parallel_groups=[["st-1", "st-2"]])
    r = validate_contract_ownership(plan, _sc(["IAlarmRobotService", "ITemplateRenderService"]))
    assert r.valid and not r.warnings, (
        "描述词边界命中 / create_files 文件名命中都算 owner（与 verify D5 同口径）")


def test_c1_unowned_over_ratio_bounces():
    plan = TaskPlan(subtasks=[_st("st-1", desc="实现登录页面")],
                    parallel_groups=[["st-1"]])
    r = validate_contract_ownership(
        plan, _sc(["IEngineService", "IConvergeService", "IEscalateService"]))
    assert not r.valid, (
        "无主符号占比超阈值（3/3=100%>40%）必须打回 PLAN——round38c 24 接口两张皮到"
        " L2 才对账的治本面（D5 前移 8 小时）")
    assert any("IEngineService" in i for i in r.issues), "issues 必须点名符号供 D09 回灌"


def test_c1_unowned_under_ratio_warns_only():
    plan = TaskPlan(subtasks=[
        _st("st-1", desc="实现 IAService 与 IBService 和 ICService"),
    ], parallel_groups=[["st-1"]])
    r = validate_contract_ownership(plan, _sc(["IAService", "IBService", "ICService", "IDService"]))
    assert r.valid, "1/4=25%≤40%：warn 级可观测，不烧 plan 重试预算"
    assert any("IDService" in w for w in r.warnings)


def test_c1_rule5_unclaimed_surfaces_as_warning():
    plan = TaskPlan(subtasks=[
        _st("st-1", create=["mod-a/pom.xml"]),
        _st("st-2", create=["mod-b/pom.xml"]),  # 两个物理模块=无法归并
    ], parallel_groups=[["st-1", "st-2"]])
    plan.shared_contract = {"dependencies": [
        {"module": "mod-c", "artifacts": ["org.x:lib:1.0"]}]}
    assert unclaimed_contract_deps(plan) == [{"module": "mod-c", "artifacts": ["org.x:lib:1.0"]}]
    r = validate_contract_ownership(plan, {})
    assert any("mod-c" in w for w in r.warnings), (
        "规则5 落空必须升 warn 可观测——round38c 98 条 artifacts 落空纯 log 无人消费")


# ── 规则0：writable 存在性 lint（git 基线夹具）──

def _git_repo(tmp_path):
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    d = tmp_path / "ruoyi-common" / "core" / "entity"
    d.mkdir(parents=True)
    (d / "SysUser.java").write_text("class SysUser {}", encoding="utf-8")
    subprocess.run(["git", "add", "-A"], cwd=tmp_path, check=True)
    subprocess.run(["git", "-c", "user.email=t@t", "-c", "user.name=t",
                    "commit", "-qm", "base"], cwd=tmp_path, check=True)
    return tmp_path


def test_rule0_hallucinated_writable_relocated_by_unique_basename(tmp_path):
    repo = _git_repo(tmp_path)
    plan = TaskPlan(subtasks=[
        _st("st-1", writable=["ruoyi-system/domain/SysUser.java"]),  # 幻觉路径（F1 实证形态）
    ], parallel_groups=[["st-1"]])
    normalize_plan_scopes(plan, project_path=str(repo))
    w = plan.subtasks[0].scope.writable
    assert w == ["ruoyi-common/core/entity/SysUser.java"], (
        "writable 不在 base 树且 basename 唯一命中 → 确定性重定位到真身——"
        "SysUser 幻觉路径让 2FA 实体改造整轮不在 diff（F1 裁决实锤）")


def test_rule0_new_file_writable_moved_to_create_files(tmp_path):
    repo = _git_repo(tmp_path)
    plan = TaskPlan(subtasks=[
        _st("st-1", writable=["mod/src/AlarmNew.java"]),
    ], parallel_groups=[["st-1"]])
    normalize_plan_scopes(plan, project_path=str(repo))
    sc = plan.subtasks[0].scope
    assert "mod/src/AlarmNew.java" not in (sc.writable or [])
    assert "mod/src/AlarmNew.java" in (sc.create_files or []), (
        "base 树无此文件且无同名 → 真新建，挪入 create_files（writable 语义=修改既有文件）")


def test_rule0_declared_create_file_untouched(tmp_path):
    repo = _git_repo(tmp_path)
    plan = TaskPlan(subtasks=[
        _st("st-1", create=["mod/src/New.java"]),
        _st("st-2", writable=["mod/src/New.java"]),  # 上游产物：合法 writable
    ], parallel_groups=[["st-1"], ["st-2"]])
    normalize_plan_scopes(plan, project_path=str(repo))
    # 规则0 不动它（∈ 全 plan create_files）；后续规则可能因写权归一调整，仅断言未被误挪
    sc2 = plan.subtasks[1].scope
    assert "mod/src/New.java" not in (sc2.create_files or []), (
        "被兄弟 create_files 声明的文件是上游产物而非幻觉路径，规则0 不得误挪")


# ── C3 哨兵：数据模型有表 ∧ file_plan 无 DDL → degraded ──

def test_c3_ddl_sentinel():
    from swarm.brain.planning_nodes import _package_tech_design_output
    result = {"data_model": "alarm_task 表：id/name/status", "stage2_failed_modules": []}
    out = _package_tech_design_output(
        {"degraded_reasons": []}, result,
        [{"path": "mod/src/A.java", "action": "create"}], [], {})
    assert any("DDL" in r for r in out.get("degraded_reasons") or []), (
        "设计了表但 file_plan 零 migration 文件必须 degraded 可观测——round38c 十几张"
        "新表零 .sql 直到产物取证才发现")
    out2 = _package_tech_design_output(
        {"degraded_reasons": []}, result,
        [{"path": "sql/alarm_task.sql", "action": "create"}], [], {})
    assert not any("DDL" in r for r in out2.get("degraded_reasons") or [])


# ── 对抗复核回归：规则0 三个 CONFIRMED 场景 ──

def test_rule0_module_pom_never_relocated_to_root_pom(tmp_path):
    repo = _git_repo(tmp_path)
    (repo / "pom.xml").write_text("<project/>", encoding="utf-8")
    subprocess.run(["git", "add", "-A"], cwd=repo, check=True)
    subprocess.run(["git", "-c", "user.email=t@t", "-c", "user.name=t",
                    "commit", "-qm", "root pom"], cwd=repo, check=True)
    plan = TaskPlan(subtasks=[
        _st("st-1", writable=["mod-b/pom.xml"]),  # 新模块 pom 被 LLM 误标 writable（规则4 自证常见形态）
    ], parallel_groups=[["st-1"]])
    normalize_plan_scopes(plan, project_path=str(repo))
    sc = plan.subtasks[0].scope
    assert "mod-b/pom.xml" in (sc.create_files or []), (
        "构建清单 basename 绝不按 basename 重定位（撞根 pom=击穿 D1 单写者+模块脚手架蒸发，"
        "对抗复核场景 A CONFIRMED）——新模块 pom 必须走挪 create_files 保住脚手架；"
        "注：writable 里出现根 pom.xml 是规则4 的合法模块注册授权，与重定位无关")
    assert "mod-b/pom.xml" not in (sc.writable or []), "原幻觉 writable 条目应已被挪走"


def test_rule0_double_hallucination_converges_not_hard_fail(tmp_path):
    from swarm.brain.plan_validator import validate_plan_structure
    repo = _git_repo(tmp_path)
    plan = TaskPlan(subtasks=[
        _st("st-1", writable=["ruoyi-system/domain/SysUser.java"]),
        _st("st-2", writable=["ruoyi-admin/entity/SysUser.java"]),  # 两个幻觉路径同 basename
    ], parallel_groups=[["st-1", "st-2"]])
    normalize_plan_scopes(plan, project_path=str(repo))
    r = validate_plan_structure(plan)
    assert r.valid, (
        "双幻觉重定位到同一真身后必须被写权归一/串行化收敛（规则0 已前移到规则1 之前），"
        "绝不双写者直通 plan_validator 硬失败（对抗复核场景 B/C CONFIRMED：把幻觉修成"
        "确定性打回烧光 plan 重试）")


def test_rule0_new_dir_context_not_relocated(tmp_path):
    repo = _git_repo(tmp_path)
    (repo / "core").mkdir()
    (repo / "core" / "StringUtils.java").write_text("class StringUtils {}", encoding="utf-8")
    subprocess.run(["git", "add", "-A"], cwd=repo, check=True)
    subprocess.run(["git", "-c", "user.email=t@t", "-c", "user.name=t",
                    "commit", "-qm", "utils"], cwd=repo, check=True)
    plan = TaskPlan(subtasks=[
        _st("st-1", create=["newmod/util/Helper.java"],
            writable=["newmod/util/StringUtils.java"]),  # 新目录里的合法同名新文件
    ], parallel_groups=[["st-1"]])
    normalize_plan_scopes(plan, project_path=str(repo))
    sc = plan.subtasks[0].scope
    assert "core/StringUtils.java" not in (sc.writable or []), (
        "writable 所在目录有本 plan create_files 兄弟=新目录新文件（合法分层复制），"
        "不得重定位到基线同名文件")
    assert "newmod/util/StringUtils.java" in (sc.create_files or [])


# ── 对抗复核回归：C2 空转修复（归属符号对账真出数据）──

def test_c2_missing_symbols_uses_shared_contract_ownership():
    from swarm.brain.nodes.dispatch import _c2_missing_symbols
    st = _st("st-1", desc="实现 IAlarmEngineService 引擎",
             contract={"input": "告警事件", "output": "收敛结果"})  # 主线真实形状：纯描述
    shared = {"interfaces": ["IAlarmEngineService", "IOtherService"]}
    missing = _c2_missing_symbols(st, shared, diff="+ class Foo {}")
    assert missing == ["IAlarmEngineService"], (
        "C2 必须按 shared_contract 归属符号对账（初版按 st.contract 抽符号=恒空整条空转，"
        "对抗复核 CONFIRMED）；未归属的 IOtherService 不算本子任务的账")
    assert _c2_missing_symbols(
        st, shared, diff="+ public class IAlarmEngineServiceImpl implements IAlarmEngineService {") == []


# ── 对抗复核回归：C4-8 早退路径 always-emit ──

def test_c4_non_ultra_early_return_clears_failed_modules():
    import asyncio as _aio
    from swarm.brain.planning_nodes import contract_design
    out = _aio.run(contract_design({
        "tech_design": {"modules": []}, "complexity": None,
        "assessed_complexity": None}))
    assert out.get("contract_failed_modules") == [], (
        "非 ultra 早退也必须清空机读键——否则上一轮真失败值跨轮粘滞（对抗复核 CONFIRMED）")


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
    print("主题C 全部通过")
