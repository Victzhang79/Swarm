"""R67L-B5（22号文批次5）行为测试：learn_failure 终态簿记永不穿透 run。

round67l 终态实锤：LLM 提炼返回合法 JSON 数组（解析不抛）→ persist_learn_failure
锁前段 dict(parsed) 抛 ValueError 穿透终态节点，炸穿整个 run 且终态 error 文案
顶替真死因 failure_escalated。

治（双层）：node 侧 parsed 非 dict → 默认错题 payload（保住学习动作）；
store 侧锁前段全兜底（对称 DB 段 except → persisted=False 如实返回）。
"""
from __future__ import annotations

import asyncio
import importlib.util
import json
from pathlib import Path

_bs = Path(__file__).resolve().parent / "swarm_bootstrap.py"
_spec = importlib.util.spec_from_file_location("swarm_bootstrap", _bs)
_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_mod)

import swarm.brain.learn_store as learn_store  # noqa: E402
import swarm.brain.nodes as nodes  # noqa: E402


class _FakeStore:
    """最小 MemoryStore 替身（照 test_b7_learn_persist_lock 同型）。"""

    async def connect(self):
        pass

    async def close(self):
        pass

    async def summary_has_idempotency_key(self, pid, key):
        return False

    def transaction(self):
        class _T:
            async def __aenter__(s):
                return None

            async def __aexit__(s, *a):
                return False
        return _T()

    async def query_mistakes(self, *a, **k):
        return []

    async def write_mistake(self, *a, **k):
        return 1

    async def write_task_summary(self, pid, ts):
        return 1


def test_b5_store_never_raises_on_list_parsed(monkeypatch):
    """round67l 事故原型：parsed=list（元素长 6）→ 不抛、学习动作照常落库。"""
    monkeypatch.setattr(learn_store, "MemoryStore", _FakeStore)
    state = {"project_id": "p1", "task_id": "t1", "task_description": "demo",
             "revision_feedback": "", "plan": {}}
    # 事故输入逐字复刻：dict() 强转即 ValueError 的 list（element #0 has length 6）
    bad_parsed = ["mistak", "e_list"]
    out = asyncio.run(learn_store.persist_learn_failure(state, bad_parsed))
    assert isinstance(out, dict) and "persisted" in out, out
    assert out["persisted"] is True, f"默认 payload 应保住学习动作: {out}"


def test_b5_store_prelock_exception_contained(monkeypatch):
    """锁前段任何构造异常 → persisted=False 如实返回，绝不穿透终态节点。"""
    monkeypatch.setattr(learn_store, "MemoryStore", _FakeStore)

    def _boom(*a, **k):
        raise RuntimeError("构造爆炸")

    monkeypatch.setattr(learn_store, "build_mistake_payload", _boom)
    state = {"project_id": "p1", "task_id": "t1", "task_description": "demo",
             "revision_feedback": "", "plan": {}}
    out = asyncio.run(learn_store.persist_learn_failure(state, {"mistake_name": "m"}))
    assert out["persisted"] is False and "构造爆炸" in out.get("error", ""), out


def test_b5_node_normalizes_json_array_llm_output(monkeypatch):
    """node 侧源头归一：LLM 返回 JSON 数组 → 默认 payload，节点正常收尾 learned=True。"""
    seen: dict = {}

    class _FakeLLM:
        async def ainvoke(self, messages):
            class _R:
                content = json.dumps(["数组", "而非对象", "长度六元"], ensure_ascii=False)
            return _R()

    monkeypatch.setattr(nodes, "_get_brain_llm", lambda: _FakeLLM())

    async def _fake_persist(state, parsed):
        seen["parsed"] = parsed
        return {"persisted": True}

    monkeypatch.setattr(learn_store, "persist_learn_failure", _fake_persist)
    state = {"task_description": "demo", "plan": {}, "revision_feedback": "真死因X",
             "failed_subtask_ids": ["st-1"]}
    out = asyncio.run(nodes.learn_failure(state))
    assert out.get("learned") is True, out
    assert isinstance(seen["parsed"], dict), f" persist 收到的必须是 dict: {seen}"
    assert seen["parsed"].get("mistake_name"), seen["parsed"]
