"""task 34fab09e C2 回归：worker 空 diff / 未过 L1 时置信度必须被校正为 LOW。

根因：worker 撞迭代上限(50)后可能产出空 diff，但 confidence 仍是 LLM 自报的 high，
误导 handle_failure 与人工审核（"high 置信度 + 空 diff" 假性成功）。
修复：DONE 前以确定性结果校正——L1 未通过 或 diff 为空 → confidence 强制 LOW。

注：这里只测「置信度校正」这个纯逻辑契约（不起沙箱）。校正逻辑见 executor.py DONE 前分支。
"""
from swarm.types import Confidence, WorkerOutput


def _correct_confidence(output: WorkerOutput) -> WorkerOutput:
    """复刻 executor.py DONE 前的置信度校正逻辑（单一事实源的契约镜像）。"""
    diff_empty = not (getattr(output, "diff", "") or "").strip()
    l1_ok = bool(getattr(output, "l1_passed", False))
    if (not l1_ok or diff_empty) and output.confidence != Confidence.LOW:
        return output.model_copy(update={"confidence": Confidence.LOW})
    return output


def _mk(diff, l1_passed, confidence):
    return WorkerOutput(
        subtask_id="st-1", diff=diff, summary="x",
        confidence=confidence, l1_passed=l1_passed,
    )


def test_empty_diff_high_confidence_corrected_to_low():
    """空 diff + 自报 high → 校正为 LOW（撞上限空转的典型假性成功）。"""
    out = _correct_confidence(_mk("", True, Confidence.HIGH))
    assert out.confidence == Confidence.LOW


def test_l1_failed_high_confidence_corrected_to_low():
    """有 diff 但 L1 未过 + 自报 high → 校正为 LOW。"""
    out = _correct_confidence(_mk("--- a/x\n+++ b/x\n@@ -1 +1 @@\n+y", False, Confidence.HIGH))
    assert out.confidence == Confidence.LOW


def test_valid_diff_l1_passed_high_kept():
    """有效 diff + L1 通过 + high → 保持 high（不误伤真成功）。"""
    out = _correct_confidence(_mk("--- a/x\n+++ b/x\n@@ -1 +1 @@\n+y", True, Confidence.HIGH))
    assert out.confidence == Confidence.HIGH


def test_whitespace_only_diff_corrected():
    """仅空白的 diff 视为空 → 校正为 LOW。"""
    out = _correct_confidence(_mk("   \n  \n", True, Confidence.HIGH))
    assert out.confidence == Confidence.LOW
