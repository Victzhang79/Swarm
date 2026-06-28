"""治本 A+B：CONTRACT_SKELETON 超时丢 consumer_map（②跨模块依赖的上游真因）。

根因（996db614 实测三盯坐实）：Stage A 全局骨架是 consumer_map（跨模块消费关系→确定性连
depends_on 的唯一来源）的【单点故障】，且是最大单次生成。GLM-5.2 在 600s 仍"未 stall"持续生成
被墙钟掐断 → asyncio.TimeoutError（str 为空 → 旧日志 `%s` 渲染成空，运维看不出是超时）→
`return {}` 整个骨架连 consumer_map 全丢 → 跨模块 depends_on 没连 → ② package does not exist。

治本：
- B：Stage A 加【重试 + 独立更大预算 _CONTRACT_SKELETON_TIMEOUT】（此前独缺重试、共用 600s）。
- A：except 区分 TimeoutError，错因可见（记"超时 Ns"非空消息），耗尽重试记 error 级降级。
"""
from __future__ import annotations

import asyncio
import logging
from unittest.mock import patch

import swarm.brain.planning_nodes as pn


class _Resp:
    def __init__(self, content):
        self.content = content


def _ultra_state():
    return {
        "assessed_complexity": "ultra",
        "task_description": "企业级预警编排平台",
        "tech_design": {
            "modules": [
                {"name": "ruoyi-alarm", "responsibility": "核心引擎"},
                {"name": "ruoyi-alarm-api", "responsibility": "对外 API"},
            ],
            "data_model": "Alarm{id,name}",
        },
    }


def _skeleton_flaky_llm(fail_times: int, skeleton_json: str, calls: dict):
    """Stage A 骨架 call 前 fail_times 次抛 TimeoutError，之后返回 skeleton_json；
    Stage B 模块 call 永远返回一个最小合法片（不拖慢/不干扰）。"""
    class _L:
        async def ainvoke(self, msgs):
            if "consumer_map" in msgs[0]["content"]:  # Stage A 骨架
                calls["skeleton"] += 1
                if calls["skeleton"] <= fail_times:
                    raise asyncio.TimeoutError()
                return _Resp(skeleton_json)
            return _Resp('{"interfaces": [], "dtos": []}')  # Stage B 片
    return lambda: _L()


# ── B：Stage A 超时一次后重试成功 → consumer_map 被抢救（旧代码 1 次即放弃丢全部）──

def test_skeleton_retries_on_timeout_and_salvages():
    calls = {"skeleton": 0}
    skel = ('{"skeleton":{"conventions":[],"constants":[],"consumer_map":'
            '[{"module":"ruoyi-alarm","consumed_by":["ruoyi-alarm-api"],'
            '"expected_surface":"AlarmDTO"}]}}')
    llm = _skeleton_flaky_llm(1, skel, calls)  # 第 1 次超时，第 2 次成功
    with patch.object(pn, "_get_brain_llm", llm):
        out = asyncio.run(pn.contract_design(_ultra_state()))
    assert calls["skeleton"] == 2, f"应重试(旧代码无重试,1 次即放弃),实际 {calls['skeleton']}"
    # 成功 → 走完 Stage B/C 返回 shared_contract_draft（彻底失败才 return {}）
    assert out and "shared_contract_draft" in out, f"重试成功不应降级成空: {out!r}"
    print("  ✅ B：Stage A 超时→重试→consumer_map 抢救成功（非一次即丢）")


# ── A+B：永远超时 → 有界重试 + 优雅降级 + 错因可见（非空消息）──

def test_skeleton_exhausts_retries_then_degrades_with_visible_error():
    calls = {"skeleton": 0}
    llm = _skeleton_flaky_llm(99, "{}", calls)  # 永远超时
    cap: list[str] = []

    class _H(logging.Handler):
        def emit(self, rec):
            cap.append(rec.getMessage())
    h = _H()
    pn.logger.addHandler(h)
    try:
        with patch.object(pn, "_get_brain_llm", llm):
            out = asyncio.run(pn.contract_design(_ultra_state()))
    finally:
        pn.logger.removeHandler(h)
    assert calls["skeleton"] == pn._CONTRACT_SKELETON_MAX_ATTEMPTS, (
        f"应有界重试 {pn._CONTRACT_SKELETON_MAX_ATTEMPTS} 次，实际 {calls['skeleton']}")
    assert out == {}, f"彻底失败应优雅降级沿用 tech_design draft，实际 {out!r}"
    msgs = " ".join(m for m in cap if "CONTRACT_SKELETON" in m)
    assert "超时" in msgs, f"A：超时错因必须可见(旧代码空消息)，日志={msgs!r}"
    print("  ✅ A+B：永远超时→有界重试→优雅降级 + 错因'超时'可见（非空）")


# ── A 的反向：非超时异常也要区分记录（type 名可见，非空）──

def test_skeleton_non_timeout_error_logged_with_type():
    calls = {"skeleton": 0}

    class _L:
        async def ainvoke(self, msgs):
            if "consumer_map" in msgs[0]["content"]:
                calls["skeleton"] += 1
                raise ValueError("boom-xyz")
            return _Resp('{"interfaces": [], "dtos": []}')

    with patch.object(pn, "_get_brain_llm", lambda: _L()):
        import logging as _lg
        cap = []

        class _H(_lg.Handler):
            def emit(self, rec):
                cap.append(rec.getMessage())
        h = _H()
        pn.logger.addHandler(h)
        try:
            out = asyncio.run(pn.contract_design(_ultra_state()))
        finally:
            pn.logger.removeHandler(h)
    assert out == {}
    joined = " ".join(m for m in cap if "CONTRACT_SKELETON" in m)
    assert "ValueError" in joined and "boom-xyz" in joined, f"非超时异常应记 type+msg: {joined!r}"
    print("  ✅ A：非超时异常记 type 名+消息（非空盲点）")


# ── B：独立 skeleton 超时 ≥ 逐模块 stage 超时（给最大单次生成更大预算）──

def test_skeleton_timeout_is_dedicated_and_larger():
    assert pn._CONTRACT_SKELETON_TIMEOUT >= pn._CONTRACT_STAGE_TIMEOUT, (
        "骨架(最大单次生成)预算不应小于逐模块 stage 预算")
    assert pn._CONTRACT_SKELETON_TIMEOUT >= 1200, "默认骨架预算应放宽到 ≥1200s"
    print("  ✅ B：骨架用独立更大预算 _CONTRACT_SKELETON_TIMEOUT")


if __name__ == "__main__":
    import sys
    fails = 0
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            try:
                import inspect
                if "caplog" in inspect.signature(fn).parameters:
                    continue  # caplog 需 pytest fixture，__main__ 跳过
                fn()
            except Exception as e:  # noqa: BLE001
                import traceback
                print(f"  ❌ {name}: {e}")
                traceback.print_exc()
                fails += 1
    sys.exit(1 if fails else 0)
