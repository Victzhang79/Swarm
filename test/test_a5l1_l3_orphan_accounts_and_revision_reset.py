"""32 号文 A5-L1 + A5-L3 治本锁。

## A5-L1 四条孤儿账，逐条判处不同（**不是一刀切**）

- `context_token_estimate` → **接线**（溢出告警 + degrade 计数）：它是 pinned 预算
  溢出的**唯一**信号（判据三），删了溢出就彻底隐形；
- `plan_elaborated` → **删**：纯死标志，两写点都无条件写 True 而 `graph.py` 零引用，
  事实由 `plan.subtasks` 非空派生；
- `user_profile` → **删**：state 键零读点（消费者只读两个派生 prompt），要结构化画像
  调 `resolve_user_profile`；
- `ingest_draft` → **本轮已作废**：findings 写于 A5-H1 之前，它现在有消费者＝
  `_deliver_review_payload` 的 `draft_chars`。

## A5-L3：`verification_coverage` 在 revision 回环不重置

`revision` 显式清了 `verification_failure`/`l2_passed`/`l3_passed`/
`runtime_smoke_passed`，注释称"verify 节点每轮会覆盖"——但 coverage 的 reducer
（`state.py:48`）是**永不清空的浅合并**，且 DELIVER 有三条**绕过 verify_l2** 的可达路径
（after_clarify 虚假前提阻断 / after_merge escalate / after_handle_failure escalate）
⇒ 修订轮走其中任一，上一轮的格原样留下，`runner.py:1400` 终态 payload 当本轮账上报。

★治法形状是本役最值钱的部分——两个"看起来对"的重置都会造回归★
① 清成 `{}`：reducer 浅合并的循环不迭代 ⇒ 对存量格**毫无作用**（假重置，测试若只断
   "写了 {}" 会全绿而缺陷照旧）；
② 清成 `""`：`gates.py:282` 的 `if _l2_cell:` 会走 else ＝**回退扫 degraded_reasons**，
   而 `:276-279` 明写那条路有永久粘滞（旧轮 unsupported 条目无人能清）⇒ 把修订后本该
   放行的交付**冤拦**。
故重置值必须 truthy 且不以 `unsupported_stack:` 开头 ⇒ `not_run:revision`。
"""
from __future__ import annotations

import logging

# ══════════════ A5-L1 ① context_token_estimate：溢出必须可观测 ══════════════

def _overflow_log(pinned_tokens: int, budget_max: int, budget_reserve: int):
    """构造 pinned 单独超预算的日志（唯一生产可达的溢出出口）。"""
    return [{
        "type": "user_request", "content": "U" * (pinned_tokens * 4),
        "tokens": pinned_tokens, "priority": 1, "pinned": True,
    }]


def test_budget_overflow_emits_warning_and_degrade_count(caplog):
    """★核心锁★ pinned 单独超预算 → 一次 WARNING + 一个机读 degrade 键。

    ★为什么这是"接线"而非"删键"★ `context_token_estimate` 是溢出的**唯一**信号
    （判据三：pinned 永不逐出，循环把 rest 抽干后静默退出，总量仍超预算），而它
    全仓零读点 ⇒ 溢出发生了也无人知。删键会让这个 fail-open 彻底隐形。
    """
    from swarm.infra.degrade import degrade_counts, reset_degrade_counts
    from swarm.memory.sliding_window import compress_context_log

    reset_degrade_counts()
    try:
        with caplog.at_level(logging.WARNING, logger="swarm.memory.sliding_window"):
            _log, _summ, total = compress_context_log(
                _overflow_log(5000, 2000, 500), "", max_tokens=2000, reserve_tokens=500,
            )
        # 前提断言：真的溢出了（否则下面测的不是溢出臂）
        assert total > max(1000, 2000 - 500), (
            f"前提不成立——没有溢出，本锁测不到目标分支。total={total}"
        )
        assert any("预算溢出" in r.message or "预算溢出" in r.getMessage()
                   for r in caplog.records), (
            f"溢出必须留一次 WARNING，实得 {[r.getMessage()[:60] for r in caplog.records]}"
        )
        _counts = degrade_counts()
        _hit = [k for k in _counts if k.startswith("memory.l3_context.budget_overflow")]
        assert _hit, (
            f"溢出必须有**机读**键（WARNING 只能人读；本仓 degrade 计数经 /api/metrics 暴露）。"
            f"实得 {dict(_counts)}"
        )
        assert _hit[0].endswith(".pinned_floor"), (
            f"reason 必须机读可辨（pinned 地板超预算 vs rest 不可逐出是两种因）。实得 {_hit}"
        )
    finally:
        reset_degrade_counts()


def test_no_warning_when_within_budget(caplog):
    """★区分力锁★ 未溢出时不得告警（否则每轮压缩都刷 WARNING，账面变噪声）。"""
    from swarm.infra.degrade import degrade_counts, reset_degrade_counts
    from swarm.memory.sliding_window import compress_context_log

    reset_degrade_counts()
    try:
        with caplog.at_level(logging.WARNING, logger="swarm.memory.sliding_window"):
            compress_context_log(
                [{"type": "x", "content": "a" * 40, "tokens": 10, "priority": 3}],
                "", max_tokens=80000, reserve_tokens=16000,
            )
        assert not [r for r in caplog.records if "预算溢出" in r.getMessage()], \
            "未溢出却告警＝狼来了"
        assert not [k for k in degrade_counts()
                    if k.startswith("memory.l3_context.budget_overflow")], \
            f"未溢出却记 degrade：{dict(degrade_counts())}"
    finally:
        reset_degrade_counts()


def test_overflow_observability_does_not_change_compression_result():
    """观测面绝不反噬主路径：加告警前后返回值必须逐字节相同。

    ★为什么单独锁★ 本仓血泪：给降级路径加观测时改了业务返回值。这条钉住
    "只加观测不改行为"——三元组的每一项都断。
    """
    from swarm.memory.sliding_window import compress_context_log

    log = _overflow_log(5000, 2000, 500)
    new_log, summary, total = compress_context_log(
        list(log), "", max_tokens=2000, reserve_tokens=500)
    # pinned 永不逐出 ⇒ 原样保留；summary 无逐出可总结 ⇒ 空；total = pinned 自身
    assert len(new_log) == 1 and new_log[0]["pinned"] is True, new_log
    assert summary == "", f"无逐出事件不该产生摘要，实得 {summary!r}"
    assert total == 5000, f"total 必须仍是真实估算值（观测不改数），实得 {total}"


# ══════════════ A5-L1 ②③ 两条死键：删除 + 不许回潮 ══════════════

def test_dead_state_keys_removed_from_schema_and_writers():
    """两条死键从 **声明 + 全部写点** 同时消失。

    ★为什么必须两侧同时★ CLAUDE.md 硬纪律：LangGraph 对未声明键**静默丢弃**。
    只删声明留着写点 = 每轮白写一次被丢弃的值（比留着更坏：读代码的人以为它在）；
    只删写点留着声明 = 声明变成过期承诺（正是 A5-M1 治过的形态）。
    """
    from swarm.brain.state import BrainState

    for _k in ("plan_elaborated", "user_profile"):
        assert _k not in BrainState.__annotations__, (
            f"BrainState.{_k} 回潮了（32 号文 A5-L1 已删）。"
            f"plan_elaborated：路由零引用，事实由 plan.subtasks 非空派生；"
            f"user_profile：state 键零读点，要结构化画像调 "
            f"memory/profile.py:resolve_user_profile（现取，非任务起点陈旧快照）。"
        )
    # 派生 prompt 键**必须仍在**——它们是真消费者读的那两个（删错了会静默丢画像注入）
    for _k in ("user_profile_prompt_brain", "user_profile_prompt_worker"):
        assert _k in BrainState.__annotations__, (
            f"{_k} 被误删——这两个是 shared._brain_profile_prompt/_worker_profile_prompt "
            f"真正读的键，丢了画像注入会静默变成「（未加载用户画像）」"
        )


def test_elaborate_outputs_do_not_carry_dead_flag():
    """elaborate 的早退出口不再写 `plan_elaborated`（写点侧，与上一条声明侧配对）。

    ★走真实函数出口而非扫源码★：驱动空 plan 早退臂，断返回 dict 里没有那个键。
    """
    import asyncio

    from swarm.brain.planning_nodes import elaborate

    out = asyncio.run(elaborate({"plan": None}))
    assert "plan_elaborated" not in out, f"早退出口仍写死键：{sorted(out)}"
    # 前提断言：这个出口确实是 A5-L1 动过的那个（同族 round 键仍在＝我没删错东西）
    assert {"t4_ambiguous_types", "oversized_subtask_ids", "invest_fail_count"} <= set(out), (
        f"前提不成立——早退出口的同族 round 键不见了，说明改动越界。实得 {sorted(out)}"
    )


def test_runner_initial_state_has_no_raw_profile_key():
    """runner 初始 state 不再放原始画像 dict，但两个派生 prompt 仍在。

    ★这条锁"三元组首项被丢弃"这件事的**后果**，不是丢弃动作本身★
    """
    import inspect

    from swarm.brain import runner

    src = inspect.getsource(runner.run_task) if hasattr(runner, "run_task") else ""
    if not src:
        import pytest
        pytest.skip("run_task 不可取源（重构过）——本锁的声明侧断言已由上面那条覆盖")
    # 只断"初始 state 字面量里没有该键"这一接线事实（非实现细节）
    assert '"user_profile":' not in src, (
        "runner 初始 state 又放回了 user_profile 原始 dict——它零读点且会被 LangGraph "
        "静默丢弃（BrainState 已无该声明），白占 checkpoint 体积"
    )
    assert '"user_profile_prompt_brain":' in src, "派生 prompt 键被误删"


# ══════════════ A5-L3 revision 回环必须重置 verification_coverage ══════════════

def _revision_state(cov: dict | None) -> dict:
    """最小 revision 入口 state（带上一轮遗留的覆盖账）。"""
    st = {
        "plan": None,               # 无 plan → revision 早退，但重置键仍须落
        "human_feedback": "改一下",
        "subtask_results": {},
    }
    if cov is not None:
        st["verification_coverage"] = cov
    return st


def _revision_out(cov: dict | None) -> dict:
    import asyncio

    from swarm.brain.nodes import revision

    out = revision(_revision_state(cov))
    return asyncio.run(out) if hasattr(out, "__await__") else out


def test_revision_resets_stale_coverage_cells():
    """★核心锁★ 修订轮必须把上一轮的覆盖格标成"本轮未跑"。

    危害路径（`graph.py` 实读）：DELIVER 有三条**绕过 verify_l2** 的可达路径
    （after_clarify 虚假前提阻断 / after_merge escalate / after_handle_failure
    escalate）⇒ 修订轮走其中任一，格原样留下，`runner.py:1400` 终态 payload
    把上一轮的 "passed" 当**本轮**覆盖账上报。
    """
    out = _revision_out({"l2": "passed", "l3": "skipped:complexity_skip"})
    cov = out.get("verification_coverage")
    assert isinstance(cov, dict) and cov, (
        f"修订轮必须回写覆盖账重置（此前整个重置块漏了这个键）。实得 {cov!r}"
    )
    assert set(cov) == {"l2", "l3"}, (
        f"只重置**已存在**的格：缺席＝任何一轮都没跑过，与「跑过但不是本轮」是两回事，"
        f"不该发明新格。实得 {cov}"
    )
    assert all(v == "not_run:revision" for v in cov.values()), cov


def test_reset_value_is_truthy_so_gates_does_not_fall_back_to_degraded():
    """★最值钱的一条：钉住"清成空串会冤拦"这个反直觉约束★

    `gates.py:282` 用 `if _l2_cell:` 分流——格为空/假值时**回退去扫
    degraded_reasons**，而 `:276-279` 明写那条路有永久粘滞（旧轮 unsupported 条目
    无人能清）⇒ 修订后本该放行的交付会被**冤拦**。
    故重置值必须 ① truthy ② 不以 `unsupported_stack:` 开头。
    """
    out = _revision_out({"l2": "unsupported_stack:php"})
    cell = out["verification_coverage"]["l2"]
    assert cell, (
        f"重置值不得为空/假值——那会让 gates 回退扫 degraded_reasons（永久粘滞→冤拦）。实得 {cell!r}"
    )
    assert not cell.startswith("unsupported_stack:"), (
        f"重置值不得以 unsupported_stack: 开头，否则修订轮永远拒 auto_accept。实得 {cell!r}"
    )
    # 行为级验证：拿重置后的账真跑一次 gates，必须不因覆盖账被拒
    from swarm.brain.gates import can_auto_accept_delivery

    allow, reason = can_auto_accept_delivery({
        "verification_coverage": out["verification_coverage"],
        # 旧轮遗留的 degraded 条目——正是"回退扫描"会捡起来冤拦的那个
        "degraded_reasons": ["verification_unsupported_stack:php:l2"],
        "l2_passed": True, "l3_passed": True, "runtime_smoke_passed": True,
        "human_decision": None, "failed_subtask_ids": [], "failure_escalated": False,
        "merged_diff": "diff --git a/x b/x\n+1\n", "plan_validation_issues": [],
    })
    assert "unsupported_stack" not in (reason or ""), (
        f"重置后仍因**上一轮**的 unsupported 条目被拒＝冤拦（本条就是为它写的）。拒因={reason!r}"
    )


def test_absent_coverage_stays_absent():
    """从未跑过 verify 的任务（无覆盖账）→ 修订轮不发明格。

    ★区分力★ 若实现改成无条件写三格，这条红：那会把"从未尝试"伪装成"尝试过但本轮未跑"。
    """
    out = _revision_out(None)
    assert "verification_coverage" not in out, (
        f"无存量覆盖账时不该回写该键（不发明格）。实得 {out.get('verification_coverage')!r}"
    )


def test_empty_dict_reset_would_be_a_no_op_reducer_is_shallow_merge():
    """★钉住治法为何不能写 `{}`★ reducer 是浅合并，`{}` 对存量格毫无作用。

    这条不测生产代码，测的是**治法所依赖的那条前提**（reducer 语义）。若哪天有人把
    reducer 改成"整表替换"，写 `{}` 就真能清空了，那时本批的 `not_run:revision`
    形状可以简化——这条锁会在那时提醒重新评估（而不是让治法悄悄变成绕远路）。
    """
    from swarm.brain.state import _merge_verification_coverage

    assert _merge_verification_coverage({"l2": "passed"}, {}) == {"l2": "passed"}, (
        "reducer 已不再是浅合并——写 {} 现在能清空了，A5-L3 的治法形状可简化，请重新评估"
    )
    assert _merge_verification_coverage({"l2": "passed"}, None) == {"l2": "passed"}


def test_empty_or_absent_cell_really_does_get_falsely_rejected():
    """★自复核补锁★ 把"清成空串/删格会冤拦"这半边因果链也变成可执行的。

    ★为什么必须补★ 上面那条 `test_reset_value_is_truthy_...` 只验了**正确值不被拒**；
    MUT-K（换成 `""`）虽然打红了它，但红在**第一条断言**（值必须 truthy）——执行流
    从未到达"真跑 gates 看拒不拒"那句 ⇒ "空串会冤拦"这个**治法选值的唯一理由**
    一直只是散文（自复核实验 `probes/selfreview_a5l3_gates_causality.py` 当场发现）。
    这条锁把它钉住：若哪天 gates 改了分流逻辑使空串不再触发回退扫描，
    `not_run:revision` 这个刻意选的形状就失去依据、该重新评估——本锁会在那时红。
    """
    from swarm.brain.gates import can_auto_accept_delivery

    def _st(cell):
        s = {
            # 上一轮遗留、append-only reducer 无人能清的那条——回退扫描会捡起它
            "degraded_reasons": ["verification_unsupported_stack:php:l2"],
            "l2_passed": True, "l3_passed": True, "runtime_smoke_passed": True,
            "human_decision": None, "failed_subtask_ids": [],
            "failure_escalated": False, "plan_validation_issues": [],
            "merged_diff": "diff --git a/x b/x\n+1\n",
        }
        if cell is not None:
            s["verification_coverage"] = {"l2": cell}
        return s

    _ok_allow, _ok_reason = can_auto_accept_delivery(_st("not_run:revision"))
    assert _ok_allow and "unsupported_stack" not in (_ok_reason or ""), (
        f"治法值应放行。实得 allow={_ok_allow} reason={_ok_reason!r}"
    )
    _checked = 0
    for _cell, _label in (("", "空串"), (None, "整格删掉")):
        _allow, _reason = can_auto_accept_delivery(_st(_cell))
        assert not _allow and "unsupported_stack" in (_reason or ""), (
            f"★因果链断了★ {_label}（cell={_cell!r}）本应因回退扫 degraded 而被冤拦，"
            f"实测却 allow={_allow} reason={_reason!r} ⇒ gates 分流逻辑可能已改，"
            f"`not_run:revision` 这个刻意选的形状失去依据，需重新评估 A5-L3 治法"
        )
        _checked += 1
    assert _checked == 2, f"两个反例都必须真跑过（防循环静默空转），实跑 {_checked}"


def test_reset_also_applies_on_llm_success_main_path(monkeypatch):
    """★夹具形状锁★ 主路径（LLM 成功 + 真 plan）同样重置。

    ★为什么单独写这条★ 上面几条的夹具无 LLM 凭据 ⇒ 走的是**异常臂**。本仓血泪：
    夹具形状决定被测命题（"非 git 目录测 git archive 路径"那一族）。这条把 LLM 打成
    成功返回、plan 给真对象，确保重置不是只在异常路径上生效。
    （`revision` 全函数体只有一个 return，所以两路本应同判——但"本应"不是证据。）
    """
    import asyncio

    from swarm.brain import nodes as nodes_pkg
    from swarm.types import (
        FileScope,
        SubTask,
        SubTaskDifficulty,
        TaskIntent,
        TaskPlan,
    )

    class _Resp:
        content = ('{"revision_subtasks":[{"id":"rev-1","description":"修订 X",'
                   '"difficulty":"medium","modality":"text",'
                   '"scope":{"writable":["a.py"],"readable":[]}}]}')

    class _LLM:
        async def ainvoke(self, _msgs):
            return _Resp()

    monkeypatch.setattr(nodes_pkg, "_get_brain_llm", lambda: _LLM())

    # 用**真** TaskPlan 而非自造壳：主路径会读 parallel_groups 等字段，自造壳会在中途
    # AttributeError 掉进异常臂 ⇒ 又变成"测异常路径"（首跑实测踩到，夹具前提断言逮住）
    plan = TaskPlan(subtasks=[
        SubTask(id="st-1", description="原任务", intent=TaskIntent.MODIFY,
                difficulty=SubTaskDifficulty.MEDIUM,
                scope=FileScope(writable=["a.py"])),
    ])

    out = asyncio.run(nodes_pkg.revision({
        "plan": plan, "revision_feedback": "改 X", "merged_diff": "diff",
        "task_description": "T", "subtask_results": {},
        "verification_coverage": {"l2": "passed", "runtime_smoke": "passed"},
    }))
    # 前提断言：真走了 LLM 成功路径（产出 rev-1 而非兜底 id）
    assert out.get("dispatch_remaining") == ["rev-1"], (
        f"前提不成立——没走 LLM 成功路径，本锁测的仍是异常臂。实得 {out.get('dispatch_remaining')}"
    )
    assert out["verification_coverage"] == {
        "l2": "not_run:revision", "runtime_smoke": "not_run:revision",
    }, out["verification_coverage"]
