"""A5 治本（round22）：L1 确定性闸门不受 worker 总预算约束 → repair 循环 runaway。

根因：verify 阶段撞 max_execution_time 后仅 break，Phase4 _phase_produce 仍调
_deterministic_l1_gate → run_l1_pipeline，而后者的 build-repair 循环自带 900s 墙钟、
与 worker 总预算解耦。_deterministic_l1_gate 全程无 _check_timeout → 预算耗尽后还能
再起一整轮 repair，远超 worker 预算 runaway。

治本：_deterministic_l1_gate 入口加 worker 总预算闸——已超时 → 不进 pipeline，降 BLOCKED。

行为测试：mock _check_timeout，断言超时短路且不调 run_l1_pipeline。
"""
from __future__ import annotations

from unittest.mock import patch

from swarm.types import FileScope, SubTask, SubTaskDifficulty, SubTaskModality
from swarm.worker.executor import WorkerExecutor

_REAL_DIFF = "--- a/A.java\n+++ b/A.java\n@@ -1 +1 @@\n-old\n+new\n"


def _mk() -> WorkerExecutor:
    st = SubTask(id="st-a5", description="改 A.java",
                 difficulty=SubTaskDifficulty.MEDIUM, modality=SubTaskModality.TEXT,
                 scope=FileScope(writable=["A.java"]), intent="modify")
    return WorkerExecutor(subtask=st, project_path="/tmp/swarm-a5-test")


def test_gate_short_circuits_when_worker_timed_out():
    """worker 总预算已耗尽 → 闸门短路降 BLOCKED，绝不进 run_l1_pipeline(不再起 repair)。"""
    ex = _mk()
    with patch.object(ex, "_check_timeout", return_value=True), \
         patch.object(ex, "_get_git_diff", return_value=_REAL_DIFF), \
         patch("swarm.worker.l1_pipeline.run_l1_pipeline") as mock_pipe:
        det_ok, details = ex._deterministic_l1_gate()
    assert det_ok is None, details
    assert details.get("not_run_kind"), details
    mock_pipe.assert_not_called()


def test_gate_runs_pipeline_when_budget_remains():
    """回归：预算未耗尽 → 正常进 pipeline 裁决。"""
    ex = _mk()
    with patch.object(ex, "_check_timeout", return_value=False), \
         patch.object(ex, "_get_git_diff", return_value=_REAL_DIFF), \
         patch("swarm.worker.l1_pipeline.run_l1_pipeline", return_value=(True, {})) as mock_pipe:
        det_ok, _ = ex._deterministic_l1_gate()
    assert det_ok is True
    mock_pipe.assert_called_once()


if __name__ == "__main__":
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for fn in fns:
        fn()
        print(f"  ✅ {fn.__name__}")
    print(f"\n=== A5 闸门总预算闸: {len(fns)}/{len(fns)} passed ===")
