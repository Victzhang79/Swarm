"""阶段5 批1（登记册 §六）：E5 资源护栏统一 salvage + E2 终态 CAS 守卫 + E10 进度派生。

E5：墙钟（TaskWallclockExceeded）/失锁（TaskLockLost）此前落泛 except 裸 FAILED 整单
    丢产物——只有 TokenLimit 走 salvage。治=三类资源护栏中止统一
    _salvage_partial_from_checkpoint→PARTIAL（无产物时 salvage 内部退回 FAILED）。
    失锁场景安全性已核：salvage 只写 DB/发事件，不碰 git 树（无锁外写树）。
E2：update_task 盲写——线程池晚到写可把 CANCELLED 复活成活跃态→永久孤儿。治=改状态
    时默认 CAS（WHERE NOT status=ANY(终态)）；retry 的终态→SUBMITTED 是唯一合法穿越，
    显式 allow_terminal_transition=True；纯字段更新（token_usage 等）不受限。
E10：progress=min(+4,90) 与完成度无关（恒 90% 挂满全程）+ completed 只在节点边界批量
    跳变。治=执行期 progress 由 completed/count 派生（规划期爬坡帽 25）；dispatch 单个
    子任务完成即回写 completed_subtasks（纯计数，不触 E2 守卫）。
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

from swarm.types import (
    Confidence,
    FileScope,
    SubTask,
    SubTaskDifficulty,
    TaskPlan,
    WorkerOutput,
)

# ─────────────── E5：run_task 护栏异常 → salvage ───────────────


class _FakeLock:
    def acquire(self):
        return True

    def release(self):
        return True

    def renew(self):
        return True


def _run_task_with_stream_raising(exc):
    from swarm.brain import runner

    salvage = AsyncMock()
    store_mock = MagicMock()
    store_mock.get_task.return_value = {}
    store_mock.get_project.return_value = None

    async def _boom(*a, **kw):
        raise exc

    with patch.object(runner, "_stream_brain_events", side_effect=_boom), \
         patch.object(runner, "_salvage_partial_from_checkpoint", salvage), \
         patch.object(runner, "store", store_mock), \
         patch.object(runner, "audit", MagicMock()), \
         patch.object(runner, "_set_workspace", MagicMock()), \
         patch.object(runner, "_emit_task_notification", MagicMock()), \
         patch("swarm.infra.redis_client.ModuleLock", lambda *a, **k: _FakeLock()), \
         patch("swarm.memory.profile.load_profile_prompts",
               return_value=({}, "", "")), \
         patch("swarm.git_base.capture_base_commit", return_value=None), \
         patch("swarm.memory.session.build_session_metadata", return_value={}):
        asyncio.run(runner.run_task("t-e5", "p-e5", "desc"))
    return salvage, store_mock


def test_e5_wallclock_goes_salvage_not_bare_failed():
    from swarm.brain.runner import TaskWallclockExceeded
    salvage, store_mock = _run_task_with_stream_raising(TaskWallclockExceeded(28800.0, 30000.0))
    assert salvage.await_count == 1, (
        "墙钟中止=资源护栏非交付失败——裸 FAILED 整单丢已完成产物（round37 91min 烧穿"
        "若叠加墙钟=全丢）；必须与 TokenLimit 同走 salvage→PARTIAL")
    assert salvage.call_args.kwargs.get("reason_code") == "wallclock_exceeded"
    # 泛 except 的裸 FAILED 不应发生
    assert not any(kw.get("status") == "FAILED"
                   for _, kw in store_mock.update_task.call_args_list)


def test_e5_lock_lost_goes_salvage():
    from swarm.brain.runner import TaskLockLost
    salvage, _ = _run_task_with_stream_raising(TaskLockLost("p-e5:default"))
    assert salvage.await_count == 1
    assert salvage.call_args.kwargs.get("reason_code") == "module_lock_lost"


def test_e5_generic_exception_still_bare_failed():
    salvage, store_mock = _run_task_with_stream_raising(RuntimeError("boom"))
    assert salvage.await_count == 0, "非护栏异常照旧 FAILED（不扩大 salvage 面）"
    assert any(kw.get("status") == "FAILED"
               for _, kw in store_mock.update_task.call_args_list)


# ─────────────── E2：终态 CAS 守卫（SQL 边界） ───────────────

class _FakeCursor:
    def __init__(self, log):
        self.log = log

    def execute(self, sql, params=None):
        self.log.append((" ".join(sql.split()), params))

    def fetchone(self):
        return None

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class _FakeConn:
    def __init__(self, log):
        self.log = log

    def cursor(self):
        return _FakeCursor(self.log)

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def _update_sql(**kw):
    from swarm.project import store
    log: list = []
    with patch.object(store, "_get_conn", lambda conn_str=None: _FakeConn(log)):
        store.update_task("t-1", **kw)
    assert log, "必须发出 UPDATE"
    return log[0]


def test_e2_status_write_guards_terminal():
    sql, params = _update_sql(status="EXECUTING")
    assert "NOT (status = ANY" in sql, (
        "改状态写默认 CAS——晚到写把 CANCELLED 复活成活跃态=永久孤儿（无 runner 在跑"
        "却显示进行中，重启对账都救不回）")
    assert any(isinstance(p, list) and "CANCELLED" in p for p in params)


def test_e2_retry_bypass_explicit():
    sql, _ = _update_sql(status="SUBMITTED", allow_terminal_transition=True)
    assert "NOT (status = ANY" not in sql, (
        "retry 的 PARTIAL/DONE→SUBMITTED 是唯一合法终态穿越（显式声明）")


def test_e2_field_only_update_unrestricted():
    sql, _ = _update_sql(completed_subtasks=3)
    assert "NOT (status = ANY" not in sql, (
        "纯字段更新不受限——终态后回填 token_usage/duration/完成计数合法")


# ─────────────── E10：dispatch 单个完成即回写 ───────────────

async def _dispatch_two(monkeypatch):
    from swarm.brain.nodes.dispatch import dispatch
    plan = TaskPlan(subtasks=[
        SubTask(id="st-1", description="a", difficulty=SubTaskDifficulty.MEDIUM,
                scope=FileScope(writable=["a.py"], readable=[])),
        SubTask(id="st-2", description="b", difficulty=SubTaskDifficulty.MEDIUM,
                scope=FileScope(writable=["b.py"], readable=[])),
    ], parallel_groups=[["st-1", "st-2"]])

    async def fake_worker(subtask, knowledge_context, project_id="", task_id="", **kw):
        return WorkerOutput(subtask_id=subtask.id, diff="+x\n", summary="",
                            l1_passed=True, confidence=Confidence.HIGH)

    calls: list = []

    def fake_update(task_id, **kw):
        calls.append(kw)
        return {}

    monkeypatch.setattr("swarm.project.store.update_task", fake_update)
    state = {
        "task_id": "t-e10", "project_id": "p1", "plan": plan,
        "subtask_results": {}, "dispatch_remaining": ["st-1", "st-2"],
        "failed_subtask_ids": [], "knowledge_context": {},
    }
    with patch("swarm.brain.nodes._dispatch_to_worker", side_effect=fake_worker):
        await dispatch(state)
    return calls


async def test_e10_per_completion_writeback(monkeypatch):
    calls = await _dispatch_two(monkeypatch)
    counts = [kw["completed_subtasks"] for kw in calls if "completed_subtasks" in kw]
    assert counts, (
        "单个子任务完成必须即时回写 completed_subtasks——旧行为只在 _SYNC_ON_NODES "
        "节点 END 批量跳变（dispatch 全批屏障内进度冻结）")
    assert counts[-1] == 2 and all(c <= 2 for c in counts)
    assert not any("status" in kw for kw in calls), "纯计数回写，不碰 status（不触 E2 守卫）"
