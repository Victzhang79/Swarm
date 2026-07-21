"""B1 round66 龙头闸红测试：#110 跨模块同 FQN REJECT / #101 契约权威去冲突 / #112 契约签名↔描述分叉。

三条都是 round66 (task=97a56c3d) 三路复盘定案的规划期确定性缺陷。修复前这些 plan 直穿 DISPATCH
死在 L2/worker 空转；修复后规划期即被确定性拦下或归一。
"""
from __future__ import annotations

from swarm.brain.contract_utils import deconflict_cross_module_creates
from swarm.brain.plan_validator import (
    validate_contract_signature_source,
    validate_module_coherence,
)
from swarm.types import FileScope, SubTask, TaskHarness, TaskPlan

_ALARM = "ruoyi-alarm/src/main/java/com/ruoyi/alarm/appkey/domain/AlarmAppSecret.java"
_ADMIN = "ruoyi-admin/src/main/java/com/ruoyi/alarm/appkey/domain/AlarmAppSecret.java"


def _st(sid, *, create=None, writable=None, readable=None, depends=None, desc="d", lang="java"):
    return SubTask(
        id=sid, description=desc,
        scope=FileScope(writable=writable or [], create_files=create or [], readable=readable or []),
        harness=TaskHarness(language=lang), depends_on=depends or [],
    )


# ── #110 跨模块同 FQN REJECT（validate_module_coherence 新增 ③）──────────────────
def test_110_cross_module_same_fqn_rejected():
    plan = TaskPlan(subtasks=[_st("st-alarm", create=[_ALARM]), _st("st-admin", create=[_ADMIN])])
    res = validate_module_coherence(plan)
    assert not res.valid, "同 FQN 跨模块重复 create 未被 #110 拦截"
    assert any("全限定类" in i and "副本遮蔽" in i for i in res.issues), res.issues


def test_110_same_fqn_single_module_ok():
    plan = TaskPlan(subtasks=[_st("st-alarm", create=[_ALARM])])
    assert validate_module_coherence(plan).valid


def test_110_distinct_fqn_different_modules_ok():
    a = "ruoyi-alarm/src/main/java/com/ruoyi/alarm/A.java"
    b = "ruoyi-admin/src/main/java/com/ruoyi/web/B.java"
    plan = TaskPlan(subtasks=[_st("st-a", create=[a]), _st("st-b", create=[b])])
    assert validate_module_coherence(plan).valid


def test_110_non_jvm_same_relpath_not_flagged():
    # Go：同相对路径落不同模块合法（import 由模块限定）——绝不误伤。
    ga = "svc-a/internal/foo/bar.go"
    gb = "svc-b/internal/foo/bar.go"
    plan = TaskPlan(subtasks=[_st("st-a", create=[ga], lang="go"),
                              _st("st-b", create=[gb], lang="go")])
    assert validate_module_coherence(plan).valid, "非 JVM 同相对路径被 #110 误判"


# ── #101 契约权威去冲突归一（deconflict_cross_module_creates）────────────────────
def test_101_deconflict_strips_dup_when_contract_owns():
    owner = _st("st-owner", create=[_ALARM])
    dup = _st("st-dup", create=[_ADMIN])
    plan = TaskPlan(subtasks=[owner, dup], shared_contract={
        "interfaces": [{"name": "AlarmAppSecret", "defined_in": _ALARM}]})
    n = deconflict_cross_module_creates(plan)
    assert n == 1
    assert _ADMIN not in (dup.scope.create_files or []), "契约有 owner 时未剥除跨模块重复 create"
    assert any("AlarmAppSecret" in r for r in (dup.scope.readable or []))
    assert "st-owner" in (dup.depends_on or []), "去冲突后未补依赖 owner"
    # owner 侧不动
    assert _ALARM in owner.scope.create_files


def test_101_no_contract_authority_left_untouched():
    # 无契约权威可判 → 不动（留给 #110 REJECT，绝不静默挑一个）。
    a = _st("st-a", create=[_ALARM])
    b = _st("st-b", create=[_ADMIN])
    plan = TaskPlan(subtasks=[a, b], shared_contract={})
    n = deconflict_cross_module_creates(plan)
    assert n == 0
    assert _ALARM in a.scope.create_files and _ADMIN in b.scope.create_files


# ── #112 契约签名↔owner 描述方法名分叉 ─────────────────────────────────────────
_IFACE = "ruoyi-alarm/src/main/java/com/ruoyi/alarm/schedule/service/IAlarmScheduleStrategyService.java"


def _contract(sig):
    return {"interfaces": [{"name": "IAlarmScheduleStrategyService",
                            "defined_in": _IFACE, "signature": sig}]}


def test_112_signature_desc_divergence_rejected():
    sig = "List<X> selectScheduleStrategyList(X q); int changeStrategyStatus(X q)"
    owner = _st("st-owner", create=[_IFACE],
                desc="实现 selectAlarmScheduleStrategyList 与 changeStrategyStatus 方法")
    plan = TaskPlan(subtasks=[owner], shared_contract=_contract(sig))
    res = validate_contract_signature_source(plan, plan.shared_contract)
    assert not res.valid, "契约短名 vs 描述长名分叉未被 #112 拦截"
    assert any("方法名分叉" in i for i in res.issues), res.issues


def test_112_consistent_names_ok():
    sig = "List<X> selectScheduleStrategyList(X q); int changeStrategyStatus(X q)"
    owner = _st("st-owner", create=[_IFACE],
                desc="实现 selectScheduleStrategyList 与 changeStrategyStatus 方法")
    plan = TaskPlan(subtasks=[owner], shared_contract=_contract(sig))
    assert validate_contract_signature_source(plan, plan.shared_contract).valid


def test_112_silent_description_not_flagged():
    # 描述不列方法名（沉默）→ 不触发（避免误伤）。
    sig = "List<X> selectScheduleStrategyList(X q)"
    owner = _st("st-owner", create=[_IFACE], desc="实现该调度策略服务接口的增删改查")
    plan = TaskPlan(subtasks=[owner], shared_contract=_contract(sig))
    assert validate_contract_signature_source(plan, plan.shared_contract).valid


def test_112_distinct_crud_methods_one_word_apart_not_flagged():
    # 复核 CONFIRMED MED：selectAlarmList（契约，合法未提及）vs selectAlarmScheduleList（描述）差一
    # 内部词但是【不同】CRUD 方法，差异词后仅共享 List=1 词 → 绝不误判分叉（防 fail-closed 误杀）。
    sig = "List<X> selectAlarmList(X q)"
    owner = _st("st-owner", create=[_IFACE],
                desc="实现 selectAlarmScheduleList 与 updateAlarmScheduleList 方法")
    plan = TaskPlan(subtasks=[owner], shared_contract=_contract(sig))
    assert validate_contract_signature_source(plan, plan.shared_contract).valid, "近变体误杀好 plan"
