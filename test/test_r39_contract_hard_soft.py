#!/usr/bin/env python3
"""R39-3（round39 治本批）—— 契约承接量对账：C1 硬/软符号分级。

取证（R39-1）：round39 胖契约 103 符号 = 37 接口 + 67 DTO（34 处边界重叠自并膨胀），
DTO 名全部计入 C1 硬性对账 → 40% 阈值三轮必挂 FAILED@PLAN。C1 原始意图（round38c）
是"接口两张皮"——24 个契约【接口】从未进计划语料；DTO 是后来"复核补漏"为 L2 可见性
加进提取面的，把它算进规划期硬性打回比率=口径过严（DTO 随其 Service 文件落地，
不必逐名出现在子任务语料）。

治本（确定性口径分级，不裁契约不瞎 L2）：
  - 硬性（进打回比率）：interfaces / types / apis / symbols —— 结构两张皮真信号；
  - 软性（unowned 只 warn）：dtos / fields / methods + 成员符号（如 X.Builder，
    前缀符号已在集合内=自并膨胀产物）；
  - contract_symbols 全量提取不变（L2 D5 消费面零影响）。
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
from swarm.types import (  # noqa: E402
    FileScope,
    SubTask,
    SubTaskDifficulty,
    TaskPlan,
)


def _st(sid, desc="", create=None, contract=None):
    return SubTask(id=sid, description=desc or f"task {sid}",
                   difficulty=SubTaskDifficulty.MEDIUM,
                   contract=contract or {},
                   scope=FileScope(writable=[], create_files=create or []))


# ── kind 标注：条目携带来源键，硬/软可判 ──

def test_entries_carry_kind():
    sc = {
        "interfaces": [{"name": "IFooService", "module": "m-a"}],
        "dtos": [{"name": "BarDTO", "module": "m-a"}],
        "apis": ["GET /api/foo/list — 查询"],
    }
    entries = contract_symbols_with_module(sc)
    kinds = {e["symbol"]: e["kind"] for e in entries}
    assert kinds["IFooService"] == "interfaces"
    assert kinds["BarDTO"] == "dtos"
    assert kinds["list"] == "apis"
    # 全量提取口径不变（L2 消费面）
    assert contract_symbols(sc) == ["IFooService", "BarDTO", "list"]


# ── DTO 洪水不再击穿闸门：接口全 owned + 60 个 DTO unowned → valid（软 warn）──

def test_dto_flood_does_not_bounce():
    plan = TaskPlan(subtasks=[
        _st("st-1", desc="实现 IFooService 与 IBarService 服务",
            create=["m-a/src/A.java"]),
    ], parallel_groups=[["st-1"]])
    sc = {
        "interfaces": [{"name": "IFooService", "module": "m-a"},
                       {"name": "IBarService", "module": "m-a"}],
        "dtos": [{"name": f"Alarm{i:02d}DTO", "module": "m-a"} for i in range(60)],
    }
    r = validate_contract_ownership(plan, sc)
    assert r.valid, "硬性符号（接口）全 owned → DTO unowned 只软告警，不打回"
    assert r.warnings, "软性 unowned 仍可观测（warn 面不静默）"


# ── 闸门牙齿不拔：硬性接口超阈值照样打回 ──

def test_hard_interfaces_still_bounce():
    plan = TaskPlan(subtasks=[_st("st-1", desc="登录页面")],
                    parallel_groups=[["st-1"]])
    sc = {"interfaces": [
        {"name": f"IService{i}", "module": "m-a"} for i in range(10)]}
    r = validate_contract_ownership(plan, sc)
    assert not r.valid, "接口 10/10 无 owner（100%>40%）必须照旧打回——闸门牙齿不拔"


# ── 成员符号（X.Builder，前缀已在集合）= 自并膨胀产物 → 软性 ──

def test_member_dotted_symbol_is_soft():
    plan = TaskPlan(subtasks=[
        _st("st-1", desc="实现 AlarmSimpleUtil 工具", create=["m-a/src/A.java"]),
    ], parallel_groups=[["st-1"]])
    sc = {"interfaces": [
        {"name": "AlarmSimpleUtil", "module": "m-a"},
        {"name": "AlarmSimpleUtil.Builder", "module": "m-a"},
    ]}
    r = validate_contract_ownership(plan, sc)
    assert r.valid, ("AlarmSimpleUtil 已 owned；成员符号 .Builder 是同一交付物的"
                     "内部结构（自并膨胀），unowned 不应计入硬性比率")


# ── 混合场景：硬性 40% 阈值只按硬性分母算 ──

def test_ratio_uses_hard_denominator():
    # 3 接口 2 owned 1 unowned（33%≤40% 不打回），外加 50 个 unowned DTO
    plan = TaskPlan(subtasks=[
        _st("st-1", desc="实现 IAService 与 IBService", create=["m-a/src/A.java"]),
    ], parallel_groups=[["st-1"]])
    sc = {
        "interfaces": [{"name": "IAService", "module": "m-a"},
                       {"name": "IBService", "module": "m-a"},
                       {"name": "ICService", "module": "m-a"}],
        "dtos": [{"name": f"D{i:02d}DTO", "module": "m-a"} for i in range(50)],
    }
    r = validate_contract_ownership(plan, sc)
    assert r.valid, "硬性 unowned 1/3=33% ≤ 40%：DTO 洪水不得混进分母把比率顶爆"
