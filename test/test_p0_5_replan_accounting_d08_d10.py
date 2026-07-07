"""P0-5 单测：D08 replan 记账清全 + D09 校验失败回灌 + D10 parallel_groups 同步。

三条治本各自的行为断言（非 getsource）：

D08 — `_surgical_replan_reset` 按签名清全四张 replan 敏感表（subtask_retry_counts /
      subtask_redecompose_count / abandoned_subtask_ids / give_up_isolated_ids）。id 复用是默认情形，
      签名变=语义新子任务 → 旧账清空（否则新 st-N 被旧 retry 上限跳过重试、旧 redecompose 拒拆、旧
      abandoned/give_up 排除永不派发→假 PARTIAL）；签名一致=同一子任务 → 继承旧账（不重置真预算）。

D09 — validate_plan 失败时把 issues 摘要写入 plan() 会读的 plan_validation_feedback；plan() 重试时
      把它注入 LLM prompt（不再盲重试）。

D10 — 单发去重 / dedupe_module_scaffolds 删子任务后，parallel_groups 无悬空引用（prune_parallel_groups
      同步剔除），plan_validator 通过。
"""
from __future__ import annotations

from swarm.brain.nodes import (
    _surgical_replan_reset,
    _format_validation_feedback,
    plan,
    validate_plan,
)
from swarm.brain.plan_batch import prune_parallel_groups
from swarm.types import (
    Complexity,
    FileScope,
    SubTask,
    SubTaskDifficulty,
    TaskPlan,
)


def _st(sid, desc="do", writable=None, creates=None, depends_on=None):
    return SubTask(
        id=sid,
        description=desc,
        difficulty=SubTaskDifficulty.MEDIUM,
        scope=FileScope(
            writable=list(writable or []),
            readable=[],
            create_files=list(creates or []),
        ),
        depends_on=list(depends_on or []),
    )


# ─────────────────────────── D08 ───────────────────────────

def test_d08_reset_clears_all_four_accounting_tables_on_signature_change():
    """id 复用 + 签名变（语义新子任务）→ 四张记账/放弃表全部清空该 id。

    旧代码 _surgical_replan_reset 不返回这四张表 → 旧账粘滞：新 st-3 被旧 retry 上限跳过重试、
    旧 redecompose 拒拆、旧 abandoned/give_up 排除永不派发。
    """
    # 旧 plan：st-3 = 描述 A；新 plan：st-3 = 描述 B（id 复用，签名变）
    old_plan = TaskPlan(subtasks=[_st("st-3", desc="旧语义A")])
    new_plan = TaskPlan(subtasks=[_st("st-3", desc="新语义B")])

    out = _surgical_replan_reset(
        old_results={},
        old_plan=old_plan,
        new_plan=new_plan,
        old_recovery_counts={"st-3": 5},
        old_retry_counts={"st-3": 9},
        old_redecompose_counts={"st-3": 1},
        old_abandoned_ids=["st-3"],
        old_give_up_ids=["st-3"],
    )
    # 四张表必须在返回键里（旧代码根本不含这些键）
    assert out.get("subtask_retry_counts") == {}, "签名变→retry 账必须清空，否则新子任务首败即 escalate"
    assert out.get("subtask_redecompose_count") == {}, "签名变→redecompose 账必须清空，否则阶梯二永拒"
    assert out.get("abandoned_subtask_ids") == [], "签名变→旧 abandoned 必须剔除，否则新子任务永不派发"
    assert out.get("give_up_isolated_ids") == [], "签名变→旧 give_up 必须剔除，否则新子任务被排除"
    assert out.get("targeted_recovery_counts") == {}, "签名变→recovery 配额清空（原有纪律）"


def test_d08_reset_preserves_accounting_when_signature_identical():
    """签名完全一致（同一子任务）→ 继承旧记账（不重置真预算，避免同一子任务无限重试/拆分）。"""
    old_plan = TaskPlan(subtasks=[_st("st-3", desc="相同语义")])
    new_plan = TaskPlan(subtasks=[_st("st-3", desc="相同语义")])

    out = _surgical_replan_reset(
        old_results={},
        old_plan=old_plan,
        new_plan=new_plan,
        old_recovery_counts={"st-3": 2},
        old_retry_counts={"st-3": 3},
        old_redecompose_counts={"st-3": 1},
        old_abandoned_ids=["st-3"],
        old_give_up_ids=["st-3"],
    )
    assert out.get("subtask_retry_counts") == {"st-3": 3}, "同签名继承 retry 账"
    assert out.get("subtask_redecompose_count") == {"st-3": 1}, "同签名继承 redecompose 账"
    assert out.get("abandoned_subtask_ids") == ["st-3"], "同签名保留 abandoned（仍是被放弃的同一子任务）"
    assert out.get("give_up_isolated_ids") == ["st-3"], "同签名保留 give_up"
    assert out.get("targeted_recovery_counts") == {"st-3": 2}


def test_d08_reset_drops_ids_absent_from_new_plan():
    """旧 abandoned/give_up id 在新 plan 里不存在 → 剔除（不残留幽灵放弃标记）。"""
    old_plan = TaskPlan(subtasks=[_st("st-9", desc="被删的老子任务")])
    new_plan = TaskPlan(subtasks=[_st("st-1", desc="新拆分")])
    out = _surgical_replan_reset(
        old_results={},
        old_plan=old_plan,
        new_plan=new_plan,
        old_retry_counts={"st-9": 4},
        old_abandoned_ids=["st-9"],
        old_give_up_ids=["st-9"],
    )
    assert out.get("subtask_retry_counts") == {}
    assert out.get("abandoned_subtask_ids") == []
    assert out.get("give_up_isolated_ids") == []


def test_d08_first_plan_no_reset_keys():
    """首规划（全部记账表空）→ 返回 {}（不发这些键，不 clobber 不存在的态）。"""
    p = TaskPlan(subtasks=[_st("st-1")])
    out = _surgical_replan_reset({}, None, p)
    assert out == {}


async def test_d08_plan_simple_path_emits_reset_tables():
    """端到端：SIMPLE plan() replan 重入携带旧账 → 返回四张清空后的表（id 复用+签名变场景）。"""
    out = await plan({
        "task_description": "fix typo",
        "complexity": Complexity.SIMPLE,
        "affected_files": ["README.md"],
        # 制造 replan 重入信号（有旧完成态）+ 旧账（旧 st-1 语义必与 SIMPLE 新 st-1 不同 → 签名变）
        "subtask_results": {"st-1": object()},
        "subtask_retry_counts": {"st-1": 9},
        "subtask_redecompose_count": {"st-1": 1},
        "abandoned_subtask_ids": ["st-1"],
        "give_up_isolated_ids": ["st-1"],
    })
    assert "subtask_retry_counts" in out and out["subtask_retry_counts"] == {}
    assert "subtask_redecompose_count" in out and out["subtask_redecompose_count"] == {}
    assert "abandoned_subtask_ids" in out and out["abandoned_subtask_ids"] == []
    assert "give_up_isolated_ids" in out and out["give_up_isolated_ids"] == []


# ─────────────────────────── D09 ───────────────────────────

async def test_d09_validate_failure_writes_feedback_channel():
    """结构校验失败 → 返回 plan_validation_feedback（含具体 issue 文本），供 PLAN 重试注入。"""
    # 悬空依赖 → 结构校验硬失败（scope 非空，避免被"空计划"更早拦下）
    bad = TaskPlan(subtasks=[_st("st-1", writable=["a.py"], depends_on=["st-does-not-exist"])])
    out = await validate_plan({
        "plan": bad,
        "task_description": "t",
        "complexity": Complexity.MEDIUM,
        "plan_retry_count": 0,
    })
    assert out.get("plan_valid") is False
    fb = out.get("plan_validation_feedback") or ""
    assert fb, "校验失败必须回灌反馈（旧代码根本不写此键）"
    assert "st-does-not-exist" in fb, "反馈须含具体失败原因（悬空依赖 id）"


async def test_d09_validate_success_clears_feedback():
    """校验通过 → plan_validation_feedback 清空（防跨轮粘滞把旧 issue 灌进无关重规划）。"""
    good = TaskPlan(subtasks=[_st("st-1", writable=["a.py"])], parallel_groups=[["st-1"]])
    out = await validate_plan({
        "plan": good,
        "task_description": "t",
        "complexity": Complexity.SIMPLE,  # SIMPLE 走结构通过快速路径，不调 LLM
        "plan_retry_count": 0,
    })
    assert out.get("plan_valid") is True
    assert out.get("plan_validation_feedback") == ""


async def test_d09_plan_injects_validation_feedback_into_prompt(monkeypatch):
    """PLAN 重试读 plan_validation_feedback 并注入 LLM prompt（非盲重试）。"""
    captured = {}

    class _FakeResp:
        content = '{"subtasks":[{"id":"st-1","description":"x","scope":{"writable":[],"readable":[]}}],"parallel_groups":[["st-1"]]}'

    class _FakeLLM:
        async def ainvoke(self, messages):
            # messages[1] = user prompt（含 sliding_context）
            captured["user"] = messages[1]["content"]
            return _FakeResp()

    import swarm.brain.nodes as nodes
    monkeypatch.setattr(nodes, "_get_brain_llm", lambda: _FakeLLM())

    await plan({
        "task_description": "build feature",
        "complexity": Complexity.MEDIUM,
        "plan_validation_feedback": "- 子任务 st-2 依赖未知任务 st-99",
        "plan_retry_count": 1,
    })
    assert "st-2 依赖未知任务 st-99" in captured.get("user", ""), \
        "上轮校验失败原因必须出现在重试 PLAN 的 prompt 里"


def test_d09_format_feedback_dedups_and_bullets():
    assert _format_validation_feedback([]) == ""
    out = _format_validation_feedback(["a", "a", " b ", ""])
    assert out == "- a\n- b"


# ─────────────────────────── D10 ───────────────────────────

def test_d10_prune_parallel_groups_drops_dangling_and_empty():
    groups = [["st-1", "st-2"], ["st-3"], ["st-4", "st-5"]]
    # st-2, st-3 被删
    out = prune_parallel_groups(groups, {"st-1", "st-4", "st-5"})
    assert out == [["st-1"], ["st-4", "st-5"]], "悬空 id 剔除 + 成员全删的组整组删除"


def test_d10_prune_handles_empty_inputs():
    assert prune_parallel_groups(None, {"st-1"}) == []
    assert prune_parallel_groups([], {"st-1"}) == []
    assert prune_parallel_groups([["st-1"]], set()) == []


async def test_d10_single_emit_dedup_syncs_groups_and_validates(monkeypatch):
    """单发路径：LLM 吐重复脚手架 st-1/st-2（同 create 签名）+ parallel_groups 引用两者 →
    去重删 st-2 后 parallel_groups 无悬空引用，plan_validator 结构校验通过（不再硬失败）。
    """
    # st-1 与 st-2 同 create 签名（同一模块 pom 脚手架）→ dedupe_subtasks 会删其一
    dup_json = (
        '{"subtasks":['
        '{"id":"st-1","description":"scaffold alarm module","scope":{"writable":[],"readable":[],"create_files":["alarm/pom.xml"]}},'
        '{"id":"st-2","description":"scaffold alarm module","scope":{"writable":[],"readable":[],"create_files":["alarm/pom.xml"]}}'
        '],"parallel_groups":[["st-1","st-2"]]}'
    )

    class _FakeResp:
        content = dup_json

    class _FakeLLM:
        async def ainvoke(self, messages):
            return _FakeResp()

    import swarm.brain.nodes as nodes
    monkeypatch.setattr(nodes, "_get_brain_llm", lambda: _FakeLLM())

    out = await plan({
        "task_description": "scaffold",
        "complexity": Complexity.MEDIUM,
    })
    tp = out["plan"]
    ids = {st.id for st in tp.subtasks}
    # 去重后 parallel_groups 里不得含已删 id
    for g in tp.parallel_groups:
        for tid in g:
            assert tid in ids, f"parallel_groups 悬空引用 {tid}（去重未同步）"

    from swarm.brain.plan_validator import validate_plan_structure
    res = validate_plan_structure(tp)
    assert res.valid, f"去重后结构校验应通过，issues={res.issues}"


def test_d10_dedupe_module_scaffolds_syncs_groups():
    """dedupe_module_scaffolds 合并重复脚手架后 parallel_groups 同步无悬空引用。"""
    from swarm.brain.contract_utils import dedupe_module_scaffolds

    a = _st("st-1", desc="创建 alarm 模块脚手架 pom", creates=["alarm/pom.xml"])
    b = _st("st-2", desc="创建 alarm 模块脚手架 pom", creates=["alarm/pom.xml"])
    plan_obj = TaskPlan(subtasks=[a, b], parallel_groups=[["st-1", "st-2"]])

    merged = dedupe_module_scaffolds(plan_obj)
    if merged:  # 只在确实识别为脚手架并合并时验证同步
        ids = {s.id for s in plan_obj.subtasks}
        for g in plan_obj.parallel_groups:
            for tid in g:
                assert tid in ids, f"dedupe_module_scaffolds 后 parallel_groups 悬空 {tid}"
