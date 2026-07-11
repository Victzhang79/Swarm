"""主题I X-1 残留（外部深审）：交付 apply 失败只记 degraded，不阻断 DONE = 假成功。

deliver 节点 apply merged_diff 全失败/不完整时写 degraded_reasons（delivery_apply_failed /
delivery_apply_incomplete），但 runner 终态只看 subtask-id 集（partial_delivery_ids），
delivery 失败信号从不参与 → 所有子任务成功但产物没落进用户项目，任务仍报 DONE（违 DONE 铁律）。
治：gates.delivery_incomplete 纳入任务级交付失败信号，runner 终态 `_partial_ids or
delivery_incomplete → PARTIAL`。诚实边界：delivery_commit_failed 不入（apply 已成功、变更在
工作树落盘，只是未提交 git 历史，交付本身已达成）。
"""
from __future__ import annotations

from swarm.brain.gates import delivery_incomplete, terminal_status


def test_x1_apply_failed_is_incomplete():
    assert delivery_incomplete({"degraded_reasons": ["delivery_apply_failed"]}) is True


def test_x1_apply_incomplete_is_incomplete():
    assert delivery_incomplete({"degraded_reasons": ["delivery_apply_incomplete"]}) is True


def test_x1_commit_failed_not_incomplete():
    """诚实边界：commit 失败=apply 已成功、变更在工作树落盘 → 交付已达成，不判 PARTIAL。"""
    assert delivery_incomplete({"degraded_reasons": ["delivery_commit_failed"]}) is False


def test_x1_other_degraded_not_incomplete():
    assert delivery_incomplete({"degraded_reasons": ["merge_secret_reported:high:x"]}) is False
    assert delivery_incomplete({"degraded_reasons": []}) is False
    assert delivery_incomplete({}) is False


def test_x1_mixed_reasons_apply_failed_wins():
    assert delivery_incomplete({
        "degraded_reasons": ["delivery_commit_failed", "delivery_apply_failed"]}) is True


# ── terminal_status 单一裁决（runner 落库据此判 DONE/PARTIAL）──

def test_x1_terminal_partial_on_delivery_apply_failed():
    """核心：子任务全成功（无 partial_ids）但交付 apply 失败 → 终态 PARTIAL 非 DONE。"""
    assert terminal_status({"degraded_reasons": ["delivery_apply_failed"]}) == "PARTIAL"


def test_x1_terminal_done_on_commit_failed_only():
    """commit 失败（apply 已成功、变更落盘）单独不降级——诚实边界。"""
    assert terminal_status({"degraded_reasons": ["delivery_commit_failed"]}) == "DONE"


def test_x1_terminal_done_clean():
    assert terminal_status({}) == "DONE"
    assert terminal_status({"degraded_reasons": ["merge_secret_reported:high:x"]}) == "DONE"


def test_x1_terminal_partial_on_subtask_abandon_still_holds():
    """既有子任务级 PARTIAL 判据不回归。"""
    assert terminal_status({"abandoned_subtask_ids": ["st-3"]}) == "PARTIAL"


def test_x1_terminal_partial_when_both():
    assert terminal_status({
        "abandoned_subtask_ids": ["st-3"],
        "degraded_reasons": ["delivery_apply_failed"]}) == "PARTIAL"


if __name__ == "__main__":
    print("run via pytest")
