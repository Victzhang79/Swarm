"""W2.1 结构守护测试 — BrainState degraded_reasons reducer + always-emit 契约。

两类断言：
1. reducer 行为：_merge_degraded_reasons 追加去重保序、None 容错。
2. always-emit 源码契约：环路源头节点（merge / dispatch / validate_plan / handle_failure）
   在干净/成功路径必须显式 emit 自己的路由控制键，防止重入读到上一轮残留导致错误路由
   （MERGE→DISPATCH 死循环 / gates 误拒成功运行）。用 inspect.getsource 静态锁定，
   这样未来若有人删掉「永远回填」就会被这里钉死。
"""

from __future__ import annotations

import inspect

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


# ─────────────────── always-emit 源码契约（防回归）───────────────────
def test_merge_always_emits_rebase_subtask_ids():
    """merge 干净路径必须显式回写 rebase_subtask_ids（Wave1 f38e4a2 已修，锁定防回归）。"""
    from swarm.brain.nodes import merge

    src = inspect.getsource(merge)
    assert 'out["rebase_subtask_ids"] = []' in src or \
        '"rebase_subtask_ids": []' in src, \
        "merge 必须在 clean 路径显式 emit rebase_subtask_ids=[]，否则上一轮残留致死循环"


def test_dispatch_always_emits_failed_subtask_ids():
    """dispatch 成功路径必须永远回填 failed_subtask_ids（空也填）。"""
    from swarm.brain.nodes.dispatch import dispatch

    src = inspect.getsource(dispatch)
    assert 'failed_subtask_ids' in src, \
        "dispatch 必须 emit failed_subtask_ids"
    assert 'result["failed_subtask_ids"] = failed_ids' in src, \
        "dispatch 必须永远回填 failed_subtask_ids（含空），否则 gates 误拒成功运行"


def test_validate_plan_emits_plan_valid():
    """validate_plan 必须显式 emit plan_valid 路由控制键。"""
    from swarm.brain.nodes import validate_plan

    src = inspect.getsource(validate_plan)
    assert "plan_valid" in src, "validate_plan 必须 emit plan_valid"


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

    fns = [v for k, v in list(globals().items()) if k.startswith("test_") and callable(v)]
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
