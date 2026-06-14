"""B2 地基：WorkerDispatcher 抽象单测。

验证：
- 默认 get_worker_dispatcher() 返回 InProcessDispatcher（单机零变化）
- 单例复用
- SWARM_WORKER_DISPATCH_MODE=queue 未实现时回退 inprocess（开箱即用不破坏）
- InProcessDispatcher.dispatch 透传参数给 WorkerExecutor 并 await run（行为等价）
- reset 后可重新选择
"""

from __future__ import annotations

import os
from unittest.mock import AsyncMock, patch

import pytest

from swarm.infra.worker_dispatcher import (
    InProcessDispatcher,
    get_worker_dispatcher,
    reset_worker_dispatcher,
)


def setup_function():
    reset_worker_dispatcher()


def teardown_function():
    reset_worker_dispatcher()
    os.environ.pop("SWARM_WORKER_DISPATCH_MODE", None)


def test_default_is_inprocess():
    d = get_worker_dispatcher()
    assert isinstance(d, InProcessDispatcher)


def test_singleton_reuse():
    d1 = get_worker_dispatcher()
    d2 = get_worker_dispatcher()
    assert d1 is d2


def test_queue_mode_falls_back_to_inprocess_when_unimplemented():
    # queue 模式尚未实现 → 回退 inprocess（不阻断业务，单机开箱即用）
    os.environ["SWARM_WORKER_DISPATCH_MODE"] = "queue"
    reset_worker_dispatcher()
    d = get_worker_dispatcher()
    assert isinstance(d, InProcessDispatcher)


def test_reset_allows_reselection():
    d1 = get_worker_dispatcher()
    reset_worker_dispatcher()
    d2 = get_worker_dispatcher()
    assert d1 is not d2


@pytest.mark.asyncio
async def test_inprocess_dispatch_delegates_to_executor():
    """InProcessDispatcher.dispatch 应构造 WorkerExecutor 并 await run()，参数透传。"""
    fake_output = object()
    fake_exec = AsyncMock()
    fake_exec.run = AsyncMock(return_value=fake_output)

    with patch("swarm.worker.executor.WorkerExecutor", return_value=fake_exec) as ctor:
        d = InProcessDispatcher()
        result = await d.dispatch(
            subtask="ST",
            model_name="m1",
            knowledge="KB",
            project_id="p1",
            project_path="/tmp/p",
            task_id="t1",
            user_profile_prompt="prof",
            shared_contract={"k": "v"},
        )

    assert result is fake_output
    fake_exec.run.assert_awaited_once()
    # 构造参数透传校验
    kwargs = ctor.call_args.kwargs
    assert kwargs["subtask"] == "ST"
    assert kwargs["model_name"] == "m1"
    assert kwargs["knowledge"] == "KB"
    assert kwargs["project_id"] == "p1"
    assert kwargs["project_path"] == "/tmp/p"
    assert kwargs["task_id"] == "t1"
    assert kwargs["user_profile_prompt"] == "prof"
    assert kwargs["shared_contract"] == {"k": "v"}


if __name__ == "__main__":
    import sys
    sys.exit(pytest.main([__file__, "-v"]))
