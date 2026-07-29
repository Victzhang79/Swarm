"""R67M2-T3（24号文批次3 小修面）行为测试。

四项：
  A4 打回日志计数头（_log_reject_issues）：issues[:3] 截断曾致陪跑误判
     （round67m2 轮1 headline 3 条/池≥7）——日志必须带"共 N 条"，且四把闸
     （C1/C2/R40-1/G1）的真实打回路径都走它。
  B3 T4 多落点观测账（elaborate→state t4_ambiguous_types→validate warnings 可见面）：
     round67m2 "跳过布线" WARNING 轮2/3 各一次零账可查（已见未治）。
  B5 OWNER-LEDGER 分层入册：详情层 60 条高危族优先 + 简表层兜住溢出——
     round67m2 实证 81/136 条预防保护被 60 帽静默丢弃。
  B6 ③b 文案口径：同子任务自撞（round67m2 st-9 channel/iface 双接口）不再说
     "被多个子任务"，且主句恒定不扰动 R67F-T2 熔断签名。
"""
from __future__ import annotations

import logging

import pytest

from swarm.types import FileScope, SubTask, SubTaskDifficulty, TaskPlan

from swarm.brain.nodes import _log_reject_issues, validate_plan
from swarm.brain.symbol_provenance import wire_created_type_references
from swarm.brain.contract_utils import contract_owner_ledger_block
from swarm.brain.plan_validator import (
    normalize_structural_signature,
    validate_module_coherence,
)


def _st(sid, *, create=None, desc=None):
    return SubTask(id=sid, description=desc or f"task {sid}",
                   difficulty=SubTaskDifficulty.MEDIUM,
                   scope=FileScope(create_files=create or [], writable=[], readable=[]),
                   acceptance_criteria=[])


def _plan(subs):
    return TaskPlan(subtasks=subs, parallel_groups=[[s.id for s in subs]])


# ─── A4 打回日志计数头 ───


def test_a4_reject_log_has_count_header(caplog):
    """打回日志必须带计数头——headline 截断数是陪跑复盘锚点，零截断/有截断都要如实。"""
    with caplog.at_level(logging.WARNING):
        _log_reject_issues("[VALIDATE_PLAN] 某闸未通过", ["i1", "i2", "i3", "i4", "i5"])
        _log_reject_issues("[VALIDATE_PLAN] 小池", ["only"])
    msgs = [r.message for r in caplog.records]
    assert any("共 5 条" in m for m in msgs), "截断时必须如实报总池规模（防 3/N 误判）"
    assert any("共 1 条" in m for m in msgs), "计数头恒在（不只在截断时）"


def _g1_reject_state():
    """G1 ③b 同名异包 create 打回语料。"""
    ch = "m1/src/main/java/com/x/channel/AlarmSender.java"
    if_ = "m1/src/main/java/com/x/iface/AlarmSender.java"
    return {
        "plan": _plan([_st("st-a", create=[ch]), _st("st-b", create=[if_])]),
        "task_description": "告警编排",
        "plan_retry_count": 0,
        "complexity": "COMPLEX",
    }


def _c1_reject_state():
    """C1 契约符号无主打回语料：契约声明一堆 owner，plan 里无人承接。"""
    ifaces = [{"name": f"IFace{i}", "defined_in": f"m1/src/main/java/com/x/IFace{i}.java",
               "methods": []} for i in range(6)]
    return {
        "plan": _plan([_st("st-1", create=["m1/src/main/java/com/x/Unrelated.java"])]),
        "shared_contract": {"interfaces": ifaces},
        "task_description": "契约无主",
        "plan_retry_count": 0,
        "complexity": "COMPLEX",
    }


@pytest.mark.parametrize("gate,builder", [("G1", _g1_reject_state), ("C1", _c1_reject_state)])
@pytest.mark.asyncio
async def test_a4_reject_paths_emit_count_header(gate, builder, caplog):
    """★调用点行为断言（R1 双方 + R2 双方点名的假绿面）★：真跑到打回的 validate_plan
    必须产出计数头——只测 helper 的话，任一调用点改回内联 issues[:3] 测试照绿。
    R2 实测：原先只锁 G1，C1/C2/R40-1 三处退回内联全绿=应答面比声称面窄 3/4。"""
    with caplog.at_level(logging.WARNING):
        out = await validate_plan(builder())
    assert out.get("plan_valid") is False, f"{gate} 语料必须被打回"
    assert any("共 " in r.message and "条，去重前）" in r.message and "打回 PLAN" in r.message
               for r in caplog.records), \
        f"{gate} 打回路径必须走 _log_reject_issues（计数头是复盘唯一锚点）"


# ─── B3 T4 多落点观测账 ───


def test_b3_wire_ambiguous_multi_location_accounted():
    """同一类型在多子任务各建不同落点 → T4 检出 skipped_ambiguous（观测账数据源），
    绝不挑任意落点布线（fail-closed 交 ③b）。"""
    subs = [
        _st("st-1", create=["m1/src/main/java/com/a/AlarmService.java"]),
        _st("st-2", create=["m2/src/main/java/com/b/AlarmService.java"]),
        _st("st-3", desc="调用 AlarmService 发送告警"),
    ]
    plan = TaskPlan(subtasks=subs, parallel_groups=[["st-1", "st-2", "st-3"]])
    res = wire_created_type_references(plan)
    assert "AlarmService" in (res.get("skipped_ambiguous") or []), \
        "多落点歧义必须进观测账（round67m2 已见未治面）"
    assert not res.get("wired"), "歧义绝不挑边布线"


def test_b3_state_key_declared_and_lifecycle():
    """t4_ambiguous_types 必须 BrainState 声明+注册表 round 生命周期（always-emit
    防粘滞纪律：未声明键会被 LangGraph 静默丢弃）。"""
    from swarm.brain.state import BrainState, ACCOUNTING_KEY_LIFECYCLE
    assert "t4_ambiguous_types" in BrainState.__annotations__
    assert ACCOUNTING_KEY_LIFECYCLE.get("t4_ambiguous_types") == "round"


@pytest.mark.asyncio
async def test_b3_elaborate_emits_account_and_early_return_clears_it():
    """★落账链行为断言★：elaborate 必须把歧义落进返回 dict；且【早退路径】也要
    always-emit 空账——否则轮 N 落账、轮 N+1 plan 空 → 陈旧账粘进 checkpoint。"""
    from swarm.brain.planning_nodes import elaborate
    subs = [
        _st("st-1", create=["m1/src/main/java/com/a/AlarmSender.java"]),
        _st("st-2", create=["m2/src/main/java/com/b/AlarmSender.java"]),
        # 消费方须自带写落点：无写 scope 的子任务会被 elaborate G3 当死子任务剪掉，
        # 剪掉即无消费边、T4 也就无从检出歧义（真实形态消费方本就有自己的文件）
        _st("st-3", create=["m1/src/main/java/com/a/AlarmDispatcher.java"],
            desc="调用 AlarmSender 投递"),
    ]
    out = await elaborate({"plan": _plan(subs), "task_description": "告警"})
    assert "AlarmSender" in (out.get("t4_ambiguous_types") or []), \
        "elaborate 必须把 T4 歧义落进 state 账（B3 全部价值所在）"
    # 早退路径（plan 空/无 subtasks）——always-emit 防粘滞
    out_empty = await elaborate({"plan": None})
    assert out_empty.get("t4_ambiguous_types") == [], "早退也须 always-emit 空账"


@pytest.mark.asyncio
async def test_b3_validate_lifts_account_into_warnings_even_on_reject(caplog):
    """★可见面行为断言★：观测账必须升进 plan_validation_warnings，且在【打回早退】
    轮同样可见——round67m2 的教训正是"最该看见的轮反而看不见"。"""
    state = {
        "plan": _plan([_st("st-1", create=["m1/src/main/java/com/a/A.java"])]),
        "task_description": "x",
        "plan_retry_count": 0,
        "complexity": "COMPLEX",
        "t4_ambiguous_types": ["AlarmSender", "AlarmLevelEnum"],
        # 触发整模块分解失败早退（B3 块上移前，这条路径完全绕过观测账）
        "plan_batch_failed_modules": [{"name": "m1", "reason": "timeout", "files": 3}],
    }
    with caplog.at_level(logging.WARNING):
        out = await validate_plan(state)
    assert out.get("plan_valid") is False
    warns = out.get("plan_validation_warnings") or []
    assert any("T4 检出 2 个" in w for w in warns), \
        f"打回早退轮也必须带 T4 观测账（已见未治治本面）：{warns}"


def _zero_warning_states():
    """零 warning 语料 × 各条 return 路径——恒发与条件发射【只在此语料下有差异】，
    R2 双复核实测：原先只驱动 plan-空一条，其余 10 个恒发点改回条件发射全绿（假绿）。"""
    ok = "m1/src/main/java/com/a/Alpha.java"
    dangling = _st("st-1", create=[ok])
    dangling.depends_on = ["st-does-not-exist"]     # 结构校验失败早退
    return {
        "plan 空": {"plan": None},
        "结构校验失败": {"plan": _plan([dangling])},
        "SIMPLE 快速路径": {"plan": _plan([_st("st-1", create=[ok])]),
                            "complexity": "SIMPLE"},
        "整模块分解失败": {"plan": _plan([_st("st-1", create=[ok])]),
                           "plan_batch_failed_modules": [
                               {"name": "m1", "reason": "timeout", "files": 3}]},
    }


@pytest.mark.parametrize("path_name", list(_zero_warning_states()))
@pytest.mark.asyncio
async def test_b3_warnings_key_always_emitted_on_every_path(path_name):
    """★round 语义兑现的【族守卫】（复核 hunter HIGH + R2 双方 MEDIUM）★：
    plan_validation_warnings 在【每一条】return 上恒发——本轮无软警告时必须写空列表把
    上轮值清掉，否则陈旧文案会带着"本轮检出 N 个"的计数语义粘滞进 API/复盘面。
    族修了族守卫也要修：只钉一个点=其余 10 点可静默回退（R2 逐点变异实测）。"""
    state = dict(_zero_warning_states()[path_name])
    state.setdefault("plan_retry_count", 0)
    state.setdefault("task_description", "x")
    out = await validate_plan(state)
    assert "plan_validation_warnings" in out, \
        f"{path_name} 必须发键（缺席=LangGraph 保留上轮值=陈旧粘滞）"
    assert out["plan_validation_warnings"] == [], f"{path_name} 零警告须如实为空"


# ─── B5 OWNER-LEDGER 分层入册 ───


def _td_plan(names):
    return [{"module": "m", "action": "create",
             "path": f"m/src/main/java/com/x/{n}.java"} for n in names]


def test_b5_ledger_high_risk_family_first(caplog):
    """60 帽下高危族（*Service/*Controller 等 bean 命名空间敏感类）必须优先进【详情层】——
    纯字母序会把撞车高危族挤出带路径的指引（round67m2 实证 81/136 条保护被丢弃）。"""
    benign = [f"Aaa{i:02d}Widget" for i in range(70)]
    risky = [f"Zzz{i}Service" for i in range(8)] + [f"Zzz{i}Controller" for i in range(2)]
    with caplog.at_level(logging.INFO):
        block = contract_owner_ledger_block(None, _td_plan(benign + risky))
    assert block, "JVM 台账必须产出"
    detail_rows = [l for l in block.splitlines() if "→ 唯一 owner：" in l]
    risky_rows = [l for l in detail_rows if "Zzz" in l]
    assert len(risky_rows) == len(risky), \
        f"高危族必须全部进详情层（纯字母序会全灭）：{len(risky_rows)}/{len(risky)}"
    assert len(detail_rows) == 60, "详情层行数=帽（防 WARNING 数字与行数漂移的假绿）"


def test_b5_ledger_high_risk_suffixes_cover_repo_history():
    """★复核 reviewer HIGH 整改断言★：后缀表须覆盖本仓 ③b 历史实锤 TOP 族——
    初版表只覆盖 29%（45 次命中），TOP-2 的 *Enum/*Sender 全在表外，
    等于把经验主力撞车族排到帽后=净退化。"""
    hist = ["AlarmLevelEnum", "AlarmChannelSender", "AlarmTypeEnum", "AlarmTemplate",
            "AlarmConstants", "ChannelSenderRegistry", "SimpleNotifyRequest",
            "NotifyMessage", "AppSecretAuthInterceptor", "AlarmOrchestrationEngine"]
    # 70 个无害名占满帽，历史撞车族若不被判高危就会被挤出详情层
    benign = [f"Aaa{i:02d}Widget" for i in range(70)]
    block = contract_owner_ledger_block(None, _td_plan(benign + hist))
    detail = "\n".join(l for l in block.splitlines() if "→ 唯一 owner：" in l)
    missing = [n for n in hist if n not in detail]
    assert not missing, f"本仓 ③b 历史实锤族必须判高危进详情层，漏：{missing}"


def test_b5_overflow_falls_back_to_brief_layer_not_silence(caplog):
    """★分层治本断言★：超详情帽的类不再"完全掉出台账"——必须落简表层拿到
    禁重名保护（帽本身才是 round67m2 的损失源）。"""
    names = [f"Aaa{i:03d}Widget" for i in range(200)]
    with caplog.at_level(logging.WARNING):
        block = contract_owner_ledger_block(None, _td_plan(names))
    detail_rows = [l for l in block.splitlines() if "→ 唯一 owner：" in l]
    assert len(detail_rows) == 60, "详情层仍受 60 帽约束"
    assert "Aaa199Widget" in block, "详情层之外的类必须落简表层（绝不静默蒸发）"
    assert not [r for r in caplog.records if "两层预算均爆" in r.message], \
        "200 条未超两层总帽，不该报丢弃"


def test_b5_both_layers_exhausted_warns_with_sample(caplog):
    """两层总帽都爆才是真丢弃——必须 WARNING 且带样本（分层后这是唯一仍会失守的路径，
    诚实边界不可静默）。"""
    names = [f"Aaa{i:04d}Widget" for i in range(400)]
    with caplog.at_level(logging.WARNING):
        contract_owner_ledger_block(None, _td_plan(names))
    hits = [r.message for r in caplog.records if "两层预算均爆" in r.message]
    assert hits, "两层全爆必须 WARNING（被丢弃类零台账保护）"
    # ★断言具体类名而非"样本"二字（R2 hunter L-7：字面命中的是格式串，把样本置空仍全绿）★
    # 400 条 = 详情 60 + 简表 300 → 丢弃集自第 361 条起，样本取其前 8 条
    assert "Aaa0360Widget" in hits[0], f"WARNING 须带真实被丢弃类名（否则丢了谁不可还原）：{hits[0]}"


def test_b5_contract_pool_also_prioritized():
    """契约池同样按高危族优先排序（复核点名：只测 td 池会让契约池的 key= 被摘掉仍全绿）。"""
    ifaces = [{"defined_in": f"m/src/main/java/com/x/Aaa{i:02d}Widget.java"}
              for i in range(70)]
    ifaces += [{"defined_in": f"m/src/main/java/com/x/Zzz{i}Service.java"} for i in range(5)]
    block = contract_owner_ledger_block({"interfaces": ifaces}, None)
    detail = "\n".join(l for l in block.splitlines() if "→ 唯一 owner：" in l)
    assert all(f"Zzz{i}Service.java" in detail for i in range(5)), \
        "契约池的高危族同样必须优先进详情层"


# ─── B6 ③b 文案口径 ───


def test_b6_samename_wording_and_signature_stability():
    """同子任务内多文件同名异包自撞（round67m2 st-9 channel/iface 双接口形态）——
    文案绝不说"被多个子任务"（责任面误判=陪跑定错因）；且主句恒定，不因分组切换
    翻转 R67F-T2 熔断签名（复核 reviewer LOW：签名只该反映结构违例本体）。"""
    ch = "m1/src/main/java/com/x/channel/AlarmSender.java"
    if_ = "m1/src/main/java/com/x/iface/AlarmSender.java"
    res = validate_module_coherence(_plan([_st("st-9", create=[ch, if_])]))
    assert not res.valid, "同子任务自撞仍须 ③b fail-closed（检出不变，只修文案）"
    hit = [i for i in res.issues if "AlarmSender" in i]
    assert hit and "被多个子任务" not in hit[0], "单子任务自撞绝不说多个子任务（B6 文案面）"
    assert "st-9" in hit[0], "归属仍须可见（落点→归属子任务）"
    # 同一违例本体在【跨子任务】形态下签名必须一致（分组翻转不扰动熔断）
    res2 = validate_module_coherence(_plan([_st("st-a", create=[ch]), _st("st-b", create=[if_])]))
    hit2 = [i for i in res2.issues if "AlarmSender" in i]
    assert hit2 and "被多个子任务" not in hit2[0]
    sig1 = normalize_structural_signature(hit)
    sig2 = normalize_structural_signature(hit2)
    assert sig1 == sig2, f"主句须恒定（签名随分组翻转会让同签名熔断少认一次）：{sig1} vs {sig2}"
