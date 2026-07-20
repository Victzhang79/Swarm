"""#33 round65e13 head-of-line 连坐死型 三闸治本（test-first 复现盘）。

round65e13 死因（三路复盘定案）：worker 模型把 ruoyi-framework（reactor SPOF）Java 写坏
→ -am 连坐 50 个下游 → 规模闸 escalate。三个编排缺陷：

  ① 量错：规模闸用 _pd_new（blast-radius=病灶+全部下游受害者，=50）当"连坐规模"，
     而非"独立根缺陷数"（实只 1-2：病灶本体；受害者自产物全绿仅被 -am 拖入）。
  ② 病灶从没换过备选：终身派发熔断(_over_cap)/B2 指纹三连(_sig_exhausted)把 deepest
     直跳 max_retries+2，越过 retry_count==max_retries→retry_alternate 那一档 → 病灶
     永远够不到"换备选"格，三次 brain 级重试全同模型。
  ③ 无模型退役：per-turn 换挡不传导到 per-subtask 的 brain 级重试模型分配。

三闸（均以 round65e13 形态构造失败测试）：
  闸1 病灶优先换备选：终局前，对【经 _over_cap/_sig_exhausted 抄近道进终局、从未换过备选】
      的连坐根，给【一次】retry_alternate（并清 force_strong 让异构备选真生效），再回 DISPATCH；
      换过仍失败者不再触发（至多一轮，绝不无限循环）。
  闸2 模型退役：复读退化在最强模型上【重复】发生=最强模型本身退化 → 换异构备选，不复用退化模型。
  闸3 规模闸量对：计量从 blast-radius 闭包改为【独立根缺陷数】——受害者不计入；只有真·根缺陷
      数超阈值才算计划覆灭 escalate（fail-closed 非静默 PARTIAL 语义不变，只收窄计量口径+触发）。
"""
from __future__ import annotations

import asyncio

from swarm.brain.nodes import handle_failure
from swarm.brain.nodes.failure import _normalize_fail_sig
from swarm.types import (
    FileScope,
    SubTask,
    SubTaskDifficulty,
    TaskPlan,
    WorkerOutput,
)

_ROOT_FILE = "ruoyi-framework/src/main/java/com/ruoyi/framework/X.java"
# round65e13 实锤病灶：写坏 reactor SPOF 的 pom（ModelParseException）——【非】缺依赖形态，
# 故不触发 A2 定向恢复(_MISSING_DEP_PATTERNS)、直落 _sig_exhausted 终局分支验证闸1。
_BUILD_FAIL_DFR = "build_fail: ModelParseException Unrecognised tag group parent.groupId is missing"
_BUILD_FAIL_SIG = "det|" + _normalize_fail_sig(_BUILD_FAIL_DFR)


def _root_sig_exhausted(sid):
    """连坐根/病灶：自模块编译崩（build_fail），失败被分类 transient 且【确定性指纹三连】
    → _sig_exhausted 抄近道进终局（round65e13 死路：同输入白跑，越过 retry_alternate 档）。"""
    return WorkerOutput(subtask_id=sid, diff="+x", summary="",
                        l1_passed=False,
                        l1_details={"det_fail_reason": _BUILD_FAIL_DFR,
                                    "failure_class": "transient",
                                    "l1_2_compile_ok": False})


def _st(sid, *, create=None, readable=None, depends=None, ua=None,
        difficulty=SubTaskDifficulty.MEDIUM):
    return SubTask(id=sid, description=f"task {sid}",
                   difficulty=difficulty,
                   scope=FileScope(create_files=create or [],
                                   readable=readable or [],
                                   upstream_artifacts=ua or []),
                   depends_on=depends or [])


def _plan(subs):
    return TaskPlan(subtasks=subs, parallel_groups=[[s.id for s in subs]])


def _root_fail(sid):
    """连坐根/病灶：自模块编译崩（build_fail），非 pipeline_blocked 受害者。"""
    return WorkerOutput(subtask_id=sid, diff="+x", summary="",
                        l1_passed=False,
                        l1_details={"det_fail_reason": "build_fail: malformed pom",
                                    "l1_2_compile_ok": False})


def _victim(sid):
    """连坐受害者：自身 compile 通过，仅被上游坏 reactor 兄弟阻塞（pipeline_blocked/transient）。"""
    return WorkerOutput(subtask_id=sid, diff="+ok", summary="",
                        l1_passed=False,
                        l1_details={"pipeline_blocked": "upstream_module_broken",
                                    "failure_class": "transient",
                                    "l1_2_compile_ok": True,
                                    "det_fail_reason": "pipeline_blocked: upstream_module_broken"})


def _done(sid):
    return WorkerOutput(subtask_id=sid, diff="+done", summary="", l1_passed=True)


# ─────────────────────────── 闸1：病灶优先换备选 ───────────────────────────

def test_gate1_sig_exhausted_root_never_alt_gets_retry_alternate():
    """★问题②本体★：连坐根经【B2 指纹三连(_sig_exhausted)】抄近道进终局、从未换过备选
    （据持久账本 subtask_alternate_ever_used）→ 闸1 给一次 retry_alternate，不直接 escalate/abandon。
    build_fail 未被本文件置 force_strong → 换备选自然生效（无 `not _fs` 吞噬）。"""
    root = _st("st-root", create=[_ROOT_FILE])
    done = _st("st-done", create=["m/src/main/java/D.java"])
    plan = _plan([root, done])
    state = {
        "plan": plan,
        "failed_subtask_ids": ["st-root"],
        "subtask_results": {"st-root": _root_sig_exhausted("st-root"), "st-done": _done("st-done")},
        # B2 指纹已两连 → 本轮第三连 → _sig_exhausted 抄近道进终局，越过 retry_alternate 档
        "subtask_block_signatures": {"st-root": {"sig": _BUILD_FAIL_SIG, "count": 2}},
        # subtask_alternate_ever_used 缺省=空 → 从未换过备选
        "dispatch_remaining": ["st-done"],
    }
    r = asyncio.run(handle_failure(state))
    assert r.get("failure_strategy") == "retry_alternate", \
        f"病灶从未换备选→必须先 retry_alternate 而非 escalate/abandon: {r.get('failure_strategy')}"
    assert r.get("subtask_use_alternate", {}).get("st-root") is True, \
        "闸1 必须置 subtask_use_alternate（本轮派备选）"
    assert r.get("subtask_alternate_ever_used", {}).get("st-root") is True, \
        "★CRITICAL★ 闸1 必须写【持久账本】subtask_alternate_ever_used（防 dispatch 消费清空后无界重触发）"
    assert "st-root" in (r.get("dispatch_remaining") or []), "病灶必须回队重派"
    assert not r.get("failure_escalated"), "闸1 是重派不是 escalate"


def _infra_block_sig_exhausted(sid, kind="sandbox_env_probe_blocked"):
    """infra/env 阻塞（非模型可修）：pipeline_blocked 置位、failure_class=transient、指纹三连。
    换模型治不了 infra → 闸1 绝不碰它（该走 partial/abandon）。"""
    return WorkerOutput(subtask_id=sid, diff="", summary="blocked", l1_passed=False,
                        l1_details={"pipeline_blocked": kind,
                                    "blocked_on_files": ["m/f.java"],
                                    "failure_class": "transient"})


def test_gate1_infra_block_not_model_swapped():
    """★复核回归本体★：infra/env 阻塞（sandbox_env_probe_blocked / build_infra_failure）经指纹
    三连进终局——虽是根缺陷（闸3 计量入账 fail-closed），但换模型没用 → 闸1【不】触发，正常落
    终局（有完成产物→partial/abandon）。对照 build_fail 根【要】触发闸1（round65e13 本体不回归）。"""
    for kind in ("sandbox_env_probe_blocked", "build_infra_failure"):
        sig = f"{kind}|m/f.java"
        root = _st("st-1", create=[_ROOT_FILE])
        done = _st("st-done", create=["m/src/main/java/D.java"])
        plan = _plan([root, done])
        state = {
            "plan": plan,
            "failed_subtask_ids": ["st-1"],
            "subtask_results": {"st-1": _infra_block_sig_exhausted("st-1", kind),
                                "st-done": _done("st-done")},
            "subtask_block_signatures": {"st-1": {"sig": sig, "count": 2}},
            "dispatch_remaining": [],
        }
        r = asyncio.run(handle_failure(state))
        assert r.get("failure_strategy") != "retry_alternate", \
            f"{kind} 是 infra，换模型没用→闸1 绝不触发: {r.get('failure_strategy')}"
        assert r.get("failure_strategy") == "abandon", \
            f"{kind} infra 三连+有完成产物→部分交付 abandon: {r.get('failure_strategy')}"


def test_gate1_over_cap_alone_does_NOT_trigger():
    """★F5（silent-hunter MED）★：仅 _over_cap（终身派发≥6 硬资源天花板）不触发闸1——
    A2 终身熔断铁律绝对，绝不因闸1 再加派（round48c 2.8h 空烧防线不破）。"""
    root = _st("st-root", create=[_ROOT_FILE])
    done = _st("st-done", create=["m/src/main/java/D.java"])
    plan = _plan([root, done])
    state = {
        "plan": plan,
        "failed_subtask_ids": ["st-root"],
        "subtask_results": {"st-root": _root_fail("st-root"), "st-done": _done("st-done")},
        "subtask_dispatch_totals": {"st-root": 6},   # _over_cap 硬熔断，无 _sig_exhausted
        "dispatch_remaining": ["st-done"],
    }
    r = asyncio.run(handle_failure(state))
    assert r.get("failure_strategy") != "retry_alternate", \
        f"仅 _over_cap 不得触发闸1 换备选（A2 硬天花板绝对）: {r.get('failure_strategy')}"
    # 单根+有完成产物 → 部分交付 abandon（诚实 PARTIAL），不再加派
    assert r.get("failure_strategy") == "abandon", r.get("failure_strategy")


def test_gate1_bounded_by_persistent_ledger_multi_round():
    """★CRITICAL（code-reviewer）本体★：跨轮【真实模拟 dispatch 消费】——每轮清空
    subtask_use_alternate（dispatch:904 派出即清）、递增 subtask_dispatch_totals——闸1 至多
    触发一次；持久账本 subtask_alternate_ever_used 一旦置位，下一轮 _sig_exhausted 再来
    也不再 retry_alternate（落终局），绝不无界重触发架空 A2 熔断。"""
    root = _st("st-root", create=[_ROOT_FILE])
    done = _st("st-done", create=["m/src/main/java/D.java"])
    plan = _plan([root, done])
    base = {
        "plan": plan,
        "failed_subtask_ids": ["st-root"],
        "subtask_results": {"st-root": _root_sig_exhausted("st-root"), "st-done": _done("st-done")},
        "subtask_block_signatures": {"st-root": {"sig": _BUILD_FAIL_SIG, "count": 2}},
        "dispatch_remaining": ["st-done"],
    }
    alt_fire_count = 0
    ledger: dict = {}
    totals: dict = {"st-root": 0}
    for _round in range(4):
        state = dict(base)
        state["subtask_alternate_ever_used"] = dict(ledger)   # 持久账本跨轮传递
        state["subtask_dispatch_totals"] = dict(totals)
        # dispatch 已消费清空上一轮 subtask_use_alternate（consume-on-use），故不喂
        r = asyncio.run(handle_failure(state))
        if r.get("failure_strategy") == "retry_alternate":
            alt_fire_count += 1
        # 模拟 dispatch 消费：合并持久账本（wrapper 已写 result）、递增终身派发（绝不清账本）
        ledger.update(r.get("subtask_alternate_ever_used") or {})
        totals["st-root"] = totals["st-root"] + 1
    assert alt_fire_count == 1, \
        f"★闸1 必须至多触发一次★（持久账本防无界重触发）：实触发 {alt_fire_count} 次"
    assert ledger.get("st-root") is True, "持久账本必须记住已换过备选"


def test_gate1_preserves_e5_force_strong():
    """★F3（code-reviewer MED）★：病灶的 force_strong 来自 E5（超大不可拆块，非本轮
    refusal/degeneration 源）→ 闸1 触发换备选时【绝不清】它，防超大块被降级弱模型。"""
    root = _st("st-root", create=[_ROOT_FILE])
    done = _st("st-done", create=["m/src/main/java/D.java"])
    plan = _plan([root, done])
    state = {
        "plan": plan,
        "failed_subtask_ids": ["st-root"],
        # 本轮失败源=build_fail（非 refusal/degeneration）→ 非本文件自置的 force_strong 来源
        "subtask_results": {"st-root": _root_sig_exhausted("st-root"), "st-done": _done("st-done")},
        "subtask_block_signatures": {"st-root": {"sig": _BUILD_FAIL_SIG, "count": 2}},
        "subtask_force_strong": {"st-root": True},   # E5 置（超大块），非本轮 refusal/degen
        "dispatch_remaining": ["st-done"],
    }
    r = asyncio.run(handle_failure(state))
    assert r.get("failure_strategy") == "retry_alternate", "闸1 仍应触发换备选"
    assert r.get("subtask_force_strong", {}).get("st-root") is True, \
        "★F3★ E5 来源的 force_strong 绝不被闸1 误清（无法判来源=不清，超大块不降级）"


def test_gate1_organic_terminal_no_shortcut_still_escalates():
    """对照面（不过激）：未走 _sig_exhausted 抄近道、组织性重试耗尽且 0 完成 → 闸1 不触发，
    仍 escalate（既有 fail-closed 语义不回归）。"""
    root = _st("st-root", create=[_ROOT_FILE])
    plan = _plan([root])
    state = {
        "plan": plan,
        "failed_subtask_ids": ["st-root"],
        "subtask_results": {"st-root": _root_fail("st-root")},
        "subtask_retry_counts": {"st-root": 3},   # 组织性耗尽（无 _sig_exhausted 抄近道）
        "dispatch_remaining": [],
    }
    r = asyncio.run(handle_failure(state))
    assert r.get("failure_strategy") == "escalate", \
        f"组织性耗尽+0完成应 escalate（闸1 只治指纹三连抄近道，不夺其它终局）: {r.get('failure_strategy')}"
    assert r.get("failure_escalated") is True


# ─────────────────────────── 闸3：规模闸量对 ───────────────────────────

def test_gate3_single_high_fanout_root_partial_not_escalated():
    """★问题①本体★：单个高扇出病灶连坐一大片受害者（blast-radius 15）——根缺陷只 1 个，
    ≠计划覆灭 → 绝不 escalate，走部分交付 abandon（受害者随闭包，诚实 PARTIAL）。
    （终局经组织性 retry 耗尽、无 _sig_exhausted → 闸1 不插手，隔离验证闸3 计量口径。）"""
    root = _st("st-root", create=[_ROOT_FILE])
    consumers = [
        _st(f"st-c{i}", create=[f"admin/src/main/java/C{i}.java"],
            readable=[_ROOT_FILE], ua=[_ROOT_FILE],   # 硬消费=硬连坐（seed 构建输入）
            depends=["st-root"])
        for i in range(14)
    ]
    done = _st("st-done", create=["m/src/main/java/D.java"])
    plan = _plan([root, *consumers, done])
    state = {
        "plan": plan,
        "failed_subtask_ids": ["st-root"],
        "subtask_results": {"st-root": _root_fail("st-root"), "st-done": _done("st-done")},
        "subtask_retry_counts": {"st-root": 99},   # 组织性耗尽进终局（无 _sig_exhausted → 闸1 不插手）
        "dispatch_remaining": [c.id for c in consumers],
    }
    r = asyncio.run(handle_failure(state))
    assert r.get("failure_strategy") == "abandon", \
        f"单根高扇出≠计划覆灭，应部分交付而非 escalate: {r.get('failure_strategy')}"
    assert not any(str(d).startswith("mass_abandon_gate")
                   for d in (r.get("degraded_reasons") or [])), \
        f"根缺陷=1 绝不触发规模闸 escalate: {r.get('degraded_reasons')}"
    assert "st-c0" in set(r.get("abandoned_subtask_ids") or []), "受害者随闭包放弃（正常剪枝）"


def test_gate3_many_root_defects_still_escalate():
    """★fail-closed 不回归★：真·多根缺陷（>阈值 个独立模块各自编译崩）=计划覆灭
    → 仍 escalate 人工，绝不静默清盘成 PARTIAL；机读 mass_abandon_gate 留痕。"""
    roots = [_st(f"st-r{i}", create=[f"m{i}/src/main/java/R{i}.java"]) for i in range(12)]
    done = _st("st-done", create=["z/src/main/java/D.java"])
    plan = _plan([*roots, done])
    state = {
        "plan": plan,
        "failed_subtask_ids": [r.id for r in roots],
        "subtask_results": {**{r.id: _root_fail(r.id) for r in roots},
                            "st-done": _done("st-done")},
        "subtask_retry_counts": {r.id: 99 for r in roots},   # 组织性耗尽（无 _sig_exhausted → 闸1 不插手）
        "dispatch_remaining": [],
    }
    r = asyncio.run(handle_failure(state))
    assert r.get("failure_strategy") == "escalate", \
        f"12 个独立根缺陷 > 阈值 10 = 计划覆灭必 escalate: {r.get('failure_strategy')}"
    assert r.get("failure_escalated") is True
    assert any(str(d).startswith("mass_abandon_gate")
               for d in (r.get("degraded_reasons") or [])), r.get("degraded_reasons")


def test_gate3_victims_in_failed_ids_not_counted_as_roots():
    """★量对边界★：failed_ids 里混入 pipeline_blocked 受害者（自 compile 过）——
    受害者不计入根缺陷；1 真根 + 大量受害者 → 不 escalate。"""
    root = _st("st-root", create=[_ROOT_FILE])
    victims = [_st(f"st-v{i}", create=[f"admin/src/main/java/V{i}.java"],
                   readable=[_ROOT_FILE], ua=[_ROOT_FILE]) for i in range(14)]
    done = _st("st-done", create=["m/src/main/java/D.java"])
    plan = _plan([root, *victims, done])
    failed = ["st-root"] + [v.id for v in victims]
    state = {
        "plan": plan,
        "failed_subtask_ids": failed,
        "subtask_results": {"st-root": _root_fail("st-root"),
                            **{v.id: _victim(v.id) for v in victims},
                            "st-done": _done("st-done")},
        "subtask_retry_counts": {fid: 99 for fid in failed},   # 无 _sig_exhausted → 闸1 不插手
        "dispatch_remaining": [],
    }
    r = asyncio.run(handle_failure(state))
    assert not any(str(d).startswith("mass_abandon_gate")
                   for d in (r.get("degraded_reasons") or [])), \
        f"14 受害者 + 1 真根：根缺陷=1 绝不 escalate: {r.get('degraded_reasons')} / {r.get('failure_strategy')}"


def test_f1_f2_self_defects_counted_as_root_not_victim():
    """★F1+F2（silent-hunter HIGH 亲裁）★：pipeline_blocked 分类字符串值域含【自身病灶】——
    只有 _INTERNAL_BLOCKED_KINDS 才是真连坐受害者；自身病灶（空 diff/超时/infra/编译过+超时）
    必须算根缺陷（不被剔出规模闸计量），否则少算根缺陷→静默 PARTIAL（round65c 死型复活）。"""
    from swarm.brain.nodes.failure import _is_pipeline_blocked_victim, _root_defect_ids
    # F1：自身病灶分类字符串（非白名单）绝不算受害者
    for kind in ("worker_deadline_exhausted", "malformed_diff_zero_files",
                 "build_infra_failure", "build_manifest_missing",
                 "test_infra_failure", "verify_infra_failure"):
        assert not _is_pipeline_blocked_victim({"pipeline_blocked": kind}), \
            f"{kind} 是自身病灶，绝不算连坐受害者"
    # F2：编译过 + transient(自己写的挂死测试超时) 也是自身病灶——删 build_ok+transient 判据后不误判
    assert not _is_pipeline_blocked_victim(
        {"l1_2_compile_ok": True, "failure_class": "transient"}), \
        "编译过+超时=自身病灶，不再误判受害者"
    # 真受害者：仅白名单内
    for kind in ("upstream_module_broken", "internal_pkg_not_built",
                 "module_registered_before_scaffold"):
        assert _is_pipeline_blocked_victim({"pipeline_blocked": kind}), f"{kind} 是真受害者"
    # 缺证据 → 根缺陷（fail-closed）
    assert not _is_pipeline_blocked_victim(None)
    assert not _is_pipeline_blocked_victim({})
    # _root_defect_ids：自身病灶入根缺陷，白名单受害者出
    sr = {
        "st-timeout": WorkerOutput(subtask_id="st-timeout", diff="", summary="", l1_passed=False,
                                   l1_details={"pipeline_blocked": "worker_deadline_exhausted"}),
        "st-empty": WorkerOutput(subtask_id="st-empty", diff="", summary="", l1_passed=False,
                                 l1_details={"pipeline_blocked": "malformed_diff_zero_files"}),
        "st-victim": WorkerOutput(subtask_id="st-victim", diff="+ok", summary="", l1_passed=False,
                                  l1_details={"pipeline_blocked": "upstream_module_broken"}),
    }
    roots = set(_root_defect_ids(list(sr), sr))
    assert roots == {"st-timeout", "st-empty"}, \
        f"自身病灶(超时/空diff)必须算根缺陷，白名单受害者(upstream)剔出: {roots}"


# ─────────────────────────── 闸2：模型退役 ───────────────────────────

def test_gate2_repeat_degeneration_switches_to_alternate():
    """★问题③本体★：复读退化(degeneration_hard_fail)在最强模型上【重复】发生
    （上一轮已 force_strong）=最强模型本身退化 → 换异构备选、清 force_strong，不复用退化模型。"""
    st = _st("st-degen", create=["m/src/main/java/G.java"])
    plan = _plan([st])
    res = WorkerOutput(subtask_id="st-degen", diff="", summary="",
                       l1_passed=False,
                       l1_details={"l1_decision_source": "degeneration_hard_fail"})
    state = {
        "plan": plan,
        "failed_subtask_ids": ["st-degen"],
        "subtask_results": {"st-degen": res},
        "subtask_retry_counts": {},   # 非终局（组织性 retry 档）
        "subtask_force_strong": {"st-degen": True},   # 上一轮已升最强 → 再退化
        "dispatch_remaining": [],
    }
    r = asyncio.run(handle_failure(state))
    assert r.get("failure_strategy") in ("retry", "retry_alternate")
    assert r.get("subtask_use_alternate", {}).get("st-degen") is True, \
        "最强模型重复退化 → 必须换异构备选（不复用退化模型）"
    assert not r.get("subtask_force_strong", {}).get("st-degen"), \
        "闸2 必须清 force_strong，否则 dispatch `not _fs` 吞掉换备选、退化模型永不退役"


def test_gate2_first_degeneration_keeps_force_strong():
    """对照面（R63-T7 不回归）：首次复读退化（无历史 force_strong）→ 仍升最强模型，
    不换弱备选（偶发退化换弱备更糟）。"""
    st = _st("st-degen", create=["m/src/main/java/G.java"])
    plan = _plan([st])
    res = WorkerOutput(subtask_id="st-degen", diff="", summary="",
                       l1_passed=False,
                       l1_details={"l1_decision_source": "degeneration_hard_fail"})
    state = {
        "plan": plan,
        "failed_subtask_ids": ["st-degen"],
        "subtask_results": {"st-degen": res},
        "subtask_retry_counts": {},
        "dispatch_remaining": [],
        # 无历史 force_strong
    }
    r = asyncio.run(handle_failure(state))
    assert r.get("subtask_force_strong", {}).get("st-degen") is True, \
        "首次退化仍走 R63-T7 升最强模型"
    assert not r.get("subtask_use_alternate", {}).get("st-degen"), \
        "首次退化不换弱备选（R63-T7 语义不回归）"
