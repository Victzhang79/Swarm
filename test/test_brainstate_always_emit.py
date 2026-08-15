"""W2.1 结构守护测试 — BrainState degraded_reasons reducer + always-emit 契约。

两类断言：
1. reducer 行为：_merge_degraded_reasons 追加去重保序、None 容错。
2. always-emit 行为契约：环路源头节点（merge / dispatch / validate_plan / handle_failure）
   在干净/成功路径必须显式 emit 自己的路由控制键，防止重入读到上一轮残留导致错误路由
   （MERGE→DISPATCH 死循环 / gates 误拒成功运行）。
   批25 GS-5w：原 inspect.getsource 源码字面断言全部换【行为锁】——真调节点断返回键在场，
   删掉/条件化 emit 即红；焊字面量的旧写法对「换个写法同语义」无区分力且脆于重构。
"""

from __future__ import annotations

from swarm.brain.state import _merge_degraded_reasons


# ─────────────────────────── reducer 行为 ───────────────────────────
def test_reducer_append_dedup_order():
    assert _merge_degraded_reasons(["a"], ["a", "b"]) == ["a", "b"]


def test_reducer_preserves_order_and_dedups_within_new():
    assert _merge_degraded_reasons(["a"], ["b", "a", "b", "c"]) == ["a", "b", "c"]


def test_reducer_none_tolerant():
    assert _merge_degraded_reasons(None, ["x"]) == ["x"]
    assert _merge_degraded_reasons(["x"], None) == ["x"]
    assert _merge_degraded_reasons(None, None) == []


def test_reducer_sequential_updates_accumulate():
    """模拟两次顺序更新（graph 内多节点先后写入）→ 累积去重。"""
    s1 = _merge_degraded_reasons([], ["analyze 降级"])
    s2 = _merge_degraded_reasons(s1, ["plan 兜底"])
    s3 = _merge_degraded_reasons(s2, ["plan 兜底"])  # 重复，应被吞
    assert s3 == ["analyze 降级", "plan 兜底"]


def test_reducer_returns_new_list_not_mutating_old():
    old = ["a"]
    out = _merge_degraded_reasons(old, ["b"])
    assert out == ["a", "b"]
    assert old == ["a"], "reducer 不得原地修改入参 old"


# ─────────────────── always-emit 行为契约（防回归）───────────────────
def test_merge_always_emits_rebase_subtask_ids(monkeypatch):
    """merge 干净路径必须显式回写 rebase_subtask_ids=[]（Wave1 f38e4a2 已修，锁定防回归）。

    批25 GS-5w 换锁（原 getsource 字面断言）：真调 merge clean 场景（单写者零冲突，
    夹具形状同 test_a_batch_merge_owner_ledger 的 merge 驱动），断返回【恒含该键且为 []】。
    红条件：删掉/条件化 nodes merge 干净段的显式回写 → 键缺席 → 红
    （LangGraph 对缺席键保留旧值 ⇒ 上一轮非空 rebase 列表粘滞 ⇒ after_merge 死循环复发）。
    与组3 换锁的互补：其「not out.get(...)」是夹具自证（键缺席也过），本锁断【键在场】。"""
    from swarm.brain import nodes
    from swarm.types import FileScope, SubTask, TaskPlan, WorkerOutput

    _solo = ("diff --git a/.gitignore b/.gitignore\n"
             "new file mode 100644\n--- /dev/null\n"
             "+++ b/.gitignore\n@@ -0,0 +1,1 @@\n+target/\n")
    plan = TaskPlan(subtasks=[
        SubTask(id="st-1", description="d",
                scope=FileScope(writable=[".gitignore"]), depends_on=[]),
    ])
    state = {
        "plan": plan,
        "subtask_results": {
            "st-1": WorkerOutput(subtask_id="st-1", diff=_solo, summary="",
                                 l1_passed=True, l1_details={}, confidence="high"),
        },
    }
    monkeypatch.setattr(nodes, "_make_base_reader", lambda s: (lambda f: None))
    monkeypatch.setattr("swarm.brain.merge_engine.verify_merged_patch_applies",
                        lambda *a, **k: (True, ""))
    out = nodes.merge(state)
    assert "rebase_subtask_ids" in out, \
        "clean 路径也必须显式 emit rebase_subtask_ids（键缺席=旧值粘滞⇒死循环，Wave1 回归）"
    assert out["rebase_subtask_ids"] == [], "clean 合并的 rebase 列表必须为空"


def test_dispatch_always_emits_failed_subtask_ids():
    """dispatch 成功路径必须永远回填 failed_subtask_ids（空也填，H3）。

    批25 GS-5w 换锁（原 getsource 字面断言）：真跑 dispatch【本轮有真失败】的分支——
    断返回恒含 failed_subtask_ids 且①本轮新失败入账②上轮残留里本轮重试通过的已移除
    （对抗复核 #3 治本：只追加不移除 ⇒ 残留 ⇒ after_monitor 空转误 escalate）。
    「空也回填」的两条出口由同命题锁
    test_audit_group3_reliability.test_h3_dispatch_always_returns_failed_ids 覆盖，不重复造。"""
    import asyncio
    from unittest.mock import patch

    from swarm.brain.nodes.dispatch import dispatch
    from swarm.types import (
        Confidence,
        FileScope,
        SubTask,
        SubTaskDifficulty,
        TaskPlan,
        WorkerOutput,
    )

    def _sub(sid):
        return SubTask(id=sid, description="x", difficulty=SubTaskDifficulty.MEDIUM,
                       scope=FileScope(writable=[f"{sid}.x"], readable=[]), depends_on=[])

    plan = TaskPlan(subtasks=[_sub("st-ok"), _sub("st-bad")],
                    parallel_groups=[["st-ok", "st-bad"]])

    async def _fake_worker(subtask, knowledge_context, **kw):
        if subtask.id == "st-bad":
            return WorkerOutput(subtask_id=subtask.id, diff="", summary="",
                                confidence=Confidence.LOW, l1_passed=False)
        return WorkerOutput(subtask_id=subtask.id, diff="+x\n", summary="",
                            confidence=Confidence.HIGH, l1_passed=True)

    with patch("swarm.brain.nodes._dispatch_to_worker", side_effect=_fake_worker):
        out = asyncio.run(dispatch({
            "task_id": "", "project_id": "", "plan": plan,
            "subtask_results": {}, "dispatch_remaining": ["st-ok", "st-bad"],
            # st-ok 上轮失败残留，本轮重试 L1 通过 → 必须从 failed_ids 移除
            "failed_subtask_ids": ["st-ok"], "knowledge_context": {},
        }))
    assert "failed_subtask_ids" in out, "dispatch 返回必须恒含 failed_subtask_ids 键（H3）"
    assert "st-bad" in out["failed_subtask_ids"], "本轮 L1 未过的新失败必须入账"
    assert "st-ok" not in out["failed_subtask_ids"], \
        "重试后 L1 通过必须从 failed_ids 移除（#3 治本，残留=空转误 escalate）"


def test_validate_plan_emits_plan_valid():
    """validate_plan 必须显式 emit plan_valid 路由控制键。

    批25 GS-5w 换锁（原 getsource 成员断言——任意位置出现 "plan_valid" 即绿，比行为锁更弱）：
    真跑 plan=None 的确定性早退（nodes/:3603，零依赖、早于任何 LLM/校验器调用），
    断返回恒含 plan_valid 且为 False；同出口顺带钉 R67M2-T3 的 plan_validation_warnings
    恒发契约（同属本文件 always-emit 主题：缺席键=旧值粘滞）。
    红条件：该早退 return 摘掉 plan_valid → 键缺席 → 红。
    边界如实登记：validate_plan 有多个 emit 出口，本锁只钉空 plan 这条（其余出口
    原 getsource 也钉不住，覆盖不降级）。"""
    import asyncio

    from swarm.brain.nodes import validate_plan

    out = asyncio.run(validate_plan({"plan": None, "task_description": "x"}))
    assert "plan_valid" in out and out["plan_valid"] is False, \
        "空 plan 早退必须显式 emit plan_valid=False（缺席=路由读旧值）"
    assert "plan_validation_warnings" in out and out["plan_validation_warnings"] == [], \
        "R67M2-T3：warnings 键恒发（空列表=本轮无软警告的如实表达，缺席=上轮粘滞）"


def test_handle_failure_emits_failure_strategy():
    """行为契约：handle_failure 返回 dict 必含 failure_strategy 路由键。

    round24 A4 后 handle_failure 拆为薄包装 + _handle_failure_impl，getsource 焊死会误挂
    （本会话踩过的脆测试坑）→ 改行为断言：真调用一条确定性路径（L2 超限 escalate，早于
    LLM 调用返回，无需 mock），断言返回含路由键。
    """
    import asyncio

    from swarm.brain.nodes import handle_failure
    from swarm.config.settings import get_config
    from swarm.types import FileScope, SubTask, TaskPlan

    plan = TaskPlan(subtasks=[SubTask(id="st-1", description="x", scope=FileScope(create_files=["a/A.java"]))])
    state = {
        "verification_failure": "l2",
        "replan_count": get_config().model.max_retries + 5,  # 超限 → 确定性 escalate 分支
        "failed_subtask_ids": [],
        "subtask_results": {},
        "plan": plan,
    }
    out = asyncio.run(handle_failure(state))
    assert "failure_strategy" in out, "handle_failure 返回必含 failure_strategy 路由键"


# ─────────────────── 注解装配验证 ───────────────────
def test_degraded_reasons_has_reducer_annotation():
    """确认 BrainState.degraded_reasons 挂上了 reducer（而非裸 list）。"""
    import typing

    from swarm.brain.state import BrainState

    hints = typing.get_type_hints(BrainState, include_extras=True)
    ann = hints["degraded_reasons"]
    meta = getattr(ann, "__metadata__", ())
    assert _merge_degraded_reasons in meta, \
        "degraded_reasons 必须 Annotated[list[str], _merge_degraded_reasons]"


if __name__ == "__main__":
    import sys

    # 批25 换锁后部分用例要 pytest 夹具（monkeypatch），直跑模式跳过它们
    fns = [v for k, v in list(globals().items())
           if k.startswith("test_") and callable(v) and not v.__code__.co_argcount]
    failed = 0
    for fn in fns:
        try:
            fn()
            print(f"  ok {fn.__name__}")
        except AssertionError as e:
            failed += 1
            print(f"  FAIL {fn.__name__}: {e}")
    print(f"\n=== always-emit guard: {len(fns) - failed}/{len(fns)} passed ===")
    sys.exit(1 if failed else 0)
