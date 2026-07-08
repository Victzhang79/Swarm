"""R31 治本批（E2E_ROUND31_REGISTER.md T1-T3）：覆盖闸门"存量已满足"申报通道。

取证（round31, task bc495876）：棕地项目 PRD 必然描述基线已有能力，PLAN 拒绝为其造
子任务（工程判断合理），而覆盖闸门只认"子任务 covers"一种覆盖方式 → 确定性死锁烧光
MAX_PLAN_RETRY 进人工闸。三条治本（全部通用多栈，零框架/领域词）：

  T1 baseline_covered 申报通道：
     - PLAN LLM 在计划 JSON 顶层申报 [{"id","reason"}]（reason 必填，fail-closed）；
     - ★结构性防丢：落独立 state 键、绝不挂 TaskPlan 字段——plan 的全部变异重构造路径
       （batched merge/revision/resplit/水平合并）天然碰不到它，消灭 v0.9.23 F1
       "变异路径丢字段"整类复发★；
     - 覆盖判定 = 子任务 covers ∪ 合法 baseline 申报；
     - 防洗白：申报条目仍生成运行时验收断言（_generate_acceptance_assertions 消费全量
       requirement_items，零改动即闭环），假申报 → acceptance_failed 回灌。
  T2 臆造 ID 近邻提示：dangling covers/baseline ID 与清单唯一近邻 → issue 文本点名候选
     （确定性 difflib，绝不自动改写）。
  T3 D09 回灌增量修补：重试 prompt 注入上一版 plan 摘要（子任务+covers+baseline），
     指令"保留已通过部分只修补点名问题"，治全量重拆掷骰子不收敛。
"""

from __future__ import annotations

import asyncio
import json
import logging
import os

from swarm.brain.nodes import (
    _plan_ultra_batched,
    _requirement_coverage_prompt_block,
    plan,
    validate_plan,
)
from swarm.brain.plan_validator import (
    build_coverage_matrix,
    normalize_baseline_covered,
    validate_requirement_coverage,
)
from swarm.brain.requirements_extract import extract_requirements
from swarm.types import (
    Complexity,
    FileScope,
    SubTask,
    SubTaskDifficulty,
    TaskPlan,
)

REQ_A = "req-aaaa1111"
REQ_B = "req-bbbb2222"
REQ_NEAR = "req-72fd98fb"  # round31 实证：LLM 臆造 req-72fd9811（尾 2 字符差）


def _items(extra=None):
    base = [
        {"id": REQ_A, "text": "系统支持条目一的功能", "kind": "functional",
         "source_quote": "条目一", "source": "description"},
        {"id": REQ_B, "text": "系统支持条目二的数据约束", "kind": "data",
         "source_quote": "条目二", "source": "description"},
    ]
    return base + list(extra or [])


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


def _clean_env():
    os.environ.pop("SWARM_VALIDATE_PLAN_LLM_GATE", None)
    os.environ.pop("SWARM_VALIDATE_PLAN_COMPLETENESS_GATE", None)
    os.environ.pop("SWARM_PLAN_COVERAGE_GATE", None)


class _FakeLLM:
    def __init__(self, content='{"valid": true, "issues": []}'):
        self._content = content
        self.captured: list[str] = []

    async def ainvoke(self, messages):
        self.captured.append(messages[-1]["content"])
        return type("R", (), {"content": self._content})()


# ─────────────── T1: normalize_baseline_covered（纯函数正反例）───────────────

def test_normalize_coerces_dedupes_and_drops_garbage():
    raw = [
        {"id": REQ_A, "reason": "现有代码已实现"},
        REQ_B,                                   # 裸字符串 → 补空 reason
        {"id": REQ_A, "reason": "重复申报后到者"},  # 按 id 去重保首
        {"id": "  ", "reason": "空 id 丢弃"},
        {"reason": "无 id 丢弃"},
        42,                                       # 非法类型丢弃
    ]
    out = normalize_baseline_covered(raw)
    assert out == [
        {"id": REQ_A, "reason": "现有代码已实现"},
        {"id": REQ_B, "reason": ""},
    ]


def test_normalize_none_and_nonlist_to_empty():
    assert normalize_baseline_covered(None) == []
    assert normalize_baseline_covered("req-aaaa1111") == []
    assert normalize_baseline_covered({"id": REQ_A}) == []


def test_normalize_reason_bounded():
    out = normalize_baseline_covered([{"id": REQ_A, "reason": "x" * 1000}])
    assert len(out[0]["reason"]) <= 300, "reason 必须有界（防 prompt/payload 膨胀）"


# ─────────────── T1: build_coverage_matrix 的 baseline 维度 ───────────────

def test_matrix_baseline_declaration_counts_as_covered():
    p = _plan_obj(_st("st-1", writable=["a"], covers=[REQ_A]))
    m = build_coverage_matrix(
        p, _items(), baseline_covered=[{"id": REQ_B, "reason": "存量已有"}])
    assert m["covered_items"] == 2 and m["uncovered"] == []
    assert m["baseline_covered"] == [{"id": REQ_B, "reason": "存量已有"}]
    assert m["dangling_baseline"] == []


def test_matrix_baseline_dangling_id_not_covered():
    p = _plan_obj(_st("st-1", writable=["a"], covers=[REQ_A]))
    m = build_coverage_matrix(
        p, _items(), baseline_covered=[{"id": "req-ghost123", "reason": "x"}])
    assert m["covered_items"] == 1
    assert [u["id"] for u in m["uncovered"]] == [REQ_B]
    assert m["dangling_baseline"] == ["req-ghost123"]


def test_matrix_baseline_empty_reason_does_not_cover():
    """fail-closed：无理由的申报不算覆盖（洗白通道必须带依据）。"""
    p = _plan_obj(_st("st-1", writable=["a"], covers=[REQ_A]))
    m = build_coverage_matrix(
        p, _items(), baseline_covered=[{"id": REQ_B, "reason": "  "}])
    assert [u["id"] for u in m["uncovered"]] == [REQ_B]


def test_matrix_old_signature_behavior_unchanged():
    """不传 baseline（既有全部调用点/老 checkpoint）→ 旧键值不变，新键为空。"""
    p = _plan_obj(_st("st-1", writable=["a"], covers=[REQ_A]))
    m = build_coverage_matrix(p, _items())
    assert m["total_items"] == 2 and m["covered_items"] == 1
    assert [u["id"] for u in m["uncovered"]] == [REQ_B]
    assert m["baseline_covered"] == [] and m["dangling_baseline"] == []


# ─────────────── T1: validate_requirement_coverage 判定与文案 ───────────────

def test_coverage_valid_when_baseline_declares_remainder():
    p = _plan_obj(_st("st-1", writable=["a"], covers=[REQ_A]))
    res = validate_requirement_coverage(
        p, _items(), baseline_covered=[{"id": REQ_B, "reason": "现有实现满足"}])
    assert res.valid, res.issues


def test_coverage_baseline_empty_reason_rejected_with_specific_issue():
    p = _plan_obj(_st("st-1", writable=["a"], covers=[REQ_A]))
    res = validate_requirement_coverage(
        p, _items(), baseline_covered=[{"id": REQ_B, "reason": ""}])
    assert not res.valid
    joined = "; ".join(res.issues)
    assert REQ_B in joined and "reason" in joined, "必须点名缺理由的申报（D09 反馈可修性）"


def test_coverage_baseline_dangling_rejected():
    p = _plan_obj(_st("st-1", writable=["a"], covers=[REQ_A, REQ_B]))
    res = validate_requirement_coverage(
        p, _items(), baseline_covered=[{"id": "req-ghost123", "reason": "x"}])
    assert not res.valid
    assert any("baseline_covered" in i and "req-ghost123" in i for i in res.issues)


def test_uncovered_issue_teaches_baseline_channel():
    """未覆盖 issue 文案必须告知申报通道（否则 LLM 不知道有此出口，round31 死锁复现）。"""
    p = _plan_obj(_st("st-1", writable=["a"], covers=[REQ_A]))
    res = validate_requirement_coverage(p, _items())
    assert not res.valid
    assert any(REQ_B in i and "baseline_covered" in i for i in res.issues)


# ─────────────── T2: 臆造 ID 近邻提示（确定性，绝不自动改写）───────────────

def test_dangling_covers_near_miss_hint():
    items = _items(extra=[{
        "id": REQ_NEAR, "text": "近邻条目", "kind": "functional",
        "source_quote": "近邻", "source": "description"}])
    p = _plan_obj(
        _st("st-1", writable=["a"], covers=[REQ_A, REQ_B, REQ_NEAR]),
        _st("st-2", writable=["b"], covers=["req-72fd9811"]),  # round31 实证臆造形态
    )
    res = validate_requirement_coverage(p, items)
    assert not res.valid
    hint = [i for i in res.issues if "req-72fd9811" in i]
    assert hint and REQ_NEAR in hint[0], "唯一近邻必须被点名供 LLM 自纠"


def test_dangling_far_id_gets_no_misleading_hint():
    p = _plan_obj(_st("st-1", writable=["a"], covers=[REQ_A, REQ_B, "xyz-000"]))
    res = validate_requirement_coverage(p, _items())
    assert not res.valid
    bad = [i for i in res.issues if "xyz-000" in i]
    assert bad and "可能想引用" not in bad[0], "无近邻绝不瞎提示（误导比不提示更糟）"


def test_baseline_dangling_also_gets_near_miss_hint():
    items = _items(extra=[{
        "id": REQ_NEAR, "text": "近邻条目", "kind": "functional",
        "source_quote": "近邻", "source": "description"}])
    p = _plan_obj(_st("st-1", writable=["a"], covers=[REQ_A, REQ_B, REQ_NEAR]))
    res = validate_requirement_coverage(
        p, items, baseline_covered=[{"id": "req-72fd9811", "reason": "x"}])
    assert not res.valid
    hint = [i for i in res.issues if "req-72fd9811" in i]
    assert hint and REQ_NEAR in hint[0]


# ─────────────── T1: prompt 纪律块 ───────────────

def test_prompt_block_contains_baseline_channel_discipline():
    block = _requirement_coverage_prompt_block(_items())
    assert "baseline_covered" in block and "reason" in block
    assert "验收" in block, "必须告知申报会被运行时验收兜底（诚实申报威慑）"


def test_prompt_block_empty_items_still_empty_string():
    assert _requirement_coverage_prompt_block([]) == ""
    assert _requirement_coverage_prompt_block(None) == ""


# ─────────────── T1: plan() 落独立 state 键（always-emit 防粘滞）───────────────

async def test_plan_writes_baseline_covered_state_key(monkeypatch):
    _clean_env()
    fake = _FakeLLM(
        '{"subtasks":[{"id":"st-1","description":"x",'
        '"scope":{"writable":["a"],"readable":[]},"covers":["%s"]}],'
        '"parallel_groups":[["st-1"]],'
        '"baseline_covered":[{"id":"%s","reason":"现有代码已满足"}]}' % (REQ_A, REQ_B)
    )
    import swarm.brain.nodes as nodes
    monkeypatch.setattr(nodes, "_get_brain_llm", lambda: fake)
    out = await plan({
        "task_description": "build feature",
        "complexity": Complexity.MEDIUM,
        "requirement_items": _items(),
    })
    assert out["baseline_covered"] == [{"id": REQ_B, "reason": "现有代码已满足"}]
    assert not hasattr(out["plan"], "baseline_covered"), \
        "结构性防丢定案：申报绝不挂 TaskPlan 字段（变异路径天然碰不到）"


async def test_plan_always_emits_key_even_without_declaration(monkeypatch):
    """LLM 未申报 → 恒发 []（last-write-wins 刷掉上一轮申报，防跨重试粘滞）。"""
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
    assert out["baseline_covered"] == []


# ─────────────── T1: ultra 分批路径申报并集 ───────────────

async def test_plan_batched_unions_baseline_declarations():
    fake = _FakeLLM(
        '{"subtasks":[{"id":"st-1","description":"x",'
        '"scope":{"writable":["m/a.txt"],"readable":[]},"covers":["%s"]}],'
        '"baseline_covered":[{"id":"%s","reason":"存量模块已有"}]}' % (REQ_A, REQ_B)
    )
    state = {
        "tech_design": {},
        "shared_contract_draft": {},
        "project_id": "",
        "requirement_items": _items(),
    }
    file_plan = [{"path": "m/a.txt", "action": "create", "responsibility": "x"}]
    task_plan, failed, baseline, _cache = await _plan_ultra_batched(
        fake, state, "总需求", {}, "", file_plan)
    assert failed == []
    assert baseline == [{"id": REQ_B, "reason": "存量模块已有"}]
    assert task_plan.subtasks[0].covers == [REQ_A]


# ─────────────── T1: validate_plan 消费 state 键 ───────────────

async def test_validate_plan_baseline_declared_passes(monkeypatch):
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
        "baseline_covered": [{"id": REQ_B, "reason": "现有实现满足"}],
    })
    assert out["plan_valid"] is True, out.get("plan_validation_feedback")


async def test_validate_plan_baseline_empty_reason_fails_with_feedback(monkeypatch):
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
        "baseline_covered": [{"id": REQ_B, "reason": ""}],
    })
    assert out["plan_valid"] is False
    assert "reason" in out["plan_validation_feedback"]
    assert fake.captured == [], "覆盖失败仍须先于 LLM 软校验返回（不烧 P6b）"


# ─────────────── T3: D09 回灌增量修补 ───────────────

async def test_plan_retry_injects_previous_plan_summary(monkeypatch):
    """校验失败重试 → prompt 含上一版 plan 摘要（子任务 id+covers）+ 增量修补纪律。"""
    _clean_env()
    fake = _FakeLLM(
        '{"subtasks":[{"id":"st-1","description":"x",'
        '"scope":{"writable":["a"],"readable":[]},"covers":["%s"]}],'
        '"parallel_groups":[["st-1"]]}' % REQ_A
    )
    import swarm.brain.nodes as nodes
    monkeypatch.setattr(nodes, "_get_brain_llm", lambda: fake)
    prev = _plan_obj(
        _st("st-old-1", writable=["a"], covers=[REQ_A], desc="旧子任务甲"),
        _st("st-old-2", writable=["b"], desc="旧子任务乙"),
    )
    await plan({
        "task_description": "build feature",
        "complexity": Complexity.MEDIUM,
        "requirement_items": _items(),
        "plan": prev,
        "plan_retry_count": 1,
        "plan_validation_feedback": f"- 需求条目未被任何子任务覆盖: {REQ_B}",
        "baseline_covered": [],
    })
    prompt = fake.captured[0]
    assert "st-old-1" in prompt and REQ_A in prompt, "上一版摘要必须含子任务与 covers"
    assert "增量修补" in prompt, "必须显式约束增量修补，禁全量重拆"
    assert REQ_B in prompt, "校验点名的问题仍在（D09 既有行为不回退）"


async def test_plan_first_attempt_has_no_incremental_block(monkeypatch):
    _clean_env()
    fake = _FakeLLM(
        '{"subtasks":[{"id":"st-1","description":"x",'
        '"scope":{"writable":["a"],"readable":[]},"covers":["%s"]}],'
        '"parallel_groups":[["st-1"]]}' % REQ_A
    )
    import swarm.brain.nodes as nodes
    monkeypatch.setattr(nodes, "_get_brain_llm", lambda: fake)
    await plan({
        "task_description": "build feature",
        "complexity": Complexity.MEDIUM,
        "requirement_items": _items(),
    })
    assert "增量修补" not in fake.captured[0], "首次规划无历史，绝不注入修补块"


# ─────────────── T4: EXTRACT 被拒明细可观测（R31-4）───────────────

def test_extract_rejected_detail_logged(monkeypatch):
    """quote 误杀率必须可事后审计：被拒条目 reason+text_head 有界落 INFO。
    caplog 猜 logger 名是 v0.9.22 教训坑——直引生产 logger 对象挂 handler。"""
    from swarm.brain import requirements_extract as rx

    payload = json.dumps({"items": [
        {"text": "合法条目", "kind": "functional", "source_quote": "系统需要支持批量导入"},
        {"text": "幻觉条目", "kind": "functional", "source_quote": "原文里根本没有这句"},
    ]}, ensure_ascii=False)

    class _Stub:
        async def ainvoke(self, messages):
            return type("R", (), {"content": payload})()

    import swarm.brain.nodes as nodes_pkg
    monkeypatch.setattr(nodes_pkg, "_get_brain_llm", lambda: _Stub())

    records: list[logging.LogRecord] = []

    class _Cap(logging.Handler):
        def emit(self, r):
            records.append(r)

    h = _Cap()
    _old_level = rx.logger.level
    rx.logger.addHandler(h)
    rx.logger.setLevel(logging.INFO)  # 生产由 root 配置放行 INFO；测试进程默认 WARNING 须显式
    try:
        out = asyncio.run(extract_requirements(
            {"task_description": "系统需要支持批量导入数据文件"}))
    finally:
        rx.logger.removeHandler(h)
        rx.logger.setLevel(_old_level)
    assert len(out["requirement_items"]) == 1
    joined = "\n".join(r.getMessage() for r in records)
    assert "quote_not_in_source" in joined and "幻觉条目" in joined, \
        "被拒条目的 reason 与 text_head 必须落日志供误杀审计"


# ═══════════ 双复核整改项（reviewer H-1/M-1/L-3/L-5 + hunter F1/F4/F5）═══════════

async def test_batched_retry_feedback_survives_beyond_2000_chars():
    """复核 H-1：ULTRA 分批 sliding_ctx 原 [:2000] 会把拼在 issues 之后的修补块整块
    截没（round31 恰是分批规划，T3 到不了主战场）。标记置于 >2000 偏移处必须存活。"""
    fake = _FakeLLM(
        '{"subtasks":[{"id":"st-1","description":"x",'
        '"scope":{"writable":["m/a.txt"],"readable":[]},"covers":["%s","%s"]}]}'
        % (REQ_A, REQ_B)
    )
    marker = "增量修补纪律MARKER_R31_H1"
    sliding = "问题" * 1500 + marker  # 标记起始偏移 3000 > 2000
    state = {"tech_design": {}, "shared_contract_draft": {}, "project_id": "",
             "requirement_items": _items()}
    file_plan = [{"path": "m/a.txt", "action": "create", "responsibility": "x"}]
    await _plan_ultra_batched(fake, state, "总需求", {}, sliding, file_plan)
    assert marker in fake.captured[0], "修补块/长反馈在分批 prompt 必须存活（原 2000 截断吃掉）"


async def test_plan_replan_branch_injects_previous_plan_summary(monkeypatch):
    """复核 M-1：执行失败 replan 分支须与校验重试分支对称注入上一版摘要——否则
    already-通过的 baseline 申报被漏申报的新输出覆写，覆盖闸重新失败白烧重试。"""
    _clean_env()
    fake = _FakeLLM(
        '{"subtasks":[{"id":"st-1","description":"x",'
        '"scope":{"writable":["a"],"readable":[]},"covers":["%s"]}],'
        '"parallel_groups":[["st-1"]]}' % REQ_A
    )
    import swarm.brain.nodes as nodes
    monkeypatch.setattr(nodes, "_get_brain_llm", lambda: fake)
    prev = _plan_obj(_st("st-old-1", writable=["a"], covers=[REQ_A]))
    await plan({
        "task_description": "build feature",
        "complexity": Complexity.MEDIUM,
        "requirement_items": _items(),
        "plan": prev,
        "replan_count": 1,
        "replan_feedback": "上轮执行失败根因xyz",
        "baseline_covered": [{"id": REQ_B, "reason": "存量已有"}],
    })
    prompt = fake.captured[0]
    assert "st-old-1" in prompt and "增量修补" in prompt
    assert REQ_B in prompt, "上一版 baseline 申报必须让 replan LLM 看见（防覆写丢失）"


def test_normalize_keeps_best_reason_on_duplicate():
    """复核 L-3：同 id 先空 reason 后带 reason → 保带 reason 的（保首会多烧一轮重试）。"""
    out = normalize_baseline_covered([
        {"id": REQ_A, "reason": ""},
        {"id": REQ_A, "reason": "后到但有依据"},
    ])
    assert out == [{"id": REQ_A, "reason": "后到但有依据"}]


def test_normalize_caps_total_entries():
    """hunter F3：LLM 失控吐数百条唯一 ID → 条数帽（state/feedback/prompt 三处联动膨胀）。"""
    raw = [{"id": f"req-{i:08x}", "reason": "r"} for i in range(500)]
    assert len(normalize_baseline_covered(raw)) == 100


def test_feedback_formatter_bounded_and_self_describing():
    """复核 L-5：60 条 issue ≈15K 字符无界 → 8K 定界且截断自述（不静默）。"""
    from swarm.brain.nodes import _format_validation_feedback
    issues = [f"需求条目未被任何子任务覆盖: req-{i:08x} — " + "描述" * 60
              for i in range(60)]
    out = _format_validation_feedback(issues)
    assert len(out) < 8500
    assert "已截断" in out and "未列出" in out


def test_near_miss_ambiguous_prefix_no_hint():
    """hunter F4 附带：同前缀候选不唯一 → 不提示（权威误导比不提示更糟）。"""
    items = _items(extra=[
        {"id": "req-72fd98fb", "text": "甲", "kind": "functional",
         "source_quote": "甲", "source": "description"},
        {"id": "req-72fd98aa", "text": "乙", "kind": "functional",
         "source_quote": "乙", "source": "description"},
    ])
    p = _plan_obj(_st("st-1", writable=["a"],
                      covers=[REQ_A, REQ_B, "req-72fd98fb", "req-72fd98aa", "req-72fd9811"]))
    res = validate_requirement_coverage(p, items)
    bad = [i for i in res.issues if "req-72fd9811" in i]
    assert bad and "可能想引用" not in bad[0]


def test_baseline_unverified_degraded_helper():
    """hunter F1：申报条目无【已执行且 pass】断言证据 → degraded 留痕；有则干净。"""
    from swarm.brain.nodes.verify import _baseline_unverified_degraded
    state = {"baseline_covered": [
        {"id": REQ_A, "reason": "存量已有"},
        {"id": REQ_B, "reason": "存量已有"},
    ]}
    # 全 manual（鉴权类断言的常态）→ 两条全未验证
    patch_manual = {"acceptance_details": {"assertions": [
        {"req_id": REQ_A, "verdict": "skipped_manual"},
        {"req_id": REQ_B, "verdict": "skipped_manual"},
    ]}}
    out = _baseline_unverified_degraded(state, patch_manual)
    assert len(out) == 1 and out[0].startswith("baseline_covered:unverified(2:")
    assert REQ_A in out[0] and REQ_B in out[0]
    # 一条已执行 pass → 只剩另一条
    patch_half = {"acceptance_details": {"assertions": [
        {"req_id": REQ_A, "verdict": "pass"},
        {"req_id": REQ_B, "verdict": "skipped_manual"},
    ]}}
    out2 = _baseline_unverified_degraded(state, patch_half)
    assert len(out2) == 1 and f"(1:{REQ_B}" in out2[0]
    # 全 pass → 干净无留痕
    patch_all = {"acceptance_details": {"assertions": [
        {"req_id": REQ_A, "verdict": "pass"},
        {"req_id": REQ_B, "verdict": "pass"},
    ]}}
    assert _baseline_unverified_degraded(state, patch_all) == []
    # 无申报 → 恒空（零开销路径）
    assert _baseline_unverified_degraded({}, patch_manual) == []


def test_gates_baseline_strict_valve(monkeypatch):
    """hunter F1 收紧阀：默认关=degraded 不阻断 auto_accept；开=拒绝放行交人工。"""
    from swarm.brain.gates import can_auto_accept_delivery
    state = {
        "l2_passed": True,
        "degraded_reasons": [f"baseline_covered:unverified(2:{REQ_A},{REQ_B})"],
    }
    monkeypatch.delenv("SWARM_BASELINE_STRICT_GATE", raising=False)
    allow, _ = can_auto_accept_delivery(state)
    assert allow is True, "默认关：诚实降级通道，不把合法棕地交付打失败"
    monkeypatch.setenv("SWARM_BASELINE_STRICT_GATE", "1")
    allow2, reason2 = can_auto_accept_delivery(state)
    assert allow2 is False and reason2.startswith("baseline_unverified")
    # 阀开但无该类留痕 → 不误伤
    allow3, _ = can_auto_accept_delivery({"l2_passed": True})
    assert allow3 is True


def test_confirm_plan_interrupt_payload_carries_baseline(monkeypatch):
    """hunter F5：PLAN 人工闸 payload 必须带申报+覆盖对账（最廉价否决点不失明）。"""
    _clean_env()
    import swarm.brain.nodes as nodes
    captured: dict = {}

    def _fake_interrupt(payload):
        captured.update(payload)
        return {"decision": "reject"}

    monkeypatch.setattr(nodes, "interrupt", _fake_interrupt)
    monkeypatch.delenv("SWARM_AUTO_ACCEPT", raising=False)
    state = {
        "task_id": "t1",
        "task_description": "d",
        "complexity": Complexity.ULTRA,
        "plan": _plan_obj(_st("st-1", writable=["a"], covers=[REQ_A])),
        "plan_valid": True,
        "requirement_items": _items(),
        "baseline_covered": [{"id": REQ_B, "reason": "存量已有"}],
    }
    nodes.confirm_plan(state)  # 非 auto_accept → interrupt 路径（confirm_plan 为同步节点）
    assert captured.get("baseline_covered") == [{"id": REQ_B, "reason": "存量已有"}]
    cov = captured.get("coverage_matrix") or {}
    assert cov.get("total") == 2 and cov.get("covered") == 2
    assert cov.get("baseline_covered_count") == 1
