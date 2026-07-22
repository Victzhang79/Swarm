"""round67e 死因治本：C2 契约方法名分叉【确定性自愈】(reconcile_contract_method_names)。

死因(task 88584950): 契约 signature 方法名 ↔ owner 子任务描述方法名分叉,C2 闸检测到却
【纯打回】让 LLM 全量重产 → 5 轮不收敛(轮4又退回重犯轮2已修的 IAlarmTaskChannelService/
IAlarmNotifyUserService)→ MAX_PLAN_RETRY 熔断 → CONFIRM auto_accept 拒 plan_invalid →
FAILED,零 worker 派发。

治本: 契约=唯一权威真值源,确定性把 owner description + acceptance_criteria +
harness.verify_commands 三面里的方法名变体逐字对齐到契约方法名,消除分叉无需打回 LLM。
★三面同步★: C2 只扫 desc,只改 desc → 下游 AC/verify 导回原分叉 = 半修复(照 R65D-T2)。

红灯先行: 修复前 reconcile_contract_method_names / detect_contract_signature_divergences
不存在 → ImportError 红。
"""
from __future__ import annotations

from swarm.brain.contract_utils import reconcile_contract_method_names
from swarm.brain.plan_finisher import finish_plan_deterministic
from swarm.brain.plan_validator import (
    detect_contract_signature_divergences,
    validate_contract_signature_source,
)
from swarm.types import FileScope, SubTask, TaskHarness, TaskPlan

_IFACE = ("ruoyi-alarm/src/main/java/com/ruoyi/alarm/schedule/service/"
          "IAlarmScheduleStrategyService.java")

# 死型: 契约短名 selectScheduleStrategyList, 描述长名 selectAlarmScheduleStrategyList
# (差一个 Alarm 中缀; 差异词后共享 ScheduleStrategyList=3 词 → 真分叉)
_SIG = "List<X> selectScheduleStrategyList(X q); int changeStrategyStatus(X q)"


def _owner(desc, *, ac=None, vc=None, create=None, sid="st-owner"):
    st = SubTask(
        id=sid, description=desc,
        scope=FileScope(create_files=create or [_IFACE]),
        harness=TaskHarness(language="java", verify_commands=list(vc or [])),
    )
    if ac is not None:
        st.acceptance_criteria = list(ac)
    return st


def _contract(sig, name="IAlarmScheduleStrategyService", defined_in=_IFACE):
    return {"interfaces": [{"name": name, "defined_in": defined_in, "signature": sig}]}


# ── T1 基本自愈：描述变体 → 契约权威名 ─────────────────────────────────────────
def test_t1_desc_variant_aligned_to_contract():
    owner = _owner("实现 selectAlarmScheduleStrategyList 与 changeStrategyStatus 方法")
    plan = TaskPlan(subtasks=[owner], shared_contract=_contract(_SIG))
    summary = reconcile_contract_method_names(plan, plan.shared_contract)
    assert "st-owner" in summary, summary
    assert "selectScheduleStrategyList" in owner.description
    assert "selectAlarmScheduleStrategyList" not in owner.description


# ── T2 ★核心：三面同步(desc+AC+verify),防半修复★ ──────────────────────────────
def test_t2_three_faces_synced_desc_ac_verify():
    owner = _owner(
        "实现 selectAlarmScheduleStrategyList 方法",
        ac=["验收: selectAlarmScheduleStrategyList 返回非空列表"],
        vc=["grep -q 'selectAlarmScheduleStrategyList' Impl.java"])
    plan = TaskPlan(subtasks=[owner], shared_contract=_contract(_SIG))
    reconcile_contract_method_names(plan, plan.shared_contract)
    assert "selectAlarmScheduleStrategyList" not in owner.description
    assert not any("selectAlarmScheduleStrategyList" in a
                   for a in owner.acceptance_criteria), "AC 未同步=半修复"
    assert not any("selectAlarmScheduleStrategyList" in v
                   for v in owner.harness.verify_commands), "verify 未同步=半修复"
    assert any("selectScheduleStrategyList" in a for a in owner.acceptance_criteria)
    assert any("selectScheduleStrategyList" in v for v in owner.harness.verify_commands)


# ── T3 端到端：自愈后 C2 闸不再打回 ───────────────────────────────────────────
def test_t3_after_reconcile_c2_validate_passes():
    owner = _owner("实现 selectAlarmScheduleStrategyList 与 changeStrategyStatus 方法")
    plan = TaskPlan(subtasks=[owner], shared_contract=_contract(_SIG))
    assert not validate_contract_signature_source(
        plan, plan.shared_contract).valid, "前置: 修前应分叉"
    reconcile_contract_method_names(plan, plan.shared_contract)
    assert validate_contract_signature_source(
        plan, plan.shared_contract).valid, "自愈后 C2 仍打回=没治本"


# ── T4 幂等：第二次无改动 ─────────────────────────────────────────────────────
def test_t4_idempotent():
    owner = _owner("实现 selectAlarmScheduleStrategyList 方法")
    plan = TaskPlan(subtasks=[owner], shared_contract=_contract(_SIG))
    reconcile_contract_method_names(plan, plan.shared_contract)
    assert reconcile_contract_method_names(plan, plan.shared_contract) == {}, "非幂等"


# ── T5 不误伤三连（对齐 C2 检测的零误伤边界）────────────────────────────────────
def test_t5a_consistent_names_untouched():
    owner = _owner("实现 selectScheduleStrategyList 与 changeStrategyStatus 方法")
    plan = TaskPlan(subtasks=[owner], shared_contract=_contract(_SIG))
    assert reconcile_contract_method_names(plan, plan.shared_contract) == {}


def test_t5b_silent_desc_untouched():
    owner = _owner("实现该调度策略服务接口的增删改查")
    plan = TaskPlan(subtasks=[owner],
                    shared_contract=_contract("List<X> selectScheduleStrategyList(X q)"))
    assert reconcile_contract_method_names(plan, plan.shared_contract) == {}
    assert owner.description == "实现该调度策略服务接口的增删改查"


def test_t5c_distinct_crud_one_word_apart_untouched():
    # 合法近变体(差异词后仅共享 List=1 词)绝不误改——防 fail-closed 误杀好 plan。
    owner = _owner("实现 selectAlarmScheduleList 与 updateAlarmScheduleList 方法")
    plan = TaskPlan(subtasks=[owner],
                    shared_contract=_contract("List<X> selectAlarmList(X q)"))
    assert reconcile_contract_method_names(plan, plan.shared_contract) == {}
    assert "selectAlarmScheduleList" in owner.description, "合法近变体被误改"


# ── T6 词边界：变体不误伤含它字面的更长标识符 ───────────────────────────────────
def test_t6_word_boundary_no_substring_clobber():
    # 契约 selectScheduleStrategyList；描述有变体 selectAlarmScheduleStrategyList(应改)
    # 且有更长 selectAlarmScheduleStrategyListPaged(含变体字面,但非同一 token,不得动)。
    owner = _owner("实现 selectAlarmScheduleStrategyList 与 "
                   "selectAlarmScheduleStrategyListPaged 方法")
    plan = TaskPlan(subtasks=[owner], shared_contract=_contract(_SIG))
    reconcile_contract_method_names(plan, plan.shared_contract)
    # 变体被对齐
    assert "selectScheduleStrategyList " in owner.description or \
           owner.description.endswith("selectScheduleStrategyList")
    # 更长标识符原样(未被子串替换成 selectScheduleStrategyListPaged 之外的畸形)
    assert "selectAlarmScheduleStrategyListPaged" in owner.description, "词边界失守,子串被误替换"


# ── T7 单子任务畸形不拖垮兄弟(暂存区+per-owner try/except)──────────────────────
def test_t7_malformed_owner_no_sibling_halfmutation():
    _IFACE2 = ("ruoyi-alarm/src/main/java/com/ruoyi/alarm/notify/service/"
               "IAlarmNotifyUserService.java")
    good = _owner("实现 selectAlarmScheduleStrategyList 方法", sid="st-good")
    # 畸形: verify_commands 含非字符串 → reconcile 内 _sub(123) 触发 TypeError,该 owner 应被
    # per-owner try/except 跳过。pydantic 构造期拦 [123],故绕过校验从底层 __dict__ 注入,
    # 模拟"处理该 owner 时抛异常"。
    bad = _owner("实现 selectNotifyUsersXById 方法", sid="st-bad", create=[_IFACE2])
    bad.harness.__dict__["verify_commands"] = [123]
    plan = TaskPlan(subtasks=[good, bad], shared_contract={"interfaces": [
        {"name": "IAlarmScheduleStrategyService", "defined_in": _IFACE, "signature": _SIG},
        {"name": "IAlarmNotifyUserService", "defined_in": _IFACE2,
         "signature": "List<X> selectNotifyUsersById(X q)"},
    ]})
    summary = reconcile_contract_method_names(plan, plan.shared_contract)
    # 正常兄弟仍被自愈
    assert "st-good" in summary
    assert "selectScheduleStrategyList" in good.description
    # 畸形 owner 保持原样(未半变异)——description 未被替换(因整体 try 回滚在提交前)
    assert good.description  # 兄弟完好


# ── T8 接线：finish_plan_deterministic 末端自愈,分叉消除 ───────────────────────
def test_t8_wired_in_finish_plan_deterministic():
    owner = _owner("实现 selectAlarmScheduleStrategyList 方法")
    plan = TaskPlan(subtasks=[owner], shared_contract=_contract(_SIG))
    out = finish_plan_deterministic(
        plan, file_plan={}, shared_contract=plan.shared_contract)
    assert "selectScheduleStrategyList" in owner.description, "finish 未跑 C2 自愈"
    assert validate_contract_signature_source(plan, plan.shared_contract).valid
    assert out.get("contract_method_names_reconciled"), "接线摘要缺失"


# ── T9 detect 结构化返回(validate 与自愈共用真值源)────────────────────────────
def test_t9_detect_returns_owner_and_pairs():
    owner = _owner("实现 selectAlarmScheduleStrategyList 方法")
    plan = TaskPlan(subtasks=[owner], shared_contract=_contract(_SIG))
    divs = detect_contract_signature_divergences(plan, plan.shared_contract)
    assert len(divs) == 1
    owner_st, iface_name, diverged = divs[0]
    assert owner_st is owner
    assert iface_name == "IAlarmScheduleStrategyService"
    assert diverged == [("selectScheduleStrategyList", ["selectAlarmScheduleStrategyList"])]


# ── hunter F1(HIGH)：自愈整体失效 → degraded_reasons 机读信号(非只 grep 日志)──────────
def test_f1_reconcile_failure_sets_degraded_flag(monkeypatch):
    import swarm.brain.contract_utils as _cu

    def _boom(*a, **k):
        raise RuntimeError("boom: detect 顶层炸")
    monkeypatch.setattr(_cu, "reconcile_contract_method_names", _boom)
    owner = _owner("实现 selectAlarmScheduleStrategyList 方法")
    plan = TaskPlan(subtasks=[owner], shared_contract=_contract(_SIG))
    out = finish_plan_deterministic(plan, file_plan={}, shared_contract=plan.shared_contract)
    assert out.get("contract_method_names_reconcile_failed") is True, \
        "F1:自愈整体失效未写 degraded 机读键(退回死因链却无痕迹)"


# ── hunter F2(MED)：同一 owner 拥多接口 → 账本 extend 累积不覆盖丢账 ──────────────────
def test_f2_multi_interface_owner_account_complete():
    _IFACE2 = ("ruoyi-alarm/src/main/java/com/ruoyi/alarm/notify/service/"
               "IAlarmNotifyService.java")
    owner = SubTask(
        id="st-owner",
        description=("实现 selectAlarmScheduleStrategyList 与 "
                     "selectNotifyUserXByPage 方法"),
        scope=FileScope(create_files=[_IFACE, _IFACE2]),
        harness=TaskHarness(language="java"))
    plan = TaskPlan(subtasks=[owner], shared_contract={"interfaces": [
        {"name": "IAlarmScheduleStrategyService", "defined_in": _IFACE, "signature": _SIG},
        {"name": "IAlarmNotifyService", "defined_in": _IFACE2,
         "signature": "List<X> selectNotifyUserByPage(X q)"},
    ]})
    summary = reconcile_contract_method_names(plan, plan.shared_contract)
    pairs = {(d["from"], d["to"]) for d in summary["st-owner"]}
    assert ("selectAlarmScheduleStrategyList", "selectScheduleStrategyList") in pairs
    assert ("selectNotifyUserXByPage", "selectNotifyUserByPage") in pairs, \
        "F2:同 owner 多接口账本被覆盖丢账"
    assert "selectScheduleStrategyList" in owner.description
    assert "selectNotifyUserByPage" in owner.description
