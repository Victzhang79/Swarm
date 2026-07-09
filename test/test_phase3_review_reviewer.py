"""阶段3.9 对抗复核（reviewer）治理批：R-F3/R-F4/R-F6/R-F7 行为锁。

reviewer 9 条 finding：3 条与猎手重合（另文件治），R-F8 观察项/R-F9 立此存照进登记册，
本文件锁定其余 4 条（逐条亲核后全 CONFIRMED）：
  R-F3 #6 covers 并回先于 A11 认领且变异其输入——凡"同 scope 唯一+新 covers⊆旧"，
       并回必使两集相等 → 无论描述改成什么都认领旧 L1 产出（意图变护栏被击穿，
       典型灾难：P6b 要求补 2FA，同 scope 子任务改描述后被旧登录 diff 顶掉永不实现）。
       治=merge 返回注入映射，A11 ②用【LLM 原始申报】比较。
  R-F4 A12 两套模块名口径（module_deps 键=tech_design 名 vs 批名=file_plan/路径推断）
       零命中时跨批门控边全丢——依赖劣化时旧行为最保守（全串行），A12 后最激进
       （全并行）。治=零命中检测 → WARNING + 回退 legacy 串行门控。
  R-F6 adversarial cap 短路分支不归零——一次 unconverged 后整个任务余生的新战役
       进场即撞 cap 免复核（3.8 归零只修了全 NICE 收敛分支）。degraded 已留痕
       (单调不清)，cap 分支归零与收敛分支语义对齐。
  R-F7 F8 同 base 容量/bisect 子批使条目重复入桶 → 需求清单块重复行（token 浪费）。
"""

from __future__ import annotations

import asyncio

from swarm.types import (
    Confidence,
    FileScope,
    SubTask,
    SubTaskDifficulty,
    TaskPlan,
    WorkerOutput,
)


def _wo(sid, l1=True):
    return WorkerOutput(subtask_id=sid, diff="+x\n", summary="", l1_passed=l1,
                        confidence=Confidence.HIGH)


# ─────────────── R-F3：covers 并回不得污染 A11 认领判据 ───────────────

def test_rf3_merge_returns_injection_map():
    from swarm.brain.nodes import _merge_prior_covers_by_scope
    old = TaskPlan(subtasks=[SubTask(
        id="st-old", description="登录", difficulty=SubTaskDifficulty.MEDIUM,
        scope=FileScope(writable=["auth/login.py"], readable=[]), covers=["req-1"])])
    new = TaskPlan(subtasks=[SubTask(
        id="st-new", description="登录+新增 TOTP 双因子验证", difficulty=SubTaskDifficulty.MEDIUM,
        scope=FileScope(writable=["auth/login.py"], readable=[]), covers=[])])
    injected = _merge_prior_covers_by_scope(new, old, {"req-1"})
    assert injected == {"st-new": {"req-1"}}, (
        "merge 必须返回注入映射（不再只返回计数）——A11 需要它剔除污染后比较原始申报")
    assert new.subtasks[0].covers == ["req-1"], "并回本身照旧（覆盖单调化不回归）"


def test_rf3_claim_rejected_when_covers_equality_is_merge_artifact():
    """新 covers 集与旧一致【纯因并回注入】+ 描述低相似 = 意图已变，绝不认领旧产出。"""
    from swarm.brain.nodes import _merge_prior_covers_by_scope, _surgical_replan_reset
    old = TaskPlan(subtasks=[SubTask(
        id="st-old", description="实现用户登录接口", difficulty=SubTaskDifficulty.MEDIUM,
        scope=FileScope(writable=["auth/login.py"], readable=[]), covers=["req-1"])])
    new = TaskPlan(subtasks=[SubTask(
        id="st-new", description="登录改造：接入 TOTP 双因子校验与恢复码流程",
        difficulty=SubTaskDifficulty.MEDIUM,
        scope=FileScope(writable=["auth/login.py"], readable=[]), covers=[])])
    injected = _merge_prior_covers_by_scope(new, old, {"req-1"})
    out = _surgical_replan_reset(
        {"st-old": _wo("st-old")}, old, new, merged_cover_injections=injected)
    assert "st-new" not in out["subtask_results"], (
        "covers 相等是 merge 伪影（LLM 原始申报为空）+描述意图已变——认领旧登录 diff"
        " 会让被要求的新工作（2FA）永不实现且 L1 恒过")


def test_rf3_claim_kept_when_llm_itself_declared_same_covers():
    """LLM 自己申报了与旧一致的 covers（非注入）→ ②通道照常认领（A11 不回归）。"""
    from swarm.brain.nodes import _surgical_replan_reset
    old = TaskPlan(subtasks=[SubTask(
        id="st-old", description="实现用户登录接口", difficulty=SubTaskDifficulty.MEDIUM,
        scope=FileScope(writable=["auth/login.py"], readable=[]), covers=["req-1"])])
    new = TaskPlan(subtasks=[SubTask(
        id="st-new", description="用户登录接口实现（含参数校验）",
        difficulty=SubTaskDifficulty.MEDIUM,
        scope=FileScope(writable=["auth/login.py"], readable=[]), covers=["req-1"])])
    out = _surgical_replan_reset(
        {"st-old": _wo("st-old")}, old, new, merged_cover_injections={})
    assert out["subtask_results"].get("st-new") is not None, (
        "同 scope+LLM 原始 covers 一致=同一工作，措辞漂移不白重烧（A11 本意）")


# ─────────────── R-F4：模块名两套口径零命中 → 回退 legacy 串行 ───────────────

def _batches_two_modules():
    return [
        [{"id": "a1", "description": "A 批任务", "depends_on": [],
          "scope": {"writable": ["a/x.py"], "readable": []}}],
        [{"id": "b1", "description": "B 批任务", "depends_on": [],
          "scope": {"writable": ["b/y.py"], "readable": []}}],
    ]


def test_rf4_name_mismatch_falls_back_to_serial():
    from swarm.brain.plan_batch import merge_subtask_batches
    merged = merge_subtask_batches(
        _batches_two_modules(),
        batch_modules=["mod-a", "mod-b"],  # file_plan/路径推断口径
        module_deps={"模块甲": ["模块乙"], "模块乙": []},  # tech_design 中文名口径——零命中
    )
    assert merged[1]["depends_on"] == [merged[0]["id"]], (
        "依赖表非空但对批名零命中=对齐从未被校验——此时必须回退旧串行门控（最保守），"
        "而非静默零边全并行（接口未定先引用烧 L1/L2）")


def test_rf4_aligned_names_keep_true_deps_mode():
    from swarm.brain.plan_batch import merge_subtask_batches
    merged = merge_subtask_batches(
        _batches_two_modules(),
        batch_modules=["mod-a", "mod-b"],
        module_deps={"mod-a": [], "mod-b": []},  # 对齐且真无依赖 → 保持并行（A12 本意）
    )
    assert merged[1]["depends_on"] == [], "名字对齐且无真实依赖=并行，A12 不回归"


def test_rf4_empty_deps_keeps_parallel():
    """deps 表为空≠对齐失败——"无真实依赖=并行"是 A12 已拍板语义（3.3 测试锁定），
    R-F4 护栏只治【非空表零命中】的静默丢边，不得扩大到空表回退（会推翻 3.3）。"""
    from swarm.brain.plan_batch import merge_subtask_batches
    merged = merge_subtask_batches(
        _batches_two_modules(), batch_modules=["mod-a", "mod-b"], module_deps={},
    )
    assert merged[1]["depends_on"] == [], "空依赖表照旧并行（A12 语义不回归）"


# ─────────────── R-F6：cap 短路分支同样归零（战役终结语义对齐）───────────────

def test_rf6_cap_short_circuit_resets_round(monkeypatch):
    from swarm.brain.nodes.adversarial import adversarial_verify
    st = {
        "complexity": "complex",
        "plan": TaskPlan(subtasks=[SubTask(
            id="st-1", description="d", difficulty=SubTaskDifficulty.MEDIUM,
            scope=FileScope(writable=["a.py"], readable=[]))],
            parallel_groups=[["st-1"]]),
        "subtask_results": {"st-1": _wo("st-1")},
        "dispatch_remaining": [], "failed_subtask_ids": [],
        "adversarial_verify_round": 2,  # 已达 MAX_ROUNDS(2) → cap 短路
    }
    out = asyncio.run(adversarial_verify(st))
    assert out["adversarial_verify_passed"] is None
    assert any("unconverged" in str(r) for r in (out.get("degraded_reasons") or [])), (
        "degraded 留痕不动（单调，永拦 auto_accept）")
    assert out["adversarial_verify_round"] == 0, (
        "cap 短路=本战役终结——不归零则此后每个新战役（重做件/rebase 重生成件）"
        "进场即撞 cap 免复核，复核信号面永久失效")


# ─────────────── R-F7：同 base 子批不重复入桶 ───────────────

def test_rf7_no_duplicate_items_across_same_base_subbatches():
    from swarm.brain.plan_batch import bucket_requirement_items
    items = [{"id": "req-1", "text": "alarm 模块支持 AlarmService 告警发送"}]
    by_mod, cross = bucket_requirement_items(items, [
        ("alarm#1/2", [{"path": "alarm/src/AlarmService.java"}]),
        ("alarm#2/2", [{"path": "alarm/src/AlarmSender.java"}]),
    ])
    assert [it["id"] for it in by_mod.get("alarm", [])] == ["req-1"], (
        "容量/bisect 子批归一同 base 后条目被重复 append → 需求清单块重复行（纯 token 浪费）")
    assert cross == []
