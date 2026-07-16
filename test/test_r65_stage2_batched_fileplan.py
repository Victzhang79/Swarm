"""R65-T1: tech_design STAGE2 大模块 file_plan 分批续写协议单测。

round65 死因（task 8cc0c907）：stage1 按「一个功能域落一个模块」产出 ruoyi-alarm
est_files=92，STAGE2 单次调用枚举全部文件 → 单流 ~28.6k chunk 在 500s 超时截断，
3 次重试全同构必超时 → 整模块 file_plan 丢失。治本 = 分批续写：
- 每批带上限（_STAGE2_BATCH_FILES），响应短、单调用远离超时；
- 续批带「已产出清单勿重复」，空批/无新增即收敛（确定性完备判据，不靠批大小猜）；
- 批失败只重试该批（累积成果不丢），失败预算仍 _STAGE2_MAX_ATTEMPTS 次/模块；
- 超时重试自适应缩批；批次硬上限触顶 WARNING 绝不静默截断；
- 模块级保持 all-or-nothing：失败预算烧尽 → file_plan=[] 走 stage2_failed_modules
  对账通道（半截 plan 静默当成功 = 我们一直在杀的 silent-pass 病）。
"""
import asyncio
import json

import pytest

from swarm.brain.planning_nodes import (
    _STAGE2_BATCH_FILES,
    _STAGE2_MAX_BATCHES,
    _tech_design_staged,
)


class _Resp:
    def __init__(self, content):
        self.content = content


class _ScriptedLLM:
    """按提示词内容路由的 mock：stage1 固定应答；每模块一个响应队列。

    比按位置的 side_effect 稳——stage2 各模块 asyncio.gather 并发，
    调用到达顺序不保证，按位置排脚本在并发下是竞态断言。
    """

    def __init__(self, stage1: dict, scripts: dict[str, list]):
        self.stage1 = stage1
        self.scripts = {k: list(v) for k, v in scripts.items()}
        self.calls: dict[str, list[str]] = {k: [] for k in scripts}
        self.stage1_calls = 0

    async def ainvoke(self, messages):
        sys_msg = messages[0]["content"]
        user = messages[-1]["content"]
        if "顶层方案" in sys_msg:
            self.stage1_calls += 1
            return _Resp(json.dumps(self.stage1))
        for name, queue in self.scripts.items():
            if f"模块名：{name}" in user:
                self.calls[name].append(user)
                assert queue, f"模块 {name} 脚本耗尽（第 {len(self.calls[name])} 次调用）"
                item = queue.pop(0)
                if isinstance(item, BaseException):
                    raise item
                if isinstance(item, _Resp):
                    return item  # 预构造响应（如带 response_metadata 的截断响应）
                if isinstance(item, str):
                    return _Resp(item)
                return _Resp(json.dumps(item))
        raise AssertionError(f"无法路由的调用: {user[:200]}")


def _files(prefix: str, start: int, n: int, module: str) -> list[dict]:
    return [{"path": f"{module}/src/{prefix}{i:03d}.java", "action": "create",
             "module": module} for i in range(start, start + n)]


def _batch(prefix, start, n, module):
    return {"file_plan": _files(prefix, start, n, module)}


_EMPTY = {"file_plan": []}


def _stage1(*mods):
    return {"architecture": "分层", "data_model": "表", "stack": {}, "fact_issues": [],
            "modules": [{"name": m, "responsibility": "职责", "est_files": e,
                         "depends_on": []} for m, e in mods]}


@pytest.mark.asyncio
async def test_large_module_batched_until_empty_batch():
    """70 文件大模块：满批×2 + 尾批 + 空批收敛 → 全量合并，无一丢失。"""
    cap = _STAGE2_BATCH_FILES
    llm = _ScriptedLLM(
        _stage1(("big", 70)),
        {"big": [_batch("F", 0, cap, "big"),
                 _batch("F", cap, cap, "big"),
                 _batch("F", 2 * cap, 70 - 2 * cap, "big"),
                 _EMPTY]},
    )
    result, fp, _fi, _c = await _tech_design_staged(
        llm, "建预警平台", "ultra", False, {}, "结构", "", "")
    assert len(fp) == 70, f"应合并 70 文件，实得 {len(fp)}"
    assert len({x["path"] for x in fp}) == 70, "路径应唯一"
    assert not result.get("stage2_failed_modules"), "不应有失败模块"
    assert len(llm.calls["big"]) == 4, "3 个产出批 + 1 次空批确认"
    # 续批提示必须带排除清单与勿重复指令
    cont = llm.calls["big"][1]
    assert "已产出" in cont and "big/src/F000.java" in cont, "续批应带已产出清单"
    # 首批提示必须带批上限指令
    assert f"最多输出 {cap} 个" in llm.calls["big"][0], "首批应声明批上限"


@pytest.mark.asyncio
async def test_small_module_converges_with_one_confirm():
    """小模块：一批出完 + 一次空批确认收敛（完备性判据是确定性的，不猜批大小）。"""
    llm = _ScriptedLLM(
        _stage1(("sdk", 2)),
        {"sdk": [_batch("S", 0, 2, "sdk"), _EMPTY]},
    )
    result, fp, _fi, _c = await _tech_design_staged(
        llm, "需求", "ultra", False, {}, "", "", "")
    assert len(fp) == 2
    assert len(llm.calls["sdk"]) == 2, "1 产出批 + 1 空批确认"
    assert not result.get("stage2_failed_modules")


@pytest.mark.asyncio
async def test_duplicate_only_batch_stops_iteration():
    """续批只复读旧文件（0 新增）→ 判收敛停止，不再发批，不无限循环。"""
    b1 = _batch("D", 0, 3, "dup")
    llm = _ScriptedLLM(_stage1(("dup", 3)), {"dup": [b1, b1]})
    result, fp, _fi, _c = await _tech_design_staged(
        llm, "需求", "ultra", False, {}, "", "", "")
    assert len(fp) == 3, "复读批去重后仍 3 文件"
    assert len(llm.calls["dup"]) == 2, "0 新增批后不应再发下一批"
    assert not result.get("stage2_failed_modules")


@pytest.mark.asyncio
async def test_batch_timeout_retries_batch_and_keeps_accumulated():
    """批 2 超时 1 次：只重试该批，批 1 累积成果不丢；重试批上限自适应减半。"""
    cap = _STAGE2_BATCH_FILES
    llm = _ScriptedLLM(
        _stage1(("mod", 40)),
        {"mod": [_batch("T", 0, cap, "mod"),
                 asyncio.TimeoutError(),
                 _batch("T", cap, 10, "mod"),
                 _EMPTY]},
    )
    result, fp, _fi, _c = await _tech_design_staged(
        llm, "需求", "ultra", False, {}, "", "", "")
    assert len(fp) == cap + 10, "超时重试后累积成果应完整"
    assert not result.get("stage2_failed_modules"), "1 次超时在失败预算内，模块应成功"
    # 超时后的重试调用，批上限应减半（大响应超时 → 更小的批才可能过）
    retry_prompt = llm.calls["mod"][2]
    assert f"最多输出 {max(10, cap // 2)} 个" in retry_prompt, "超时重试应缩批"


@pytest.mark.asyncio
async def test_module_fails_all_or_nothing_after_budget_exhausted():
    """失败预算（3 次）烧尽 → 整模块 file_plan=[] 走 stage2_failed_modules，
    绝不把半截 plan 当成功（silent-pass 禁令）；其他模块不受连坐。"""
    llm = _ScriptedLLM(
        _stage1(("bad", 40), ("ok", 1)),
        {"bad": [_batch("B", 0, 5, "bad"),
                 "garbage not json", "garbage not json", "garbage not json"],
         "ok": [_batch("K", 0, 1, "ok"), _EMPTY]},
    )
    result, fp, _fi, _c = await _tech_design_staged(
        llm, "需求", "ultra", False, {}, "", "", "")
    failed = result.get("stage2_failed_modules") or []
    assert [m["name"] for m in failed] == ["bad"], f"bad 应入失败对账: {failed}"
    assert all(x.get("module") != "bad" for x in fp), "失败模块不得输出半截 file_plan"
    assert any(x["path"] == "ok/src/K000.java" for x in fp), "ok 模块不应连坐"


@pytest.mark.asyncio
async def test_empty_first_batch_counts_as_failure():
    """首批就空（模块 0 产出）→ 计失败重试，3 次全空 → 模块失败（保持旧语义）。"""
    llm = _ScriptedLLM(_stage1(("hollow", 5)), {"hollow": [_EMPTY, _EMPTY, _EMPTY]})
    result, fp, _fi, _c = await _tech_design_staged(
        llm, "需求", "ultra", False, {}, "", "", "")
    failed = result.get("stage2_failed_modules") or []
    assert [m["name"] for m in failed] == ["hollow"]
    assert fp == []
    assert len(llm.calls["hollow"]) == 3, "空首批应烧满失败预算后放弃"


@pytest.mark.asyncio
async def test_max_batches_cap_is_loud_not_silent(caplog):
    """病理性无限产出：触批次硬上限 → 收下已产出并 WARNING，绝不静默截断。"""
    cap = _STAGE2_BATCH_FILES
    batches = [_batch("X", i * cap, cap, "huge") for i in range(_STAGE2_MAX_BATCHES)]
    llm = _ScriptedLLM(_stage1(("huge", 999)), {"huge": batches})
    import logging
    with caplog.at_level(logging.WARNING, logger="swarm.brain.planning_nodes"):
        result, fp, _fi, _c = await _tech_design_staged(
            llm, "需求", "ultra", False, {}, "", "", "")
    assert len(fp) == _STAGE2_MAX_BATCHES * cap
    assert len(llm.calls["huge"]) == _STAGE2_MAX_BATCHES, "触顶后不得再发批"
    assert any("批次上限" in r.message for r in caplog.records), "触顶必须 WARNING 可观测"
    assert not result.get("stage2_failed_modules"), "触顶收下成果，不判失败"


# ────────── 对抗双复核整改锁（R65-T1 复核 R-1 + 猎手 F1/F2/F3/F4/F6/F7）──────────

@pytest.mark.asyncio
async def test_offschema_continuation_is_failure_not_convergence():
    """复核 R-1（CONFIRMED HIGH）：续批返回合法 JSON 但缺 file_plan 键 = off-schema
    退化，绝不作收敛信号——重试烧尽后整模块走失败对账，半截 plan 不得静默成功。"""
    llm = _ScriptedLLM(
        _stage1(("m", 40)),
        {"m": [_batch("O", 0, 5, "m"),
               {"note": "model forgot schema"},
               {"note": "again"},
               {"note": "and again"}]},
    )
    result, fp, _fi, _c = await _tech_design_staged(
        llm, "需求", "ultra", False, {}, "", "", "")
    failed = result.get("stage2_failed_modules") or []
    assert [m["name"] for m in failed] == ["m"], "off-schema 连续退化必须判模块失败"
    assert fp == [], "半截 plan 不得当成功输出"


@pytest.mark.asyncio
async def test_conflicting_reemission_first_wins_and_logged(caplog):
    """猎手 F1（CONFIRMED HIGH）：同路径冲突复读（字段不同）保首见，但必须 WARNING
    留痕；纯复读批（含冲突修正）仍判收敛不无限循环。"""
    import logging
    b1 = {"file_plan": [{"path": "m/src/A.java", "action": "create", "module": "m"}]}
    b2 = {"file_plan": [{"path": "m/src/A.java", "action": "modify", "module": "m"}]}
    llm = _ScriptedLLM(_stage1(("m", 1)), {"m": [b1, b2]})
    with caplog.at_level(logging.WARNING, logger="swarm.brain.planning_nodes"):
        result, fp, _fi, _c = await _tech_design_staged(
            llm, "需求", "ultra", False, {}, "", "", "")
    assert len(fp) == 1 and fp[0]["action"] == "create", "冲突保首见"
    assert not result.get("stage2_failed_modules")
    assert any("冲突复读" in r.message for r in caplog.records), "冲突丢弃必须留痕"


@pytest.mark.asyncio
async def test_topped_out_module_machine_readable_incomplete():
    """猎手 F2（CONFIRMED HIGH）：触批次上限的模块必须机读可辨
    （stage2_incomplete_modules + degraded 通道），不能只靠日志 WARNING。"""
    cap = _STAGE2_BATCH_FILES
    batches = [_batch("X", i * cap, cap, "huge") for i in range(_STAGE2_MAX_BATCHES)]
    llm = _ScriptedLLM(_stage1(("huge", 999)), {"huge": batches})
    result, fp, _fi, _c = await _tech_design_staged(
        llm, "需求", "ultra", False, {}, "", "", "")
    inc = result.get("stage2_incomplete_modules") or []
    assert [m["name"] for m in inc] == ["huge"], "触顶模块必须进机读 incomplete 账"
    assert inc[0]["files"] == _STAGE2_MAX_BATCHES * cap
    assert not result.get("stage2_failed_modules"), "触顶≠失败，产出保留"


@pytest.mark.asyncio
async def test_failure_budget_is_consecutive_not_lifetime():
    """猎手 F3（CONFIRMED HIGH）：零星瞬时失败被成功批清零，不跨批累积成死刑
    （旧终身 3 次预算会杀死恰好要救的大模块）。4 次分散失败仍成功。"""
    cap = _STAGE2_BATCH_FILES
    import asyncio as _a
    llm = _ScriptedLLM(
        _stage1(("m", 200)),
        {"m": [_a.TimeoutError(), _batch("F", 0, cap, "m"),
               _a.TimeoutError(), _batch("F", cap, cap, "m"),
               _a.TimeoutError(), _batch("F", 2 * cap, cap, "m"),
               _a.TimeoutError(), _batch("F", 3 * cap, 5, "m"),
               _EMPTY]},
    )
    result, fp, _fi, _c = await _tech_design_staged(
        llm, "需求", "ultra", False, {}, "", "", "")
    assert not result.get("stage2_failed_modules"), "分散瞬时失败不得判死"
    assert len(fp) == 3 * cap + 5


@pytest.mark.asyncio
async def test_est_files_nonint_estimate_still_observable(caplog):
    """猎手 F4（CONFIRMED MED-HIGH）：est_files="~40" 等 LLM 花式格式宽松抽数字，
    完备性 WARNING 不得被 int() 直转失败静默废掉。"""
    import logging
    llm = _ScriptedLLM(_stage1(("m", "~40")), {"m": [_batch("E", 0, 5, "m"), _EMPTY]})
    with caplog.at_level(logging.WARNING, logger="swarm.brain.planning_nodes"):
        await _tech_design_staged(llm, "需求", "ultra", False, {}, "", "", "")
    assert any("est_files" in r.message and "一半" in r.message for r in caplog.records), \
        "5 < 40/2 必须触发完备性 WARNING（宽松抽数字）"


@pytest.mark.asyncio
async def test_path_alias_variants_dedup_across_batches():
    """猎手 F6（CONFIRMED MED）：./ 前缀/反斜杠变体复读不得当新文件
    （污染计数+重复条目）；纯别名复读批判收敛。"""
    b1 = {"file_plan": [{"path": "m/src/A.java", "action": "create", "module": "m"}]}
    b2 = {"file_plan": [{"path": "./m/src/A.java", "action": "create", "module": "m"},
                        {"path": "m\\src\\A.java", "action": "create", "module": "m"}]}
    llm = _ScriptedLLM(_stage1(("m", 1)), {"m": [b1, b2]})
    result, fp, _fi, _c = await _tech_design_staged(
        llm, "需求", "ultra", False, {}, "", "", "")
    assert len(fp) == 1, f"别名变体必须归一去重: {[x['path'] for x in fp]}"
    assert not result.get("stage2_failed_modules")


@pytest.mark.asyncio
async def test_max_tokens_truncation_counts_failure_and_shrinks(caplog):
    """猎手 F7（CONFIRMED HIGH，尽力而为面）：finish_reason=length 的响应在解析前
    判失败+缩批——json_repair 会把截断 JSON"修好"成幻影残路径静默入账。"""
    cap = _STAGE2_BATCH_FILES
    trunc = _Resp('{"file_plan": [{"path": "m/src/Trunca')
    trunc.response_metadata = {"finish_reason": "length"}
    llm = _ScriptedLLM(
        _stage1(("m", 20)),
        {"m": [trunc, _batch("T", 0, 5, "m"), _EMPTY]},
    )
    result, fp, _fi, _c = await _tech_design_staged(
        llm, "需求", "ultra", False, {}, "", "", "")
    assert len(fp) == 5, "截断响应不得入账，重试批产出才算"
    assert all("Trunca" not in x["path"] for x in fp), "幻影残路径绝不入 file_plan"
    assert not result.get("stage2_failed_modules")
    # 截断后的重试批上限应减半
    retry_prompt = llm.calls["m"][1]
    assert f"最多输出 {max(10, cap // 2)} 个" in retry_prompt, "截断重试应缩批"
