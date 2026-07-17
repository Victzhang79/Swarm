"""R65D-T1（round65d 死因本体）：HANDLE_FAILURE 失败处置完备性铁律。

round65d 实锤（task b583df8f，三路交叉印证）：10:45:30 HANDLE_FAILURE 处理 4 失败——
st-29-1 走 round36 自愈重派、st-30-1/st-31 补 C9 动态边、st-26（trivial verify 失败，
无 pipeline_blocked）三条处置日志均无它。代码病灶=_healed 早退 return 只把【已愈】项
放回 dispatch_remaining，其余失败 failed_subtask_ids 直接清空、不回队、失败 result
滞留 subtask_results——对照 _unrecoverable 分支有「其余失败放回重派」对称段，自愈
分支缺失。后果链：st-26 僵尸化 → 10:59:02 A2 定向恢复把滞留 result 数成"保留 3 个
完成态"（_kept 不查 l1_passed）→ 宣称重派 4 个但依赖链穿过 st-26 僵尸永不可就绪
→ 实派 0 无人核销 → 90/94 经 C9 汇流饿死 → 25min 后 R13-4 才发声。

治本三面：
① 根修：_healed return 与 _unrecoverable 分支对称——其余失败放回重派+result 出账；
② 铁律（唯一咽喉=handle_failure 包装）：入口失败数≡出口处置数（重派∪放弃∪保留失败态），
   缺账 fail-loud ERROR+强制回队+degraded_reasons 机读留痕，绝不静默僵尸化；
③ 处方↔派发闭环核销：重派进队者的传递依赖必须终结在 完成/在队/桩豁免——命中已放弃
   （非桩）上游=处方注定落空，ERROR fail-loud（绝不等 25min 后终态才发声）。
"""
import asyncio
import logging
from unittest.mock import patch

from swarm.brain.nodes import handle_failure
from swarm.types import FileScope, SubTask, TaskPlan, WorkerOutput

_JAVA_BO = "[ERROR] cannot find symbol\n  symbol:   class TwoFactorSetupVO\n  location: var user\n"


def _st(sid, depends=None, writable=None):
    return SubTask(id=sid, description="d",
                   scope=FileScope(writable=writable or [f"{sid}.java"]),
                   depends_on=depends or [])


def _wo_blocked(sid, pkgs, build_output=""):
    return WorkerOutput(
        subtask_id=sid, diff="", summary="", l1_passed=False,
        l1_details={"pipeline_blocked": "internal_pkg_not_built",
                    "blocked_on_packages": pkgs, "not_run_kind": "blocked",
                    "failure_class": "transient", "build_output": build_output},
        confidence="low")


def _wo_verify_fail(sid):
    """st-26 形态：trivial 确定性闸 verify 失败——无 pipeline_blocked，纯考卷不过。"""
    return WorkerOutput(
        subtask_id=sid, diff="+x", summary="", l1_passed=False,
        l1_details={"verify_failed": "grep -q 'jackson' mod/pom.xml",
                    "deterministic_gate": "fail"},
        confidence="low")


def _run(state):
    return asyncio.run(handle_failure(state))


def _mixed_batch_state():
    """round65d 10:45:30 最小复刻：自愈项 + verify 失败项（st-26 形态）同批。"""
    jf = "mod/src/main/java/com/x/service/Foo.java"
    plan = TaskPlan(subtasks=[
        _st("st-heal", writable=[jf]),        # round36 自愈形态（无生产者自造引用）
        _st("st-26x"),                        # verify 失败形态（无 pipeline_blocked）
        _st("st-down", depends=["st-26x"]),   # 依赖 st-26x 的下游（在队等待）
    ])
    return {
        "failed_subtask_ids": ["st-heal", "st-26x"],
        "subtask_results": {
            "st-heal": _wo_blocked("st-heal", ["com.x.domain.vo"], _JAVA_BO),
            "st-26x": _wo_verify_fail("st-26x"),
        },
        "dispatch_remaining": ["st-down"],
        "plan": plan,
    }


# ── ①+②：st-26 死因本体——自愈早退绝不静默掉账其余失败 ──

def test_selfheal_return_does_not_drop_sibling_failures():
    """★round65d 死因本体★：同批自愈项触发早退 return 时，未愈未放弃的其余失败
    （st-26 形态）必须回到重派队列，绝不静默掉账。"""
    state = _mixed_batch_state()
    with patch("swarm.brain.nodes.failure._blocked_pkg_unrecoverable",
               return_value=True):
        r = _run(state)
    assert "st-heal" in r["dispatch_remaining"], "前置：自愈项照常重派"
    accounted = (
        "st-26x" in (r.get("dispatch_remaining") or [])
        or "st-26x" in (r.get("failed_subtask_ids")
                        if "failed_subtask_ids" in r
                        else state["failed_subtask_ids"])
        or "st-26x" in (r.get("abandoned_subtask_ids") or []))
    assert accounted, \
        f"st-26x 无任何处置=静默掉账（round65d 饿死 90/94 的死因本体）: {r.keys()}"


def test_dropped_failure_result_must_not_linger_as_settled():
    """★冻结完成态面★：被回队的失败者，其 L1-fail result 必须出账——滞留
    subtask_results 会被后续机制（A2 _kept 等）数成'完成态'（10:59:02 实锤）。"""
    state = _mixed_batch_state()
    with patch("swarm.brain.nodes.failure._blocked_pkg_unrecoverable",
               return_value=True):
        r = _run(state)
    if "st-26x" in (r.get("dispatch_remaining") or []):
        _sr = r.get("subtask_results", state["subtask_results"])
        assert "st-26x" not in _sr, \
            "回队重派者的失败 result 必须 pop 出账，绝不留僵尸被数成完成态"


def test_disposition_leak_fails_loud_with_machine_account():
    """★铁律 fail-loud 面★：任何分支掉账都必须 ERROR 响亮留痕 + degraded_reasons
    机读账（依赖根失败无处置绝不能等 25min 后 R13-4 终态才发声）。"""
    state = _mixed_batch_state()
    with patch("swarm.brain.nodes.failure._blocked_pkg_unrecoverable",
               return_value=True), \
         patch("swarm.brain.nodes.failure._derive_missing_type_files",
               return_value=["mod/src/main/java/com/x/vo/TwoFactorSetupVO.java"]):
        with_logs = logging.getLogger("swarm.brain.nodes")
        import logging as _lg
        records: list = []
        h = _lg.Handler()
        h.emit = lambda rec: records.append(rec)  # type: ignore[assignment]
        _lg.getLogger().addHandler(h)
        try:
            r = _run(state)
        finally:
            _lg.getLogger().removeHandler(h)
    # 掉账要么根修后不发生（st-26x 已回队），要么铁律兜底并留痕——两者必居其一
    if "st-26x" not in (r.get("dispatch_remaining") or []) \
            and "st-26x" not in (r.get("failed_subtask_ids") or []):
        assert any("处置完备" in (rec.getMessage() or "") for rec in records), \
            "掉账未被根修拦下时，铁律必须 fail-loud"
    _ = with_logs


def test_prescription_unsatisfiable_dep_fails_loud(caplog):
    """★处方核销面★：重派进队者的依赖已被放弃（非桩）=处方注定落空（10:59:02
    '宣称重派 4 实派 0'形态）→ 必须 ERROR fail-loud + 机读留痕，绝不静默等饿死。"""
    plan = TaskPlan(subtasks=[
        _st("st-dead"),
        _st("st-m", depends=["st-dead"],
            writable=["mod/src/main/java/com/x/M.java"]),
    ])
    state = {
        "failed_subtask_ids": ["st-m"],
        "subtask_results": {
            # 缺依赖编译失败签名 → 走 A2 定向恢复分支（requeue 全部 failed_ids）
            "st-m": WorkerOutput(
                subtask_id="st-m", diff="", summary="", l1_passed=False,
                l1_details={"build_output":
                            "[ERROR] package com.missing does not exist",
                            "failure_class": "capability"},
                confidence="low"),
        },
        "abandoned_subtask_ids": ["st-dead"],   # 上游已放弃（非桩）
        "dispatch_remaining": [],
        "plan": plan,
    }
    with caplog.at_level(logging.WARNING):
        r = _run(state)
    if "st-m" in (r.get("dispatch_remaining") or []):
        assert any("处方" in rec.message or "不可就绪" in rec.message
                   for rec in caplog.records), \
            "重派者依赖已放弃上游=永不可派发，必须 fail-loud 核销"
        assert any(str(d).startswith("recovery_prescription_unsatisfiable")
                   for d in (r.get("degraded_reasons") or [])), \
            f"必须 degraded_reasons 机读留痕: {r.get('degraded_reasons')}"


def test_giveup_settled_is_valid_disposition_not_resurrected():
    """★双复核 CRITICAL 锁★：阶梯三 give-up（settled-with-product 终局）是有效处置——
    铁律绝不毁桩+复活重派（那是比原 bug 更凶的无界破坏循环）。"""
    from swarm.brain.nodes.failure import audit_failure_disposition
    stub_wo = WorkerOutput(
        subtask_id="st-x", diff="+stub", summary="", l1_passed=True,
        l1_details={"give_up_mode": "stub"}, confidence="low")
    state = {"failed_subtask_ids": ["st-x"],
             "subtask_results": {"st-x": stub_wo},
             "dispatch_remaining": [], "plan": TaskPlan(subtasks=[_st("st-x")])}
    result = {"failed_subtask_ids": [], "dispatch_remaining": [],
              "give_up_isolated_ids": ["st-x"],
              "subtask_results": {"st-x": stub_wo}}
    audit_failure_disposition(state, result)
    assert result["subtask_results"].get("st-x") is stub_wo, "桩产物绝不可被 pop 销毁"
    assert "st-x" not in result["dispatch_remaining"], "settled 终局绝不可复活重派"
    assert not any(str(d).startswith("failure_disposition_leak")
                   for d in (result.get("degraded_reasons") or [])), \
        "give-up 终局不是掉账，绝不误报"


def test_full_replan_handoff_is_valid_disposition():
    """★复核 HIGH 锁★：strategy=replan 交棒 PLAN 全量重规划（旧 fid 语义已尽）
    ——铁律绝不误报掉账、绝不给健康 replan 永久打上死因签名。"""
    from swarm.brain.nodes.failure import audit_failure_disposition
    state = {"failed_subtask_ids": ["st-only"], "subtask_results": {},
             "dispatch_remaining": [], "plan": TaskPlan(subtasks=[_st("st-only")])}
    result = {"failed_subtask_ids": [], "failure_strategy": "replan",
              "subtask_results": {}}
    audit_failure_disposition(state, result)
    assert "dispatch_remaining" not in result or "st-only" not in result["dispatch_remaining"]
    assert not any(str(d).startswith("failure_disposition_leak")
                   for d in (result.get("degraded_reasons") or [])), \
        f"replan 交棒不是掉账: {result.get('degraded_reasons')}"


def test_audit_crash_fails_loud_with_machine_account():
    """★猎手 MED 锁★：审计自身异常=安全网下线，必须 ERROR + degraded_reasons 机读账
    ——绝不 WARNING 静默降级回 pre-T1 行为。"""
    state = _mixed_batch_state()
    with patch("swarm.brain.nodes.failure.audit_failure_disposition",
               side_effect=RuntimeError("boom")), \
         patch("swarm.brain.nodes.failure._blocked_pkg_unrecoverable",
               return_value=True):
        r = _run(state)
    assert any(str(d).startswith("failure_disposition_audit_error:RuntimeError")
               for d in (r.get("degraded_reasons") or [])), \
        f"审计下线必须机读留痕: {r.get('degraded_reasons')}"


def test_prescription_detects_historical_zombie_dep(caplog):
    """★猎手 F3 锁★：依赖链命中【历史僵尸】（有失败 result 却不在队/不在失败集/未终局
    ——st-26 形态本体，可能来自本闸上线前）同样=处方落空，必须 fail-loud。"""
    from swarm.brain.nodes.failure import audit_failure_disposition
    plan = TaskPlan(subtasks=[_st("st-z"), _st("st-m", depends=["st-z"])])
    zombie = _wo_verify_fail("st-z")
    state = {"failed_subtask_ids": ["st-m"],
             "subtask_results": {"st-z": zombie, "st-m": _wo_verify_fail("st-m")},
             "dispatch_remaining": [], "plan": plan}
    result = {"failed_subtask_ids": [], "dispatch_remaining": ["st-m"],
              "subtask_results": {"st-z": zombie}}
    with caplog.at_level(logging.ERROR):
        audit_failure_disposition(state, result)
    assert any(str(d) == "recovery_prescription_unsatisfiable:st-m"
               for d in (result.get("degraded_reasons") or [])), \
        f"历史僵尸依赖必须被核销面看见: {result.get('degraded_reasons')}"


def test_normal_retry_channel_untouched_is_accounted():
    """对照面：常规 retry 分支不动 failed_subtask_ids 通道（沿用旧值走重试面）
    ——铁律不得误报（通道未动=全员仍在失败集，有处置）。"""
    plan = TaskPlan(subtasks=[_st("st-r")])
    state = {
        "failed_subtask_ids": ["st-r"],
        "subtask_results": {"st-r": _wo_verify_fail("st-r")},
        "dispatch_remaining": [],
        "plan": plan,
    }
    r = _run(state)
    # 常规阶梯：strategy 面存在即可，绝不因铁律误注 degraded/误改队列
    assert not any(str(d).startswith("failure_disposition_leak")
                   for d in (r.get("degraded_reasons") or [])), \
        f"通道未清空时铁律不得误报掉账: {r}"
