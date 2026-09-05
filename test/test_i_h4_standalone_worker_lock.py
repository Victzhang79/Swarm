"""主题I H-4（外部深审 HIGH）：Standalone Worker 绕过 ModuleLock 写共享 git 树。

病根：standalone worker 本地模式 agent 直改 project_path、沙箱模式 pull-back 也写回本地树——
此前【绝不持模块锁】→ 可与 Brain runner(持模块锁写树)/审批 apply(持 default)/另一 standalone
run 并发写同一 git 树=污染。治：与 Brain 同源按 writable 派生项目锁（whole-project→default 写者；
否则顶层目录→模块读者），经 H-3 读写门与全体写树者互斥；拿不到=fail-loud 让位，绝不静默并发写。
"""
from __future__ import annotations

import asyncio

import pytest

import swarm.infra.redis_client as rc
import swarm.worker.runner as wr


class _StubExecutor:
    """替身 executor：run() 不做真活，只记录被调用；供验证锁在执行前后的获取/释放。"""

    ran = False
    last_scope = None
    l1_passed = True

    def __init__(self, **kwargs):
        type(self).last_scope = kwargs.get("scope")
        self.execution_log: list[str] = []
        self.phase = type("P", (), {"value": "preparing"})()

    async def run(self):
        _StubExecutor.ran = True
        from swarm.types import Confidence, WorkerOutput
        return WorkerOutput(subtask_id="x", diff="", summary="stub",
                            confidence=Confidence.MEDIUM, l1_passed=self.l1_passed,
                            l1_details={}, execution_log="", notes="")


@pytest.fixture(autouse=True)
def _isolate(monkeypatch, tmp_path):
    rc._reset_project_gates()
    rc._LOCAL_LOCKS.clear()
    monkeypatch.setattr(rc, "get_redis", lambda: None)  # 内存锁模式
    # store.get_project 返回带 path 的项目（供 _set_workspace）
    monkeypatch.setattr(wr.store, "get_project",
                        lambda pid: {"id": pid, "path": str(tmp_path)})
    monkeypatch.setattr("swarm.worker.executor.WorkerExecutor", _StubExecutor)
    monkeypatch.setattr("swarm.tools.paths.set_workspace_root", lambda *a, **k: None)
    _StubExecutor.ran = False
    _StubExecutor.last_scope = None
    _StubExecutor.l1_passed = True
    # 清进程内 standalone 全局态
    wr._worker_queues.clear()
    wr._worker_running.clear()
    wr._worker_run_project.clear()
    yield
    rc._reset_project_gates()
    rc._LOCAL_LOCKS.clear()
    wr._worker_queues.clear()
    wr._worker_running.clear()


def _drain(run_id):
    q = wr._worker_queues.get(run_id)
    out = []
    while q and not q.empty():
        out.append(q.get_nowait())
    return out


def test_h4_standalone_acquires_and_releases_project_lock():
    """无冲突：run 正常执行 executor.run()，结束后项目锁已释放（可再被 default 写者获取）。"""
    asyncio.run(wr.run_standalone_worker("run-ok", "proj-h4", "desc", writable=None))
    assert _StubExecutor.ran is True, "无锁冲突 → executor 正常执行"
    events = _drain("run-ok")
    assert any(e.get("step") == "complete" for e in events), "干净跑发 complete"
    assert not any(e.get("step") == "error" for e in events), "干净跑不应有 error 事件"
    # 锁已释放：全新 default 写者可获取（若未释放则读写门写者被挡）。
    probe = rc.ModuleLock("proj-h4", "default")
    assert probe.acquire() is True, "run 结束后项目锁必须已释放"
    probe.release()


def test_standalone_omitted_scope_is_explicit_full_project_access():
    """留空的 API/CLI scope 用 allow_any 表达，不再依赖空字符串哨兵。"""
    asyncio.run(wr.run_standalone_worker("run-scope", "proj-h4", "desc"))

    scope = _StubExecutor.last_scope
    assert scope is not None
    assert scope.allow_any is True
    assert scope.writable == [] and scope.readable == []
    assert scope.is_writable("src/a.py") is True


def test_standalone_l1_failure_is_not_reported_as_done():
    _StubExecutor.l1_passed = False

    asyncio.run(wr.run_standalone_worker("run-failed", "proj-h4", "desc"))

    events = _drain("run-failed")
    assert any(e.get("step") == "result" and e.get("status") == "failed" for e in events)
    assert any(e.get("step") == "complete" and e.get("status") == "failed" for e in events)


def test_standalone_partial_scope_is_minimum_privilege():
    asyncio.run(wr.run_standalone_worker(
        "run-partial", "proj-h4", "desc", writable=["src/a.py"], readable=None
    ))

    scope = _StubExecutor.last_scope
    assert scope.allow_any is False
    assert scope.is_readable("src/a.py") is True
    assert scope.is_readable("secret.txt") is False


def test_h4_locked_project_read_error_releases_lock_and_running_marker(monkeypatch, tmp_path):
    """acquire 后的 PG 复读异常也必须经过唯一 finally，不能泄漏锁/运行标记。"""
    reads = 0

    def get_project(pid):
        nonlocal reads
        reads += 1
        if reads == 1:
            return {"id": pid, "path": str(tmp_path), "status": "READY"}
        raise RuntimeError("pg read failed")

    class TrackingLock:
        released = 0

        def __init__(self, *_args):
            pass

        def acquire(self):
            return True

        def release(self):
            type(self).released += 1

    monkeypatch.setattr(wr.store, "get_project", get_project)
    monkeypatch.setattr(rc, "ModuleLock", TrackingLock)
    monkeypatch.setattr(rc, "MultiModuleLock", TrackingLock)
    TrackingLock.released = 0

    asyncio.run(wr.run_standalone_worker("run-pg-fail", "proj-h4", "desc"))

    assert TrackingLock.released == 1
    assert "run-pg-fail" not in wr._worker_running
    events = _drain("run-pg-fail")
    assert any(e.get("step") == "error" and "pg read failed" in e.get("message", "")
               for e in events)


def test_h4_standalone_blocked_when_tree_being_written():
    """冲突：同项目已有 default 写者在写树 → standalone 拿不到锁 → fail-loud 让位，绝不执行写树。"""
    holder = rc.ModuleLock("proj-h4", "default")
    assert holder.acquire() is True  # 模拟 Brain apply / 另一写者正持树
    try:
        asyncio.run(wr.run_standalone_worker("run-blk", "proj-h4", "desc", writable=None))
        assert _StubExecutor.ran is False, "锁被占用时绝不执行 executor（不并发写树）"
        events = _drain("run-blk")
        assert any(e.get("step") == "error" and "模块锁" in e.get("message", "")
                   for e in events), "必须发 fail-loud 让位错误事件"
    finally:
        holder.release()


def test_h4_scoped_writable_maps_to_module_reader():
    """带非空 writable → 派生模块读者：与不相交模块并行，但与 default 写者互斥。"""
    # 先占一个不相交模块读者，standalone 取 moduleA 应能并行（读者共存）。
    other = rc.ModuleLock("proj-h4", "moduleB")
    assert other.acquire() is True
    try:
        asyncio.run(wr.run_standalone_worker(
            "run-scoped", "proj-h4", "desc", writable=["moduleA/src/X.java"]))
        assert _StubExecutor.ran is True, "不相交模块读者并行 → standalone 正常执行"
    finally:
        other.release()
    # 释放后 default 写者可入（standalone 的 moduleA 读者已释放）。
    probe = rc.ModuleLock("proj-h4", "default")
    assert probe.acquire() is True
    probe.release()


def test_h4_scoped_standalone_blocked_by_project_wide_writer():
    """default 写者在场 → 即便 standalone 只要 moduleA（读者）也被挡（H-3 层级互斥）。"""
    holder = rc.ModuleLock("proj-h4", "default")
    assert holder.acquire() is True
    try:
        asyncio.run(wr.run_standalone_worker(
            "run-blk2", "proj-h4", "desc", writable=["moduleA/src/X.java"]))
        assert _StubExecutor.ran is False, "整项目写者在场 → 模块读者 standalone 也被挡"
    finally:
        holder.release()


def test_h4_aborts_on_lock_lost_midrun(monkeypatch):
    """hunter F1：运行期 renew 确认丢锁（被抢/过期）→ 中止 executor 写树 + fail-loud error，
    绝不静默续跑（那正是 H-4 要防的并发写树）；锁最终释放。"""
    # 强制：pacer 恒 due + renew 恒判丢锁。
    monkeypatch.setattr(rc.RenewPacer, "due", lambda self, lock, now=None: True)
    monkeypatch.setattr(rc.ModuleLock, "renew", lambda self: False)

    class _SlowExec(_StubExecutor):
        async def run(self):
            _StubExecutor.ran = True
            await asyncio.sleep(30)  # 慢跑：让 _stream_logs 先探到丢锁并中止
            raise AssertionError("丢锁后 executor 不应跑完")

    monkeypatch.setattr("swarm.worker.executor.WorkerExecutor", _SlowExec)
    asyncio.run(wr.run_standalone_worker("run-lost", "proj-h4", "desc", writable=None))
    events = _drain("run-lost")
    assert any(e.get("step") == "error" for e in events), "丢锁中止必须发 error"
    assert not any(e.get("step") == "complete" for e in events), "丢锁绝不 complete"
    # 锁最终释放（可被新 default 写者获取）。
    probe = rc.ModuleLock("proj-h4", "default")
    assert probe.acquire() is True, "丢锁中止后项目锁仍须释放"
    probe.release()


if __name__ == "__main__":
    print("run via pytest")
