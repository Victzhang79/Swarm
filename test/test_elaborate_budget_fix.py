"""ELABORATE 上下文预算根因回归（task e3618f1e）：

预算应基于 worker 主力(primary)窗口，不被异常降级用的小兜底模型(122B-A10B 64K)绑架。
否则 budget 被压到 49152 < medium est 基线 50000 → 全员误触发二次拆分 → 逐个 LLM 拆碎+卡死。
"""
from swarm.brain.planning_nodes import _context_budget, _needs_resplit
from swarm.types import FileScope, SubTask, SubTaskDifficulty, SubTaskModality


def test_budget_not_dragged_by_small_fallback():
    """预算基于双主力池窗口(MiniMax 196K)，不被次级 Saka(112K)拖低。

    budget=147456(196608×0.75)，medium est 基线50000 仅占 34%，子任务拆得宽松不碎。
    安全性：worker 裁剪后输入≈103K < Saka 112K，降级到任一 worker 都装得下。
    """
    budget = _context_budget()
    assert budget >= 140000, f"预算应基于双主力窗口(~147456)，被次级模型拖低了: {budget}"
    # 裁剪后输入(budget×0.7)必须 < 最小 worker 窗口 Saka 112000，否则降级会撑穿
    assert int(budget * 0.7) < 112000, f"裁剪后输入 {int(budget*0.7)} 会撑穿 Saka 112K"


def test_medium_subtask_not_force_resplit():
    """典型 medium 子任务(基线50k+少量文件)不应被误判需二次拆分。"""
    budget = _context_budget()
    st = SubTask(
        id="st-1", description="实现预警任务CRUD",
        difficulty=SubTaskDifficulty.MEDIUM, modality=SubTaskModality.TEXT,
        scope=FileScope(writable=["a/Foo.java", "a/Bar.java"], create_files=[], readable=[]),
        depends_on=[], contract={},
        est_context_tokens=50000 + 2 * 6000,  # 基线 + 2 文件
    )
    # 62000 < 84000 budget → 不需拆
    assert not _needs_resplit(st, budget), f"medium 子任务被误判需拆分(est={st.est_context_tokens}, budget={budget})"


def test_truly_oversized_still_resplits():
    """真正超大的子任务(est 远超预算)仍应触发拆分(不误伤治本意图)。"""
    budget = _context_budget()
    st = SubTask(
        id="st-big", description="巨型多文件",
        difficulty=SubTaskDifficulty.COMPLEX, modality=SubTaskModality.TEXT,
        scope=FileScope(writable=[f"f{i}.java" for i in range(30)], create_files=[], readable=[]),
        depends_on=[], contract={},
        est_context_tokens=budget + 100000,  # 远超预算
    )
    assert _needs_resplit(st, budget), "真超预算的子任务仍应拆分"
