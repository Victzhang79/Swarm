#!/usr/bin/env python3
"""Phase 3 — parallel dispatch, merge engine, L2 sandbox 测试"""

from __future__ import annotations

import asyncio
import importlib.util
import time
from pathlib import Path
from unittest.mock import AsyncMock, patch

_bs = Path(__file__).resolve().parent / "swarm_bootstrap.py"
_spec = importlib.util.spec_from_file_location("swarm_bootstrap", _bs)
_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_mod)

from swarm.brain.graph import after_handle_failure, after_merge
from swarm.brain.merge_engine import merge_diffs
from swarm.brain.nodes import dispatch, handle_failure, merge, verify_l2, verify_l3
from swarm.brain.state import BrainState
from swarm.types import (
    Complexity,
    Confidence,
    FileScope,
    SubTask,
    SubTaskDifficulty,
    SubTaskModality,
    TaskPlan,
    WorkerOutput,
)


def _subtask(sid: str, *, depends_on: list[str] | None = None) -> SubTask:
    return SubTask(
        id=sid,
        description=f"task {sid}",
        difficulty=SubTaskDifficulty.MEDIUM,
        modality=SubTaskModality.TEXT,
        scope=FileScope(writable=[f"{sid}.py"], readable=[f"{sid}.py"]),
        depends_on=depends_on or [],
    )


def _plan_with_groups(groups: list[list[str]], deps: dict[str, list[str]] | None = None) -> TaskPlan:
    deps = deps or {}
    subtasks = [_subtask(sid, depends_on=deps.get(sid, [])) for sid in {t for g in groups for t in g}]
    return TaskPlan(subtasks=subtasks, parallel_groups=groups)


DIFF_A = """--- a/a.py
+++ b/a.py
@@ -1,3 +1,4 @@
 line1
+added by a
 line2
"""

DIFF_B = """--- a/b.py
+++ b/b.py
@@ -1,3 +1,4 @@
 line1
+added by b
 line2
"""

DIFF_X_LOW = """--- a/x.py
+++ b/x.py
@@ -5,3 +5,4 @@
 context5
+low change
 context6
"""

DIFF_X_HIGH = """--- a/x.py
+++ b/x.py
@@ -20,3 +20,4 @@
 context20
+high change
 context21
"""

DIFF_X_OVERLAP_A = """--- a/x.py
+++ b/x.py
@@ -10,3 +10,4 @@
 context10
+from st-a
 context11
"""

DIFF_X_OVERLAP_B = """--- a/x.py
+++ b/x.py
@@ -10,3 +10,4 @@
 context10
+from st-b
 context11
"""


def test_merge_engine_non_conflicting_diffs():
    result = merge_diffs([("st-1", DIFF_A), ("st-2", DIFF_B)])
    assert result.success is True
    assert result.conflicts == []
    assert "a/a.py" in result.merged_diff or "a.py" in result.merged_diff
    assert "b/b.py" in result.merged_diff or "b.py" in result.merged_diff
    assert "added by a" in result.merged_diff
    assert "added by b" in result.merged_diff
    print("  ✅ merge_engine — non-conflicting diffs merge cleanly")


def test_merge_engine_same_file_non_overlapping():
    result = merge_diffs([("st-1", DIFF_X_LOW), ("st-2", DIFF_X_HIGH)])
    assert result.success is True
    assert result.conflicts == []
    assert "low change" in result.merged_diff
    assert "high change" in result.merged_diff
    print("  ✅ merge_engine — same file non-overlapping hunks")


def test_merge_engine_overlapping_conflict():
    result = merge_diffs([("st-a", DIFF_X_OVERLAP_A), ("st-b", DIFF_X_OVERLAP_B)])
    assert result.success is False
    assert len(result.conflicts) == 1
    assert result.conflicts[0].file_path == "x.py"
    assert set(result.conflicts[0].subtask_ids) == {"st-a", "st-b"}
    assert "MERGE CONFLICT" in result.merged_diff
    assert "<<<<<<< st-a" in result.merged_diff
    print("  ✅ merge_engine — overlapping same-file diffs detected")


BASE_X_PY = "\n".join(
    [f"line{i}" for i in range(1, 10)]
    + ["context10", "context11", "context12"]
    + [f"line{i}" for i in range(13, 22)]
) + "\n"


def test_merge_engine_overlap_auto_resolve_with_base():
    """A-P1-26 (a)：两个子任务在【同一锚点】插入【不同】内容，不再被静默拼接成"clean"。

    旧行为（已修正）：merge_insert_only_changes 把 `from st-a` 与 `from st-b` 顺序拼接，
    当成无冲突合并 —— 这是无声吞冲突。现在同锚点不同内容 → insert-only 返回 None →
    升级到 3-way；3-way 无法判定取舍 → rebase 策略：保留首个子任务、其余标记待 rebase 重生成。
    关键是不再"两条都塞进去当没事"。
    """
    def reader(path: str) -> str | None:
        if path == "x.py":
            return BASE_X_PY
        return None

    result = merge_diffs(
        [("st-a", DIFF_X_OVERLAP_A), ("st-b", DIFF_X_OVERLAP_B)],
        base_reader=reader,
        auto_resolve=True,
    )
    # 不再静默把两条不同插入都当 clean 合并
    assert not (
        "from st-a" in result.merged_diff and "from st-b" in result.merged_diff
    ), "同锚点不同插入不应被静默拼接：" + result.merged_diff
    # 升级走 rebase：保留 st-a，st-b 待重生成（安全、可恢复，非无声丢失）
    assert "st-b" in result.rebase_subtask_ids
    assert "from st-a" in result.merged_diff
    print("  ✅ merge_engine — 同锚点不同插入升级冲突(非静默拼接)")


def test_merge_engine_same_anchor_identical_insert_dedupes():
    """A-P1-26 (a)：两个子任务在同一锚点插入【相同】内容 → 去重为一份，不重复两遍。"""
    def reader(path: str) -> str | None:
        if path == "x.py":
            return BASE_X_PY
        return None

    # 两边插入完全相同的行
    result = merge_diffs(
        [("st-a", DIFF_X_OVERLAP_A), ("st-b", DIFF_X_OVERLAP_A.replace("st-a", "st-a"))],
        base_reader=reader,
        auto_resolve=True,
    )
    assert result.success is True, result.merged_diff
    assert result.conflicts == []
    # 仅一份，不是两遍
    assert result.merged_diff.count("from st-a") == 1, result.merged_diff
    print("  ✅ merge_engine — 同锚点相同插入去重")


DIFF_X_REPLACE_A = """--- a/x.py
+++ b/x.py
@@ -11,1 +11,1 @@
-context11
+from-replace-a
"""

DIFF_X_REPLACE_B = """--- a/x.py
+++ b/x.py
@@ -11,1 +11,1 @@
-context11
+from-replace-b
"""


def test_merge_engine_overlap_rebase_on_same_line():
    """当两个子任务替换同一行且 3-way 无法解决时，rebase 策略生效:
    保留第一个子任务的 diff，标记第二个子任务待 rebase 重生成。
    """
    def reader(path: str) -> str | None:
        if path == "x.py":
            return BASE_X_PY
        return None

    result = merge_diffs(
        [("st-a", DIFF_X_REPLACE_A), ("st-b", DIFF_X_REPLACE_B)],
        base_reader=reader,
        auto_resolve=True,
    )
    # rebase 策略: success=True（无硬冲突），但有 rebase 子任务
    assert result.success is True
    assert len(result.conflicts) == 0
    assert "st-b" in result.rebase_subtask_ids
    # st-a 的 diff 被保留到合并结果中
    assert "from-replace-a" in result.merged_diff
    print("  ✅ merge_engine — true conflict triggers rebase strategy")


def test_merge_engine_overlap_hard_conflict_without_base():
    """当 base_reader 不可用时，真正冲突走原有的硬冲突路径。"""
    result = merge_diffs(
        [("st-a", DIFF_X_REPLACE_A), ("st-b", DIFF_X_REPLACE_B)],
        base_reader=None,
        auto_resolve=True,
    )
    assert result.success is False
    assert len(result.conflicts) >= 1
    assert result.rebase_subtask_ids == []
    print("  ✅ merge_engine — true conflict → hard conflict without base_reader")


def test_merge_node_uses_merge_engine():
    state: BrainState = {
        "subtask_results": {
            "st-1": WorkerOutput(
                subtask_id="st-1",
                diff=DIFF_A,
                summary="ok",
                l1_passed=True,
            ),
            "st-2": WorkerOutput(
                subtask_id="st-2",
                diff=DIFF_B,
                summary="ok",
                l1_passed=True,
            ),
        },
    }
    out = merge(state)
    assert "added by a" in out["merged_diff"]
    assert "added by b" in out["merged_diff"]
    assert "merge_conflicts" not in out
    print("  ✅ merge node — uses merge_engine")


def test_merge_node_reports_conflicts():
    state: BrainState = {
        "subtask_results": {
            "st-a": WorkerOutput(subtask_id="st-a", diff=DIFF_X_OVERLAP_A, summary="ok", l1_passed=True),
            "st-b": WorkerOutput(subtask_id="st-b", diff=DIFF_X_OVERLAP_B, summary="ok", l1_passed=True),
        },
    }
    out = merge(state)
    assert "merge_conflicts" in out
    assert len(out["merge_conflicts"]) == 1
    assert out["merge_conflicts"][0]["file_path"] == "x.py"
    assert set(out["failed_subtask_ids"]) == {"st-a", "st-b"}
    print("  ✅ merge node — reports merge_conflicts + failed_subtask_ids")


def test_after_merge_routes_to_handle_failure_on_conflicts():
    state: BrainState = {
        "merge_conflicts": [{"file_path": "x.py", "subtask_ids": ["st-a", "st-b"], "message": "overlap"}],
    }
    assert after_merge(state) == "handle_failure"
    assert after_merge({}) == "verify_l2"
    print("  ✅ after_merge — conflicts → handle_failure")


def test_after_merge_routes_to_dispatch_on_rebase():
    """rebase 子任务存在时，merge 后路由到 dispatch（重跑 rebase 子任务）。"""
    state: BrainState = {
        "rebase_subtask_ids": ["st-b"],
    }
    assert after_merge(state) == "dispatch"
    # 硬冲突 + rebase 同时存在时，硬冲突优先
    state_with_conflict: BrainState = {
        "merge_conflicts": [{"file_path": "x.py", "subtask_ids": ["st-a", "st-b"], "message": "overlap"}],
        "rebase_subtask_ids": ["st-c"],
    }
    assert after_merge(state_with_conflict) == "handle_failure"
    print("  ✅ after_merge — rebase → dispatch; 硬冲突优先于 rebase")


def test_merge_node_rebase_path():
    """merge 节点在有 base_reader 且 3-way 失败时，走 rebase 路径:
    - 合并结果保留 base 方 diff
    - rebase 子任务被加入 dispatch_remaining
    - 从 subtask_results 移除 rebase 子任务
    - 不报硬冲突
    """
    import tempfile
    from pathlib import Path

    # 创建临时项目目录并提供 base 文件
    with tempfile.TemporaryDirectory() as tmpdir:
        x_py = Path(tmpdir) / "x.py"
        x_py.write_text(BASE_X_PY, encoding="utf-8")

        state: BrainState = {
            "project_id": "test-rebase-proj",
            "subtask_results": {
                "st-a": WorkerOutput(
                    subtask_id="st-a",
                    diff=DIFF_X_REPLACE_A,
                    summary="ok",
                    l1_passed=True,
                ),
                "st-b": WorkerOutput(
                    subtask_id="st-b",
                    diff=DIFF_X_REPLACE_B,
                    summary="ok",
                    l1_passed=True,
                ),
            },
        }

        # 需要让 _make_base_reader 能读到文件
        # 通过 patch _get_project_path 让它指向临时目录
        with patch("swarm.brain.nodes._get_project_path", return_value=tmpdir):
            out = merge(state)

        # rebase 路径: 无硬冲突
        assert "merge_conflicts" not in out
        # st-a 的 diff 被保留
        assert "from-replace-a" in out["merged_diff"]
        # st-b 被标记为 rebase
        assert "rebase_subtask_ids" in out
        assert "st-b" in out["rebase_subtask_ids"]
        # st-b 被加入 dispatch_remaining，从 subtask_results 移除
        assert "st-b" in out["dispatch_remaining"]
        assert "st-b" not in out["subtask_results"]
        # st-a 仍在 subtask_results 中
        assert "st-a" in out["subtask_results"]
        print("  ✅ merge node — rebase path preserves base diff, reboots conflicting subtask")


def test_handle_failure_retry_strategy():
    state: BrainState = {
        "complexity": Complexity.MEDIUM,
        "failed_subtask_ids": ["st-1"],
        "dispatch_remaining": [],
        "subtask_results": {
            "st-1": WorkerOutput(subtask_id="st-1", diff="", summary="fail", l1_passed=False),
        },
        "plan": TaskPlan(subtasks=[_subtask("st-1")]),
    }
    mock_response = type("R", (), {"content": '{"strategy": "retry", "reasoning": "transient"}'})()
    with patch("swarm.brain.nodes._get_brain_llm") as llm_get:
        llm_get.return_value.ainvoke = AsyncMock(return_value=mock_response)
        out = asyncio.run(handle_failure(state))
    assert out["failure_strategy"] == "retry"
    assert out["failed_subtask_ids"] == []
    assert "st-1" in out["dispatch_remaining"]
    assert "st-1" not in out["subtask_results"]
    print("  ✅ handle_failure — retry strategy re-queues failed ids")


def test_handle_failure_replan_strategy():
    state: BrainState = {
        "complexity": Complexity.COMPLEX,
        "failed_subtask_ids": ["st-1", "st-2"],
        "subtask_results": {
            "st-1": WorkerOutput(subtask_id="st-1", diff="", summary="fail", l1_passed=False),
            "st-2": WorkerOutput(subtask_id="st-2", diff="", summary="fail", l1_passed=False),
        },
        "plan": _plan_with_groups([["st-1", "st-2"]]),
    }
    mock_response = type("R", (), {"content": '{"strategy": "replan", "reasoning": "plan broken"}'})()
    with patch("swarm.brain.nodes._get_brain_llm") as llm_get:
        llm_get.return_value.ainvoke = AsyncMock(return_value=mock_response)
        out = asyncio.run(handle_failure(state))
    assert out["failure_strategy"] == "replan"
    assert out["plan_valid"] is False
    assert out["failed_subtask_ids"] == []
    assert "st-1" not in out["subtask_results"]
    assert after_handle_failure(out) == "plan"
    print("  ✅ handle_failure — replan strategy clears results")


def test_handle_failure_escalate_routes_to_deliver():
    state: BrainState = {
        "complexity": Complexity.COMPLEX,
        "failed_subtask_ids": ["st-1"],
        "subtask_results": {
            "st-1": WorkerOutput(subtask_id="st-1", diff="", summary="fail", l1_passed=False),
        },
        "plan": TaskPlan(subtasks=[_subtask("st-1")]),
    }
    mock_response = type("R", (), {"content": '{"strategy": "escalate", "reasoning": "human needed"}'})()
    with patch("swarm.brain.nodes._get_brain_llm") as llm_get:
        llm_get.return_value.ainvoke = AsyncMock(return_value=mock_response)
        out = asyncio.run(handle_failure(state))
    assert out["failure_strategy"] == "escalate"
    assert out["failure_escalated"] is True
    assert out["l2_passed"] is False
    assert after_handle_failure(out) == "deliver"
    print("  ✅ handle_failure — escalate routes to deliver")


def test_verify_l3_skip_simple():
    state: BrainState = {
        "complexity": Complexity.SIMPLE,
        "merged_diff": DIFF_A,
        "task_description": "test",
    }
    out = asyncio.run(verify_l3(state))
    assert out["l3_skipped"] is True
    assert out["l3_passed"] is None
    print("  ✅ verify_l3 — SIMPLE skips quickly")


def test_verify_l3_skip_without_staging_url():
    state: BrainState = {
        "complexity": Complexity.COMPLEX,
        "merged_diff": DIFF_A,
        "task_description": "test",
    }
    with patch.dict("os.environ", {}, clear=False):
        import os
        os.environ.pop("SWARM_STAGING_URL", None)
        out = asyncio.run(verify_l3(state))
    assert out["l3_skipped"] is True
    assert out["l3_passed"] is None
    assert "No staging URL" in out["l3_message"]
    print("  ✅ verify_l3 — skip when no SWARM_STAGING_URL")


def test_verify_l3_pass_with_staging():
    state: BrainState = {
        "complexity": Complexity.ULTRA,
        "merged_diff": DIFF_A,
        "task_description": "test",
    }
    mock_response = type("R", (), {"content": '{"l3_passed": true, "message": "staging ok"}'})()
    with patch.dict("os.environ", {"SWARM_STAGING_URL": "https://staging.example.com"}):
        with patch("swarm.brain.nodes._get_brain_llm") as llm_get:
            llm_get.return_value.ainvoke = AsyncMock(return_value=mock_response)
            out = asyncio.run(verify_l3(state))
    assert out["l3_skipped"] is False
    assert out["l3_passed"] is True
    assert "staging ok" in out["l3_message"]
    print("  ✅ verify_l3 — pass path with staging URL")


def test_verify_l2_sandbox_pass():
    st = _subtask("t1")
    st.acceptance_criteria = ["pytest -q tests/"]
    plan = TaskPlan(subtasks=[st])
    state: BrainState = {
        "complexity": Complexity.MEDIUM,
        "merged_diff": DIFF_A,
        "project_id": "proj-1",
        "plan": plan,
        "task_description": "test task",
        "subtask_results": {},
    }
    with patch("swarm.brain.nodes._try_l2_sandbox_verify", return_value=True):
        with patch("swarm.brain.nodes._verify_l2_via_llm") as llm_mock:
            out = asyncio.run(verify_l2(state))
    assert out["l2_passed"] is True
    llm_mock.assert_not_called()
    print("  ✅ verify_l2 — sandbox path returns l2_passed True")


def test_verify_l2_sandbox_fail():
    # 显式带测试命令的 subtask → test_cmd 非空 → 走沙箱验证路径（保留本测试意图：
    # 沙箱测试失败时 L2 失败）。无显式测试命令时 L2 会跳过测试验证（见
    # test_verify_l2_skips_test_when_no_command），是另一条路径。
    _st = _subtask("t1")
    _st.acceptance_criteria = ["pytest -q tests/test_t1.py"]
    state: BrainState = {
        "complexity": Complexity.COMPLEX,
        "merged_diff": DIFF_A,
        "project_id": "proj-1",
        "plan": TaskPlan(subtasks=[_st]),
        "task_description": "test task",
        "subtask_results": {},
    }
    with patch("swarm.brain.nodes._try_l2_sandbox_verify", return_value=False):
        with patch("swarm.brain.nodes._try_l2_local_verify", return_value=None):
            with patch("swarm.brain.nodes._verify_l2_via_llm") as llm_mock:
                out = asyncio.run(verify_l2(state))
    assert out["l2_passed"] is False
    llm_mock.assert_not_called()
    print("  ✅ verify_l2 — sandbox path returns l2_passed False")


def test_dispatch_batch_independent_tasks_parallelize():
    """依赖驱动：无 depends_on 的独立子任务即使被 LLM 拆进不同 group 也并行派发。

    （旧行为只派第一个 group；新行为按 depends_on DAG，独立任务全并行——
    这是 P4 修复：消除 LLM 过度保守分组导致的无谓串行。）
    """
    plan = _plan_with_groups([["st-1", "st-2"], ["st-3"]])  # st-3 无依赖
    batch = plan.get_dispatch_batch(set(), ["st-1", "st-2", "st-3"], max_concurrent=4)
    ids = {t.id for t in batch}
    assert ids == {"st-1", "st-2", "st-3"}, f"独立任务应全部并行, got {ids}"
    print("  ✅ get_dispatch_batch — 独立子任务并行（不受 LLM 分组限制）")


def test_dispatch_batch_respects_group_order_after_deps():
    plan = _plan_with_groups(
        [["st-1", "st-2"], ["st-3"]],
        deps={"st-3": ["st-1", "st-2"]},
    )
    batch = plan.get_dispatch_batch({"st-1", "st-2"}, ["st-3"], max_concurrent=4)
    assert len(batch) == 1 and batch[0].id == "st-3"
    print("  ✅ get_dispatch_batch — sequential groups after deps")


def test_dispatch_batch_max_concurrent_within_group():
    plan = _plan_with_groups([["a", "b", "c"]])
    batch = plan.get_dispatch_batch(set(), ["a", "b", "c"], max_concurrent=2)
    assert len(batch) == 2
    print("  ✅ get_dispatch_batch — max_concurrent 截断")


def test_dispatch_batch_blocked_when_dep_unmet():
    """依赖未满足的任务不派发；依赖已满足的（含独立的）才派发。"""
    plan = _plan_with_groups(
        [["st-1", "st-2"], ["st-3"]],
        deps={"st-2": ["st-1"]},  # st-2 依赖 st-1；st-1/st-3 独立
    )
    batch = plan.get_dispatch_batch(set(), ["st-1", "st-2", "st-3"], max_concurrent=4)
    ids = {t.id for t in batch}
    # st-1 和 st-3 无依赖可并行；st-2 依赖未满足被阻塞
    assert ids == {"st-1", "st-3"}, f"应派发无依赖的 st-1/st-3, got {ids}"
    print("  ✅ get_dispatch_batch — 依赖未满足的任务被阻塞，独立任务仍并行")


def test_dispatch_batch_fallback_without_parallel_groups():
    scope = FileScope()
    plan = TaskPlan(
        subtasks=[
            SubTask(id="x", description="x", scope=scope),
            SubTask(id="y", description="y", scope=scope, depends_on=["x"]),
        ],
        parallel_groups=[],
    )
    batch = plan.get_dispatch_batch(set(), ["x", "y"], max_concurrent=4)
    assert len(batch) == 1 and batch[0].id == "x"
    print("  ✅ get_dispatch_batch — fallback without parallel_groups")


async def _test_parallel_dispatch_gather_async():
    """同一批次内子任务应并行执行（mock 延迟）。"""
    plan = TaskPlan(
        subtasks=[
            _subtask("p1"),
            _subtask("p2"),
        ],
        parallel_groups=[["p1", "p2"]],
    )
    delays: dict[str, float] = {}

    async def fake_worker(subtask, knowledge_context, project_id="", task_id="", **kwargs):
        t0 = time.monotonic()
        await asyncio.sleep(0.08)
        delays[subtask.id] = time.monotonic() - t0
        return WorkerOutput(
            subtask_id=subtask.id,
            diff=f"--- a\n+++ b\n+line {subtask.id}\n",
            summary="ok",
            confidence=Confidence.HIGH,
            l1_passed=True,
        )

    state: BrainState = {
        "task_id": "t-par",
        "project_id": "proj-1",
        "plan": plan,
        "subtask_results": {},
        "dispatch_remaining": ["p1", "p2"],
        "failed_subtask_ids": [],
        "knowledge_context": {},
    }

    with patch("swarm.brain.nodes._dispatch_to_worker", side_effect=fake_worker), patch(
        "swarm.worker.sandbox.SandboxPool.warmup", return_value=None
    ):
        t0 = time.monotonic()
        result = await dispatch(state)
        elapsed = time.monotonic() - t0

    assert result["dispatch_remaining"] == []
    assert set(result["subtask_results"].keys()) == {"p1", "p2"}
    assert elapsed < 0.25, f"expected parallel ~0.08s, took {elapsed:.3f}s"
    print("  ✅ dispatch — asyncio.gather parallel mock")


async def _test_dispatch_failure_stops_batch_async():
    plan = TaskPlan(
        subtasks=[_subtask("ok1"), _subtask("bad")],
        parallel_groups=[["ok1", "bad"]],
    )

    async def fake_worker(subtask, knowledge_context, project_id="", task_id="", **kwargs):
        if subtask.id == "bad":
            return WorkerOutput(
                subtask_id="bad",
                diff="",
                summary="no diff",
                confidence=Confidence.LOW,
                l1_passed=False,
            )
        return WorkerOutput(
            subtask_id=subtask.id,
            diff="--- a\n+++ b\n+ok\n",
            summary="ok",
            l1_passed=True,
        )

    state: BrainState = {
        "task_id": "t-fail",
        "project_id": "proj-1",
        "plan": plan,
        "subtask_results": {},
        "dispatch_remaining": ["ok1", "bad"],
        "failed_subtask_ids": [],
        "knowledge_context": {},
    }

    with patch("swarm.brain.nodes._dispatch_to_worker", side_effect=fake_worker):
        result = await dispatch(state)

    assert "bad" in result.get("failed_subtask_ids", [])
    print("  ✅ dispatch — failure recorded from parallel batch")


def test_parallel_dispatch_gather():
    asyncio.run(_test_parallel_dispatch_gather_async())


def test_dispatch_failure_stops_batch():
    asyncio.run(_test_dispatch_failure_stops_batch_async())


def main():
    print("\n🐝 Phase 3 — brain pipeline tests\n")
    test_merge_engine_non_conflicting_diffs()
    test_merge_engine_same_file_non_overlapping()
    test_merge_engine_overlapping_conflict()
    test_merge_engine_overlap_auto_resolve_with_base()
    test_merge_engine_same_anchor_identical_insert_dedupes()
    test_merge_engine_overlap_rebase_on_same_line()
    test_merge_engine_overlap_hard_conflict_without_base()
    test_merge_node_uses_merge_engine()
    test_merge_node_reports_conflicts()
    test_after_merge_routes_to_handle_failure_on_conflicts()
    test_after_merge_routes_to_dispatch_on_rebase()
    test_merge_node_rebase_path()
    test_handle_failure_retry_strategy()
    test_handle_failure_replan_strategy()
    test_handle_failure_escalate_routes_to_deliver()
    test_verify_l3_skip_simple()
    test_verify_l3_skip_without_staging_url()
    test_verify_l3_pass_with_staging()
    test_verify_l2_sandbox_pass()
    test_verify_l2_sandbox_fail()
    test_dispatch_batch_independent_tasks_parallelize()
    test_dispatch_batch_respects_group_order_after_deps()
    test_dispatch_batch_max_concurrent_within_group()
    test_dispatch_batch_blocked_when_dep_unmet()
    test_dispatch_batch_fallback_without_parallel_groups()
    test_parallel_dispatch_gather()
    test_dispatch_failure_stops_batch()
    print("\n✅ 全部 Phase 3 测试通过\n")


if __name__ == "__main__":
    main()
