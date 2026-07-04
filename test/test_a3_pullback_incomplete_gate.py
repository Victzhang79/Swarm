"""A3 治本（round22 假绿门）：pull-back 部分失败/skip → 本地 diff 静默缺改。

根因：sync_files_from_sandbox 对 >1MiB 文件静默 skipped++、对读失败文件仅 errors.append+log
不中断；executor 只打日志继续。沙箱内 compile/test 全过 → run_l1_pipeline ok=True，但本地
_get_git_diff 基于【不完整】的工作区/_post_sync_contents → 交付 diff 漏掉已在沙箱验证过的变更。

治本：pull-back 的 skipped/errors 是【交付完整性信号】——rel_files 全是交付相关文件。
_deterministic_l1_gate 在 ok=True 时若最近一次 pull-back 有 skip/err → 不得判 True，
降为 None(BLOCKED) + CRITICAL 可观测日志（走 transient 退避重试拉全，而非静默假绿）。

行为测试：直接置 executor 的 sync 完整性标志，mock diff 与 pipeline。
"""
from __future__ import annotations

from unittest.mock import patch

from swarm.types import FileScope, SubTask, SubTaskDifficulty, SubTaskModality
from swarm.worker.executor import WorkerExecutor


def _mk(scope: FileScope) -> WorkerExecutor:
    st = SubTask(id="st-a3", description="改 A.java",
                 difficulty=SubTaskDifficulty.MEDIUM, modality=SubTaskModality.TEXT,
                 scope=scope, intent="modify")
    return WorkerExecutor(subtask=st, project_path="/tmp/swarm-a3-test")


_REAL_DIFF = "--- a/A.java\n+++ b/A.java\n@@ -1 +1 @@\n-old\n+new\n"


def test_gate_downgrades_true_when_pullback_skipped():
    """沙箱 pipeline 判 True，但本轮 pull-back 有 skip → 闸门不得判 True（降 None BLOCKED）。"""
    ex = _mk(FileScope(writable=["A.java"]))
    ex._sync_skipped_count = 1  # >1MiB 被跳过
    ex._sync_error_rels = []
    with patch.object(ex, "_get_git_diff", return_value=_REAL_DIFF), \
         patch("swarm.worker.l1_pipeline.run_l1_pipeline", return_value=(True, {})):
        det_ok, details = ex._deterministic_l1_gate()
    assert det_ok is None, f"pull-back skip 时不得判 True，got {det_ok} {details}"
    assert details.get("not_run_kind"), details


def test_gate_downgrades_true_when_pullback_errored():
    """pull-back 读失败（errors 非空）→ 同样不得判 True。"""
    ex = _mk(FileScope(writable=["A.java"]))
    ex._sync_skipped_count = 0
    ex._sync_error_rels = ["A.java: read timeout"]
    with patch.object(ex, "_get_git_diff", return_value=_REAL_DIFF), \
         patch("swarm.worker.l1_pipeline.run_l1_pipeline", return_value=(True, {})):
        det_ok, details = ex._deterministic_l1_gate()
    assert det_ok is None, f"pull-back error 时不得判 True，got {det_ok} {details}"


def test_gate_passes_when_pullback_clean():
    """回归：pull-back 干净（无 skip/err）→ pipeline True 正常判 True。"""
    ex = _mk(FileScope(writable=["A.java"]))
    ex._sync_skipped_count = 0
    ex._sync_error_rels = []
    with patch.object(ex, "_get_git_diff", return_value=_REAL_DIFF), \
         patch("swarm.worker.l1_pipeline.run_l1_pipeline", return_value=(True, {})):
        det_ok, details = ex._deterministic_l1_gate()
    assert det_ok is True, details


def test_gate_keeps_false_when_incomplete_and_pipeline_false():
    """pipeline 判 False 且 pull-back 不完整 → 仍 False（不因 A3 逻辑把 False 洗成 None）。"""
    ex = _mk(FileScope(writable=["A.java"]))
    ex._sync_skipped_count = 2
    ex._sync_error_rels = []
    with patch.object(ex, "_get_git_diff", return_value=_REAL_DIFF), \
         patch("swarm.worker.l1_pipeline.run_l1_pipeline", return_value=(False, {"deterministic_gate": "fail"})):
        det_ok, details = ex._deterministic_l1_gate()
    assert det_ok is False, details


if __name__ == "__main__":
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for fn in fns:
        fn()
        print(f"  ✅ {fn.__name__}")
    print(f"\n=== A3 pull-back 不完整不假绿: {len(fns)}/{len(fns)} passed ===")
