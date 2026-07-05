#!/usr/bin/env python3
"""verify_l2/l3 的同步阻塞调用必须放线程池，不卡 async 事件循环。

R23-1 + round25 #10 全局扫尾。**行为测试**（非 inspect.getsource 结构焊死）：patch 被调的同步函数
记录它运行时所在线程；节点经 asyncio.run 在【主线程】的事件循环里跑——若同步调用被 to_thread 卸到
线程池则运行在【工作线程】，若直接阻塞调用则运行在主线程。断言运行在非主线程即证明已卸载。

覆盖 5 个同步阻塞点：verify_l2 的 run_integration_review / _try_l2_sandbox_verify / _try_l2_local_verify，
verify_l3 的 push_merged_diff_branch / trigger_and_poll_pipeline。
"""
from __future__ import annotations

import asyncio
import threading

from swarm.brain import nodes
from swarm.brain.nodes import verify
from swarm.types import Complexity


def _thread_recorder(box, retval):
    """返回一个记录【自身运行线程】并返回 retval 的假函数（接受任意参数）。"""
    def _rec(*_a, **_k):
        box["thread"] = threading.current_thread()
        return retval
    return _rec


_MAIN = threading.main_thread()


# ── verify_l2 三个同步阻塞点 ────────────────────────────────────────────
def test_verify_l2_offloads_integration_review(monkeypatch):
    import swarm.brain.integration_review as ir
    box: dict = {}
    monkeypatch.setattr(verify, "effective_complexity", lambda s: Complexity.MEDIUM)
    monkeypatch.setattr(nodes, "_get_project_path", lambda pid: "/tmp/_test_proj")
    monkeypatch.setattr(ir, "run_integration_review", _thread_recorder(box, (True, [], {})))
    monkeypatch.setattr(verify, "_l2_test_command_from_criteria", lambda c: "")  # 空 test_cmd → 不进后续块
    asyncio.run(verify.verify_l2({"merged_diff": "diff x", "project_id": "p", "subtask_results": {}}))
    assert box.get("thread") is not None, "run_integration_review 未被调用（路径没走到）"
    assert box["thread"] is not _MAIN, "run_integration_review 必须经 to_thread 卸到工作线程"


def test_verify_l2_offloads_sandbox_verify(monkeypatch):
    box: dict = {}
    monkeypatch.setattr(verify, "effective_complexity", lambda s: Complexity.MEDIUM)
    monkeypatch.setattr(nodes, "_get_project_path", lambda pid: None)  # 跳过 integration_review 块
    monkeypatch.setattr(verify, "_l2_test_command_from_criteria", lambda c: "pytest")
    monkeypatch.setattr(nodes, "_try_l2_sandbox_verify", _thread_recorder(box, True))
    asyncio.run(verify.verify_l2({"merged_diff": "diff x", "project_id": "p", "subtask_results": {}}))
    assert box.get("thread") is not None and box["thread"] is not _MAIN, \
        "_try_l2_sandbox_verify 必须经 to_thread 卸到工作线程"


def test_verify_l2_offloads_local_verify(monkeypatch):
    box: dict = {}
    monkeypatch.setattr(verify, "effective_complexity", lambda s: Complexity.MEDIUM)
    monkeypatch.setattr(nodes, "_get_project_path", lambda pid: None)
    monkeypatch.setattr(verify, "_l2_test_command_from_criteria", lambda c: "pytest")
    monkeypatch.setattr(nodes, "_try_l2_sandbox_verify", lambda *a, **k: None)  # None → 落本地兜底
    monkeypatch.setattr(nodes, "_try_l2_local_verify", _thread_recorder(box, True))
    asyncio.run(verify.verify_l2({"merged_diff": "diff x", "project_id": "p", "subtask_results": {}}))
    assert box.get("thread") is not None and box["thread"] is not _MAIN, \
        "_try_l2_local_verify 必须经 to_thread 卸到工作线程"


# ── verify_l3 两个同步阻塞点 ────────────────────────────────────────────
def _l3_state():
    return {"merged_diff": "diff x", "project_id": "p", "task_id": "t"}


def test_verify_l3_offloads_push(monkeypatch):
    import swarm.brain.l3_gitlab as g
    box: dict = {}
    monkeypatch.setattr(verify, "effective_complexity", lambda s: Complexity.COMPLEX)
    monkeypatch.setattr(nodes, "_get_project_path", lambda pid: "/tmp/_test_proj")
    monkeypatch.setattr(g, "gitlab_configured", lambda: True)
    monkeypatch.setattr(g, "l3_push_enabled", lambda: True)
    monkeypatch.setattr(g, "push_merged_diff_branch", _thread_recorder(box, (None, None)))
    monkeypatch.setattr(g, "trigger_and_poll_pipeline", lambda **k: (True, "ok"))
    asyncio.run(verify.verify_l3(_l3_state()))
    assert box.get("thread") is not None and box["thread"] is not _MAIN, \
        "push_merged_diff_branch 必须经 to_thread 卸到工作线程"


def test_verify_l3_offloads_poll(monkeypatch):
    import swarm.brain.l3_gitlab as g
    box: dict = {}
    monkeypatch.setattr(verify, "effective_complexity", lambda s: Complexity.COMPLEX)
    monkeypatch.setattr(nodes, "_get_project_path", lambda pid: "/tmp/_test_proj")
    monkeypatch.setattr(g, "gitlab_configured", lambda: True)
    monkeypatch.setattr(g, "l3_push_enabled", lambda: False)  # 不推分支，直接轮询
    monkeypatch.setattr(g, "trigger_and_poll_pipeline", _thread_recorder(box, (True, "ok")))
    asyncio.run(verify.verify_l3(_l3_state()))
    assert box.get("thread") is not None and box["thread"] is not _MAIN, \
        "trigger_and_poll_pipeline 必须经 to_thread 卸到工作线程"


if __name__ == "__main__":
    import sys

    import pytest
    sys.exit(pytest.main([__file__, "-q", "-p", "no:warnings"]))
