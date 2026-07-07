#!/usr/bin/env python3
"""D01 治本单测 —— dispatch 成功判据按【预期变更形态】驱动，而非"存在 + 行"。

旧 bug（`_diff_has_changes` 只认 `+` 行）：
  · AUDIT 子任务：审计通过 = 空 diff + l1_passed=True → 被判失败 → 反复重试至 abandon，审计无成功终态；
  · 纯删除子任务：diff 只有 `-` 行 + `+++ /dev/null` → 无 `+` 行 → 同样被误判失败。
治本：引入 `_subtask_produced_expected(worker_output, subtask)`——AUDIT 以 l1_passed 为准（空 diff 合法），
纯删除 scope 认删除段，普通子任务维持 `+` 行判据（fail-closed，空产出仍失败）。栈/后缀/项目无关。
"""
from __future__ import annotations

import importlib.util
from pathlib import Path

_bs = Path(__file__).resolve().parent / "swarm_bootstrap.py"
_spec = importlib.util.spec_from_file_location("swarm_bootstrap", _bs)
_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_mod)

from swarm.brain.nodes.shared import (  # noqa: E402
    _diff_has_changes,
    _diff_has_deletions,
    _is_pure_delete_scope,
    _subtask_produced_expected,
)
from swarm.types import (  # noqa: E402
    Confidence,
    FileScope,
    SubTask,
    SubTaskDifficulty,
    SubTaskModality,
    TaskIntent,
    WorkerOutput,
)


def _sub(sid, intent=TaskIntent.MODIFY, scope=None):
    return SubTask(
        id=sid, description=f"task {sid}", intent=intent,
        difficulty=SubTaskDifficulty.MEDIUM, modality=SubTaskModality.TEXT,
        scope=scope or FileScope(writable=["a.py"]),
    )


def _out(sid, diff="", l1=True):
    return WorkerOutput(
        subtask_id=sid, diff=diff, summary="", confidence=Confidence.HIGH, l1_passed=l1,
    )


# 复现 dispatch 的成功判据（与 dispatch.py 一致）：L1 通过 且 产出符合预期形态。
def _is_success(out, sub):
    return bool(out.l1_passed) and _subtask_produced_expected(out, sub)


_DELETE_DIFF = "--- a/old.py\n+++ /dev/null\n@@ -1,2 +0,0 @@\n-line1\n-line2\n"


def test_old_criterion_would_fail_audit_and_delete():
    # 坐实旧判据的病根：空 diff 与纯删除 diff 都无 `+` 行 → 旧 `_diff_has_changes` 恒 False。
    assert _diff_has_changes("") is False
    assert _diff_has_changes(_DELETE_DIFF) is False
    print("  ✅ 旧判据对 AUDIT 空 diff / 纯删除 diff 均误判无产出")


def test_audit_pass_is_success():
    sub = _sub("st-audit", intent=TaskIntent.AUDIT)
    out = _out("st-audit", diff="", l1=True)  # 审计通过=空 diff
    assert _subtask_produced_expected(out, sub) is True
    assert _is_success(out, sub) is True, "AUDIT 干净扫描应成功终态，不进 failed"
    print("  ✅ AUDIT 通过(空 diff + l1_passed) → 成功")


def test_audit_block_is_failure():
    # 审计发现高危 → l1_passed=False → 仍应判失败（进恢复而非误当成功）。
    sub = _sub("st-audit", intent=TaskIntent.AUDIT)
    out = _out("st-audit", diff="", l1=False)
    assert _is_success(out, sub) is False
    print("  ✅ AUDIT 阻断(l1_passed=False) → 失败（不被误当成功）")


def test_pure_delete_is_success():
    scope = FileScope(delete_files=["old.py"])
    assert _is_pure_delete_scope(scope) is True
    sub = _sub("st-del", scope=scope)
    out = _out("st-del", diff=_DELETE_DIFF, l1=True)
    assert _diff_has_deletions(_DELETE_DIFF) is True
    assert _subtask_produced_expected(out, sub) is True
    assert _is_success(out, sub) is True, "纯删除子任务产出删除段应成功"
    print("  ✅ 纯删除子任务(只有 - 行 + /dev/null) → 成功")


def test_normal_empty_diff_still_fails():
    # fail-closed：普通(非 AUDIT/非纯删除)子任务空产出仍判失败。
    sub = _sub("st-mod")
    out = _out("st-mod", diff="", l1=True)
    assert _subtask_produced_expected(out, sub) is False
    assert _is_success(out, sub) is False
    print("  ✅ 普通子任务空 diff → 仍失败(fail-closed)")


def test_normal_with_additions_is_success():
    sub = _sub("st-mod")
    out = _out("st-mod", diff="--- a/a.py\n+++ b/a.py\n@@ -0,0 +1 @@\n+new line\n", l1=True)
    assert _is_success(out, sub) is True
    print("  ✅ 普通子任务有 + 变更 → 成功")


def test_pure_delete_scope_requires_only_delete():
    # writable/create_files/allow_any 任一非空 → 非纯删除（走普通判据）。
    assert _is_pure_delete_scope(FileScope(delete_files=["x"], writable=["y"])) is False
    assert _is_pure_delete_scope(FileScope(delete_files=["x"], create_files=["z"])) is False
    assert _is_pure_delete_scope(FileScope(delete_files=["x"], allow_any=True)) is False
    assert _is_pure_delete_scope(FileScope(writable=["y"])) is False
    print("  ✅ 纯删除 scope 判据严格(混写/allow_any 不算纯删除)")


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
    print("D01 全部通过")
