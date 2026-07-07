"""round29 遗漏项#1：worker react agent 被当子图继承父 checkpointer → msgpack 崩溃治本。

现场（task d37a52a3 st-26，260s 整轮作废）：`Type is not msgpack serializable: AIMessage`。
三方取证坐实根因链：
1. worker agent 在 brain 图 dispatch 节点内 ainvoke → LangGraph 经 config 传播把【父图的
   PG checkpointer】自动继承给子图（DB 铁证：checkpoints 表 checkpoint_ns='dispatch:<uuid>|N'
   ×12，worker 每步 messages 都在序列化入库）;
2. 模型返回的 AIMessage 带不可 msgpack 序列化负载 → 子图 checkpoint 写入
   MsgpackEncodeError → 炸进 worker 执行异常兜底，整轮作废；
3. 附带伤害：每个 worker 每步一次无用 checkpoint 写入（PG 膨胀+慢）。

治本：worker agent 是【无状态一次性执行体】（失败恢复靠 brain 层重派整个子任务，从不
resume agent 内部状态），create_react_agent(checkpointer=False) 显式阻断继承
（LangGraph Checkpointer = None|bool|BaseCheckpointSaver，False=官方"不继承"语义）。
"""
from __future__ import annotations

import asyncio
import importlib.util
from pathlib import Path
from typing import TypedDict
from unittest.mock import patch

_bs = Path(__file__).resolve().parent / "swarm_bootstrap.py"
_spec = importlib.util.spec_from_file_location("swarm_bootstrap", _bs)
_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_mod)

from langchain_core.language_models.fake_chat_models import GenericFakeChatModel
from langchain_core.messages import AIMessage
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.graph import END, StateGraph

from swarm.types import FileScope, SubTask, TaskHarness


class _Weird:  # 不可 msgpack 序列化对象（模拟模型返回的奇异负载）
    pass


def _subtask() -> SubTask:
    return SubTask(
        id="st-x", description="d",
        scope=FileScope(writable=["A.java"]),
        harness=TaskHarness(language="java"),
    )


def _make_worker_agent(reply: AIMessage):
    """经真实 create_worker_agent 构建 agent（mock 掉模型路由与工具集）。"""
    from swarm.worker import agent as agent_mod

    fake_llm = GenericFakeChatModel(messages=iter([reply]))
    with patch.object(agent_mod, "ModelRouter") as mock_router, \
         patch.object(agent_mod, "_get_worker_tools", return_value=[]):
        mock_router.return_value.get_worker_llm.return_value = fake_llm
        bundle = agent_mod.create_worker_agent(subtask=_subtask())
    return bundle["agent"]


def _run_inside_checkpointed_parent(reply: AIMessage) -> tuple[object, list]:
    """在【带 checkpointer 的父图节点内】运行 worker agent——复刻 dispatch 节点的真实拓扑。

    返回 (子图运行结果, 父 saver 中出现的全部 checkpoint_ns)。
    """
    saver = InMemorySaver()

    class _S(TypedDict):
        ok: bool

    agent = _make_worker_agent(reply)

    async def _node(state: _S):
        # 与 executor_agent 同构：节点内直接 ainvoke worker agent（config 自动传播）
        await agent.ainvoke({"messages": [("human", "go")]},
                            config={"recursion_limit": 5})
        return {"ok": True}

    g = StateGraph(_S)
    g.add_node("dispatch", _node)
    g.set_entry_point("dispatch")
    g.add_edge("dispatch", END)
    compiled = g.compile(checkpointer=saver)

    result = asyncio.run(compiled.ainvoke(
        {"ok": False}, config={"configurable": {"thread_id": "t1"}}))
    # CheckpointTuple.config["configurable"] 恒为 dict（RunnableConfig 契约，复核确认）
    nss = [c.config["configurable"].get("checkpoint_ns", "")
           for c in saver.list({"configurable": {"thread_id": "t1"}})]
    return result, nss


def test_worker_agent_does_not_inherit_parent_checkpointer():
    """核心不变量：worker agent 子图【零 checkpoint】——父 saver 只有根 ns（''），
    绝不出现 'dispatch:…' 子图 ns（那意味着 worker 每步 messages 在入库）。"""
    result, nss = _run_inside_checkpointed_parent(AIMessage(content="done"))
    assert result["ok"] is True
    sub_ns = [ns for ns in nss if ns]
    assert sub_ns == [], (
        f"worker agent 不得被父 checkpointer 持久化（无状态一次性执行体），"
        f"实际出现子图 checkpoint_ns={sub_ns}"
    )


def test_worker_agent_survives_unserializable_ai_message():
    """d37a52a3 崩溃场景复刻：AIMessage 带不可 msgpack 序列化负载，在带 checkpointer 的
    父图内运行 worker agent 必须【不炸】（修前 MsgpackEncodeError 整轮作废）。"""
    weird = AIMessage(content="done", additional_kwargs={"raw": _Weird()})
    result, _ = _run_inside_checkpointed_parent(weird)
    assert result["ok"] is True
