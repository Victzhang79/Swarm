#!/usr/bin/env python3
"""主题G P0（round38c）—— G3-1 机读键三终态统一进 token_usage jsonb。

取证（主题G 盘点）：degraded_summary 只进 SSE payload 与 PARTIAL/FAILED 账，DONE 终态
API 全盲；contract_failed_modules/l2_details/validate 降级标记只活在 LangGraph state
（SSE+API 双盲）——round38 造这些键就是给盯跑脚本的。
"""
from __future__ import annotations

import importlib.util
from pathlib import Path

_bs = Path(__file__).resolve().parent / "swarm_bootstrap.py"
_spec = importlib.util.spec_from_file_location("swarm_bootstrap", _bs)
_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_mod)

from swarm.brain.runner import _attach_observability_account  # noqa: E402
from swarm.types import Confidence, WorkerOutput  # noqa: E402


def test_g3_1_machine_keys_attached():
    wo = WorkerOutput(subtask_id="st-1", diff="x", summary="", confidence=Confidence.HIGH,
                      l1_passed=True,
                      l1_details={"build_cmd_downgraded_to_validate": True})
    state = {
        "degraded_reasons": ["merge_secret_reported:high:x@a:1"],
        "contract_failed_modules": ["mod-a", "mod-b"],
        "l2_details": {"issues": ["stub_fingerprint: 子任务 st-9 假实现", "x2"],
                       "retry_guidance": "内部指引不外泄"},
        "subtask_results": {"st-1": wo},
    }
    tu: dict = {"cloud_tokens_in": 1}
    _attach_observability_account(tu, state)
    assert tu.get("degraded_summary"), "DONE 终态 degraded_summary 不再 API 全盲"
    assert tu.get("contract_failed_modules") == ["mod-a", "mod-b"]
    assert tu.get("l2_issues_head") and "stub_fingerprint" in tu["l2_issues_head"][0]
    assert tu.get("validate_downgraded_subtasks") == ["st-1"]
    assert tu["cloud_tokens_in"] == 1, "既有账键不被覆写"


def test_g3_1_empty_state_noop():
    tu: dict = {"cloud_tokens_in": 5}
    out = _attach_observability_account(tu, {})
    assert out == {"cloud_tokens_in": 5}, "无机读键时账面零变化（不塞空键）"
    _attach_observability_account(tu, None)
    assert tu == {"cloud_tokens_in": 5}


if __name__ == "__main__":
    print("run via pytest")
