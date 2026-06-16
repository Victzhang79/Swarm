"""垂直切片守卫 _merge_horizontal_subtasks 测试（方向A）。

验证：同语言无依赖的水平切分子任务被合并；跨语言/有依赖/multimodal 保持独立。
"""
from swarm.brain.nodes.shared import _merge_horizontal_subtasks
from swarm.types import (
    FileScope,
    SubTask,
    SubTaskDifficulty,
    SubTaskModality,
    TaskHarness,
    TaskPlan,
)


def _st(sid, files, lang="java", depends=None, modality=SubTaskModality.TEXT,
        diff=SubTaskDifficulty.MEDIUM):
    return SubTask(
        id=sid,
        description=f"task {sid}",
        difficulty=diff,
        modality=modality,
        scope=FileScope(writable=files),
        depends_on=depends or [],
        harness=TaskHarness(language=lang),
        acceptance_criteria=[f"ac-{sid}"],
    )


def test_merge_same_language_no_dep():
    """两个同语言(java)无依赖子任务 → 合并成 1 个，scope 并集。"""
    plan = TaskPlan(
        subtasks=[
            _st("st-1", ["A.java"]),
            _st("st-2", ["B.java"]),
        ],
        parallel_groups=[["st-1"], ["st-2"]],
    )
    out = _merge_horizontal_subtasks(plan)
    assert len(out.subtasks) == 1, f"应合并成1个，实际{len(out.subtasks)}"
    merged = out.subtasks[0]
    assert set(merged.scope.writable) == {"A.java", "B.java"}, merged.scope.writable
    assert "ac-st-1" in merged.acceptance_criteria and "ac-st-2" in merged.acceptance_criteria
    assert merged.depends_on == []


def test_no_merge_cross_language():
    """跨语言(java + node) → 不合并(沙箱镜像不同)。"""
    plan = TaskPlan(
        subtasks=[
            _st("st-1", ["A.java"], lang="java"),
            _st("st-2", ["app.vue"], lang="node"),
        ],
        parallel_groups=[["st-1"], ["st-2"]],
    )
    out = _merge_horizontal_subtasks(plan)
    assert len(out.subtasks) == 2, "跨语言不该合并"


def test_no_merge_with_dependency():
    """有 depends_on(真串行) → 不合并。"""
    plan = TaskPlan(
        subtasks=[
            _st("st-1", ["A.java"]),
            _st("st-2", ["B.java"], depends=["st-1"]),
        ],
        parallel_groups=[["st-1"], ["st-2"]],
    )
    out = _merge_horizontal_subtasks(plan)
    assert len(out.subtasks) == 2, "有依赖不该合并"


def test_no_merge_multimodal():
    """multimodal 看图子任务隔离，不参与合并。"""
    plan = TaskPlan(
        subtasks=[
            _st("st-1", ["A.java"]),
            _st("st-2", ["ui.png"], modality=SubTaskModality.MULTIMODAL),
        ],
        parallel_groups=[["st-1"], ["st-2"]],
    )
    out = _merge_horizontal_subtasks(plan)
    # st-1 单独一组无可合并 + multimodal 隔离 → 仍 2 个
    assert len(out.subtasks) == 2


def test_single_subtask_unchanged():
    """单子任务原样返回。"""
    plan = TaskPlan(subtasks=[_st("st-1", ["A.java"])], parallel_groups=[["st-1"]])
    out = _merge_horizontal_subtasks(plan)
    assert len(out.subtasks) == 1


def test_merge_difficulty_takes_hardest():
    """合并后 difficulty 取组内最高。"""
    plan = TaskPlan(
        subtasks=[
            _st("st-1", ["A.java"], diff=SubTaskDifficulty.TRIVIAL),
            _st("st-2", ["B.java"], diff=SubTaskDifficulty.COMPLEX),
        ],
        parallel_groups=[["st-1"], ["st-2"]],
    )
    out = _merge_horizontal_subtasks(plan)
    assert len(out.subtasks) == 1
    assert out.subtasks[0].difficulty == SubTaskDifficulty.COMPLEX


def test_three_same_lang_merge_to_one():
    """三个同语言无依赖(Controller/Service/Mapper 按层水平切的典型) → 合并成 1。"""
    plan = TaskPlan(
        subtasks=[
            _st("st-1", ["Controller.java"]),
            _st("st-2", ["Service.java"]),
            _st("st-3", ["Mapper.java"]),
        ],
        parallel_groups=[["st-1"], ["st-2"], ["st-3"]],
    )
    out = _merge_horizontal_subtasks(plan)
    assert len(out.subtasks) == 1
    assert set(out.subtasks[0].scope.writable) == {
        "Controller.java", "Service.java", "Mapper.java",
    }
