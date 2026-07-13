"""P1（round37 龙头）：覆盖未过闸时【只补缺 covers 不全量重拆】的外科补丁 — 行为测试。

定案依据 memory/swarm-e2e-round37-postmortem.md：
  - round37 黑洞真因：VALIDATE 判"未覆盖"→ 唯一下一步是回 PLAN 全量重拆（_plan_ultra_batched
    从恒定 tech_design_file_plan 重拆所有模块），covers 由 LLM 每批现生成→非单调丢弃→
    Round0 只差 2 条也全量重拆→丢 16 条底座→3 轮内结构不收敛=费用黑洞。
  - #6 _merge_prior_covers_by_scope 只并回"已被某子任务声明过"的 covers，对"从未被任何
    子任务声明的底座需求"够不着（plan_validator 边界）。
  - 治本：在 _plan_ultra_batched 之前拦一道——纯覆盖重试（plan_validation_feedback 非空 &
    replan_feedback 空 & 上一版结构合法）时【不重拆】，从上一版 plan 深拷贝 + 确定性剥离
    悬空 covers + 对 uncovered 子集做一次廉价定向 LLM 微调用（挂现有子任务 covers / baseline
    申报），绝不新增子任务、绝不重拆。LLM 失败/无增量→回退全量重拆（零回归）。
    泄压阀 SWARM_PLAN_COVERAGE_TOPUP（对照 SWARM_PLAN_COVERAGE_GATE 先例）。

栈无关：全部用抽象 req/子任务，无任何语言/框架/领域词汇。
"""

from __future__ import annotations

import os

from swarm.brain.nodes import (
    _maybe_surgical_coverage_topup,
    _targeted_coverage_topup,
    plan,
)
from swarm.brain.plan_validator import build_coverage_matrix
from swarm.types import Complexity, FileScope, SubTask, SubTaskDifficulty, TaskPlan

REQ_A = "req-aaaa1111"
REQ_B = "req-bbbb2222"
REQ_C = "req-cccc3333"


def _items():
    return [
        {"id": REQ_A, "text": "系统支持条目一", "kind": "functional",
         "source_quote": "一", "source": "description"},
        {"id": REQ_B, "text": "系统支持条目二", "kind": "data",
         "source_quote": "二", "source": "description"},
        {"id": REQ_C, "text": "系统支持条目三（存量能力）", "kind": "other",
         "source_quote": "三", "source": "description"},
    ]


def _st(sid, writable=None, covers=None, depends_on=None, desc="do"):
    return SubTask(
        id=sid,
        description=desc,
        difficulty=SubTaskDifficulty.MEDIUM,
        scope=FileScope(writable=list(writable or []), readable=[]),
        covers=list(covers or []),
        depends_on=list(depends_on or []),
    )


def _plan(*subtasks):
    return TaskPlan(subtasks=list(subtasks),
                    parallel_groups=[[st.id] for st in subtasks])


class _Resp:
    def __init__(self, content):
        self.content = content


class _FakeLLM:
    """捕获 prompt 并回吐固定 JSON 的假 LLM（既有 D09/覆盖测试同款形态）。"""

    def __init__(self, content="{}"):
        self._content = content
        self.captured: list[str] = []

    async def ainvoke(self, messages):
        self.captured.append(messages[-1]["content"])
        return _Resp(self._content)


class _BoomLLM:
    """必抛的假 LLM——验证补丁失败时回退（返回 None）而非炸链。"""

    def __init__(self):
        self.captured: list[str] = []

    async def ainvoke(self, messages):
        raise RuntimeError("LLM boom")


def _valid_ids():
    return {REQ_A, REQ_B, REQ_C}


# ─────────────────── _targeted_coverage_topup 核心（补覆盖不重拆）───────────────────

async def test_topup_assigns_uncovered_to_existing_subtask():
    """uncovered 条目挂到 LLM 指定的【现有】子任务 covers；子任务数不变、无重拆。"""
    prior = _plan(_st("st-1", writable=["a"], covers=[REQ_A]),
                  _st("st-2", writable=["b"], covers=[]))
    uncovered = [{"id": REQ_B, "text": "系统支持条目二"}]
    fake = _FakeLLM('{"assignments":[{"req_id":"%s","subtask_id":"st-2"}],'
                    '"baseline_covered":[]}' % REQ_B)
    res = await _targeted_coverage_topup(fake, prior, uncovered, _valid_ids())
    assert res is not None
    new_plan, baseline = res
    assert len(new_plan.subtasks) == 2, "绝不新增/删除子任务"
    by_id = {s.id: s for s in new_plan.subtasks}
    assert REQ_B in by_id["st-2"].covers, "uncovered 挂到指定现有子任务 covers"
    # 补齐后整计划对 items 全覆盖（与覆盖校验闭环）
    m = build_coverage_matrix(new_plan, _items(), baseline)
    assert {u["id"] for u in m["uncovered"]} == {REQ_C}, "仅补了 B，C 仍未覆盖（本例未申报）"


async def test_topup_declares_baseline_for_existing_capability():
    """存量能力型 uncovered → LLM 走 baseline_covered 申报（P3 落点），covers 不动。"""
    prior = _plan(_st("st-1", writable=["a"], covers=[REQ_A, REQ_B]))
    uncovered = [{"id": REQ_C, "text": "系统支持条目三（存量能力）"}]
    fake = _FakeLLM('{"assignments":[],"baseline_covered":'
                    '[{"id":"%s","reason":"现有 X 模块已实现"}]}' % REQ_C)
    res = await _targeted_coverage_topup(fake, prior, uncovered, _valid_ids())
    assert res is not None
    new_plan, baseline = res
    assert [e["id"] for e in baseline] == [REQ_C]
    assert baseline[0]["reason"], "baseline 申报必带 reason"
    # 子任务 covers 未被 baseline 分支污染
    assert new_plan.subtasks[0].covers == [REQ_A, REQ_B]
    m = build_coverage_matrix(new_plan, _items(), baseline)
    assert m["uncovered"] == [], "assign+baseline 合起来全覆盖"


async def test_topup_preserves_prior_baseline_declarations():
    """上一轮已申报的 baseline 单调保留（并集），不被本轮覆盖丢弃。"""
    prior = _plan(_st("st-1", writable=["a"], covers=[REQ_A]))
    uncovered = [{"id": REQ_C, "text": "条目三"}]
    fake = _FakeLLM('{"assignments":[],"baseline_covered":'
                    '[{"id":"%s","reason":"存量满足 C"}]}' % REQ_C)
    res = await _targeted_coverage_topup(
        fake, prior, uncovered, _valid_ids(),
        prior_baseline=[{"id": REQ_B, "reason": "上一轮已申报 B"}])
    assert res is not None
    _new_plan, baseline = res
    ids = {e["id"] for e in baseline}
    assert ids == {REQ_B, REQ_C}, "上一轮 B + 本轮 C 并集，绝不丢已申报"


async def test_topup_deterministic_strips_dangling_covers():
    """悬空 covers（指向不存在 req）被确定性剥离——零 LLM 也能做的确定性净化。"""
    prior = _plan(_st("st-1", writable=["a"], covers=[REQ_A, "req-ghost999"]))
    # 无 uncovered（A 已覆盖），仅悬空——不该调 LLM
    uncovered: list[dict] = []
    fake = _FakeLLM('{"assignments":[],"baseline_covered":[]}')
    res = await _targeted_coverage_topup(fake, prior, uncovered, {REQ_A})
    assert res is not None
    new_plan, _baseline = res
    assert new_plan.subtasks[0].covers == [REQ_A], "悬空 covers 被剥离，合法 covers 保留"
    assert fake.captured == [], "无 uncovered 时纯确定性净化，不烧 LLM"


async def test_topup_never_adds_new_subtasks_even_if_llm_returns_them():
    """LLM 越权吐新子任务/未知 subtask_id → 一律忽略，绝不重拆。"""
    prior = _plan(_st("st-1", writable=["a"], covers=[REQ_A]))
    uncovered = [{"id": REQ_B, "text": "条目二"}]
    # 指向不存在的 st-99 + 顶层 subtasks 越权字段
    fake = _FakeLLM('{"assignments":[{"req_id":"%s","subtask_id":"st-99"}],'
                    '"subtasks":[{"id":"st-2","description":"越权"}],'
                    '"baseline_covered":[]}' % REQ_B)
    res = await _targeted_coverage_topup(fake, prior, uncovered, _valid_ids())
    # 未知 subtask_id 无处可挂 + 无 baseline → 无有效增量 → 回退（None）
    assert res is None, "无有效增量必须回退全量重拆，不返回原地踏步的空补丁"


async def test_topup_ignores_dangling_and_out_of_set_assignments():
    """assign 的 req_id 不在 valid/uncovered 集 → 忽略（不引入臆造覆盖）。"""
    prior = _plan(_st("st-1", writable=["a"], covers=[REQ_A]),
                  _st("st-2", writable=["b"], covers=[]))
    uncovered = [{"id": REQ_B, "text": "条目二"}]
    fake = _FakeLLM('{"assignments":['
                    '{"req_id":"req-fake0000","subtask_id":"st-2"},'
                    '{"req_id":"%s","subtask_id":"st-2"}],"baseline_covered":[]}' % REQ_B)
    res = await _targeted_coverage_topup(fake, prior, uncovered, _valid_ids())
    assert res is not None
    new_plan, _baseline = res
    by_id = {s.id: s for s in new_plan.subtasks}
    assert "req-fake0000" not in by_id["st-2"].covers, "臆造 req 不得挂上"
    assert REQ_B in by_id["st-2"].covers


async def test_topup_does_not_mutate_prior_plan_in_place():
    """深拷贝：补丁绝不原地改 state 里的上一版 plan（防 replan 认领/别名 bug）。"""
    prior = _plan(_st("st-1", writable=["a"], covers=[REQ_A]),
                  _st("st-2", writable=["b"], covers=[]))
    uncovered = [{"id": REQ_B, "text": "条目二"}]
    fake = _FakeLLM('{"assignments":[{"req_id":"%s","subtask_id":"st-2"}],'
                    '"baseline_covered":[]}' % REQ_B)
    res = await _targeted_coverage_topup(fake, prior, uncovered, _valid_ids())
    assert res is not None
    assert prior.subtasks[1].covers == [], "原 plan 的 st-2 covers 必须仍为空（未被原地改）"


async def test_topup_returns_none_on_llm_failure():
    """LLM 抛异常 → 返回 None（调用方回退全量重拆，绝不炸链）。"""
    prior = _plan(_st("st-1", writable=["a"], covers=[REQ_A]))
    uncovered = [{"id": REQ_B, "text": "条目二"}]
    res = await _targeted_coverage_topup(_BoomLLM(), prior, uncovered, _valid_ids())
    assert res is None


# ─────────────────── P3：棕地存量 baseline 接地（现有项目结构进 topup prompt）───────────────────

async def test_topup_injects_project_structure_for_brownfield_baseline():
    """P3：现有项目结构 + 棕地 baseline 框架注入 topup prompt，让 LLM 能对照存量代码申报。"""
    prior = _plan(_st("st-1", writable=["a"], covers=[REQ_A]))
    uncovered = [{"id": REQ_C, "text": "系统需要基础认证能力"}]
    fake = _FakeLLM('{"assignments":[],"baseline_covered":'
                    '[{"id":"%s","reason":"现有 auth/TokenService.x 已实现"}]}' % REQ_C)
    struct = "- auth/TokenService.x  (符号: issue, verify)\n- rbac/RoleGuard.x"
    res = await _targeted_coverage_topup(
        fake, prior, uncovered, _valid_ids(), project_structure=struct)
    assert res is not None
    prompt = fake.captured[0]
    assert "auth/TokenService.x" in prompt, "现有项目结构必须进 prompt 作 baseline 接地依据"
    assert "棕地" in prompt and "baseline" in prompt, "棕地 baseline 框架必须注入"
    _new_plan, baseline = res
    assert [e["id"] for e in baseline] == [REQ_C]


async def test_topup_no_brownfield_block_when_structure_empty():
    """greenfield/无结构 → 不注入棕地块（加法式，空结构一字不加）。"""
    prior = _plan(_st("st-1", writable=["a"], covers=[REQ_A]))
    uncovered = [{"id": REQ_B, "text": "条目二"}]
    fake = _FakeLLM('{"assignments":[{"req_id":"%s","subtask_id":"st-1"}],'
                    '"baseline_covered":[]}' % REQ_B)
    res = await _targeted_coverage_topup(
        fake, prior, uncovered, _valid_ids(), project_structure="")
    assert res is not None
    assert "棕地" not in fake.captured[0], "无结构时不注入棕地框架"


# ─────────────────── _maybe_surgical_coverage_topup 闸门（何时启用外科路径）───────────────────

def _clean_env():
    os.environ.pop("SWARM_PLAN_COVERAGE_TOPUP", None)


async def test_maybe_topup_skips_when_not_coverage_retry(monkeypatch):
    """无 plan_validation_feedback（首规划）→ 不走外科路径（None）。"""
    _clean_env()
    out = await _maybe_surgical_coverage_topup({
        "plan": _plan(_st("st-1", writable=["a"], covers=[REQ_A])),
        "requirement_items": _items(),
    })
    assert out is None


async def test_maybe_topup_skips_on_replan_feedback(monkeypatch):
    """执行失败 replan（replan_feedback 非空）→ 必须真跑，不走外科补齐（守 F-3）。"""
    _clean_env()
    out = await _maybe_surgical_coverage_topup({
        "plan": _plan(_st("st-1", writable=["a"], covers=[REQ_A])),
        "requirement_items": _items(),
        "complexity": "ultra",
        "plan_validation_feedback": "未覆盖: req-bbbb2222",
        "replan_feedback": "上轮执行失败：编译错误",
    })
    assert out is None


async def test_maybe_topup_skips_non_ultra(monkeypatch):
    """收窄到 ULTRA：MEDIUM 覆盖重试不走外科路径（保 #T3 增量修补重拆，零回归）。"""
    _clean_env()
    out = await _maybe_surgical_coverage_topup({
        "plan": _plan(_st("st-1", writable=["a"], covers=[REQ_A])),
        "requirement_items": _items(),
        "complexity": "medium",
        "plan_validation_feedback": "未覆盖: req-bbbb2222",
    })
    assert out is None


async def test_maybe_topup_bails_when_module_decompose_failed(monkeypatch):
    """复核 CONFIRMED#1：上一轮有整模块分解失败 → 不走外科补齐（缺真模块，必须全量重拆真跑），
    保住 round29 真因4 的 plan_batch_failed_modules fail-fast 信号不被抹。"""
    _clean_env()
    out = await _maybe_surgical_coverage_topup({
        "plan": _plan(_st("st-1", writable=["a"], covers=[REQ_A])),
        "requirement_items": _items(),
        "complexity": "ultra",
        "plan_validation_feedback": "未覆盖: req-bbbb2222",
        "plan_batch_failed_modules": [{"name": "module-B", "files": 5, "reason": "timeout"}],
    })
    assert out is None


async def test_plan_node_topup_preserves_batch_cache(monkeypatch):
    """复核 CONFIRMED#2：topup 路径原样带走 state 的 plan_batch_cache（R35-C 护栏缓存），
    不被本地恒空 {} 覆盖——否则后续回退全量重拆时已成功批的缓存丢失、需重烧。"""
    _clean_env()
    import swarm.brain.nodes as nodes
    topup_fake = _FakeLLM('{"assignments":[{"req_id":"%s","subtask_id":"st-keep-2"}],'
                          '"baseline_covered":[]}' % REQ_B)
    monkeypatch.setattr(nodes, "_get_brain_llm", lambda: topup_fake)
    monkeypatch.setattr(nodes, "_get_brain_fallback_llm", lambda: None)
    _seed_cache = {"sig-x": {"module": "m", "subtasks": [{"id": "s"}], "baseline": []}}
    prior = TaskPlan(
        subtasks=[_st("st-keep-1", writable=["a"], covers=[REQ_A], desc="甲"),
                  _st("st-keep-2", writable=["b"], covers=[], depends_on=["st-keep-1"],
                      desc="乙")],
        parallel_groups=[["st-keep-1"], ["st-keep-2"]])
    out = await plan({
        "task_description": "big ultra task",
        "complexity": Complexity.ULTRA,
        "requirement_items": [_items()[0], _items()[1]],  # 仅 A/B，B 待补
        "tech_design_file_plan": [{"path": f"m/f{i}.txt", "action": "create"}
                                  for i in range(40)],
        "plan": prior,
        "plan_retry_count": 1,
        "plan_validation_feedback": f"- 未覆盖: {REQ_B}",
        "baseline_covered": [],
        "plan_batch_cache": _seed_cache,
    })
    assert out["plan_batch_cache"] == _seed_cache, "topup 必须原样保留 R35-C 缓存"
    assert out["plan_batch_failed_modules"] == [], "无失败模块时如实空"


async def test_maybe_topup_skips_when_structure_invalid(monkeypatch):
    """上一版 plan 结构非法（悬空依赖）→ 补覆盖救不了，回退全量重拆（None）。"""
    _clean_env()
    bad = _plan(_st("st-1", writable=["a"], covers=[REQ_A], depends_on=["st-ghost"]))
    out = await _maybe_surgical_coverage_topup({
        "plan": bad,
        "requirement_items": _items(),
        "complexity": "ultra",
        "plan_validation_feedback": "未覆盖: req-bbbb2222",
    })
    assert out is None


async def test_maybe_topup_skips_when_killswitch_off(monkeypatch):
    """SWARM_PLAN_COVERAGE_TOPUP=0 泄压阀 → 回退旧全量重拆行为（None）。"""
    _clean_env()
    monkeypatch.setenv("SWARM_PLAN_COVERAGE_TOPUP", "0")
    out = await _maybe_surgical_coverage_topup({
        "plan": _plan(_st("st-1", writable=["a"], covers=[REQ_A])),
        "requirement_items": _items(),
        "plan_validation_feedback": "未覆盖: req-bbbb2222",
    })
    assert out is None


async def test_maybe_topup_engages_and_converges_on_coverage_retry(monkeypatch):
    """纯覆盖重试 + 结构合法 + 有 uncovered → 走外科路径，一次微调用补齐（不重拆）。"""
    _clean_env()
    import swarm.brain.nodes as nodes
    fake = _FakeLLM('{"assignments":[{"req_id":"%s","subtask_id":"st-2"}],'
                    '"baseline_covered":[{"id":"%s","reason":"存量满足 C"}]}'
                    % (REQ_B, REQ_C))
    monkeypatch.setattr(nodes, "_get_brain_llm", lambda: fake)
    monkeypatch.setattr(nodes, "_get_brain_fallback_llm", lambda: None)
    prior = _plan(_st("st-1", writable=["a"], covers=[REQ_A]),
                  _st("st-2", writable=["b"], covers=[]))
    out = await _maybe_surgical_coverage_topup({
        "plan": prior,
        "requirement_items": _items(),
        "complexity": "ultra",
        "plan_validation_feedback": "未覆盖: req-bbbb2222; req-cccc3333",
    })
    assert out is not None
    new_plan, baseline = out
    assert len(new_plan.subtasks) == 2, "无重拆：子任务数与上一版一致"
    m = build_coverage_matrix(new_plan, _items(), baseline)
    assert m["uncovered"] == [], "一次外科补齐即收敛（B 挂 covers，C 走 baseline）"


# ─────────────────── plan() 节点集成：ULTRA 覆盖重试走外科路径不重拆 ───────────────────

async def test_plan_node_ultra_coverage_retry_uses_topup_not_redecompose(monkeypatch):
    """完整 plan() 节点：ULTRA 覆盖重试命中外科补齐——复用上一版子任务 id、不触发全量重拆。

    断言"没重拆"的确定性证据：返回 plan 的子任务 id 集与上一版【完全一致】（全量重拆会
    经 merge_subtask_batches 重编号 st-N，id 必变），且 uncovered 收敛为空。"""
    _clean_env()
    import swarm.brain.nodes as nodes
    # 若误走全量重拆，_plan_ultra_batched 会调这个 fake 产生【新 id】子任务；命中 topup 则不调它。
    redecompose_fake = _FakeLLM(
        '{"subtasks":[{"id":"st-NEW","description":"全量重拆产物",'
        '"scope":{"writable":["z"],"readable":[]},"covers":[]}],'
        '"parallel_groups":[["st-NEW"]]}')
    topup_fake = _FakeLLM('{"assignments":[{"req_id":"%s","subtask_id":"st-keep-2"}],'
                          '"baseline_covered":[{"id":"%s","reason":"存量满足 C"}]}'
                          % (REQ_B, REQ_C))
    # plan() 先算 topup（_get_brain_llm 用于 topup），命中则 llm 永不被建下游重拆调用。
    monkeypatch.setattr(nodes, "_get_brain_llm", lambda: topup_fake)
    monkeypatch.setattr(nodes, "_get_brain_fallback_llm", lambda: None)
    # st-keep-2 依赖 st-keep-1 → 不被 _merge_horizontal 合并，id 保持稳定可断言
    prior = TaskPlan(
        subtasks=[_st("st-keep-1", writable=["a"], covers=[REQ_A], desc="保留甲"),
                  _st("st-keep-2", writable=["b"], covers=[], depends_on=["st-keep-1"],
                      desc="保留乙")],
        parallel_groups=[["st-keep-1"], ["st-keep-2"]])
    out = await plan({
        "task_description": "big ultra task",
        "complexity": Complexity.ULTRA,
        "requirement_items": _items(),
        "tech_design_file_plan": [{"path": f"m/f{i}.txt", "action": "create"}
                                  for i in range(40)],  # >30 触发原分批重拆
        "plan": prior,
        "plan_retry_count": 1,
        "plan_validation_feedback": f"- 未覆盖: {REQ_B}; {REQ_C}",
        "baseline_covered": [],
    })
    got = out["plan"]
    got_ids = {s.id for s in got.subtasks}
    assert "st-NEW" not in got_ids, "绝不触发全量重拆（重拆 fake 的新 id 不应出现）"
    # R48-1（v0.9.38+）：收尾器会为 file_plan 无主孤儿确定性新建 st-fileplan-* 承接
    # 子任务——P1 复用语义不变（原 id 全保留、无重编号），新增承接是设计行为。
    assert {"st-keep-1", "st-keep-2"} <= got_ids, "复用上一版子任务 id，无重编号"
    _extras = got_ids - {"st-keep-1", "st-keep-2"}
    assert all(x.startswith(("st-fileplan-", "st-contract-", "st-scaffold-"))
               for x in _extras), f"非收尾器来源的意外新子任务: {_extras}"
    assert redecompose_fake.captured == [], "重拆 LLM 一次都不该被调"
    m = build_coverage_matrix(got, _items(), out.get("baseline_covered"))
    assert m["uncovered"] == [], "外科补齐后覆盖收敛"
    assert out.get("plan_generation_failed") is False
