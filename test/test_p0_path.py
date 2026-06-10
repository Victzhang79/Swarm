#!/usr/bin/env python3
"""P0 — L2 gate + L3 push + validate routing 测试"""

from __future__ import annotations

import asyncio
import importlib.util
from pathlib import Path
from unittest.mock import patch

_bs = Path(__file__).resolve().parent / "swarm_bootstrap.py"
_spec = importlib.util.spec_from_file_location("swarm_bootstrap", _bs)
_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_mod)

from swarm.brain.graph import after_validate, after_verify_l2
from swarm.brain.nodes import handle_failure, verify_l3
from swarm.brain.state import BrainState
from swarm.types import Complexity


def test_after_verify_l2_gate():
    assert after_verify_l2({"l2_passed": True}) == "verify_l3"
    assert after_verify_l2({"l2_passed": False}) == "handle_failure"


def test_after_validate_exhausted_routes_confirm():
    state: BrainState = {
        "plan_valid": False,
        "plan_retry_count": 3,
        "complexity": Complexity.MEDIUM,
    }
    assert after_validate(state) == "confirm"


def test_handle_failure_l2_replan():
    out = asyncio.run(handle_failure({"verification_failure": "l2", "failed_subtask_ids": ["st-1"]}))
    assert out["failure_strategy"] == "replan"
    assert out.get("verification_failure") is None


def test_verify_l3_push_before_pipeline():
    state: BrainState = {
        "complexity": Complexity.COMPLEX,
        "merged_diff": "--- a/f.py\n+++ b/f.py\n@@ -1 +1 @@\n-old\n+new\n",
        "task_id": "task-abc",
        "project_id": "proj-1",
        "task_description": "test",
    }
    with patch("swarm.brain.l3_gitlab.gitlab_configured", return_value=True), patch(
        "swarm.brain.l3_gitlab.l3_push_enabled", return_value=True
    ), patch("swarm.brain.nodes._get_project_path", return_value="/tmp/proj"), patch(
        "swarm.brain.l3_gitlab.push_merged_diff_branch",
        return_value=("swarm/l3-task-abc", ""),
    ) as mock_push, patch(
        "swarm.brain.l3_gitlab.trigger_and_poll_pipeline",
        return_value=(True, "ok"),
    ) as mock_trigger:
        out = asyncio.run(verify_l3(state))
        mock_push.assert_called_once()
        mock_trigger.assert_called_once()
        assert mock_trigger.call_args.kwargs.get("ref") == "swarm/l3-task-abc"
        assert out["l3_passed"] is True


if __name__ == "__main__":
    test_after_verify_l2_gate()
    test_after_validate_exhausted_routes_confirm()
    test_handle_failure_l2_replan()
    test_verify_l3_push_before_pipeline()
    print("test_p0_path: all passed")
