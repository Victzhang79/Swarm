"""T3 — TDD 红绿闸（ECC §C 移植）测试。

现有 DEBUG 闸只验 GREEN（failing_test_command 修复后通过），从不验 RED（该测试在【未修】代码
上确实先失败）。缺 RED 证明时，一个在未修代码上就恒绿的 failing_test（不复现 bug/平凡通过）
会让 DEBUG 子任务假性"修复成功"。T3 补 RED 半环：

1. `_maybe_capture_tdd_red_baseline`：DEBUG 意图在【编码前 HEAD 基线】跑一次 failing_test_command，
   三态存 `self._tdd_red_exit_code`（ec≠0=RED 成立 / ec==0=红证证伪 / None=跳过或异常，不阻断）。
2. `_tdd_red_green_verdict`：综合 GREEN + RED 三态裁决 DEBUG L1；红证证伪(ec==0)→fail-closed；
   三态 None 不 fail-closed（对齐 l3/runtime_smoke：None≠False，避免环境问题误伤合法修复）。

纪律：只读 harness.failing_test_command（栈无关，退出码语义栈无关）；复用 _run_l1_command
（沙箱优先+本地兜底）；默认开、泄压阀 SWARM_WORKER_TDD_RED_GATE=0 关。
"""

from __future__ import annotations

from unittest.mock import patch

from swarm.types import FileScope, SubTask, SubTaskDifficulty, TaskHarness, TaskIntent


def _make_subtask(
    intent: TaskIntent = TaskIntent.DEBUG,
    failing_test_command: str = "python -m pytest test_bug.py -q",
) -> SubTask:
    harness = TaskHarness(
        language="python",
        build_command="",
        test_command="python -m pytest -q",
        failing_test_command=failing_test_command,
    )
    return SubTask(
        id="st-tdd",
        description="修 bug",
        intent=intent,
        harness=harness,
        difficulty=SubTaskDifficulty.MEDIUM,
        scope=FileScope(writable=["calc.py"]),
    )


def _executor(subtask: SubTask):
    from swarm.worker.executor import WorkerExecutor

    return WorkerExecutor(subtask=subtask, project_path="/tmp/fake_project")


# ─────────────────────────────────────────────────────────────
# 1. 基线 RED 采集 — 三态
# ─────────────────────────────────────────────────────────────

def test_baseline_red_captured_when_failing_test_fails():
    """基线(未修)failing_test 失败(exit≠0) → RED 成立，记退出码。"""
    ex = _executor(_make_subtask())
    with patch("swarm.worker.l1_pipeline._run_l1_command", return_value=(1, "1 failed")):
        ex._maybe_capture_tdd_red_baseline()
    assert ex._tdd_red_exit_code == 1


def test_baseline_red_disproven_when_failing_test_passes():
    """基线(未修)failing_test 就通过(exit==0) → 红证证伪（不复现 bug）。"""
    ex = _executor(_make_subtask())
    with patch("swarm.worker.l1_pipeline._run_l1_command", return_value=(0, "1 passed")):
        ex._maybe_capture_tdd_red_baseline()
    assert ex._tdd_red_exit_code == 0


def test_baseline_capture_skipped_for_non_debug_intent():
    ex = _executor(_make_subtask(intent=TaskIntent.MODIFY))
    with patch("swarm.worker.l1_pipeline._run_l1_command", return_value=(1, "x")) as m:
        ex._maybe_capture_tdd_red_baseline()
    assert ex._tdd_red_exit_code is None
    m.assert_not_called()


def test_baseline_capture_skipped_without_failing_command():
    ex = _executor(_make_subtask(failing_test_command=""))
    ex._maybe_capture_tdd_red_baseline()
    assert ex._tdd_red_exit_code is None


def test_baseline_capture_skipped_when_env_off(monkeypatch):
    monkeypatch.setenv("SWARM_WORKER_TDD_RED_GATE", "0")
    ex = _executor(_make_subtask())
    with patch("swarm.worker.l1_pipeline._run_l1_command", return_value=(1, "x")) as m:
        ex._maybe_capture_tdd_red_baseline()
    assert ex._tdd_red_exit_code is None
    m.assert_not_called()


def test_baseline_capture_exception_is_three_state_none():
    """基线跑不动(异常) → 三态 None（不阻断、不误判红证证伪）。"""
    ex = _executor(_make_subtask())
    with patch(
        "swarm.worker.l1_pipeline._run_l1_command",
        side_effect=FileNotFoundError("sandbox gone"),
    ):
        ex._maybe_capture_tdd_red_baseline()
    assert ex._tdd_red_exit_code is None


def test_baseline_timeout_124_is_three_state_none():
    """基线超时(exit 124)=基础设施/hang 噪声，不冒充红证 → 三态 None（对抗复核 F3）。"""
    ex = _executor(_make_subtask())
    with patch("swarm.worker.l1_pipeline._run_l1_command", return_value=(124, "command timeout")):
        ex._maybe_capture_tdd_red_baseline()
    assert ex._tdd_red_exit_code is None


def test_baseline_blocked_126_is_three_state_none():
    """基线命令被黑名单拒(exit 126)=未真执行，不冒充红证 → 三态 None（对抗复核 F3）。"""
    ex = _executor(_make_subtask())
    with patch("swarm.worker.l1_pipeline._run_l1_command", return_value=(126, "command_blocked")):
        ex._maybe_capture_tdd_red_baseline()
    assert ex._tdd_red_exit_code is None


# ─────────────────────────────────────────────────────────────
# 2. 红绿裁决（纯函数）
# ─────────────────────────────────────────────────────────────

def test_verdict_green_failed():
    ex = _executor(_make_subtask())
    ok, reason = ex._tdd_red_green_verdict(debug_green_ok=False, red_exit_code=1)
    assert ok is False
    assert reason == "green_failed"


def test_verdict_green_after_red_passes():
    """GREEN 过 + RED 成立(基线 exit≠0) → 真红转绿，通过。"""
    ex = _executor(_make_subtask())
    ok, reason = ex._tdd_red_green_verdict(debug_green_ok=True, red_exit_code=1)
    assert ok is True
    assert reason == "green_after_red"


def test_verdict_red_not_proven_observed_by_default():
    """默认(非 strict)：GREEN 过 + RED 证伪 → 只观测不阻断（避免误伤间歇/跨栈合法修复）。"""
    ex = _executor(_make_subtask())
    ok, reason = ex._tdd_red_green_verdict(debug_green_ok=True, red_exit_code=0)
    assert ok is True
    assert reason == "red_not_proven_observed"


def test_verdict_red_not_proven_fails_closed_only_in_strict():
    """strict 模式：GREEN 过 但 RED 证伪(基线 exit==0=测试恒绿不复现 bug) → fail-closed。"""
    ex = _executor(_make_subtask())
    ok, reason = ex._tdd_red_green_verdict(debug_green_ok=True, red_exit_code=0, strict=True)
    assert ok is False
    assert reason == "red_not_proven_failclosed"


def test_verdict_red_unknown_does_not_fail_closed():
    """GREEN 过 + RED 三态未知(None=基线跳过/异常) → 不 fail-closed（None≠False）。"""
    ex = _executor(_make_subtask())
    ok, reason = ex._tdd_red_green_verdict(debug_green_ok=True, red_exit_code=None)
    assert ok is True
    assert reason == "green_red_unknown"
