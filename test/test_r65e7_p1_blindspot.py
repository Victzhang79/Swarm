"""R65E7 兜底 L1：P1 外科补齐盲区——build_coverage_matrix 不得把 T1-拒的假 baseline 当已覆盖。

死因链下游（round65e7）：pass-1 P1 谎报 6 条假 baseline_covered→进 state。pass-2+ T1 在 VALIDATE
拦下（缺基线证据），但 P1 闸的 build_coverage_matrix 见这些 req 在 baseline_ids→算已覆盖→uncovered=0
→P1「覆盖已满足」return None→回退全量重拆→丢已覆盖回归振荡→3 retry 耗尽 FAILED@PLAN。

治本：build_coverage_matrix 加可选 baseline_vocab——传入时把【无基线证据】的 baseline 申报（T1 口径
同源 baseline_claims_missing_evidence）踢出 baseline_ids→它们如实变 uncovered→P1 据真相 engage
（剥假 baseline+定向补），不再据幻影覆盖回退全量重拆。不传 vocab→逐字节不变（向后兼容）。
"""
from __future__ import annotations

from swarm.brain.baseline_candidates import build_baseline_vocab
from swarm.brain.plan_validator import build_coverage_matrix
from swarm.types import FileScope, SubTask, SubTaskDifficulty, TaskPlan

REQ_2FA = "req-4067d7fb"
REQ_USER = "req-e2e2e2e2"


def _items():
    return [
        {"id": REQ_2FA, "text": "支持Google 2FA双因素认证，包含绑定/解绑/验证。"},
        {"id": REQ_USER, "text": "用户管理支持Excel导入导出。"},
    ]


def _vocab():
    # 基线有 Excel 存量，无 2FA
    return build_baseline_vocab(
        [{"file_path": "ruoyi-common/utils/poi/ExcelUtil.java", "module_name": "ruoyi-common"}],
        [{"file_path": "ruoyi-common/utils/poi/ExcelUtil.java",
          "symbol_name": "importExcel", "class_name": "ExcelUtil"}])


def _plan():
    # 两条需求都没被任何子任务 covers（只能靠 baseline 申报）
    st = SubTask(id="st-1", description="x", difficulty=SubTaskDifficulty.MEDIUM,
                 scope=FileScope(writable=["a.java"], readable=[]), covers=[])
    return TaskPlan(subtasks=[st], parallel_groups=[["st-1"]])


def test_without_vocab_backward_compat_baseline_counts_covered():
    """不传 baseline_vocab → 假 2FA baseline 仍被当已覆盖（逐字节向后兼容，不改既有调用点）。"""
    bc = [{"id": REQ_2FA, "reason": "内置于 SysUser"}, {"id": REQ_USER, "reason": "ExcelUtil"}]
    m = build_coverage_matrix(_plan(), _items(), bc)
    unc = {u["id"] for u in m["uncovered"]}
    assert REQ_2FA not in unc and REQ_USER not in unc, f"无 vocab 时 baseline 申报算覆盖；uncovered={unc}"


def test_with_vocab_evidenceless_baseline_becomes_uncovered():
    """★RED 核★ 传 baseline_vocab → 无证据的假 2FA baseline 被踢出覆盖→如实 uncovered；
    有证据的 Excel baseline 仍算覆盖（不误伤合法存量申报）。"""
    bc = [{"id": REQ_2FA, "reason": "内置于 SysUser"}, {"id": REQ_USER, "reason": "ExcelUtil"}]
    m = build_coverage_matrix(_plan(), _items(), bc, baseline_vocab=_vocab())
    unc = {u["id"] for u in m["uncovered"]}
    assert REQ_2FA in unc, f"无证据的假 2FA baseline 应变 uncovered；uncovered={unc}"
    assert REQ_USER not in unc, f"有证据的 Excel baseline 不该被误踢；uncovered={unc}"


def test_empty_vocab_fail_open_no_exclusion():
    """baseline_vocab 空 → fail-open，不踢任何 baseline（与 T1 同纪律，缺索引不误伤）。"""
    bc = [{"id": REQ_2FA, "reason": "内置于 SysUser"}]
    m = build_coverage_matrix(_plan(), _items(), bc, baseline_vocab="")
    unc = {u["id"] for u in m["uncovered"]}
    assert REQ_2FA not in unc, f"空 vocab 应 fail-open 不踢 baseline；uncovered={unc}"


def test_covered_by_subtask_unaffected_by_vocab():
    """被子任务真 covers 的需求，无论 vocab 如何都算覆盖（vocab 只作用于 baseline 申报路径）。"""
    st = SubTask(id="st-1", description="x", difficulty=SubTaskDifficulty.MEDIUM,
                 scope=FileScope(writable=["a.java"], readable=[]), covers=[REQ_2FA])
    plan = TaskPlan(subtasks=[st], parallel_groups=[["st-1"]])
    m = build_coverage_matrix(plan, _items(), None, baseline_vocab=_vocab())
    unc = {u["id"] for u in m["uncovered"]}
    assert REQ_2FA not in unc, f"子任务真 covers 的需求不受 vocab 影响；uncovered={unc}"
