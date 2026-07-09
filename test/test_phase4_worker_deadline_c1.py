"""阶段4 C1（登记册 §四）：worker 总预算 deadline 全链透传进 L1 pipeline。

取证（更正登记册）：A5 已在 _deterministic_l1_gate 入口加 _check_timeout【布尔快照】——
但那是进门查一次；进门后 pipeline 内部（build 300s + repair 独立 900s 墙钟×每轮全量
重跑 + test/verify）与 worker 总预算完全解耦，单次最坏 ~35min，预算无从中途打断。

治法：run_l1_pipeline 增 deadline（monotonic 绝对时刻）——
  ① 每阶段（build/L1.3 test/L1.3.5 verify/L1.4）前查剩余，耗尽 → pipeline_blocked=
     "worker_deadline_exhausted"（沿用 BLOCKED 契约：ok=True + blocked 置位，executor
     侧降 None/BLOCKED 走重试，绝不假 PASS）；
  ② repair 收敛循环墙钟 = min(900, 剩余)；
  ③ 阶段命令超时钳到剩余（不再 max(timeout,300) 冲破 deadline）。
调用链：executor_l1gate._deterministic_l1_gate 与 executor Phase-4 自检两处调用点
透传 start_time + max_execution_time。
"""

from __future__ import annotations

import time
from unittest.mock import patch

from swarm.types import FileScope, SubTask, SubTaskDifficulty, SubTaskModality
from swarm.worker.executor import WorkerExecutor
from swarm.worker.l1_pipeline import run_l1_pipeline

_DIFF = "--- a/a.py\n+++ b/a.py\n@@ -1 +1 @@\n-old\n+new\n"


def _sub():
    return SubTask(id="st-c1", description="改 a.py",
                   difficulty=SubTaskDifficulty.MEDIUM, modality=SubTaskModality.TEXT,
                   scope=FileScope(writable=["a.py"]), intent="modify")


def test_pipeline_entry_deadline_exhausted_blocks(tmp_path):
    (tmp_path / "a.py").write_text("x = 1\n")
    ok, details = run_l1_pipeline(
        str(tmp_path), _sub(), _DIFF, llm=None,
        deadline=time.monotonic() - 1)  # 预算已耗尽
    assert ok is True and details.get("pipeline_blocked") == "worker_deadline_exhausted", (
        f"预算耗尽必须走 BLOCKED 契约（ok=True+blocked），绝不白跑/假 PASS: {details}")
    assert details.get("not_run_kind"), details


def test_pipeline_no_deadline_backward_compatible(tmp_path):
    (tmp_path / "a.py").write_text("x = 1\n")
    ok, details = run_l1_pipeline(str(tmp_path), _sub(), _DIFF, llm=None)
    assert details.get("pipeline_blocked") != "worker_deadline_exhausted", (
        "不传 deadline=老行为一字不变（legacy 调用方零回归）")


def test_repair_budget_clamped_to_remaining():
    from swarm.worker.l1_pipeline import _repair_loop_budget
    assert _repair_loop_budget(None) >= 60.0, "无 deadline=纯 900s 墙钟（默认）"
    b = _repair_loop_budget(time.monotonic() + 100)
    assert 60.0 <= b <= 101.0, (
        f"repair 墙钟必须钳到 min(900, 剩余预算)——独立 900s 是 35min runaway 的主推手: {b}")
    b2 = _repair_loop_budget(time.monotonic() + 99999)
    assert b2 <= 901.0, "剩余预算大时仍受 900s 上界约束"


def test_stage_timeout_clamped_to_remaining():
    from swarm.worker.l1_pipeline import _stage_timeout
    assert _stage_timeout(300, None) == 300, "无 deadline 不钳"
    t = _stage_timeout(300, time.monotonic() + 100)
    assert 60 <= t <= 101, f"阶段命令超时钳到剩余（不冲破 deadline）: {t}"
    assert _stage_timeout(300, time.monotonic() + 99999) == 300


def test_gate_passes_deadline_to_pipeline():
    ex = WorkerExecutor(subtask=_sub(), project_path="/tmp/swarm-c1-test")
    ex.start_time = time.monotonic()
    with patch.object(ex, "_check_timeout", return_value=False), \
         patch.object(ex, "_get_git_diff", return_value=_DIFF), \
         patch("swarm.worker.l1_pipeline.run_l1_pipeline",
               return_value=(True, {})) as mock_pipe:
        ex._deterministic_l1_gate()
    kwargs = mock_pipe.call_args.kwargs
    dl = kwargs.get("deadline")
    assert dl is not None, "闸门必须把 worker 总预算 deadline 透传进 pipeline"
    expect = ex.start_time + ex.max_execution_time
    assert abs(dl - expect) < 5.0, f"deadline 应=start_time+max_execution_time: {dl} vs {expect}"
