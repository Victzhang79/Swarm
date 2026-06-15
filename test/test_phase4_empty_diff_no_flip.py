"""P0-B 回归：Phase4 最终复核统一走 _deterministic_l1_gate，杜绝空 diff 翻盘为通过。

根因（task 37460a5b）：run() 的 Phase4 过去裸调 run_l1_pipeline()，绕过了
_deterministic_l1_gate 的 "empty_diff + expects_changes → False" 拦截。占位/空 diff
(如 "(无变更)") 经 run_l1_pipeline 走 "no diff changes → return True" → Phase4
"翻盘为通过 ✅"。结果：模型拒答/600s 超时/零产出的子任务被判 L1 通过。

修复正解：Phase4 与 Phase3 循环(L374)、trivial 通道(L1121)同源，统一过
_deterministic_l1_gate 拿三态(None/True/False)，det_ok is False 时禁止翻盘。
这是整体修复（消除三个 L1 调用点中漏改的特例），不是针对某任务写死。

本测试直击复现路径：空 diff + scope 期望变更 → 闸门必须判 False。
"""

from __future__ import annotations

from unittest.mock import patch

from swarm.types import (
    FileScope,
    SubTask,
    SubTaskDifficulty,
    SubTaskModality,
)
from swarm.worker.executor import WorkerExecutor


def _mk_executor(scope: FileScope, *, intent: str = "modify") -> WorkerExecutor:
    st = SubTask(
        id="st-1-1",
        description="实现 NumberUtils.isNumeric/toInt",
        difficulty=SubTaskDifficulty.MEDIUM,
        modality=SubTaskModality.TEXT,
        scope=scope,
        intent=intent,
    )
    return WorkerExecutor(subtask=st, project_path="/tmp/swarm-p0b-test")


def test_empty_diff_with_expected_changes_gate_fails():
    """task 37460a5b 复现：空 diff + scope 期望修改文件 → 确定性闸门判 False。"""
    scope = FileScope(writable=["ruoyi-common/.../NumberUtils.java"])
    ex = _mk_executor(scope)
    with patch.object(ex, "_get_git_diff", return_value="(无变更)"):
        det_ok, details = ex._deterministic_l1_gate()
    assert det_ok is False, details
    assert details.get("reason") == "empty_diff_but_changes_expected", details


def test_empty_diff_placeholder_variants_all_fail():
    """各种占位 diff 文本都应被识别为空 → 期望变更时判 False（不被当 'no diff changes' 放行）。"""
    scope = FileScope(create_files=["NewFile.java"])
    for placeholder in ["(无变更)", "(无法获取 git diff)", "", "   "]:
        ex = _mk_executor(scope)
        with patch.object(ex, "_get_git_diff", return_value=placeholder):
            det_ok, details = ex._deterministic_l1_gate()
        assert det_ok is False, f"placeholder={placeholder!r} 应判 False, got {det_ok} {details}"


def test_empty_diff_no_expected_changes_no_harness_returns_none():
    """空 diff 且 scope 不期望变更且无 harness → 三态 None（回退 LLM，不主动判失败也不翻盘）。"""
    scope = FileScope(readable=["only_read.py"])  # 无 writable/create_files
    ex = _mk_executor(scope)
    with patch.object(ex, "_get_git_diff", return_value="(无变更)"):
        det_ok, details = ex._deterministic_l1_gate()
    assert det_ok is None, details
    assert "skipped" in details.get("deterministic_gate", ""), details


def test_phase4_does_not_flip_when_gate_false():
    """端到端语义（不跑沙箱）：复刻 Phase4 翻盘判定，det_ok is False 时绝不翻盘。

    复刻 run() Phase4 的核心决策树：l1_passed 初始 False（循环内拒答/超时未通过），
    确定性闸门返回 False → 最终 l1_passed 必须保持 False。
    """
    scope = FileScope(writable=["a.java"])
    ex = _mk_executor(scope)
    l1_passed = False  # 循环内拒答/超时，未通过
    with patch.object(ex, "_get_git_diff", return_value="(无变更)"):
        det_ok, _ = ex._deterministic_l1_gate()
    if det_ok is False:
        l1_passed = False
    elif det_ok is True and not l1_passed:
        l1_passed = True  # 仅确定性通过才翻盘
    assert l1_passed is False, "空 diff 拒答任务绝不能被翻盘为通过（37460a5b 核心 bug）"


if __name__ == "__main__":
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for fn in fns:
        fn()
        print(f"  ✅ {fn.__name__}")
    print(f"\n=== P0-B Phase4 空 diff 不翻盘: {len(fns)}/{len(fns)} passed ===")
