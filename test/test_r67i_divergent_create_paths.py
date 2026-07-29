#!/usr/bin/env python3
"""round67i 治本红灯先行：下游 LLM 发明分叉包路径→同名异包 create 累积不收敛（task=8461797b）。

死因（录像 cassettes/round67i/llm-90771.jsonl 逐 node 定位，非推测）：
- tech_design 文件级设计【权威】把 AlarmCallbackController 落在 alarm/controller/（seq18），契约不声明
  该实现细节类（非接口面）→ 契约权威落空。
- 下游 plan_batch(65)+elaborate(145) 各批独立 LLM 调用【自由发明】create 包路径 → 同一逻辑类又落到
  alarminterface/controller/（背离设计）→ 分叉变体【跨轮单调累积】→ retry2 两变体并存 → G1 ③b 爆。
- R64-T3 层②熔断只判【整份违例签名逐字相等】，但每轮违例是【不同的类】（三套签名互不相交）→ 3 轮全烧。

三层治本（北极星：流程/规范约束大模型产出，绝不裸 basename 挑边=round67c 纪律）：
  层②（确定性·安全）deconflict_same_name_cross_package_creates 加【tech_design_file_plan 唯一 create
    权威】作契约后备——同名跨包 create 恰有一落点==tech_design 唯一落点则归位剥副本（Category A）；
    无权威/歧义/modify → fail-closed 留 ③b（Category B：AlarmSender 无任何权威）。
  层①（预防）contract_owner_ledger_block 并入 tech_design 唯一 create 落点→分批 prompt 禁别包重建。
  层③（止血）R64-T3 熔断增维：2 轮窗口违例数【零净收敛】(cur>=count_2ago) 且当前非空→提前 fail-fast
    （同族换类重犯：round67i 3→2→3 异签名，触发一逮不住，本触发在 retry2 就止血）。
"""
from __future__ import annotations

import asyncio
import importlib.util
import logging
from pathlib import Path

import pytest

_bs = Path(__file__).resolve().parent / "swarm_bootstrap.py"
_spec = importlib.util.spec_from_file_location("swarm_bootstrap", _bs)
_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_mod)

from swarm.brain.contract_utils import (  # noqa: E402
    _tech_design_authority,
    contract_owner_ledger_block,
    deconflict_same_name_cross_package_creates,
    resolve_plan_conflicts,
)
from swarm.types import (  # noqa: E402
    FileScope,
    SubTask,
    SubTaskDifficulty,
    SubTaskModality,
    TaskHarness,
    TaskPlan,
)

# round67i 真实分叉对（AlarmCallbackController：tech_design 有权威=Category A）
_AUTH = "ruoyi-alarm/src/main/java/com/ruoyi/alarm/controller/AlarmCallbackController.java"
_DIV = "ruoyi-alarm/src/main/java/com/ruoyi/alarminterface/controller/AlarmCallbackController.java"
# AlarmSender：tech_design/契约都没有=Category B
_SEND = "ruoyi-alarm/src/main/java/com/ruoyi/alarm/send/AlarmSender.java"
_SENDER = "ruoyi-alarm/src/main/java/com/ruoyi/alarm/sender/AlarmSender.java"


def _st(sid, *, create=None, readable=None, depends=None, ac=None, verify=None, lang="java"):
    return SubTask(
        id=sid, description="d",
        scope=FileScope(writable=[], create_files=create or [], readable=readable or []),
        harness=TaskHarness(language=lang, verify_commands=verify or []),
        acceptance_criteria=ac or [], depends_on=depends or [])


def _td(path, action="create"):
    return {"path": path, "action": action}


# ════════════════ 层②：tech_design 权威归位（Category A 确定性治） ════════════════

def test_l2_tech_design_authority_relocates_divergent_create():
    """★Category A 主治★ 契约无权威但 tech_design 有唯一 create 落点 → 背离副本归位剥除。"""
    owner = _st("st-66", create=[_AUTH])
    dup = _st("st-6-2", create=[_DIV], ac=[f"{_DIV} 实现并编译", "无关验收保留"],
              verify=[f"javac {_DIV}"])
    plan = TaskPlan(subtasks=[owner, dup], shared_contract={})
    n = deconflict_same_name_cross_package_creates(
        plan, tech_design_file_plan=[_td(_AUTH)])
    assert n == 1, "tech_design 有唯一权威时必须归位（Category A）"
    assert _DIV not in (dup.scope.create_files or [])
    assert _AUTH in owner.scope.create_files, "权威 owner 侧不动"
    # 三面同步 + 消费方改指
    assert not any(_DIV in str(a) for a in (dup.acceptance_criteria or []))
    assert "无关验收保留" in dup.acceptance_criteria
    assert any("AlarmCallbackController" in r for r in (dup.scope.readable or []))
    assert "st-66" in (dup.depends_on or [])


def test_l2_category_b_no_authority_fail_closed():
    """★Category B 诚实边界★ tech_design/契约都无（AlarmSender 纯 batch 发明）→ fail-closed 留 ③b。"""
    a = _st("st-a", create=[_SEND])
    b = _st("st-b", create=[_SENDER])
    plan = TaskPlan(subtasks=[a, b], shared_contract={})
    n = deconflict_same_name_cross_package_creates(plan, tech_design_file_plan=[])
    assert n == 0, "无任何权威绝不裸挑边（round67c 纪律）"
    assert _SEND in a.scope.create_files and _SENDER in b.scope.create_files


def test_l2_tech_design_ambiguous_fail_closed():
    """tech_design 自身把同名放两落点（上游 tech_design bug）→ 歧义 fail-closed 不用作权威。"""
    a = _st("st-1", create=[_AUTH])
    b = _st("st-2", create=[_DIV])
    plan = TaskPlan(subtasks=[a, b], shared_contract={})
    n = deconflict_same_name_cross_package_creates(
        plan, tech_design_file_plan=[_td(_AUTH), _td(_DIV)])
    assert n == 0, "tech_design 歧义时绝不挑边"
    assert _AUTH in a.scope.create_files and _DIV in b.scope.create_files


def test_l2_tech_design_modify_not_used_as_create_authority():
    """tech_design action=modify=base 既有类（③f/CVB 领域）→ 绝不作 create 权威（防越界）。"""
    a = _st("st-1", create=[_AUTH])
    b = _st("st-2", create=[_DIV])
    plan = TaskPlan(subtasks=[a, b], shared_contract={})
    n = deconflict_same_name_cross_package_creates(
        plan, tech_design_file_plan=[_td(_AUTH, "modify")])
    assert n == 0


def test_l2_authority_path_not_created_fail_closed():
    """tech_design 权威落点【无人创建】（两创建者都在别包）→ 无从确定保哪个 → fail-closed。"""
    a = _st("st-a", create=[_DIV])
    b = _st("st-b", create=[_SENDER.replace("AlarmSender", "AlarmCallbackController")])
    plan = TaskPlan(subtasks=[a, b], shared_contract={})
    n = deconflict_same_name_cross_package_creates(
        plan, tech_design_file_plan=[_td(_AUTH)])   # 权威 alarm/controller/ 无人建
    assert n == 0


def test_l2_contract_authority_takes_precedence():
    """契约有权威时以契约为准（tech_design 不翻案）——纯加治契约漏声明维，契约路径零回归。"""
    # 契约声明 owner=alarm/controller，tech_design 声明 alarminterface/controller（冲突）→ 用契约
    owner = _st("st-66", create=[_AUTH])
    dup = _st("st-6-2", create=[_DIV])
    plan = TaskPlan(subtasks=[owner, dup],
                    shared_contract={"interfaces": [
                        {"name": "AlarmCallbackController", "defined_in": _AUTH}]})
    n = deconflict_same_name_cross_package_creates(
        plan, tech_design_file_plan=[_td(_DIV)])
    assert n == 1
    assert _AUTH in owner.scope.create_files          # 契约 owner 保留
    assert _DIV not in dup.scope.create_files          # 归位到契约 owner，非 tech_design


def test_l2_contract_ambiguous_not_overridden_by_tech_design():
    """契约自身歧义（同名两 defined_in）→ 强歧义信号，tech_design 绝不翻案（fail-closed 优先）。"""
    a = _st("st-1", create=[_AUTH])
    b = _st("st-2", create=[_DIV])
    plan = TaskPlan(subtasks=[a, b], shared_contract={"interfaces": [
        {"name": "AlarmCallbackController", "defined_in": _AUTH},
        {"name": "AlarmCallbackController", "defined_in": _DIV}]})
    n = deconflict_same_name_cross_package_creates(
        plan, tech_design_file_plan=[_td(_AUTH)])
    assert n == 0, "契约歧义绝不让 tech_design 翻案"


def test_l2_cross_module_divergence_fail_closed_no_corruption():
    """★对抗复核 HIGH 整改锁（真实函数复现的反例）★ tech_design 权威在 alarm 模块，batch 在
    【另一物理模块】发明合法异职责同名新类（notify/vo/Result）→ 绝不归位（跨模块 fail-closed 留
    ③b）——否则合法新类被静默剥除改指错类 = round67c 腐化换语料源复现。"""
    auth = "ruoyi-alarm/src/main/java/com/ruoyi/alarm/domain/Result.java"
    legit = "ruoyi-notify/src/main/java/com/ruoyi/notify/vo/Result.java"
    a = _st("st-a", create=[auth])
    b = _st("st-b", create=[legit])
    plan = TaskPlan(subtasks=[a, b], shared_contract={})
    n = deconflict_same_name_cross_package_creates(
        plan, tech_design_file_plan=[_td(auth)])
    assert n == 0, "跨物理模块分叉绝不用 tech_design 权威裸挑边（腐化面）"
    assert legit in b.scope.create_files, "合法异职责新类绝不被静默剥除"
    assert not (b.depends_on or []), "绝不被强行接错依赖"


def test_l2_same_module_divergence_still_relocated():
    """同物理构建模块内同名双 create（JVM classpath 必然 bean 冲突非法）+ tech_design 唯一设计
    落点 → 归位仍生效（整改后 Category A 的安全子集）。"""
    owner = _st("st-1", create=[_AUTH])
    dup = _st("st-2", create=[_DIV])     # _AUTH/_DIV 同在 ruoyi-alarm 物理模块
    plan = TaskPlan(subtasks=[owner, dup], shared_contract={})
    n = deconflict_same_name_cross_package_creates(
        plan, tech_design_file_plan=[_td(_AUTH)])
    assert n == 1
    assert _DIV not in dup.scope.create_files


def test_l2_non_jvm_stack_neutral():
    """非 JVM：Go 同名跨包合法（import 由模块限定）——tech_design 权威亦栈中立不误伤。"""
    a = _st("st-a", create=["svc-a/internal/handler/handler.go"], lang="go")
    b = _st("st-b", create=["svc-b/internal/handler/handler.go"], lang="go")
    plan = TaskPlan(subtasks=[a, b], shared_contract={})
    n = deconflict_same_name_cross_package_creates(
        plan, tech_design_file_plan=[_td("svc-a/internal/handler/handler.go")])
    assert n == 0


def test_l2_default_none_zero_regression():
    """不传 tech_design_file_plan（离线 plan-quality 评测默认 None）→ 行为等同治前（零回归）。"""
    a = _st("st-a", create=[_DIV])
    b = _st("st-b", create=[_AUTH])
    plan = TaskPlan(subtasks=[a, b], shared_contract={})
    assert deconflict_same_name_cross_package_creates(plan) == 0


def test_l2_wired_through_resolve_plan_conflicts():
    """resolve_plan_conflicts 传 file_plan→deconflict 生效（elaborate/离线评测共用同一条唯一顺序）。"""
    owner = _st("st-66", create=[_AUTH])
    dup = _st("st-6-2", create=[_DIV])
    plan = TaskPlan(subtasks=[owner, dup], shared_contract={})
    counts = resolve_plan_conflicts(plan, file_plan=[_td(_AUTH)])
    assert counts["samename_creates_deconflicted"] == 1
    assert _DIV not in dup.scope.create_files


def test_tech_design_authority_helper():
    """_tech_design_authority：唯一 create→权威；同名两 create→歧义；modify/非JVM→不入。"""
    auth, amb = _tech_design_authority([
        _td(_AUTH), _td(_SEND), _td(_SENDER),          # AlarmSender 两落点=歧义
        _td("x/Y.java", "modify"), _td("svc/h.go")])
    assert auth.get("alarmcallbackcontroller.java") and "alarmcallbackcontroller.java" not in amb
    assert "alarmsender.java" in amb                    # send/ vs sender/ 歧义
    assert "y.java" not in auth                          # modify 不入


# ════════════════ 层①：台账并入 tech_design 权威落点 ════════════════

def test_l1_ledger_includes_tech_design_authority():
    """★层① 主治★ 契约漏声明但 tech_design 有落点 → 类入分批禁写台账。"""
    block = contract_owner_ledger_block({}, tech_design_file_plan=[_td(_AUTH)])
    assert "AlarmCallbackController" in block and "alarm/controller" in block


def test_l1_contract_precedence_in_ledger():
    """契约与 tech_design 对同 basename 冲突 → 台账以契约落点为准。"""
    common = "ruoyi-common/src/main/java/com/ruoyi/common/Foo.java"
    alarm = "ruoyi-alarm/src/main/java/com/ruoyi/alarm/Foo.java"
    block = contract_owner_ledger_block(
        {"interfaces": [{"name": "Foo", "defined_in": common}]},
        tech_design_file_plan=[_td(alarm)])
    assert "ruoyi-common" in block and "alarm/Foo.java" not in block


def test_l1_ledger_tech_design_ambiguous_excluded():
    """tech_design 自身歧义的 basename 不入台账（避免误导 batch 挑错落点）。"""
    block = contract_owner_ledger_block(
        {}, tech_design_file_plan=[_td(_SEND), _td(_SENDER)])
    assert "AlarmSender" not in block


def test_l1_ledger_modify_and_non_jvm_excluded():
    block = contract_owner_ledger_block(
        {}, tech_design_file_plan=[_td(_AUTH, "modify"), _td("svc/h.go")])
    assert block == ""


def test_l1_ledger_empty_when_nothing():
    assert contract_owner_ledger_block({}, tech_design_file_plan=[]) == ""
    assert contract_owner_ledger_block(None, tech_design_file_plan=None) == ""


def test_l1_ledger_contract_never_evicted_by_td_flood(caplog):
    """★Hunter HIGH 整改锁（真实复现的反例）★ 契约 owner（Z 开头字母序最末）+ 100 条 tech_design
    条目 → 契约条目【保底不被逐出】详情层（分池预算，契约池先占）。

    R67M2-T3 B5 语义更新：溢出详情帽的条目不再【丢弃】而是落【简表层】（仅类名、禁重名
    约束同样生效）——故本例不再打"截断 WARNING"，改断言溢出条目确实拿到简表层保护
    （守护意图未削弱：契约保底 + 溢出可观测两条都在，且从"可观测地丢失"升级为"不丢失"）。
    两层总帽都爆才 WARNING，见 test_round67m2_batch3_polish 的分层用例。"""
    import logging
    contract = {"interfaces": [{
        "name": "ZOwnerInterface",
        "defined_in": "ruoyi-common/src/main/java/com/ruoyi/common/ZOwnerInterface.java"}]}
    fp = [_td(f"ruoyi-alarm/src/main/java/com/ruoyi/alarm/gen/AClass{i:03d}.java")
          for i in range(100)]
    with caplog.at_level(logging.INFO):
        block = contract_owner_ledger_block(contract, tech_design_file_plan=fp)
    assert "ZOwnerInterface" in block, "契约条目绝不被 tech_design 洪泛挤出（分池保底）"
    _detail = [l for l in block.splitlines() if "→ 唯一 owner：" in l]
    assert any("ZOwnerInterface" in l for l in _detail), "契约条目须在【详情层】（带 owner 路径）"
    assert "AClass099" in block, "溢出详情帽的条目落简表层，绝不静默蒸发"
    assert any("分层" in r.message for r in caplog.records), "分层入册必须可观测"


def test_l1_ledger_no_warning_under_budget(caplog):
    """预算内（契约+tech_design ≤60）不打截断 WARNING（不噪声）。"""
    import logging
    with caplog.at_level(logging.WARNING):
        block = contract_owner_ledger_block({}, tech_design_file_plan=[_td(_AUTH)])
    assert "AlarmCallbackController" in block
    assert not any("截断" in r.message for r in caplog.records)


def test_l2_td_authority_relocation_logs_warning(caplog):
    """★Hunter MEDIUM 整改锁★ tech_design 后备权威归位（新启发式 soak 期）打 WARNING 独立可审计；
    契约权威归位保持 INFO。"""
    import logging
    owner = _st("st-1", create=[_AUTH])
    dup = _st("st-2", create=[_DIV])
    plan = TaskPlan(subtasks=[owner, dup], shared_contract={})
    with caplog.at_level(logging.WARNING):
        n = deconflict_same_name_cross_package_creates(
            plan, tech_design_file_plan=[_td(_AUTH)])
    assert n == 1
    assert any("tech_design" in r.message and "[DECONFLICT-SAMENAME]" in r.message
               for r in caplog.records), "tech_design 归位必须 WARNING 级可审计"


# ════════════════ H-1：#101/层③ 剥离-复活环 sibling 捞净（file_plan 联动） ════════════════
# 体检 TOP1 铁证：契约权威剥离子任务 create 后 file_plan 双条目留孤儿 → R40-1 REJECT →
# 孤儿挂靠复活 → round67h CVB 完全同构的环。修=剥离联动删 file_plan 孤儿条目+depends_on resync。

def test_h1_xmod_strip_syncs_file_plan_no_orphan():
    """★H-1 主治(#101)★ 同 FQN 跨模块剥离 → file_plan 被剥条目联动删除 → R40-1 无孤儿。"""
    from swarm.brain.plan_validator import validate_file_plan_ownership
    A = "ruoyi-alarm/src/main/java/com/ruoyi/alarm/domain/AlarmMsg.java"
    B = "ruoyi-admin/src/main/java/com/ruoyi/alarm/domain/AlarmMsg.java"   # 同 FQN 异模块
    plan = TaskPlan(subtasks=[_st("st-a", create=[A]), _st("st-b", create=[B])],
                    shared_contract={"interfaces": [{"name": "AlarmMsg", "defined_in": A}]})
    fp = [_td(A), _td(B)]
    counts = resolve_plan_conflicts(plan, file_plan=fp)
    assert counts["xmod_creates_deconflicted"] == 1
    assert counts["file_plan_entries_stripped"] == 1, "被剥路径的 file_plan 条目必须联动删除"
    assert not any((e.get("path") if isinstance(e, dict) else e) == B for e in fp)
    r = validate_file_plan_ownership(plan, fp)
    assert r.valid, f"联动后 R40-1 必须无孤儿（治前铁证 valid=False）: {r.issues[:2]}"


def test_h1_samename_strip_syncs_file_plan_no_orphan():
    """★H-1 主治(层③契约路径)★ 异 FQN 同名剥离 → file_plan 副本条目联动删除 → R40-1 无孤儿。"""
    from swarm.brain.plan_validator import validate_file_plan_ownership
    A = "ruoyi-common/src/main/java/com/ruoyi/common/utils/AesUtils.java"
    B = "ruoyi-alarm/src/main/java/com/ruoyi/alarm/util/AesUtils.java"
    plan = TaskPlan(subtasks=[_st("st-a", create=[A]), _st("st-b", create=[B])],
                    shared_contract={"interfaces": [{"name": "AesUtils", "defined_in": A}]})
    fp = [_td(A), _td(B)]
    counts = resolve_plan_conflicts(plan, file_plan=fp)
    assert counts["samename_creates_deconflicted"] == 1
    assert counts["file_plan_entries_stripped"] == 1
    assert not any((e.get("path") if isinstance(e, dict) else e) == B for e in fp)
    assert validate_file_plan_ownership(plan, fp).valid


def test_h1_strip_resyncs_file_plan_depends_on():
    """被删条目被其它 file_plan 条目 depends_on 引用 → 改指 owner 落点（防陈旧边静默丢）。"""
    A = "ruoyi-common/src/main/java/com/ruoyi/common/utils/AesUtils.java"
    B = "ruoyi-alarm/src/main/java/com/ruoyi/alarm/util/AesUtils.java"
    C = "ruoyi-alarm/src/main/java/com/ruoyi/alarm/service/UseAes.java"
    plan = TaskPlan(subtasks=[_st("st-a", create=[A]), _st("st-b", create=[B, C])],
                    shared_contract={"interfaces": [{"name": "AesUtils", "defined_in": A}]})
    fp = [_td(A), _td(B), {"path": C, "action": "create", "depends_on": [B]}]
    resolve_plan_conflicts(plan, file_plan=fp)
    c_entry = next(e for e in fp if isinstance(e, dict) and e.get("path") == C)
    assert B not in (c_entry.get("depends_on") or [])
    assert A in (c_entry.get("depends_on") or []), "depends_on 必须改指 owner 落点"


def test_h1_owner_and_modify_entries_never_stripped():
    """owner 侧条目与 modify 条目绝不被联动删除（只删被剥副本的 create 条目）。"""
    A = "ruoyi-common/src/main/java/com/ruoyi/common/utils/AesUtils.java"
    B = "ruoyi-alarm/src/main/java/com/ruoyi/alarm/util/AesUtils.java"
    M = "ruoyi-common/src/main/java/com/ruoyi/common/core/domain/Base.java"
    plan = TaskPlan(subtasks=[_st("st-a", create=[A]), _st("st-b", create=[B])],
                    shared_contract={"interfaces": [{"name": "AesUtils", "defined_in": A}]})
    fp = [_td(A), _td(B), _td(M, "modify")]
    resolve_plan_conflicts(plan, file_plan=fp)
    paths = [(e.get("path") if isinstance(e, dict) else e) for e in fp]
    assert A in paths and M in paths and B not in paths


def test_h1_no_strip_when_fail_closed():
    """无权威 fail-closed（不剥子任务）→ file_plan 一条不动、strip 计数 0（零误删）。"""
    A = "ruoyi-alarm/src/main/java/com/ruoyi/alarm/send/AlarmSender.java"
    B = "ruoyi-alarm/src/main/java/com/ruoyi/alarm/sender/AlarmSender.java"
    plan = TaskPlan(subtasks=[_st("st-a", create=[A]), _st("st-b", create=[B])],
                    shared_contract={})
    fp = [_td(A), _td(B)]
    counts = resolve_plan_conflicts(plan, file_plan=fp)
    assert counts["file_plan_entries_stripped"] == 0
    assert len(fp) == 2


def test_h1_default_none_file_plan_zero_regression():
    """file_plan=None（离线 plan-quality 评测默认）→ 行为等同治前（剥子任务但无 fp 可联动）。"""
    A = "ruoyi-common/src/main/java/com/ruoyi/common/utils/AesUtils.java"
    B = "ruoyi-alarm/src/main/java/com/ruoyi/alarm/util/AesUtils.java"
    plan = TaskPlan(subtasks=[_st("st-a", create=[A]), _st("st-b", create=[B])],
                    shared_contract={"interfaces": [{"name": "AesUtils", "defined_in": A}]})
    counts = resolve_plan_conflicts(plan)
    assert counts["samename_creates_deconflicted"] == 1
    assert counts["file_plan_entries_stripped"] == 0


# ════════════════ 层③：熔断增维（2 轮窗口零净收敛提前 fail-fast） ════════════════

def _st_g1(sid, create):
    return SubTask(id=sid, description=sid, difficulty=SubTaskDifficulty.MEDIUM,
                   modality=SubTaskModality.TEXT,
                   scope=FileScope(writable=[], readable=[], create_files=create))


def _pairs_plan(names):
    """构造 len(names) 对同名异包 create（无权威）→ G1 ③b 逐个违例，count=len(names)。"""
    subs = []
    for i, nm in enumerate(names):
        subs.append(_st_g1(f"a{i}", [f"ruoyi-alarm/src/main/java/com/ruoyi/alarm/p1/{nm}.java"]))
        subs.append(_st_g1(f"b{i}", [f"ruoyi-alarm/src/main/java/com/ruoyi/alarm/p2/{nm}.java"]))
    return TaskPlan(subtasks=subs, parallel_groups=[[s.id for s in subs]])


def _g1_state(retry, names, prev=None):
    st = {
        "plan": _pairs_plan(names), "task_description": "t", "complexity": "ultra",
        "plan_retry_count": retry, "tech_design_file_plan": [], "project_id": "p",
        "requirement_items": [],
    }
    if prev is not None:
        st["plan_validation_prev_structural"] = prev
    return st


def _run_validate(state, monkeypatch):
    import swarm.brain.nodes as _n
    monkeypatch.setattr(_n, "_get_project_path", lambda pid: None)
    return asyncio.run(_n.validate_plan(state))


def test_l3_window_fuse_on_churning_non_convergence(monkeypatch, caplog):
    """★层③ 主治★ round67i 轨迹：违例数 3→2→3、每轮【不同】类（异签名）→ 触发一（整份签名相等）
    逮不住 → 2 轮窗口零净收敛（retry2 count 3 >= 2 轮前 count 3）在 retry2 就熔断止血。"""
    from swarm.brain.graph import MAX_PLAN_RETRY

    r0 = _run_validate(_g1_state(0, ["X1", "X2", "X3"]), monkeypatch)          # count 3
    assert r0["plan_retry_count"] == 0, "首败绝不熔"
    r1 = _run_validate(_g1_state(1, ["Y1", "Y2"],
                                 prev=r0["plan_validation_prev_structural"]), monkeypatch)  # 2
    assert r1["plan_retry_count"] == 1, "第2轮 count 下降(3→2)+异签名→不熔（给机会）"
    with caplog.at_level(logging.WARNING):
        r2 = _run_validate(_g1_state(2, ["Z1", "Z2", "Z3"],
                                     prev=r1["plan_validation_prev_structural"]), monkeypatch)  # 3
    assert r2["plan_retry_count"] >= MAX_PLAN_RETRY, "2 轮窗口零净收敛(3→2→3)必须在 retry2 熔断"
    assert any("零净收敛" in r.message for r in caplog.records), "窗口熔断必须可观测"


def test_l3_window_fuse_does_not_kill_converging_plan(monkeypatch):
    """★安全·防误熔★ 收敛轨迹 5→3→2（异签名）：2 轮窗口净下降(cur<count_2ago)→绝不熔，让其继续。"""
    from swarm.brain.graph import MAX_PLAN_RETRY

    r0 = _run_validate(_g1_state(0, ["A1", "A2", "A3", "A4", "A5"]), monkeypatch)   # 5
    r1 = _run_validate(_g1_state(1, ["B1", "B2", "B3"],
                                 prev=r0["plan_validation_prev_structural"]), monkeypatch)  # 3
    r2 = _run_validate(_g1_state(2, ["C1", "C2"],
                                 prev=r1["plan_validation_prev_structural"]), monkeypatch)  # 2
    assert r2["plan_retry_count"] < MAX_PLAN_RETRY, "净收敛(5→…→2)绝不许被窗口熔断误杀"


def test_l3_window_fuse_needs_three_consecutive_rounds(monkeypatch):
    """窗口熔断须严格连续三轮（count_2ago 有值）——非连续（陈旧/跨 replan 周期）绝不熔。"""
    from swarm.brain.graph import MAX_PLAN_RETRY
    # retry=2 但 prev 来自 retry=0（跳过 retry1，不连续）→ count_1ago 应为 None → 不熔
    r0 = _run_validate(_g1_state(0, ["X1", "X2", "X3"]), monkeypatch)
    r2 = _run_validate(_g1_state(2, ["Z1", "Z2", "Z3"],
                                 prev=r0["plan_validation_prev_structural"]), monkeypatch)
    assert r2["plan_retry_count"] < MAX_PLAN_RETRY, "非连续账绝不窗口熔断"


def test_l3_window_fuse_records_count_history(monkeypatch):
    """prev_structural 必须带 count + count_1ago（连续时继承上轮 count），供下轮算 2 轮窗口。"""
    r0 = _run_validate(_g1_state(0, ["X1", "X2"]), monkeypatch)
    p0 = r0["plan_validation_prev_structural"]
    assert p0.get("count") == 2 and p0.get("count_1ago") is None, f"首轮账: {p0}"
    r1 = _run_validate(_g1_state(1, ["Y1", "Y2", "Y3"], prev=p0), monkeypatch)
    p1 = r1["plan_validation_prev_structural"]
    assert p1.get("count") == 3 and p1.get("count_1ago") == 2, f"连续轮继承上轮 count: {p1}"


def test_l3_window_fuse_kill_switch(monkeypatch):
    """SWARM_G1_RETRY_FUSE=0 → 窗口熔断同步失效（复用既有泄压阀，运维可关）。"""
    from swarm.brain.graph import MAX_PLAN_RETRY
    monkeypatch.setenv("SWARM_G1_RETRY_FUSE", "0")
    r0 = _run_validate(_g1_state(0, ["X1", "X2", "X3"]), monkeypatch)
    r1 = _run_validate(_g1_state(1, ["Y1", "Y2"],
                                 prev=r0["plan_validation_prev_structural"]), monkeypatch)
    r2 = _run_validate(_g1_state(2, ["Z1", "Z2", "Z3"],
                                 prev=r1["plan_validation_prev_structural"]), monkeypatch)
    assert r2["plan_retry_count"] < MAX_PLAN_RETRY, "泄压阀关闭时窗口熔断不得触发"
