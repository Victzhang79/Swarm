#!/usr/bin/env python3
"""#10 回归：files_from_unified_diff 须覆盖纯删除 + 重命名（源端文件），
否则快照漏备份→回滚无法恢复被删/被改名文件。"""

from __future__ import annotations

from swarm.project.diff_apply import files_from_unified_diff


def test_added_and_modified():
    diff = (
        "diff --git a/foo.py b/foo.py\n"
        "--- a/foo.py\n"
        "+++ b/foo.py\n"
        "@@ -1 +1 @@\n-x\n+y\n"
    )
    assert files_from_unified_diff(diff) == ["foo.py"]


def test_delete_only_captured():
    """纯删除：+++ /dev/null 被跳过，须从 --- a/ 采集源端，否则回滚不恢复被删文件。"""
    diff = (
        "diff --git a/gone.py b/gone.py\n"
        "deleted file mode 100644\n"
        "--- a/gone.py\n"
        "+++ /dev/null\n"
        "@@ -1 +0,0 @@\n-bye\n"
    )
    assert files_from_unified_diff(diff) == ["gone.py"]


def test_rename_captures_both_sides():
    diff = (
        "diff --git a/old.py b/new.py\n"
        "similarity index 100%\n"
        "rename from old.py\n"
        "rename to new.py\n"
    )
    out = files_from_unified_diff(diff)
    assert "old.py" in out and "new.py" in out, out


def test_pure_addition_skips_dev_null():
    diff = (
        "diff --git a/added.py b/added.py\n"
        "new file mode 100644\n"
        "--- /dev/null\n"
        "+++ b/added.py\n"
        "@@ -0,0 +1 @@\n+hi\n"
    )
    assert files_from_unified_diff(diff) == ["added.py"]


if __name__ == "__main__":
    test_added_and_modified()
    test_delete_only_captured()
    test_rename_captures_both_sides()
    test_pure_addition_skips_dev_null()
    print("ok")
