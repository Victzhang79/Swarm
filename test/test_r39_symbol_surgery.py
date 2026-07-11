#!/usr/bin/env python3
"""R39-2（round39 治本批）—— C1 缺 owner 符号的确定性外科挂靠通道。

取证（TASK_REGISTER round39 批 R39-1）：C1 闸正确但打回后无符号类修复通道，
LLM 全量重拆三轮缺口 71→71→68 不动（D09 裸文本 issues 对符号缺口无效）。
治本：零 LLM 确定性挂靠——契约条目自带 module 归属（_merge_module_contracts D10
按 (module,name) 合并），按子任务 scope 文件的模块归属把缺 owner 符号点名进该
子任务 contract["symbols"]；挂靠前先查项目存量（棕地已有同名文件=存量承接，
C1 豁免不硬性要求新 owner）；无模块归属/无候选绝不乱挂（防 #28 式毒映射）。
"""
from __future__ import annotations

import importlib.util
from pathlib import Path

_bs = Path(__file__).resolve().parent / "swarm_bootstrap.py"
_spec = importlib.util.spec_from_file_location("swarm_bootstrap", _bs)
_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_mod)

from swarm.brain.contract_utils import (  # noqa: E402
    contract_symbols,
    contract_symbols_with_module,
)
from swarm.brain.plan_validator import validate_contract_ownership  # noqa: E402
from swarm.brain.symbol_surgery import surgical_symbol_attach  # noqa: E402
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


def _sc(interfaces=None, dtos=None):
    sc = {}
    if interfaces is not None:
        sc["interfaces"] = interfaces
    if dtos is not None:
        sc["dtos"] = dtos
    return sc


# ── 口径统一：contract_symbols 与带 module 版必须同源（防两份事实漂移）──

def test_symbols_with_module_parity():
    sc = _sc(
        interfaces=[
            {"name": "IFooService", "module": "mod-a", "signature": "void f()"},
            {"name": "IBarService", "module": "mod-b"},
        ],
        dtos=[{"name": "BazDTO", "module": "mod-b"}, "QuxVO — 描述文字"],
    )
    entries = contract_symbols_with_module(sc)
    assert [e["symbol"] for e in entries] == contract_symbols(sc), (
        "带 module 版与 contract_symbols 必须逐项同序同值（单一事实源）")
    by_sym = {e["symbol"]: e["module"] for e in entries}
    assert by_sym["IFooService"] == "mod-a"
    assert by_sym["BazDTO"] == "mod-b"
    assert by_sym["QuxVO"] == ""  # 字符串条目无模块归属 → 空串


# ── 主路径：按模块确定性挂靠，挂完 C1 必须通过 ──

def test_attach_by_module_then_c1_passes():
    plan = TaskPlan(subtasks=[
        _st("st-1", create=["mod-a/src/main/java/com/x/AController.java"]),
        _st("st-2", create=["mod-b/src/main/java/com/x/BService.java",
                            "mod-b/src/main/java/com/x/BServiceImpl.java"]),
    ], parallel_groups=[["st-1", "st-2"]])
    sc = _sc(interfaces=[
        {"name": "IFooService", "module": "mod-a"},
        {"name": "IBarService", "module": "mod-b"},
        {"name": "IQuxService", "module": "mod-b"},
    ])
    report = surgical_symbol_attach(plan, sc)
    assert set(report["attached"]) == {"IFooService", "IBarService", "IQuxService"}
    assert report["attached"]["IFooService"] == "st-1", "mod-a 符号挂 mod-a 子任务"
    assert report["attached"]["IBarService"] == "st-2"
    assert not report["remainder"]
    # 挂靠后 C1 同口径复核必须通过（挂了但闸仍红=白挂）
    r = validate_contract_ownership(plan, sc)
    assert r.valid and not r.issues


# ── 存量豁免：项目里已有同名文件的符号=存量承接，不挂子任务、C1 不硬性要求 owner ──

def test_baseline_symbols_exempt(tmp_path):
    proj = tmp_path / "proj"
    (proj / "ruoyi-system/src/main/java/com/r").mkdir(parents=True)
    (proj / "ruoyi-system/src/main/java/com/r/ISysConfigService.java").write_text(
        "public interface ISysConfigService {}", encoding="utf-8")
    plan = TaskPlan(subtasks=[
        _st("st-1", create=["mod-a/src/A.java"]),
    ], parallel_groups=[["st-1"]])
    sc = _sc(interfaces=[
        {"name": "ISysConfigService", "module": "ruoyi-system"},
        {"name": "IFooService", "module": "mod-a"},
    ])
    report = surgical_symbol_attach(plan, sc, project_path=str(proj))
    assert report["baseline_owned"] == ["ISysConfigService"], "存量文件命中 → 存量承接"
    assert "ISysConfigService" not in report["attached"]
    # C1 带 project_path：存量符号豁免，不再算 unowned
    r = validate_contract_ownership(plan, sc, project_path=str(proj))
    assert r.valid and not r.issues


# ── 防毒映射：无模块归属 / 无该模块候选子任务 → 留 remainder，绝不乱挂 ──

def test_no_module_or_no_candidate_goes_remainder():
    plan = TaskPlan(subtasks=[
        _st("st-1", create=["mod-a/src/A.java"]),
    ], parallel_groups=[["st-1"]])
    sc = _sc(interfaces=[
        {"name": "IOrphanService", "module": ""},          # 无模块归属
        {"name": "IGhostService", "module": "mod-zzz"},    # 无该模块子任务
    ])
    report = surgical_symbol_attach(plan, sc)
    assert set(report["remainder"]) == {"IOrphanService", "IGhostService"}
    assert not report["attached"]
    assert plan.subtasks[0].contract.get("symbols") is None or \
        not plan.subtasks[0].contract.get("symbols"), "remainder 绝不写进任何子任务"


# ── 单子任务挂靠上限：防 68 个符号全倒进一个 st ──

def test_cap_per_subtask():
    plan = TaskPlan(subtasks=[
        _st("st-1", create=["mod-a/src/A.java"]),
    ], parallel_groups=[["st-1"]])
    syms = [{"name": f"IService{i:02d}", "module": "mod-a"} for i in range(10)]
    report = surgical_symbol_attach(plan, _sc(interfaces=syms), max_per_subtask=4)
    assert len(report["attached"]) == 4
    assert len(report["remainder"]) == 6
    assert len(plan.subtasks[0].contract["symbols"]) == 4


# ── 幂等：重复执行不重复写入 ──

def test_idempotent_rerun():
    plan = TaskPlan(subtasks=[
        _st("st-1", create=["mod-a/src/A.java"]),
    ], parallel_groups=[["st-1"]])
    sc = _sc(interfaces=[{"name": "IFooService", "module": "mod-a"}])
    r1 = surgical_symbol_attach(plan, sc)
    r2 = surgical_symbol_attach(plan, sc)
    assert r1["attached"] == {"IFooService": "st-1"}
    assert not r2["attached"], "已 owned 的符号第二次不再挂"
    assert plan.subtasks[0].contract["symbols"].count("IFooService") == 1


# ── 已 owned 符号不动：外科只补缺口，不碰健康部分 ──

def test_scaffold_only_subtask_never_owns_symbols():
    """对抗复核 CRITICAL：只写构建清单的子任务（脚手架）绝不能成为符号 owner——
    幻影 ownership 骗过 C1 但结构上永远实现不了接口=两张皮复活。"""
    plan = TaskPlan(subtasks=[
        _st("st-scaffold-mod-a", create=["mod-a/pom.xml"]),
        _st("st-1", create=["mod-b/src/B.java"]),
    ], parallel_groups=[["st-scaffold-mod-a", "st-1"]])
    sc = _sc(interfaces=[{"name": "IFooService", "module": "mod-a"}])
    report = surgical_symbol_attach(plan, sc)
    assert report["remainder"] == ["IFooService"], (
        "mod-a 只有 pom 脚手架无代码子任务 → 诚实 remainder，绝不挂脚手架")
    assert not plan.subtasks[0].contract.get("symbols")


def test_repair_declines_when_module_has_no_code_subtask():
    """CRITICAL 端到端面：模块整个没被计划（只有契约设想）→ maybe_symbol_repair
    必须如实 None 回退全量重拆（真缺模块是重拆的正当理由），不得造幻影修复版。"""
    from swarm.brain.symbol_surgery import maybe_symbol_repair
    plan = TaskPlan(subtasks=[
        _st("st-1", create=["mod-b/src/B.java"]),
    ], parallel_groups=[["st-1"]])
    plan.shared_contract = {"dependencies": [
        {"module": "mod-a", "artifacts": ["g:a"]},
        {"module": "mod-b", "artifacts": ["g:b"]},
    ]}
    sc = {"interfaces": [
        {"name": f"IUnplanned{i}Service", "module": "mod-a"} for i in range(5)]}
    msg = "契约符号无 owner 子任务承接 5/5（占比 100% 超阈值 40%）"
    state = {"plan": plan, "shared_contract": sc,
             "plan_validation_feedback": msg, "plan_validation_issues": [msg],
             "replan_feedback": "", "plan_batch_failed_modules": []}
    assert maybe_symbol_repair(state) is None, (
        "注入的脚手架只写 pom，接不住 5 个接口 → C1 复核仍红 → 诚实回退")


def test_owned_symbols_untouched():
    plan = TaskPlan(subtasks=[
        _st("st-1", desc="实现 IAlreadyService 服务", create=["mod-a/src/A.java"]),
        _st("st-2", create=["mod-b/src/B.java"]),
    ], parallel_groups=[["st-1", "st-2"]])
    sc = _sc(interfaces=[
        {"name": "IAlreadyService", "module": "mod-a"},   # 描述已点名=owned
        {"name": "INewService", "module": "mod-b"},
    ])
    report = surgical_symbol_attach(plan, sc)
    assert "IAlreadyService" not in report["attached"]
    assert report["attached"] == {"INewService": "st-2"}
