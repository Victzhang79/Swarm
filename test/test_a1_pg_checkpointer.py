"""A1 批1 真实验证：PG checkpointer 跨副本 interrupt/resume。

验证目标（设计文档批1步骤5）：
1. init_postgres_checkpointer 真能连 PG 并 setup 表（不再是原 async with 立即关闭的 bug）。
2. 一个带 interrupt 的 graph 用 PG checkpointer 跑到中断点，状态写入 PG。
3. 模拟"另一个副本"：新建独立的 AsyncPostgresSaver 连同一 PG + 同 thread_id，
   能读到 checkpoint 并 Command(resume=) 恢复——证明跨副本共享状态。

测试铁律：_test_ 前缀 thread_id + try/finally 清理，绝不碰生产 thread。
"""

from __future__ import annotations

import asyncio
import uuid

from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
from langgraph.graph import END, START, StateGraph
from langgraph.types import Command, interrupt
from typing_extensions import TypedDict

from swarm.config.settings import get_config

THREAD_ID = f"_test_a1_ckpt_{uuid.uuid4().hex[:8]}"


class _S(TypedDict):
    value: str
    confirmed: bool


def _node_start(state: _S) -> dict:
    return {"value": "started"}


def _node_confirm(state: _S) -> dict:
    # 中断等待外部输入（模拟 confirm_plan 的 interrupt）
    decision = interrupt({"ask": "confirm?"})
    return {"confirmed": decision == "yes"}


def _node_end(state: _S) -> dict:
    return {"value": "done" if state.get("confirmed") else "rejected"}


def _build():
    g = StateGraph(_S)
    g.add_node("start", _node_start)
    g.add_node("confirm", _node_confirm)
    g.add_node("finish", _node_end)
    g.add_edge(START, "start")
    g.add_edge("start", "confirm")
    g.add_edge("confirm", "finish")
    g.add_edge("finish", END)
    return g


async def _run():
    uri = get_config().db.postgres_uri
    cfg = {"configurable": {"thread_id": THREAD_ID}}

    # ── 副本 1：跑到 interrupt，状态入 PG ──
    async with AsyncPostgresSaver.from_conn_string(uri) as cp1:
        await cp1.setup()
        graph1 = _build().compile(checkpointer=cp1)
        result1 = await graph1.ainvoke({"value": "", "confirmed": False}, config=cfg)
        # 应在 confirm 处中断（__interrupt__ 存在，未到 finish）
        assert "__interrupt__" in result1, f"应中断，实际: {result1}"
        print("  ✅ 副本1：跑到 interrupt，状态已写入 PG")

    # ── 副本 2：全新 checkpointer 连同一 PG + 同 thread，resume ──
    async with AsyncPostgresSaver.from_conn_string(uri) as cp2:
        graph2 = _build().compile(checkpointer=cp2)
        # 先确认能读到副本1留下的 checkpoint
        state = await graph2.aget_state(cfg)
        assert state is not None and state.next, "副本2应能读到副本1的中断态 checkpoint"
        print(f"  ✅ 副本2：读到副本1的 checkpoint（next={state.next}）")
        # resume
        result2 = await graph2.ainvoke(Command(resume="yes"), config=cfg)
        assert result2.get("value") == "done", f"resume 后应完成，实际: {result2}"
        print("  ✅ 副本2：Command(resume) 成功恢复并完成 —— 跨副本 resume 验证通过")


async def _cleanup():
    import psycopg
    uri = get_config().db.postgres_uri
    async with await psycopg.AsyncConnection.connect(uri, autocommit=True) as conn:
        async with conn.cursor() as cur:
            for tbl in ("checkpoints", "checkpoint_writes", "checkpoint_blobs"):
                try:
                    await cur.execute(f"DELETE FROM {tbl} WHERE thread_id = %s", (THREAD_ID,))
                except Exception:
                    pass


def test_pg_checkpointer_cross_replica_resume():
    async def _main():
        try:
            await _run()
        finally:
            await _cleanup()
    asyncio.run(_main())


if __name__ == "__main__":
    try:
        test_pg_checkpointer_cross_replica_resume()
        print("\n=== A1 批1 PG checkpointer 跨副本 resume: PASS ===")
    except Exception as e:
        print(f"\n  💥 {type(e).__name__}: {e}")
        raise
