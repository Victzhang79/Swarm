"""S2-3（task#24）：PRD 覆盖矩阵进 plan validator — 行为测试（禁 getsource）。

定案依据 docs/ACCEPTANCE_DESIGN.md 定案3/§2.5/task#24 指引：
  - validate_plan 内、结构校验+SIMPLE 早退后新增【确定性覆盖维度】：
    ①每个 requirement item 至少被一个子任务 covers；②covers 无悬空引用；
    ③requirement_items 缺失/空 → 跳过校验 + degraded 留痕（诚实降级不阻塞主链）。
  - 失败走现成 D09 通道 plan_validation_feedback（反馈逐条含条目 id+text），
    熔断复用 plan_retry_count/MAX_PLAN_RETRY，绝不另起计数器。
  - 覆盖矩阵不进 state：build_coverage_matrix(plan, requirement_items) 纯函数现算
    （供 task#26/#27 交付报告复用）。
  - PLAN prompt 加法式注入条目清单+covers 纪律：items 空=一字不加（老行为零变化）。
  - _merge_horizontal_subtasks 水平合并必须并集 covers（丢了会误判未覆盖白烧重试）。
"""

from __future__ import annotations

import os

from swarm.brain.nodes import (
    _plan_ultra_batched,
    _requirement_coverage_prompt_block,
    plan,
    validate_plan,
)
from swarm.brain.nodes.shared import _merge_horizontal_subtasks
from swarm.brain.plan_validator import (
    build_coverage_matrix,
    validate_requirement_coverage,
    validate_plan_structure,
)
from swarm.types import (
    Complexity,
    FileScope,
    SubTask,
    SubTaskDifficulty,
    TaskPlan,
)

REQ_A = "req-aaaa1111"
REQ_B = "req-bbbb2222"


def _items():
    return [
        {"id": REQ_A, "text": "系统支持条目一的功能", "kind": "functional",
         "source_quote": "条目一", "source": "description"},
        {"id": REQ_B, "text": "系统支持条目二的数据约束", "kind": "data",
         "source_quote": "条目二", "source": "description"},
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


def _plan_obj(*subtasks):
    return TaskPlan(subtasks=list(subtasks),
                    parallel_groups=[[st.id] for st in subtasks])


class _Resp:
    def __init__(self, content):
        self.content = content


class _FakeLLM:
    """捕获 user prompt 并回吐固定 JSON 的假 LLM（既有 D09 测试同款形态）。"""

    def __init__(self, content='{"valid": true, "issues": []}'):
        self._content = content
        self.captured: list[str] = []

    async def ainvoke(self, messages):
        self.captured.append(messages[1]["content"])
        return _Resp(self._content)


def _clean_env():
    os.environ.pop("SWARM_VALIDATE_PLAN_LLM_GATE", None)
    os.environ.pop("SWARM_VALIDATE_PLAN_COMPLETENESS_GATE", None)


# ─────────────────── build_coverage_matrix（纯函数正反例）───────────────────

def test_matrix_full_coverage():
    p = _plan_obj(_st("st-1", writable=["a"], covers=[REQ_A]),
                  _st("st-2", writable=["b"], covers=[REQ_B, REQ_A]))
    m = build_coverage_matrix(p, _items())
    assert m["total_items"] == 2 and m["covered_items"] == 2
    assert m["uncovered"] == [] and m["dangling_covers"] == {}
    by_id = {it["id"]: it for it in m["items"]}
    assert by_id[REQ_A]["covered_by"] == ["st-1", "st-2"]
    assert by_id[REQ_B]["covered_by"] == ["st-2"]


def test_matrix_uncovered_and_dangling():
    p = _plan_obj(_st("st-1", writable=["a"], covers=[REQ_A, "req-nope0000"]))
    m = build_coverage_matrix(p, _items())
    assert m["covered_items"] == 1
    assert [u["id"] for u in m["uncovered"]] == [REQ_B]
    assert m["uncovered"][0]["text"] == "系统支持条目二的数据约束"
    assert m["dangling_covers"] == {"st-1": ["req-nope0000"]}


def test_matrix_empty_or_malformed_items():
    p = _plan_obj(_st("st-1", writable=["a"]))
    for items in (None, [], ["not-a-dict", {"text": "无 id"}]):
        m = build_coverage_matrix(p, items)
        assert m["total_items"] == 0 and m["uncovered"] == []


# ─────────────────── validate_requirement_coverage ───────────────────

def test_coverage_valid_when_all_items_covered():
    p = _plan_obj(_st("st-1", writable=["a"], covers=[REQ_A, REQ_B]))
    res = validate_requirement_coverage(p, _items())
    assert res.valid and res.issues == []


def test_coverage_uncovered_item_invalid_with_id_and_text():
    p = _plan_obj(_st("st-1", writable=["a"], covers=[REQ_A]))
    res = validate_requirement_coverage(p, _items())
    assert not res.valid
    joined = "\n".join(res.issues)
    assert REQ_B in joined, "issue 必须带未覆盖条目 id（LLM 才知道补什么）"
    assert "系统支持条目二的数据约束" in joined, "issue 必须带条目 text"


def test_coverage_dangling_covers_invalid():
    p = _plan_obj(_st("st-1", writable=["a"], covers=[REQ_A, REQ_B, "req-fake9999"]))
    res = validate_requirement_coverage(p, _items())
    assert not res.valid
    joined = "\n".join(res.issues)
    assert "st-1" in joined and "req-fake9999" in joined


def test_coverage_empty_items_returns_valid():
    """空 items 的"跳过+degraded"是节点侧决策；纯函数如实返回 valid（无可对账项）。"""
    p = _plan_obj(_st("st-1", writable=["a"]))
    assert validate_requirement_coverage(p, []).valid


# ─────────────────── validate_plan 节点接线（D09 回灌 + 降级 + 共存）───────────────────

async def test_validate_plan_uncovered_rejects_with_feedback(monkeypatch):
    """未覆盖条目 → plan_valid=False + feedback 含条目 id/text（走现成 D09 通道）。
    LLM mock 返回 valid:true —— plan_valid=False 只能来自覆盖维度（确定性阻断先于软校验）。"""
    _clean_env()
    import swarm.brain.nodes as nodes
    fake = _FakeLLM()
    monkeypatch.setattr(nodes, "_get_brain_llm", lambda: fake)
    out = await validate_plan({
        "plan": _plan_obj(_st("st-1", writable=["a"], covers=[REQ_A])),
        "task_description": "t",
        "complexity": "medium",
        "plan_retry_count": 0,
        "requirement_items": _items(),
    })
    assert out["plan_valid"] is False
    fb = out["plan_validation_feedback"]
    assert REQ_B in fb and "系统支持条目二的数据约束" in fb
    assert fake.captured == [], "覆盖失败必须在 LLM 软校验之前返回（不与 P6b 各烧一轮）"
    assert "degraded_reasons" not in out, "items 非空未跳过，不得发 degraded"


async def test_validate_plan_dangling_covers_rejects(monkeypatch):
    _clean_env()
    import swarm.brain.nodes as nodes
    monkeypatch.setattr(nodes, "_get_brain_llm", lambda: _FakeLLM())
    out = await validate_plan({
        "plan": _plan_obj(_st("st-1", writable=["a"], covers=[REQ_A, REQ_B, "req-ghost123"])),
        "task_description": "t",
        "complexity": "medium",
        "plan_retry_count": 0,
        "requirement_items": _items(),
    })
    assert out["plan_valid"] is False
    assert "req-ghost123" in out["plan_validation_feedback"]


async def test_validate_plan_full_coverage_passes(monkeypatch):
    _clean_env()
    import swarm.brain.nodes as nodes
    monkeypatch.setattr(nodes, "_get_brain_llm", lambda: _FakeLLM())
    out = await validate_plan({
        "plan": _plan_obj(_st("st-1", writable=["a"], covers=[REQ_A]),
                          _st("st-2", writable=["b"], covers=[REQ_B])),
        "task_description": "t",
        "complexity": "medium",
        "plan_retry_count": 0,
        "requirement_items": _items(),
    })
    assert out["plan_valid"] is True
    assert out["plan_validation_feedback"] == "", "通过即清空，防跨轮粘滞"
    assert "degraded_reasons" not in out


async def test_validate_plan_missing_items_skips_with_degraded(monkeypatch):
    """items 缺失/空（抽取降级）→ 跳过校验 + degraded 追加，不阻塞主链。"""
    _clean_env()
    import swarm.brain.nodes as nodes
    monkeypatch.setattr(nodes, "_get_brain_llm", lambda: _FakeLLM())
    for state_extra in ({}, {"requirement_items": []}):
        out = await validate_plan({
            "plan": _plan_obj(_st("st-1", writable=["a"])),
            "task_description": "t",
            "complexity": "medium",
            "plan_retry_count": 0,
            **state_extra,
        })
        assert out["plan_valid"] is True, "items 缺失绝不硬失败（诚实降级）"
        assert out.get("degraded_reasons") == ["plan_coverage:skipped(no_requirement_items)"]


async def test_validate_plan_coverage_kill_switch_off_skips_with_degraded(monkeypatch):
    """S2 复核 S2：SWARM_PLAN_COVERAGE_GATE=0 泄压阀（对照 SWARM_RUNTIME_SMOKE_ENABLED
    先例）——covers 不服从的存量任务不至于烧光 plan 重试进人工；关闭必可观测。"""
    _clean_env()
    import swarm.brain.nodes as nodes
    monkeypatch.setattr(nodes, "_get_brain_llm", lambda: _FakeLLM())
    monkeypatch.setenv("SWARM_PLAN_COVERAGE_GATE", "0")
    out = await validate_plan({
        "plan": _plan_obj(_st("st-1", writable=["a"])),  # 两条目均未覆盖
        "task_description": "t",
        "complexity": "medium",
        "plan_retry_count": 0,
        "requirement_items": _items(),
    })
    assert out["plan_valid"] is True, "关闸后未覆盖不阻断"
    assert out.get("degraded_reasons") == ["plan_coverage:skipped(disabled)"], "关闭必留痕"


async def test_validate_plan_coverage_gate_default_and_explicit_on(monkeypatch):
    """S2：缺省与显式 '1' 都是闸门全开——未覆盖照常硬拒。"""
    _clean_env()
    import swarm.brain.nodes as nodes
    monkeypatch.setattr(nodes, "_get_brain_llm", lambda: _FakeLLM())
    for env_val in (None, "1"):
        if env_val is None:
            monkeypatch.delenv("SWARM_PLAN_COVERAGE_GATE", raising=False)
        else:
            monkeypatch.setenv("SWARM_PLAN_COVERAGE_GATE", env_val)
        out = await validate_plan({
            "plan": _plan_obj(_st("st-1", writable=["a"], covers=[REQ_A])),
            "task_description": "t",
            "complexity": "medium",
            "plan_retry_count": 0,
            "requirement_items": _items(),
        })
        assert out["plan_valid"] is False, f"env={env_val!r} 闸门应全开"
        assert REQ_B in out["plan_validation_feedback"]


async def test_validate_plan_structure_failure_takes_precedence():
    """既有结构校验失败优先：结构 issue 返回，覆盖维度不参与（不混入 feedback）。"""
    _clean_env()
    bad = _plan_obj(_st("st-1", writable=["a"], depends_on=["st-ghost"]))
    out = await validate_plan({
        "plan": bad,
        "task_description": "t",
        "complexity": "medium",
        "plan_retry_count": 0,
        "requirement_items": _items(),  # 两条目均未覆盖，但结构失败先返回
    })
    assert out["plan_valid"] is False
    fb = out["plan_validation_feedback"]
    assert "st-ghost" in fb
    assert "未被任何子任务覆盖" not in fb, "结构失败时覆盖校验不应叠加（一轮修一类）"


async def test_validate_plan_simple_path_skips_coverage():
    """SIMPLE 快速路径在覆盖维度之前早退（单 trivial 子任务自证覆盖，强校验只会误伤）。"""
    _clean_env()
    out = await validate_plan({
        "plan": _plan_obj(_st("st-1", writable=["a"])),  # covers 空 + items 非空
        "task_description": "t",
        "complexity": Complexity.SIMPLE,
        "plan_retry_count": 0,
        "requirement_items": _items(),
    })
    assert out["plan_valid"] is True


# ─────────────────── PLAN prompt 注入（加法式；空=一字不加）───────────────────

def test_prompt_block_empty_items_is_empty_string():
    assert _requirement_coverage_prompt_block(None) == ""
    assert _requirement_coverage_prompt_block([]) == ""
    assert _requirement_coverage_prompt_block(["bad", {"text": "无id"}]) == ""


def test_prompt_block_contains_ids_and_discipline():
    block = _requirement_coverage_prompt_block(_items())
    assert REQ_A in block and REQ_B in block
    assert "covers" in block and "至少一个子任务" in block
    assert "分批" not in block
    batched = _requirement_coverage_prompt_block(_items(), batched=True)
    assert "本批" in batched, "分批路径须提示只声明本批相关条目"


async def test_plan_prompt_injects_requirement_items(monkeypatch):
    _clean_env()
    fake = _FakeLLM(
        '{"subtasks":[{"id":"st-1","description":"x",'
        '"scope":{"writable":["a"],"readable":[]},"covers":["%s"]}],'
        '"parallel_groups":[["st-1"]]}' % REQ_A
    )
    import swarm.brain.nodes as nodes
    monkeypatch.setattr(nodes, "_get_brain_llm", lambda: fake)
    out = await plan({
        "task_description": "build feature",
        "complexity": Complexity.MEDIUM,
        "requirement_items": _items(),
    })
    prompt = fake.captured[0]
    assert REQ_A in prompt and REQ_B in prompt and "covers" in prompt
    # LLM 声明的 covers 落进 SubTask（types.SubTask.covers 加法字段）
    assert out["plan"].subtasks[0].covers == [REQ_A]


async def test_plan_prompt_unchanged_when_no_items(monkeypatch):
    """items 缺失/空 → prompt 与老行为一字不差（两态互等 + 不含注入块标题）。"""
    _clean_env()
    prompts: list[str] = []

    async def _run(state):
        fake = _FakeLLM(
            '{"subtasks":[{"id":"st-1","description":"x",'
            '"scope":{"writable":["a"],"readable":[]}}],"parallel_groups":[["st-1"]]}'
        )
        import swarm.brain.nodes as nodes
        monkeypatch.setattr(nodes, "_get_brain_llm", lambda: fake)
        await plan(state)
        prompts.append(fake.captured[0])

    base = {"task_description": "build feature", "complexity": Complexity.MEDIUM}
    await _run(dict(base))
    await _run({**base, "requirement_items": []})
    assert prompts[0] == prompts[1], "items 空与缺失的 prompt 必须逐字节一致（零变化）"
    assert "需求条目清单" not in prompts[0]


async def test_plan_batched_prompt_injects_and_covers_survive_merge():
    """ultra 分批路径：批 prompt 含条目清单；covers 穿过 merge_subtask_batches 重编号存活。"""
    fake = _FakeLLM(
        '{"subtasks":[{"id":"st-1","description":"x",'
        '"scope":{"writable":["m/a.txt"],"readable":[]},"covers":["%s","%s"]}]}'
        % (REQ_A, REQ_B)
    )
    state = {
        "tech_design": {},
        "shared_contract_draft": {},
        "project_id": "",
        "requirement_items": _items(),
    }
    file_plan = [{"path": "m/a.txt", "action": "create", "responsibility": "x"}]
    task_plan, failed = await _plan_ultra_batched(
        fake, state, "总需求", {}, "", file_plan)
    assert failed == []
    prompt = fake.captured[0]
    assert REQ_A in prompt and "covers" in prompt and "本批" in prompt
    assert task_plan.subtasks[0].covers == [REQ_A, REQ_B]


# ─────────────────── 水平合并保 covers（task#24 必改点）───────────────────

def test_merge_horizontal_subtasks_unions_covers():
    p = TaskPlan(
        subtasks=[
            _st("st-1", writable=["a"], covers=[REQ_A]),
            _st("st-2", writable=["b"], covers=[REQ_B, REQ_A]),
        ],
        parallel_groups=[["st-1"], ["st-2"]],
    )
    merged = _merge_horizontal_subtasks(p)
    assert len(merged.subtasks) == 1, "同语言无依赖子任务应被水平合并（既有行为）"
    assert merged.subtasks[0].covers == [REQ_A, REQ_B], "covers 必须并集去重，绝不因合并丢失"
    # 合并后计划对同一 items 仍全覆盖（与覆盖校验闭环）
    assert validate_requirement_coverage(merged, _items()).valid


# ─────────────────── 与既有结构校验共存（回归锚）───────────────────

def test_structure_validator_untouched_by_coverage_addition():
    """covers 字段的存在不影响既有结构校验判定（加法兼容）。"""
    p = _plan_obj(_st("st-1", writable=["a"], covers=[REQ_A]))
    res = validate_plan_structure(p)
    assert res.valid, f"带 covers 的合法计划结构校验必须照常通过: {res.issues}"
