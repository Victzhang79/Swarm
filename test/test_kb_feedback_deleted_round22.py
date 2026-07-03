#!/usr/bin/env python3
"""#4(a) round22：任务回灌识别删除的文件 → 发 DELETED（复现"删除文件索引永不清"）。

根因：dispatch._feedback_to_knowledge 只 `^\\+\\+\\+ b/` 匹配 ADDED/MODIFIED，删除的 target 是
`+++ /dev/null` 匹配不到 → 从不发 DELETED → 任务交付删除的文件索引永不清。

治本：_changes_from_diff 纯函数额外识别 `--- a/X` + `+++ /dev/null` 删除段发 DELETED。
"""
from __future__ import annotations

import importlib.util
from pathlib import Path

_bs = Path(__file__).resolve().parent / "swarm_bootstrap.py"
_spec = importlib.util.spec_from_file_location("swarm_bootstrap", _bs)
_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_mod)

from swarm.brain.nodes.dispatch import _changes_from_diff  # noqa: E402
from swarm.knowledge.updater import ChangeType  # noqa: E402


def test_detects_added_modified_deleted():
    diff = (
        # 新增
        "diff --git a/src/New.java b/src/New.java\n"
        "--- /dev/null\n+++ b/src/New.java\n@@ -0,0 +1 @@\n+class New {}\n"
        # 修改
        "diff --git a/src/Mod.java b/src/Mod.java\n"
        "--- a/src/Mod.java\n+++ b/src/Mod.java\n@@ -1 +1 @@\n-a\n+b\n"
        # 删除
        "diff --git a/src/Gone.java b/src/Gone.java\n"
        "deleted file mode 100644\n"
        "--- a/src/Gone.java\n+++ /dev/null\n@@ -1 +0,0 @@\n-gone\n"
    )
    changes = _changes_from_diff(diff)
    by_path = {c.file_path: c.change_type for c in changes}
    assert by_path.get("src/New.java") == ChangeType.ADDED
    assert by_path.get("src/Mod.java") == ChangeType.MODIFIED
    assert by_path.get("src/Gone.java") == ChangeType.DELETED, "删除的文件必须发 DELETED（复现 bug：当前漏）"
    print("  ✅ ADDED/MODIFIED/DELETED 全识别")


def test_no_dup_deleted_vs_modified():
    # 纯删除段不应又被当 MODIFIED（--- a/Gone 也匹配 modify 的源端）
    diff = ("--- a/src/Gone.java\n+++ /dev/null\n@@ -1 +0,0 @@\n-x\n")
    changes = _changes_from_diff(diff)
    kinds = [c.change_type for c in changes if c.file_path == "src/Gone.java"]
    assert kinds == [ChangeType.DELETED], f"Gone 只应有一条 DELETED，得到 {kinds}"
    print("  ✅ 删除段不重复计为 MODIFIED")


def test_empty_diff():
    assert _changes_from_diff("") == []
    print("  ✅ 空 diff → 空")


if __name__ == "__main__":
    test_detects_added_modified_deleted()
    test_no_dup_deleted_vs_modified()
    test_empty_diff()
    print("\n✅ #4(a) 回灌 DELETED 识别全部通过")
