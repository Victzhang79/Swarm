#!/usr/bin/env python3
"""记忆架构 L0-L6 对齐测试"""

from __future__ import annotations

import importlib.util
from pathlib import Path

_bs = Path(__file__).resolve().parent / "swarm_bootstrap.py"
_spec = importlib.util.spec_from_file_location("swarm_bootstrap", _bs)
_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_mod)

from swarm.brain.state import BrainState
from swarm.memory.pattern_extractor import (
    build_l2_summary,
    build_mistake_payload,
    build_one_line_digest,
    build_success_payload,
    extract_key_lines,
    should_write_success,
)
from swarm.memory.session import build_session_metadata
from swarm.memory.task_digest import format_recent_tasks_for_brain
from swarm.types import Complexity, FileScope, SubTask, SubTaskDifficulty, SubTaskModality, TaskPlan


def test_l0_session_ephemeral_fields():
    meta = build_session_metadata(client="cli")
    assert "started_at_utc" in meta
    assert "platform" in meta


def test_l2_one_line_digest_not_raw():
    state: BrainState = {
        "task_description": "x" * 500,
        "merged_diff": "--- a/f.py\n+++ b/f.py\n@@\n+line\n",
        "plan": TaskPlan(
            subtasks=[
                SubTask(
                    id="s1",
                    description="d",
                    difficulty=SubTaskDifficulty.MEDIUM,
                    modality=SubTaskModality.TEXT,
                    scope=FileScope(writable=["src/a.py"]),
                )
            ],
            parallel_groups=[["s1"]],
        ),
    }
    digest = build_one_line_digest(state, outcome="success")
    assert len(digest) <= 240
    assert "x" * 100 not in digest


def test_l2_format_for_brain():
    text = format_recent_tasks_for_brain([
        {"summary": "给用户列表加排序", "outcome": "success", "metadata": {"modules": ["src/user"]}},
    ])
    assert "近期任务摘要" in text
    assert "给用户列表加排序" in text


def test_simple_skips_l6():
    # TD2606-A7：写 L6 成功模式需【真实成功】信号（l2_passed 等）。给齐成功状态。
    _ok = {"l2_passed": True}
    assert should_write_success({"complexity": Complexity.SIMPLE, **_ok}) is False
    assert should_write_success({"complexity": Complexity.MEDIUM, **_ok}) is True
    # 防毒化：L2 未过 / 升级人工 / 仍有失败子任务 → 即便 medium 也不得写 L6 成功模式
    assert should_write_success({"complexity": Complexity.MEDIUM}) is False  # l2_passed 缺失
    assert should_write_success({"complexity": Complexity.MEDIUM, "l2_passed": True, "failure_escalated": True}) is False
    assert should_write_success({"complexity": Complexity.MEDIUM, "l2_passed": True, "failed_subtask_ids": ["st-2"]}) is False
    # TD2606-C10：降级交付（degraded_reasons 非空）不学成 L6 成功模式（防降级污染）
    assert should_write_success({"complexity": Complexity.MEDIUM, "l2_passed": True, "degraded_reasons": ["l2_no_test_executed"]}) is False


def test_mistake_snippet():
    diff = "--- a/x.py\n+++ b/x.py\n@@\n+bad code\n+more\n"
    snippet = extract_key_lines(diff, max_lines=5)
    assert "bad code" in snippet
    state: BrainState = {
        "task_description": "fix controller",
        "merged_diff": diff,
        "revision_feedback": "Controller 不应直接注入 Repository",
    }
    payload = build_mistake_payload(state, {"mistake_description": "分层违规"})
    assert payload["error_type"]
    assert payload.get("code_snippet")


def test_success_payload_tags():
    state: BrainState = {
        "task_description": "add pagination",
        "complexity": Complexity.COMPLEX,
        "merged_diff": "--- a/api.py\n+++ b/api.py\n@@\n+def page():\n",
    }
    payload = build_success_payload(state, {"pattern_name": "pagination-api"})
    assert payload["pattern_name"] == "pagination-api"
    assert payload.get("code_snippet")


def test_l2_summary_metadata_modules():
    state: BrainState = {
        "task_description": "task",
        "plan": TaskPlan(
            subtasks=[
                SubTask(
                    id="s1",
                    description="d",
                    difficulty=SubTaskDifficulty.MEDIUM,
                    modality=SubTaskModality.TEXT,
                    scope=FileScope(writable=["pkg/mod/file.py"]),
                )
            ],
            parallel_groups=[["s1"]],
        ),
    }
    l2 = build_l2_summary(state, outcome="success", parsed={})
    assert l2["metadata"].get("modules")


if __name__ == "__main__":
    test_l0_session_ephemeral_fields()
    test_l2_one_line_digest_not_raw()
    test_l2_format_for_brain()
    test_simple_skips_l6()
    test_mistake_snippet()
    test_success_payload_tags()
    test_l2_summary_metadata_modules()
    print("test_memory_architecture: all passed")
