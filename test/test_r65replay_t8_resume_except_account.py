"""R65REPLAY-T8（#73）：resume 两处泛 except 的终态账与清扫补齐。

治后回放 #71/#72 双复核确立铁律：未捕获异常路径是【最留幽灵件】的死法，
必须 best-effort 取 state 做 _sweep_unverified_footprints + _failed_machine_account
（否则 dispatched_unaccounted/acceptance_unverified/清扫全失效）。run_task 泛 except
已按此治（取 _accumulated_state），但 resume_task / resume_planning 两处泛 except
仍直接传 state=None——resume 途中未捕获异常同样留幽灵、丢账。

resume 无累积 state（state 仅在 _stream_brain_events 返回后赋值，异常早于它则未绑定），
best-effort 源=_load_state_snapshot(task_id)（从 checkpoint 读，与 salvage 同源）。
"""

from __future__ import annotations

import asyncio
from unittest.mock import patch

import swarm.brain.runner as runner


def _base_patches(snapshot_state):
    """公共 patch：get_task 返回可 resume 态、锁可得、_stream 抛异常、snapshot 可读。"""
    calls = {"sweep": [], "account": [], "update": []}

    async def _boom(*a, **k):
        raise RuntimeError("resume 途中崩")

    async def _load_snap(task_id, thread_id=None):
        return snapshot_state

    def _sweep(task_id, state, project_path=None):
        calls["sweep"].append(state)
        return {}

    def _account(task_id, state, reason):
        calls["account"].append((state, reason))
        return {"salvage_reason": reason}

    return calls, _boom, _load_snap, _sweep, _account


def _run_resume(entry, snapshot_state):
    calls, _boom, _load_snap, _sweep, _account = _base_patches(snapshot_state)

    class _Lock:
        def acquire(self): return True
        def release(self): pass

    def _upd(tid, **k):
        calls["update"].append(k)

    with patch.object(runner.store, "get_task",
                      lambda tid: {"project_id": "p", "status": "CONFIRMING"}), \
         patch.object(runner.store, "update_task", _upd), \
         patch.object(runner, "_stream_brain_events", _boom), \
         patch.object(runner, "_load_state_snapshot", _load_snap), \
         patch.object(runner, "_sweep_unverified_footprints", _sweep), \
         patch.object(runner, "_failed_machine_account", _account), \
         patch.object(runner, "ModuleLock", lambda *a, **k: _Lock(), create=True), \
         patch("swarm.infra.redis_client.ModuleLock", lambda *a, **k: _Lock()), \
         patch.object(runner, "_emit_task_notification", lambda *a, **k: None), \
         patch("swarm.infra.redis_client.get_redis", lambda: None):
        if entry == "task":
            asyncio.run(runner.resume_task("t-r8", "accept", revert_status="CONFIRMING"))
        else:
            asyncio.run(runner.resume_planning("t-r8p", {"action": "skip"},
                                               revert_status="CLARIFYING"))
    return calls


def test_resume_task_except_uses_snapshot_state_for_account():
    snap = {"subtask_dispatch_totals": {"st-x": 1}, "subtask_results": {}}
    calls = _run_resume("task", snap)
    assert calls["account"], "resume_task 泛异常必须落机读账"
    _state, _reason = calls["account"][-1]
    assert _state is snap, \
        f"泛异常账必须用 best-effort snapshot（非 None）: {_state}"
    assert calls["sweep"] and calls["sweep"][-1] is snap, \
        "泛异常路径必须 best-effort 清扫幽灵件"
    # 猎手 F7：断言终态写真发生（status=FAILED + 非空 token_usage）
    _failed_writes = [k for k in calls["update"] if k.get("status") == "FAILED"]
    assert _failed_writes and _failed_writes[-1].get("token_usage"), \
        f"泛异常必须写 FAILED 终态+非空机读账: {calls['update']}"


def test_resume_planning_except_uses_snapshot_state_for_account():
    snap = {"subtask_dispatch_totals": {"st-y": 1}, "subtask_results": {}}
    calls = _run_resume("planning", snap)
    assert calls["account"], "resume_planning 泛异常必须落机读账"
    _state, _reason = calls["account"][-1]
    assert _state is snap, f"泛异常账必须用 snapshot（非 None）: {_state}"


def test_resume_except_snapshot_unavailable_falls_back_gracefully():
    """snapshot 取不到（None）→ 账仍落（空账不阻断），绝不因取快照失败而崩终态。"""
    calls = _run_resume("task", None)
    assert calls["account"], "snapshot 为 None 时账仍必须落（best-effort 语义）"
    _state, _reason = calls["account"][-1]
    assert _state is None, "snapshot 不可得时诚实传 None（空账）"


def test_best_effort_snapshot_swallows_load_failure():
    """猎手 F7：_best_effort_snapshot 自身 except 分支——_load_state_snapshot 抛异常
    （非仅返回 None）→ 兜底返回 None、绝不冒泡崩终态。"""
    async def _raising(task_id, thread_id=None):
        raise RuntimeError("checkpointer 读崩")

    async def _go():
        with patch.object(runner, "_load_state_snapshot", _raising):
            return await runner._best_effort_snapshot("t-boom")
    assert asyncio.run(_go()) is None, "取快照失败必须回退 None（空账不阻断终态）"


def test_run_task_no_longer_references_dead_accumulated_state():
    """猎手 CONFIRMED HIGH 回归锁：run_task 的 _accumulated_state 是死代码
    （_stream_brain_events 局部，恒 NameError→_exc_state 恒 None，#71/#72 静默失效）。
    行为契约=三条终态兜底路径统一走 _best_effort_snapshot；此处 AST 守 run_task
    作用域内不得再 Load _accumulated_state（非 getsource 文本守卫，查语法树引用）。"""
    import ast
    import inspect
    tree = ast.parse(inspect.getsource(runner))
    for node in ast.walk(tree):
        if isinstance(node, ast.AsyncFunctionDef) and node.name == "run_task":
            loads = {n.id for n in ast.walk(node)
                     if isinstance(n, ast.Name) and isinstance(n.ctx, ast.Load)}
            assert "_accumulated_state" not in loads, \
                "run_task 不得引用跨函数局部 _accumulated_state（死代码，恒 None）"
            return
    raise AssertionError("未找到 run_task")


if __name__ == "__main__":
    import pytest
    raise SystemExit(pytest.main([__file__, "-q"]))
