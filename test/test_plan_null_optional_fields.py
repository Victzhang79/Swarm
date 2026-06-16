"""task 88d69519 回归：LLM 输出 harness=null / contract=null 时 plan 必须能解析。

根因：SubTask.harness 类型是 TaskHarness（非 Optional，靠 default_factory），LLM 显式
输出 "harness": null 会触发 pydantic validation error → 整个 plan 解析失败 → 降级空 scope
兜底 → worker 无文件可依、cat 探索耗尽步数。
修复：PLAN 解析前剔除值为 None 的可选字段（harness/contract），让 default_factory 生效。
"""
from swarm.types import TaskPlan


def _clean_and_parse(result: dict) -> TaskPlan:
    """复刻 nodes.plan 的清洗逻辑（单一事实源契约镜像）。"""
    for _st in result.get("subtasks", []) or []:
        if isinstance(_st, dict):
            for _opt in ("harness", "contract"):
                if _opt in _st and _st[_opt] is None:
                    _st.pop(_opt)
    return TaskPlan(**result)


def _base_subtask(**overrides):
    st = {
        "id": "st-1", "description": "导出功能",
        "difficulty": "medium", "modality": "text",
        "scope": {"writable": ["Ctrl.java"], "create_files": [], "delete_files": [], "readable": []},
        "acceptance_criteria": ["编译通过"],
        "depends_on": [], "model_preference": None,
    }
    st.update(overrides)
    return st


def test_harness_null_parses():
    result = {"subtasks": [_base_subtask(harness=None)], "parallel_groups": [["st-1"]]}
    plan = _clean_and_parse(result)
    assert plan.subtasks[0].harness is not None  # default_factory 生效
    assert plan.subtasks[0].scope.writable == ["Ctrl.java"]


def test_contract_null_parses():
    result = {"subtasks": [_base_subtask(contract=None)], "parallel_groups": [["st-1"]]}
    plan = _clean_and_parse(result)
    assert plan.subtasks[0].contract == {}


def test_both_null_parses():
    result = {"subtasks": [_base_subtask(harness=None, contract=None)], "parallel_groups": [["st-1"]]}
    plan = _clean_and_parse(result)
    assert plan.subtasks[0].harness is not None
    assert plan.subtasks[0].contract == {}


def test_valid_harness_kept():
    """显式给了合法 harness 时保留。"""
    result = {"subtasks": [_base_subtask(harness={"language": "java"})], "parallel_groups": [["st-1"]]}
    plan = _clean_and_parse(result)
    assert plan.subtasks[0].harness.language == "java"
