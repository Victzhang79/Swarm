"""阶段4 C2+C3（登记册 §四）：Phase-4 复用 Phase-3 确定性结果 + 沙箱超时 124 语义统一。

C2 取证：executor.py Phase-4（:797）无条件把确定性 pipeline 整遍重跑（与 Phase-3 循环
:673 同一函数），且 det_ok=True 时还再跑一次带 LLM 的 pipeline——happy-path 每子任务
≥3 次全量 L1。治=_deterministic_l1_gate 按 diff 内容签名缓存【PASS】结果：签名未变+
上次确定性 PASS → 复用 details 不重跑（FAIL/BLOCKED 绝不缓存——修复后必须真重验）。

C3 取证：sandbox.py run_command 的 except 块只认「带 exit_code 的业务失败」，SDK 超时
（无 exit_code）塌进 infra 失败路径 → ①计入 5 次熔断误弃好沙箱②上层拿不到 124 语义，
错误处方随机落 capability/transient。治=超时异常合成本地 subprocess 同款 exit_code=124
（_run_l1_command 解析器现成认识），不计 infra 失败（envd 通着、命令真跑了只是慢）。
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

from swarm.types import FileScope, SubTask, SubTaskDifficulty, SubTaskModality
from swarm.worker.executor import WorkerExecutor

_DIFF = "--- a/a.py\n+++ b/a.py\n@@ -1 +1 @@\n-old\n+new\n"


def _mk() -> WorkerExecutor:
    st = SubTask(id="st-c2", description="改 a.py",
                 difficulty=SubTaskDifficulty.MEDIUM, modality=SubTaskModality.TEXT,
                 scope=FileScope(writable=["a.py"]), intent="modify")
    return WorkerExecutor(subtask=st, project_path="/tmp/swarm-c2-test")


# ─────────────── C2：diff 签名未变 + 上次 PASS → 复用不重跑 ───────────────

def test_gate_reuses_pass_result_when_diff_unchanged():
    ex = _mk()
    with patch.object(ex, "_check_timeout", return_value=False), \
         patch.object(ex, "_get_git_diff", return_value=_DIFF), \
         patch("swarm.worker.l1_pipeline.run_l1_pipeline",
               return_value=(True, {"l1_2_compile_ok": True})) as mock_pipe:
        ok1, d1 = ex._deterministic_l1_gate()
        ok2, d2 = ex._deterministic_l1_gate()
    assert ok1 is True and ok2 is True
    assert mock_pipe.call_count == 1, (
        "diff 未变+上次确定性 PASS → Phase-4 必须复用，不再整遍重跑"
        "（happy-path 每子任务 ≥3 次全量 L1 的主推手）")
    assert d2.get("reused_deterministic_gate"), "复用必须自述（可观测）"


def test_gate_reruns_when_diff_changed():
    ex = _mk()
    diffs = iter([_DIFF, _DIFF.replace("new", "newer")])
    with patch.object(ex, "_check_timeout", return_value=False), \
         patch.object(ex, "_get_git_diff", side_effect=lambda: next(diffs)), \
         patch("swarm.worker.l1_pipeline.run_l1_pipeline",
               return_value=(True, {})) as mock_pipe:
        ex._deterministic_l1_gate()
        ex._deterministic_l1_gate()
    assert mock_pipe.call_count == 2, "diff 变了必须真重验（缓存键=内容签名）"


def test_gate_never_reuses_fail_or_blocked():
    ex = _mk()
    with patch.object(ex, "_check_timeout", return_value=False), \
         patch.object(ex, "_get_git_diff", return_value=_DIFF), \
         patch("swarm.worker.l1_pipeline.run_l1_pipeline",
               return_value=(False, {"l1_2_compile_ok": False})) as mock_pipe:
        ok1, _ = ex._deterministic_l1_gate()
        ok2, _ = ex._deterministic_l1_gate()
    assert ok1 is False and ok2 is False
    assert mock_pipe.call_count == 2, (
        "FAIL 绝不缓存——同 diff 重验是修复回路的既有语义（沙箱态可被修复动作改变）")


# ─────────────── C3：沙箱超时 → exit_code=124，不计 infra 熔断 ───────────────

class _TimeoutExc(Exception):
    pass


class _SdkTimeoutException(Exception):
    """模拟 e2b TimeoutException（类型名含 Timeout，无 exit_code 属性）。"""


def _run_with_exc(exc):
    from swarm.worker.sandbox import SandboxManager
    pool = SandboxManager.__new__(SandboxManager)  # 不连真服务
    pool._record_sandbox_failure = MagicMock()
    pool._record_sandbox_success = MagicMock()
    pool.append_activity = MagicMock()
    sandbox = MagicMock()
    sandbox.sandbox_id = "sb-1"
    sandbox.commands.run.side_effect = exc
    return pool, pool.run_command(sandbox, "mvn -q compile", timeout=5)


def test_sandbox_timeout_synthesizes_124_and_no_breaker():
    pool, cr = _run_with_exc(_SdkTimeoutException("command timed out after 5s"))
    assert cr.error == "exit_code=124", (
        f"SDK 超时必须合成 124（与本地 subprocess 同语义，_run_l1_command 现成解析）: {cr.error}")
    assert cr.success is False
    pool._record_sandbox_failure.assert_not_called(), (
        "超时=命令太慢非沙箱坏——计入 5 次熔断会误弃好沙箱")


def test_sandbox_real_infra_error_still_counts():
    pool, cr = _run_with_exc(ConnectionError("connection refused"))
    assert "exit_code=124" not in (cr.error or "")
    pool._record_sandbox_failure.assert_called_once()
