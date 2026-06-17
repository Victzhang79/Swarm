"""ELABORATE 上下文预算根因回归（task e3618f1e）：

预算应基于 worker 主力(primary)窗口，不被异常降级用的小兜底模型(122B-A10B 64K)绑架。
否则 budget 被压到 49152 < medium est 基线 50000 → 全员误触发二次拆分 → 逐个 LLM 拆碎+卡死。
"""
from swarm.brain.planning_nodes import _context_budget, _needs_resplit
from swarm.types import FileScope, SubTask, SubTaskDifficulty, SubTaskModality


def test_budget_not_dragged_by_small_fallback():
    """预算应 > medium est 基线(50000)，不被 64K 兜底模型压到 49152。"""
    budget = _context_budget()
    assert budget > 50000, f"预算被小兜底模型绑架了: {budget}（应基于 primary 主力窗口）"


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
