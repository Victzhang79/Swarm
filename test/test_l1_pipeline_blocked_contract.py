"""CODEWALK 根因B：run_l1_pipeline "BLOCKED 一律 return True" 是隐式契约。

所有 BLOCKED 路径返回 ok=True + details["pipeline_blocked"] 置位，靠 executor 侧
_deterministic_l1_gate 把 ok∧blocked 降级 None 纠正——任何新调用方裸用 bool 返回值
即假绿（BLOCKED 被当 PASS）。本测锁定该契约两半：
① BLOCKED 路径确实 ok=True + pipeline_blocked 置位（裸用即假绿的事实本身）；
② 契约文档已写进 docstring（结构化返回改造是后续批次，先让契约显式可发现）。
"""
from __future__ import annotations

from swarm.types import FileScope, SubTask, SubTaskDifficulty, SubTaskModality
from swarm.worker.l1_pipeline import run_l1_pipeline


def _subtask() -> SubTask:
    return SubTask(
        id="st-1", description="x", difficulty=SubTaskDifficulty.MEDIUM,
        modality=SubTaskModality.TEXT,
        scope=FileScope(writable=["src/A.java"]),
    )


def test_blocked_path_returns_true_with_pipeline_blocked(tmp_path):
    """malformed diff（非空但解析 0 文件）→ ok=True 且 pipeline_blocked 置位。
    这正是"裸用返回值即假绿"的契约事实：True 不等于 PASS。"""
    ok, details = run_l1_pipeline(
        str(tmp_path), _subtask(),
        diff="这不是一个合法的 unified diff，没有 +++ b/ 头",
    )
    assert ok is True
    assert details.get("pipeline_blocked") == "malformed_diff_zero_files"
    assert details.get("not_run_kind") == "blocked"


def test_benign_empty_diff_not_blocked(tmp_path):
    """对照：真空 diff 是 BENIGN no-op，不得误标 blocked。"""
    ok, details = run_l1_pipeline(str(tmp_path), _subtask(), diff="")
    assert ok is True
    assert not details.get("pipeline_blocked")
    assert details.get("not_run_kind") == "benign"


def test_contract_documented_in_docstring():
    """契约必须写在函数首屏（新调用方第一眼可见），直到结构化返回改造落地。"""
    doc = run_l1_pipeline.__doc__ or ""
    assert "pipeline_blocked" in doc and "假绿" in doc, \
        "run_l1_pipeline docstring 必须声明 BLOCKED→True 契约与裸用风险"
