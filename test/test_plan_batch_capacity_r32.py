"""R32 治本批（E2E_ROUND32_REGISTER.md U1/U2）：ULTRA 分批规划容量与重复劳动治理。

取证（round32, task d9411910）：143 文件→6 模块批，大批（alarm-core/engine/web）4 轮
共 16 次 LLM 分解确定性超时 >300s（小批 2-4 子任务几乎全成）；validation 重试全量重跑
所有批（interface 白烧 4 遍），重负载把后端越压越慢（批成功率 3/6→2/6→2/6→1/6）。

U1 超大批二次切分：单批文件数上限（SWARM_PLAN_BATCH_MAX_FILES，默认 20），超限模块
   按序切成 mod#i/k 子批（批间串行门控沿用 merge_subtask_batches 既有机制）。
U2 成功批缓存：state 键 plan_batch_cache（签名=模块+批文件清单 hash），★只在"上一轮
   有失败批"的补齐型重试复用——上一轮批全成的纯覆盖分歧重试（round31 形态）绝不吃
   缓存，否则复用产出同一 plan，T3 增量修补/baseline 申报永远无法生效★。
"""

from __future__ import annotations

import json

from swarm.brain.nodes import _plan_ultra_batched
from swarm.brain.plan_batch import batch_signature, split_oversized_batches

REQ_A = "req-aaaa1111"


def _fp(path, resp="r"):
    return {"path": path, "action": "create", "responsibility": resp}


# ─────────────── U1: split_oversized_batches 纯函数 ───────────────

def test_split_leaves_small_batches_untouched():
    batches = [("mod-a", [_fp("a/1"), _fp("a/2")]), ("mod-b", [_fp("b/1")])]
    assert split_oversized_batches(batches, 20) == batches


def test_split_oversized_batch_into_ordered_subbatches():
    files = [_fp(f"big/{i}") for i in range(45)]
    out = split_oversized_batches([("big-mod", files)], 20)
    names = [n for n, _ in out]
    assert names == ["big-mod#1/3", "big-mod#2/3", "big-mod#3/3"]
    assert [len(fs) for _, fs in out] == [20, 20, 5]
    # 顺序守恒：拼回去与原清单逐项一致（批间串行门控依赖此序）
    assert [f["path"] for _, fs in out for f in fs] == [f["path"] for f in files]


def test_split_preserves_inter_module_order():
    out = split_oversized_batches(
        [("m1", [_fp(f"m1/{i}") for i in range(25)]), ("m2", [_fp("m2/1")])], 20)
    assert [n for n, _ in out] == ["m1#1/2", "m1#2/2", "m2"]


def test_split_invalid_cap_no_split():
    batches = [("m", [_fp(f"m/{i}") for i in range(30)])]
    assert split_oversized_batches(batches, 0) == batches
    assert split_oversized_batches(batches, -5) == batches


# ─────────────── U2: batch_signature ───────────────

def test_signature_stable_and_content_sensitive():
    files = [_fp("m/a", "甲"), _fp("m/b", "乙")]
    s1 = batch_signature("mod", files)
    assert s1 == batch_signature("mod", [dict(f) for f in files]), "同内容同签名"
    assert s1 != batch_signature("mod2", files), "模块名参与签名"
    assert s1 != batch_signature("mod", files[:1]), "文件集参与签名"
    assert s1 != batch_signature(
        "mod", [_fp("m/a", "甲"), _fp("m/b", "变更")]), "responsibility 参与签名"


# ─────────────── U1+U2: _plan_ultra_batched 集成 ───────────────

class _CountingLLM:
    """按 prompt 中模块名路由回放；记录每模块被真实调用次数。"""

    def __init__(self, payloads: dict, timeout_mods=()):
        self.payloads = payloads
        self.timeout_mods = set(timeout_mods)
        self.calls: dict[str, int] = {}

    async def ainvoke(self, messages):
        prompt = messages[-1]["content"]
        for mod, payload in self.payloads.items():
            if f"模块 '{mod}'" in prompt:
                self.calls[mod] = self.calls.get(mod, 0) + 1
                if mod in self.timeout_mods:
                    import asyncio
                    await asyncio.sleep(9)  # > 测试注入的 SWARM_PLAN_BATCH_TIMEOUT
                return type("R", (), {"content": payload})()
        raise AssertionError(f"prompt 未命中任何已知模块: {prompt[:200]}")


def _payload(mod, n=1):
    return json.dumps({"subtasks": [
        {"id": f"st-{mod}-{i}", "description": f"{mod} 工作 {i}",
         "scope": {"writable": [f"{mod}/f{i}"], "readable": []}}
        for i in range(n)
    ]})


def _state(extra=None):
    return {"tech_design": {}, "shared_contract_draft": {}, "project_id": "",
            **(extra or {})}


async def _run(llm, state, file_plan, monkeypatch):
    monkeypatch.setenv("SWARM_PLAN_BATCH_TIMEOUT", "2")
    monkeypatch.setenv("SWARM_PLAN_BATCH_MAX_ATTEMPTS", "1")
    monkeypatch.setenv("SWARM_PLAN_BATCH_MAX_FILES", "20")
    return await _plan_ultra_batched(llm, state, "需求", {}, "", file_plan)


async def test_oversized_module_is_subsplit_end_to_end(monkeypatch):
    """45 文件模块在集成路径被切 3 子批（每子批独立 LLM 调用，prompt 各含本子批文件）。"""
    llm = _CountingLLM({"big#1/3": _payload("b1"), "big#2/3": _payload("b2"),
                        "big#3/3": _payload("b3")})
    file_plan = [_fp(f"big/{i}") for i in range(45)]
    plan, failed, _bl, _cache = await _run(llm, _state(), file_plan, monkeypatch)
    assert failed == []
    assert sum(llm.calls.values()) == 3
    assert len(plan.subtasks) == 3


async def test_cache_reuses_successful_batch_on_repair_retry(monkeypatch):
    """补齐型重试（上一轮有失败批）：成功批走缓存零 LLM 调用，失败批真跑。"""
    file_plan = [_fp("ok/ok_main.txt"), _fp("bad/bad_main.txt")]
    # 第一轮：ok 成功，bad 超时
    llm1 = _CountingLLM({"ok": _payload("ok"), "bad": _payload("bad")},
                        timeout_mods={"bad"})
    _plan1, failed1, _bl1, cache1 = await _run(llm1, _state(), file_plan, monkeypatch)
    assert [m["name"] for m in failed1] == ["bad"]
    assert len(cache1) == 1, "成功批必须入缓存"
    # 第二轮（重试）：state 带上一轮 failed_modules + cache → ok 不再调 LLM
    llm2 = _CountingLLM({"ok": _payload("ok"), "bad": _payload("bad")})
    state2 = _state({"plan_batch_failed_modules": failed1,
                     "plan_batch_cache": cache1})
    plan2, failed2, _bl2, cache2 = await _run(llm2, state2, file_plan, monkeypatch)
    assert failed2 == []
    assert llm2.calls == {"bad": 1}, "ok 批必须缓存命中零 LLM 调用"
    assert len(plan2.subtasks) == 2, "缓存批+新批合并完整"
    assert len(cache2) == 2, "本轮缓存=全部成功批（含缓存命中的）"


async def test_cache_not_used_when_previous_attempt_fully_succeeded(monkeypatch):
    """纯覆盖分歧重试（上一轮批全成）：绝不吃缓存——复用=产出同一 plan，
    T3 增量修补/baseline 申报永远无法生效（round31 形态回归锚）。"""
    file_plan = [_fp("ok/ok_main.txt")]
    llm1 = _CountingLLM({"ok": _payload("ok")})
    _p, failed1, _b, cache1 = await _run(llm1, _state(), file_plan, monkeypatch)
    assert failed1 == [] and len(cache1) == 1
    llm2 = _CountingLLM({"ok": _payload("ok")})
    state2 = _state({"plan_batch_failed_modules": [],  # 上一轮全成
                     "plan_batch_cache": cache1})
    await _run(llm2, state2, file_plan, monkeypatch)
    assert llm2.calls == {"ok": 1}, "上一轮无失败批 → 缓存必须被忽略"


async def test_cache_signature_mismatch_not_reused(monkeypatch):
    """file_plan 变更（replan/新 tech_design）→ 签名不同 → 不复用陈旧产物。"""
    llm1 = _CountingLLM({"ok": _payload("ok"), "bad": _payload("bad")},
                        timeout_mods={"bad"})
    _p, failed1, _b, cache1 = await _run(
        llm1, _state(), [_fp("ok/ok_main.txt"), _fp("bad/bad_main.txt")], monkeypatch)
    llm2 = _CountingLLM({"ok": _payload("ok"), "bad": _payload("bad")})
    state2 = _state({"plan_batch_failed_modules": failed1,
                     "plan_batch_cache": cache1})
    # ok 批文件内容变了
    await _run(llm2, state2, [_fp("ok/ok_main.txt", "变更后职责"), _fp("bad/bad_main.txt")], monkeypatch)
    assert llm2.calls.get("ok") == 1, "签名不匹配必须真跑"


async def test_cached_batch_baseline_decls_survive_reuse(monkeypatch):
    """缓存命中批的 baseline_covered 申报必须随缓存回放（不丢申报）。"""
    payload = json.dumps({
        "subtasks": [{"id": "st-1", "description": "x",
                      "scope": {"writable": ["ok/ok_main.txt"], "readable": []}}],
        "baseline_covered": [{"id": REQ_A, "reason": "存量已有"}],
    })
    llm1 = _CountingLLM({"ok": payload, "bad": _payload("bad")},
                        timeout_mods={"bad"})
    _p, failed1, bl1, cache1 = await _run(
        llm1, _state(), [_fp("ok/ok_main.txt"), _fp("bad/bad_main.txt")], monkeypatch)
    assert bl1 == [{"id": REQ_A, "reason": "存量已有", "evidence": ""}]
    llm2 = _CountingLLM({"ok": payload, "bad": _payload("bad")})
    state2 = _state({"plan_batch_failed_modules": failed1,
                     "plan_batch_cache": cache1})
    _p2, _f2, bl2, _c2 = await _run(
        llm2, state2, [_fp("ok/ok_main.txt"), _fp("bad/bad_main.txt")], monkeypatch)
    assert llm2.calls == {"bad": 1}
    assert bl2 == [{"id": REQ_A, "reason": "存量已有", "evidence": ""}], "缓存回放必须带申报"


# ═══════════ 双复核整改（F-1/F-2/F-3/F-4）═══════════

async def test_invalid_batch_not_cached(monkeypatch):
    """F-1 [M]：字段畸形批（SubTask 构造失败被剔除记账）绝不入缓存——否则补齐重试
    确定性回放同一畸形产物，LLM 永远不被重问，盲烧重试预算至 escalate。"""
    bad_payload = json.dumps({"subtasks": [
        {"id": "st-x", "description": "x", "scope": "不是dict会构造失败"}]})
    llm1 = _CountingLLM({"ok": _payload("ok"), "bad": bad_payload})
    file_plan = [_fp("ok/ok_main.txt"), _fp("bad/bad_main.txt")]
    _p, failed1, _b, cache1 = await _run(llm1, _state(), file_plan, monkeypatch)
    assert any("invalid_subtasks" in m["reason"] for m in failed1)
    assert all(v.get("module") != "bad" for v in cache1.values()), \
        "畸形批必须被逐出缓存（重试须真跑 LLM 给自愈机会）"
    # 补齐重试：bad 必须真跑
    llm2 = _CountingLLM({"ok": _payload("ok"), "bad": _payload("bad")})
    state2 = _state({"plan_batch_failed_modules": failed1,
                     "plan_batch_cache": cache1})
    _p2, failed2, _b2, _c2 = await _run(llm2, state2, file_plan, monkeypatch)
    assert failed2 == [] and llm2.calls.get("bad") == 1


async def test_scaffold_hint_only_on_first_subbatch(monkeypatch):
    """F-2 [L]：新模块被切分后 scaffold 提示只进首子批——否则每子批各造一份脚手架，
    非 Maven 栈的去重网（dedupe_module_scaffolds 只认 pom.xml）兜不住。"""
    llm = _CountingLLM({"newmod#1/2": _payload("a"), "newmod#2/2": _payload("b")})
    file_plan = [_fp(f"newmod/f{i}.txt") for i in range(25)]
    await _run(llm, _state(), file_plan, monkeypatch)
    prompts = {}
    # _CountingLLM 不存 prompt，改用捕获型跑一遍
    class _Cap:
        captured = {}
        async def ainvoke(self, messages):
            p = messages[-1]["content"]
            for mod in ("newmod#1/2", "newmod#2/2"):
                if f"模块 '{mod}'" in p:
                    _Cap.captured[mod] = p
                    return type("R", (), {"content": _payload(mod.replace("/", "_"))})()
            raise AssertionError("no match")
    await _run(_Cap(), _state(), file_plan, monkeypatch)
    assert "脚手架" in _Cap.captured["newmod#1/2"], "首子批必须带脚手架提示"
    assert "脚手架" not in _Cap.captured["newmod#2/2"], "后续子批绝不重复触发脚手架"


async def test_replan_feedback_disables_cache(monkeypatch):
    """F-3 [L]：执行失败 replan（replan_feedback 非空）绝不吃缓存——人工闸放行残缺
    计划后执行失败，缓存批必须带着 replan 教训真跑（宁慢勿错，回退 pre-U2 行为）。"""
    file_plan = [_fp("ok/ok_main.txt"), _fp("bad/bad_main.txt")]
    llm1 = _CountingLLM({"ok": _payload("ok"), "bad": _payload("bad")},
                        timeout_mods={"bad"})
    _p, failed1, _b, cache1 = await _run(llm1, _state(), file_plan, monkeypatch)
    llm2 = _CountingLLM({"ok": _payload("ok"), "bad": _payload("bad")})
    state2 = _state({"plan_batch_failed_modules": failed1,
                     "plan_batch_cache": cache1,
                     "replan_feedback": "上轮执行失败根因"})
    await _run(llm2, state2, file_plan, monkeypatch)
    assert llm2.calls.get("ok") == 1, "replan 路径缓存必须禁用"


# ═══════════ R35-C：纯覆盖重试前向回退护栏（round35 坐实治本）═══════════
# 坐实：attempt0 全成 12/12→纯覆盖重试丢弃该轮缓存全量重拆→burst 压垮 SiliconFlow→
# attempt0 成功的批反超时被丢→11/12 残缺 fail-fast。治=全成轮也落缓存(site-1)+纯覆盖重试
# 轮某批失败回放上轮成功子任务(护栏,守 F-3 replan 轮不回放)+校验通过清缓存(site-2 兜 F-4)。

async def test_coverage_retry_replays_prev_cache_on_timeout(monkeypatch):
    """护栏：纯覆盖重试(上一轮全成 failed=[])某批本轮 timeout→回放上轮成功缓存不丢模块。
    先真跑争新 covers，仅失败才回放；回放件是已校验成功子任务=合法完整交付不计 failed。"""
    file_plan = [_fp("ok/ok_main.txt")]
    llm1 = _CountingLLM({"ok": _payload("ok")})
    _p1, failed1, _b1, cache1 = await _run(llm1, _state(), file_plan, monkeypatch)
    assert failed1 == [] and len(cache1) == 1
    llm2 = _CountingLLM({"ok": _payload("ok")}, timeout_mods={"ok"})
    state2 = _state({"plan_batch_failed_modules": [], "plan_batch_cache": cache1})
    plan2, failed2, _b2, cache2 = await _run(llm2, state2, file_plan, monkeypatch)
    assert llm2.calls.get("ok") == 1, "先真跑 LLM 争新 covers（非顶部快路径跳过）"
    assert failed2 == [], "超时批回放上轮缓存，模块不丢（不计 failed）"
    assert len(plan2.subtasks) == 1 and len(cache2) == 1


async def test_replan_feedback_round_does_not_replay_cache(monkeypatch):
    """守 F-3（复核 HIGH 修复锚）：执行失败 replan 轮(replan_feedback 非空)绝不回放缓存——
    须带教训真跑，绝不重灌被反馈否决的旧子任务。批失败→照常记账，护栏不介入。"""
    file_plan = [_fp("ok/ok_main.txt")]
    llm1 = _CountingLLM({"ok": _payload("ok")})
    _p1, _f1, _b1, cache1 = await _run(llm1, _state(), file_plan, monkeypatch)
    llm2 = _CountingLLM({"ok": _payload("ok")}, timeout_mods={"ok"})
    state2 = _state({"plan_batch_failed_modules": [], "plan_batch_cache": cache1,
                     "replan_feedback": "上轮执行失败根因（须真跑修）"})
    import pytest
    with pytest.raises(RuntimeError, match="全部"):
        await _run(llm2, state2, file_plan, monkeypatch)


async def test_coverage_retry_sentinel_not_replayed(monkeypatch):
    """护栏：上一轮 bisect 哨兵(无 subtasks)不当兜底回放——无真子任务可补，须照常失败记账。"""
    monkeypatch.setenv("SWARM_PLAN_BATCH_BISECT", "0")
    file_plan = [_fp("ok/a.txt"), _fp("ok/b.txt")]
    llm1 = _CountingLLM({"ok": _payload("ok", 2)})
    _p1, _f1, _b1, cache1 = await _run(llm1, _state(), file_plan, monkeypatch)
    sig = next(iter(cache1))
    cache1[sig] = {"module": "ok", "bisected": True}
    llm2 = _CountingLLM({"ok": _payload("ok", 2)}, timeout_mods={"ok"})
    state2 = _state({"plan_batch_failed_modules": [], "plan_batch_cache": cache1})
    import pytest
    with pytest.raises(RuntimeError, match="全部"):
        await _run(llm2, state2, file_plan, monkeypatch)


async def test_first_round_timeout_without_cache_still_fails(monkeypatch):
    """无上一轮缓存（首轮/全新模块）→无兜底，失败照常记账，护栏绝不伪装成功。"""
    file_plan = [_fp("ok/ok_main.txt")]
    llm = _CountingLLM({"ok": _payload("ok")}, timeout_mods={"ok"})
    import pytest
    with pytest.raises(RuntimeError, match="全部"):
        await _run(llm, _state(), file_plan, monkeypatch)


async def test_plan_persists_cache_on_all_success_ultra(monkeypatch):
    """R35-C site-1（推翻 F-4 no-op 病灶，复核 HIGH 修复锚）：ULTRA 全成轮(failed=[])也把
    缓存落 state 供纯覆盖重试护栏消费。此前 `if _plan_batch_failed else {}` 使全成轮缓存恒
    {}→护栏在其设计场景 no-op。"""
    from swarm.brain.nodes import plan as plan_node
    from swarm.types import Complexity, TaskPlan, SubTask
    import swarm.brain.nodes as nodes

    _CACHE = {"sig1": {"module": "m", "subtasks": [{"id": "st-1"}], "baseline": []}}

    async def _fake_ultra(llm, state, desc, kctx, sctx, fp):
        return (TaskPlan(subtasks=[SubTask(
            id="st-1", description="x", scope={"writable": ["a"], "readable": []})]),
            [], [], dict(_CACHE))   # failed=[] 全成

    monkeypatch.setattr(nodes, "_get_brain_llm", lambda: object())
    monkeypatch.setattr(nodes, "_plan_ultra_batched", _fake_ultra)
    out = await plan_node({
        "task_description": "t", "complexity": Complexity.ULTRA,
        "tech_design_file_plan": [{"path": f"f{i}"} for i in range(40)],
    })
    assert out.get("plan_batch_cache") == _CACHE, "全成轮必须落缓存(非 {})，否则护栏 no-op"


async def test_validate_plan_clears_cache_on_pass(monkeypatch):
    """R35-C site-2（F-4 膨胀兜底）：覆盖+校验【通过】→清空 plan_batch_cache，不让死重随后续
    checkpoint 长途漂流（过闸进 CONFIRM/DISPATCH 无更多 PLAN 回炉轮，缓存无人再消费）。"""
    from swarm.brain.nodes import validate_plan
    from swarm.types import FileScope, SubTask, SubTaskDifficulty, TaskPlan
    import swarm.brain.nodes as nodes

    class _OkLLM:
        async def ainvoke(self, m):
            return type("R", (), {"content": '{"valid": true, "issues": []}'})()

    monkeypatch.setattr(nodes, "_get_brain_llm", lambda: _OkLLM())
    st = SubTask(id="st-1", description="x", difficulty=SubTaskDifficulty.MEDIUM,
                 scope=FileScope(writable=["a"], readable=[]), covers=[REQ_A])
    out = await validate_plan({
        "plan": TaskPlan(subtasks=[st], parallel_groups=[["st-1"]]),
        "task_description": "t", "complexity": "medium", "plan_retry_count": 0,
        "requirement_items": [{"id": REQ_A, "text": "甲", "kind": "functional",
                               "source_quote": "甲", "source": "description"}],
        "plan_batch_cache": {"sig1": {"module": "m", "subtasks": [{"id": "x"}]}},
    })
    assert out.get("plan_valid") is True, "覆盖+软门应通过"
    assert out.get("plan_batch_cache") == {}, "通过必须清空缓存(F-4 膨胀兜底)"


async def test_validate_plan_keeps_cache_on_coverage_fail(monkeypatch):
    """site-2 对称：覆盖未通过(回炉 PLAN)→返回不含该键→state 缓存原样保留，供下一轮护栏消费。"""
    from swarm.brain.nodes import validate_plan
    from swarm.types import FileScope, SubTask, SubTaskDifficulty, TaskPlan

    st = SubTask(id="st-1", description="x", difficulty=SubTaskDifficulty.MEDIUM,
                 scope=FileScope(writable=["a"], readable=[]), covers=[])  # 不覆盖任何需求
    out = await validate_plan({
        "plan": TaskPlan(subtasks=[st], parallel_groups=[["st-1"]]),
        "task_description": "t", "complexity": "medium", "plan_retry_count": 0,
        "requirement_items": [{"id": REQ_A, "text": "甲", "kind": "functional",
                               "source_quote": "甲", "source": "description"}],
        "plan_batch_cache": {"sig1": {"module": "m"}},
    })
    assert out.get("plan_valid") is False, "REQ_A 未覆盖应回炉"
    assert "plan_batch_cache" not in out, "覆盖失败返回不含该键→state 缓存保留（护栏下轮要用）"


async def test_plan_emits_empty_cache_when_all_batches_succeed(monkeypatch):
    """F-4 [L]：批全成轮按自身规则缓存永远无人消费——plan() 落 state 应为 {}，
    不把数十 KB 死重灌进每次 checkpoint（D51 plan 体积病灶同族）。"""
    from swarm.brain.nodes import plan as plan_node
    from swarm.types import Complexity
    import swarm.brain.nodes as nodes

    class _One:
        async def ainvoke(self, messages):
            return type("R", (), {"content": json.dumps({"subtasks": [
                {"id": "st-1", "description": "x",
                 "scope": {"writable": ["a"], "readable": []}}],
                "parallel_groups": [["st-1"]]})})()

    monkeypatch.setattr(nodes, "_get_brain_llm", lambda: _One())
    monkeypatch.setattr(nodes, "_plan_ultra_batched", None)  # 单发路径不该碰它
    out = await plan_node({"task_description": "t", "complexity": Complexity.MEDIUM})
    assert out["plan_batch_cache"] == {}, "非分批路径恒空缓存（always-emit 防粘滞）"
