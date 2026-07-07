"""P2-14 深读登记册 D50 / D56 / D60 行为测试。

D50：handle_failure / learn_* 的 plan 注入改走 slim 瘦身（剥每子任务 contract 副本）。
D56：recovery._package_in_baseline 项目树索引 memo（阳性可吃 TTL 缓存、阴性新鲜确认）。
D60：YuqueSource 不再 install_opener 污染进程全局；learn_store finally 不再 NameError 顶替原异常。
"""

from __future__ import annotations

import asyncio
import json

import pytest

from swarm.types import FileScope, SubTask, TaskPlan


def _plan_with_fat_contract() -> TaskPlan:
    st = SubTask(
        id="st-1",
        description="demo subtask",
        scope=FileScope(writable=["a/b.py"], readable=[]),
        contract={"blob": "CONTRACT_FAT_MARKER_" + "x" * 200},
        context_snippets="SNIPPET_FAT_MARKER_" + "y" * 200,
    )
    return TaskPlan(subtasks=[st], shared_contract={"k": "v"})


# ─── D50 ───────────────────────────────────────────────


def test_d50_slim_or_empty_none_and_nonplan():
    from swarm.brain.plan_validator import slim_plan_json_or_empty

    assert slim_plan_json_or_empty(None) == "{}"
    assert slim_plan_json_or_empty(object()) == "{}"


def test_d50_slim_or_empty_strips_contract_keeps_structure():
    from swarm.brain.plan_validator import slim_plan_json_or_empty

    out = slim_plan_json_or_empty(_plan_with_fat_contract())
    assert "st-1" in out and "a/b.py" in out          # 结构字段保留
    assert "CONTRACT_FAT_MARKER_" not in out           # contract 剥离
    assert "SNIPPET_FAT_MARKER_" not in out            # context_snippets 剥离
    json.loads(out)                                    # 仍是合法 JSON


def test_d50_learn_failure_prompt_uses_slim_plan(monkeypatch):
    """node 级：learn_failure 喂 LLM 的 prompt 不再携带子任务 contract 全量副本。"""
    import swarm.brain.learn_store as learn_store
    import swarm.brain.nodes as nodes

    captured: dict = {}

    class _FakeLLM:
        async def ainvoke(self, messages):
            captured["prompt"] = "\n".join(m["content"] for m in messages)

            class _R:
                content = json.dumps({"mistake_name": "m", "mistake_description": "d"})

            return _R()

    monkeypatch.setattr(nodes, "_get_brain_llm", lambda: _FakeLLM())

    async def _fake_persist(state, parsed):
        return {"persisted": False, "reason": "test"}

    monkeypatch.setattr(learn_store, "persist_learn_failure", _fake_persist)

    state = {
        "task_description": "demo",
        "plan": _plan_with_fat_contract(),
        "revision_feedback": "",
        "failed_subtask_ids": ["st-1"],
    }
    out = asyncio.run(nodes.learn_failure(state))
    assert out.get("learned") is True
    assert "st-1" in captured["prompt"]                      # 计划结构仍注入
    assert "CONTRACT_FAT_MARKER_" not in captured["prompt"]  # 42K 级 contract 副本不再进 prompt
    assert "SNIPPET_FAT_MARKER_" not in captured["prompt"]


# ─── D56 ───────────────────────────────────────────────


@pytest.fixture()
def _clean_baseline_index():
    from swarm.brain.nodes import recovery

    recovery._BASELINE_DIR_INDEX.clear()
    yield
    recovery._BASELINE_DIR_INDEX.clear()


def test_d56_package_in_baseline_correctness(tmp_path, _clean_baseline_index):
    from swarm.brain.nodes.recovery import _package_in_baseline

    (tmp_path / "mod" / "src" / "com" / "acme" / "util").mkdir(parents=True)
    p = str(tmp_path)
    assert _package_in_baseline(p, "com.acme.util") is True
    assert _package_in_baseline(p, "com.acme.ghost") is False
    # 保守分支不受影响
    assert _package_in_baseline(None, "com.acme.util") is True
    assert _package_in_baseline(p, "") is True


def test_d56_positive_queries_share_one_walk(tmp_path, _clean_baseline_index, monkeypatch):
    """同一项目树上的多次阳性查询共享一次 os.walk（memo 生效，改前每查一走）。"""
    import os as _os

    from swarm.brain.nodes import recovery

    (tmp_path / "src" / "com" / "acme" / "a").mkdir(parents=True)
    (tmp_path / "src" / "com" / "acme" / "b").mkdir(parents=True)
    calls = {"n": 0}
    real_walk = _os.walk

    def counting_walk(*a, **kw):
        calls["n"] += 1
        return real_walk(*a, **kw)

    monkeypatch.setattr(recovery.os, "walk", counting_walk)
    p = str(tmp_path)
    assert recovery._package_in_baseline(p, "com.acme.a") is True
    assert recovery._package_in_baseline(p, "com.acme.b") is True
    assert recovery._package_in_baseline(p, "com.acme.a") is True
    assert calls["n"] == 1


def test_d56_negative_verdict_requires_fresh_index(tmp_path, _clean_baseline_index):
    """阴性判定（可触发 abandon）不吃 stale 缓存：包在缓存后落地，仍能被新鲜重扫看见。"""
    from swarm.brain.nodes import recovery

    (tmp_path / "src" / "com" / "acme" / "old").mkdir(parents=True)
    p = str(tmp_path)
    assert recovery._package_in_baseline(p, "com.acme.old") is True  # 建立缓存
    # 包随 apply 落地；把缓存人为老化到超过阴性新鲜窗（仍在 30s TTL 内）
    (tmp_path / "src" / "com" / "acme" / "fresh").mkdir(parents=True)
    built, roots = recovery._BASELINE_DIR_INDEX[p]
    recovery._BASELINE_DIR_INDEX[p] = (built - 5.0, roots)
    # 旧行为(每次现走)返回 True；stale 缓存若直接用会误判 False → 误 abandon
    assert recovery._package_in_baseline(p, "com.acme.fresh") is True


def test_d56_walk_oserror_stays_conservative(_clean_baseline_index, monkeypatch):
    from swarm.brain.nodes import recovery

    def boom_walk(*a, **kw):
        raise OSError("disk gone")
        yield  # pragma: no cover

    monkeypatch.setattr(recovery.os, "walk", boom_walk)
    assert recovery._package_in_baseline("/nonexistent-xyz", "com.acme.x") is True


# ─── D60a：Yuque 局部 opener ───────────────────────────


def test_d60_yuque_get_json_does_not_mutate_global_opener(monkeypatch):
    import urllib.request

    import swarm.knowledge.ingest.sources as srcmod

    sentinel = urllib.request.build_opener()
    urllib.request.install_opener(sentinel)
    try:
        class _Resp:
            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

            def read(self):
                return json.dumps({"data": []}).encode()

        # 改前无此 seam（AttributeError）；改后生产路径走局部 opener
        monkeypatch.setattr(srcmod, "_guarded_open", lambda opener, req, timeout=None: _Resp())
        monkeypatch.setenv("YUQUE_TOKEN", "tk")
        monkeypatch.setenv("YUQUE_NAMESPACE", "u/r")
        monkeypatch.delenv("YUQUE_BASE", raising=False)
        src = srcmod.YuqueSource()
        assert src.list_documents() == []
        assert urllib.request._opener is sentinel  # 全局默认 opener 未被改写
    finally:
        urllib.request.install_opener(None)  # 还原进程默认


# ─── D60b：learn_store finally 不吞原异常 ───────────────


@pytest.mark.parametrize("fn_name", ["persist_learn_success", "persist_learn_failure"])
def test_d60_learn_store_ctor_failure_returns_original_error(monkeypatch, fn_name):
    import swarm.brain.learn_store as ls

    class _BoomStore:
        def __init__(self):
            raise RuntimeError("ctor-boom-original")

    monkeypatch.setattr(ls, "MemoryStore", _BoomStore)
    fn = getattr(ls, fn_name)
    # 改前：finally `await store.close()` 抛 UnboundLocalError 顶替 error dict
    result = asyncio.run(fn({"project_id": "p1", "task_id": "t1"}, {}))
    assert result["persisted"] is False
    assert "ctor-boom-original" in result.get("error", "")
