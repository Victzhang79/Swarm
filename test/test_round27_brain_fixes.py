"""round27 brain 走查 5 项治本的行为回归（全部为审计 CONFIRMED 后修复）。

F1 模块锁泄漏：acquire 与 try/finally 之间的初始化段异常 → 锁永久泄漏（进程内锁兜底路径）。
F2 粘滞 merge_conflicts：冲突轮写入、clean 轮不清 → after_merge 读残留误路由 HANDLE_FAILURE。
F3 加宽 scope 恒空输入：先 pop(subtask_results) 再取 l1_details → 加宽自引入以来从未生效。
F4 桩清理裸 revert：_generate_compile_stub 的 diff 失败清理不带 H-exec2 protected_files。
F5 ultra 全批失败静默兜底：绕过 plan_generation_failed → auto_accept 放行空 scope 假计划。
"""
from __future__ import annotations

import asyncio
import json
from unittest.mock import patch

import pytest

import swarm.brain.nodes as nodes
from swarm.brain.graph import after_merge
from swarm.types import (
    Complexity, FileScope, SubTask, SubTaskDifficulty, SubTaskModality, TaskPlan, WorkerOutput,
)

DIFF_A = """--- /dev/null
+++ b/a.py
@@ -0,0 +1,1 @@
+# added by a
"""
DIFF_B = """--- /dev/null
+++ b/b.py
@@ -0,0 +1,1 @@
+# added by b
"""
# 同文件同行替换为不同内容 → 无 base 时硬冲突（见 merge_engine 既有行为测试）
DIFF_X_OVERLAP_A = """--- a/x.py
+++ b/x.py
@@ -11,1 +11,1 @@
-context11
+from-replace-a
"""
DIFF_X_OVERLAP_B = """--- a/x.py
+++ b/x.py
@@ -11,1 +11,1 @@
-context11
+from-replace-b
"""


class _FakeResp:
    def __init__(self, content): self.content = content


def _fake_llm(payload: str):
    class _L:
        async def ainvoke(self, _msgs):
            return _FakeResp(payload)
    return lambda: _L()


# ── F2：冲突轮 → clean 轮，粘滞键必须被清、路由必须回 verify_l2 ──────────────
def test_merge_clears_stale_conflicts_on_clean_round():
    """第 1 轮 MERGE 冲突（写 merge_conflicts/failed_subtask_ids 的路径由既有冲突测试覆盖）
    → HANDLE_FAILURE 重试成功 → 第 2 轮 clean merge。BrainState 无 reducer（last-write-wins），
    clean 轮不显式回写空值时上一轮冲突键残留 → after_merge 把已成功的合并再次路由 HANDLE_FAILURE
    （空失败集喂 LLM → 可能 escalate 判 FAILED / replan 推倒重来）。"""
    state: dict = {
        # 模拟上一轮冲突残留（与 merge 节点冲突路径写入的结构一致）
        "merge_conflicts": [
            {"file_path": "x.py", "subtask_ids": ["st-a", "st-b"], "message": "overlap"},
        ],
        "failed_subtask_ids": ["st-a", "st-b"],
        "rebase_subtask_ids": [],
        # 本轮：冲突子任务重试成功，diff 不再重叠 → clean merge
        "subtask_results": {
            "st-a": WorkerOutput(subtask_id="st-a", diff=DIFF_A, summary="", l1_passed=True),
            "st-b": WorkerOutput(subtask_id="st-b", diff=DIFF_B, summary="", l1_passed=True),
        },
    }
    out2 = nodes.merge(state)
    assert out2.get("merge_conflicts") == [], "clean merge 必须显式清 merge_conflicts（H3 同族）"
    assert out2.get("failed_subtask_ids") == [], "clean merge 必须显式清 failed_subtask_ids"
    state.update(out2)
    assert after_merge(state) == "verify_l2", \
        "clean 轮残留上轮冲突会把已成功合并误路由回 HANDLE_FAILURE"


# ── F3：编译失败重试必须真的加宽 scope（旧序 pop 后取详情 → 恒不加宽）─────────
def test_handle_failure_widens_scope_from_l1_details():
    plan = TaskPlan(subtasks=[
        SubTask(id="st-9", description="实现服务", difficulty=SubTaskDifficulty.MEDIUM,
                modality=SubTaskModality.TEXT,
                scope=FileScope(writable=["m/src/Svc.java"])),
    ], parallel_groups=[["st-9"]])
    state = {
        "complexity": Complexity.ULTRA,
        "plan": plan,
        "failed_subtask_ids": ["st-9"],
        "subtask_results": {
            "st-9": WorkerOutput(
                subtask_id="st-9", diff="", summary="编译失败", l1_passed=False,
                l1_details={"l1_2_1_build_ok": False,
                            "build_output": "COMPILATION ERROR: cannot find symbol"},
            ),
        },
        "subtask_retry_counts": {"st-9": 1},
        "dispatch_remaining": [],
        "degraded_reasons": [],
    }
    payload = json.dumps({"strategy": "retry", "reasoning": "缺依赖"}, ensure_ascii=False)
    with patch.object(nodes, "_get_brain_llm", _fake_llm(payload)):
        out = asyncio.run(nodes.handle_failure(state))
    st9 = next(s for s in out["plan"].subtasks if s.id == "st-9")
    assert "m/pom.xml" in (st9.scope.writable or []), \
        "编译失败重试应把模块 pom 纳入 writable（l1_details 须在 pop 前留存，否则恒不加宽）"


# ── F5：ultra 全批失败必须抛出（走 plan_generation_failed 降级），绝不静默返回空 scope 计划 ──
def test_plan_ultra_batched_all_failed_raises():
    from swarm.brain.nodes import _plan_ultra_batched

    class _BoomLLM:
        async def ainvoke(self, _msgs):
            raise RuntimeError("endpoint down")

    file_plan = [{"path": "m/src/A.java", "action": "create", "module": "m",
                  "responsibility": "A"}]
    with pytest.raises(RuntimeError):
        asyncio.run(_plan_ultra_batched(
            _BoomLLM(), {}, "任务", Complexity.ULTRA, {}, {}, "", "", "", file_plan,
        ))


# ── F4：桩生成 diff 失败清理路径必须带 protected_files 护栏 ──────────────────
def test_compile_stub_cleanup_revert_carries_protected_files(tmp_path):
    from swarm.brain.nodes import planning_core as pc

    st = SubTask(id="st-x", description="svc", difficulty=SubTaskDifficulty.MEDIUM,
                 modality=SubTaskModality.TEXT,
                 scope=FileScope(create_files=["m/src/A.java"]))
    payload = json.dumps({"files": {"m/src/A.java": "public class A {}"}}, ensure_ascii=False)
    captured: dict = {}

    def _capture_revert(project_path, st_, protected_files=None, base_ref=None):
        captured["protected_files"] = protected_files
        return {"reverted": [], "removed": [], "revert_failed": []}

    with patch.object(nodes, "_get_brain_llm", _fake_llm(payload)), \
         patch.object(pc, "_git_diff_for_paths", lambda *a, **k: ""), \
         patch.object(pc, "_local_tree_revert_subtask", _capture_revert):
        out = asyncio.run(pc._generate_compile_stub(
            {}, st, str(tmp_path), protected_files={"keep/Me.java"}))
    assert out is None, "diff 为空应回退 revert 路径（测试前提）"
    assert captured.get("protected_files") == {"keep/Me.java"}, \
        "桩清理 revert 必须护住已完成兄弟产物（H-exec2），不得裸清全足迹"


# ── F1：acquire 后初始化段异常 → 模块锁必须被释放（进程内锁兜底路径无 TTL 自愈）──
def test_resume_planning_releases_lock_when_init_raises():
    import swarm.brain.runner as runner
    from swarm.infra.redis_client import ModuleLock

    task_id = "t-lockleak-27"
    project_id = "proj-lockleak-27"

    def _update_task(tid, **kw):
        if kw.get("status") == "ANALYZING":
            raise RuntimeError("db down")  # 初始化段第一笔 DB 写即失败

    with patch.object(runner.store, "get_task",
                      lambda tid: {"project_id": project_id, "status": "CLARIFYING"}), \
         patch.object(runner.store, "update_task", _update_task), \
         patch("swarm.infra.redis_client.get_redis", lambda: None):
        asyncio.run(runner.resume_planning(task_id, {"action": "skip"}))
        # 旧代码：update_task 在 try 之外抛 → finally 不执行 → 进程内锁永久泄漏，下面 acquire 失败
        probe = ModuleLock(project_id, "default")
        assert probe.acquire() is True, \
            "resume_planning 初始化段异常后模块锁必须已释放（F1：try 前移到紧贴 acquire）"
        probe.release()
    assert task_id not in runner._task_running, "_task_running 也必须被 finally 清理"
