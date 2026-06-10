#!/usr/bin/env python3
"""LLM 调用可中断性 + 取消传播 单测。

验证：(1) provider 启用 streaming + 本地 max_retries=0（取消时连接关闭→服务端 abort）；
(2) 取消正在执行的 worker 时，CancelledError 传播到 finally 触发 kill_sandbox（释放资源）。
"""

from __future__ import annotations

import asyncio
import importlib.util
from pathlib import Path
from unittest.mock import MagicMock, patch

_bs = Path(__file__).resolve().parent / "swarm_bootstrap.py"
_spec = importlib.util.spec_from_file_location("swarm_bootstrap", _bs)
_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_mod)


# ── provider streaming / retry 配置 ──────────────


def test_local_provider_streaming_no_retry():
    """本地小模型必须 streaming=True + max_retries=0（取消即释放 GPU，不重试）。"""
    from swarm.config.settings import ModelConfig
    from swarm.models.router import LocalProvider

    llm = LocalProvider(ModelConfig()).get_chat_model("local-model")
    assert llm.streaming is True, "本地模型未启用 streaming → 取消无法中断服务端解码"
    assert llm.max_retries == 0, "本地模型重试>0 → 取消瞬间可能又发新请求占 GPU"
    print("  ✅ LocalProvider streaming=True, max_retries=0")


def test_siliconflow_provider_streaming():
    from swarm.config.settings import ModelConfig
    from swarm.models.router import SiliconFlowProvider

    llm = SiliconFlowProvider(ModelConfig()).get_chat_model("cloud-model")
    assert llm.streaming is True
    print("  ✅ SiliconFlowProvider streaming=True")


# ── 取消传播 → kill_sandbox ──────────────────────


def test_cancel_propagates_to_worker_finally_kills_sandbox():
    """取消正在 ainvoke 的 worker，CancelledError 应传播到 finally 触发 kill_sandbox。"""
    from swarm.types import FileScope, SubTask, SubTaskDifficulty
    from swarm.worker.executor import WorkerExecutor

    st = SubTask(
        id="sub-cancel",
        description="慢任务",
        difficulty=SubTaskDifficulty.MEDIUM,
        scope=FileScope(writable=["a.py"], readable=["a.py"]),
    )
    ex = WorkerExecutor(st, project_id="p1", task_id="task-cancel")

    killed = {"called": False}

    def fake_kill():
        killed["called"] = True

    # agent.ainvoke 永远 pending（模拟 LLM 还在生成），取消时应被打断
    async def never_returns(*a, **k):
        await asyncio.Event().wait()

    ex.kill_sandbox = fake_kill  # type: ignore[method-assign]

    async def scenario():
        with patch.object(ex, "_create_agent", return_value={"agent": MagicMock()}):
            with patch.object(ex, "_run_agent", side_effect=never_returns):
                # 沙箱关闭以走纯 LLM 路径
                with patch("swarm.config.settings.get_config") as gc:
                    cfg = MagicMock()
                    cfg.sandbox.use_for_worker = False
                    cfg.sandbox.api_url = ""
                    cfg.worker.max_execution_time = 300
                    cfg.worker.max_iterations = 10
                    cfg.worker.max_fix_rounds = 1
                    gc.return_value = cfg
                    task = asyncio.create_task(ex.run())
                    await asyncio.sleep(0.1)  # 让它进入 _run_agent 的 await
                    task.cancel()
                    try:
                        await task
                    except asyncio.CancelledError:
                        pass

    asyncio.run(scenario())
    assert killed["called"], "取消后 finally 未调用 kill_sandbox（沙箱泄漏）"
    print("  ✅ 取消传播到 worker finally → kill_sandbox 被调用")


def test_run_agent_uses_ainvoke_with_timeout():
    """worker 必须用 await ainvoke + wait_for（异步可取消），而非同步 invoke。"""
    import inspect

    from swarm.worker.executor import WorkerExecutor

    src = inspect.getsource(WorkerExecutor._run_agent)
    assert "ainvoke" in src, "_run_agent 应使用 ainvoke（异步，可被取消中断）"
    assert "wait_for" in src, "_run_agent 应有 wait_for 超时（防永久挂起占资源）"
    assert ".invoke(" not in src.replace("ainvoke", ""), "不应使用同步 invoke（线程内无法取消）"
    print("  ✅ _run_agent 用 ainvoke + wait_for（可取消）")


def test_brain_llm_nodes_are_async():
    """所有调用 LLM 的 Brain 节点必须是 async（同步节点在线程池里无法被取消→占 GPU）。"""
    import inspect

    from swarm.brain import nodes

    llm_nodes = [
        "analyze", "plan", "validate_plan", "handle_failure",
        "verify_l2", "verify_l3", "revision", "learn_success", "learn_failure",
    ]
    for name in llm_nodes:
        fn = getattr(nodes, name)
        assert inspect.iscoroutinefunction(fn), f"节点 {name} 必须是 async（否则取消无法中断其 LLM 调用）"
    print("  ✅ 所有 LLM Brain 节点均为 async（可取消）")


def test_scheduler_registers_task_handle():
    """调度器执行任务时必须把 handle 注册到 _task_handles（否则 cancel_task 找不到→无法中断）。

    这是真实 bug：_run_with_slot 曾直接 create_task 不登记 handle，导致取消只翻 DB
    状态而 asyncio 任务+LLM 调用继续跑，小模型资源不释放。
    """
    import inspect

    from swarm.brain import scheduler

    src = inspect.getsource(scheduler._run_with_slot)
    assert "_task_handles[task_id]" in src, "_run_with_slot 必须登记 _task_handles[task_id]（供 cancel_task 中断）"
    print("  ✅ 调度器登记 task handle（cancel 可中断）")


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
    print("\nLLM 可中断性 单测通过。")
