#!/usr/bin/env python3
"""W4b 生产加固：apply-diff 快照回滚机制 单测。"""

from __future__ import annotations

import importlib.util
import os
import subprocess
import tempfile
from pathlib import Path

_bs = Path(__file__).resolve().parent / "swarm_bootstrap.py"
_spec = importlib.util.spec_from_file_location("swarm_bootstrap", _bs)
_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_mod)


def _git_init(d: str) -> None:
    for args in (["init"], ["config", "user.email", "t@t"], ["config", "user.name", "t"]):
        subprocess.run(["git", *args], cwd=d, capture_output=True)


def test_snapshot_restore_modified_file():
    """修改已存在文件后，restore_snapshot 恢复原内容。"""
    from swarm.project.diff_apply import restore_snapshot, snapshot_files

    d = tempfile.mkdtemp(prefix="swarm_rb_")
    fp = os.path.join(d, "a.txt")
    with open(fp, "w") as f:
        f.write("original\n")
    snap = snapshot_files(d, ["a.txt"])
    with open(fp, "w") as f:
        f.write("MODIFIED\n")
    res = restore_snapshot(snap)
    assert res["ok"] and res["restored"] == 1
    assert open(fp).read() == "original\n", "未恢复原内容"
    print("  ✅ 快照恢复已修改文件")


def test_snapshot_restore_deletes_new_file():
    """对原本不存在的文件(diff 新建)，回滚应删除它。"""
    from swarm.project.diff_apply import restore_snapshot, snapshot_files

    d = tempfile.mkdtemp(prefix="swarm_rb_")
    snap = snapshot_files(d, ["new.txt"])  # 此时不存在
    # 模拟 apply 新建了它
    with open(os.path.join(d, "new.txt"), "w") as f:
        f.write("created by diff\n")
    res = restore_snapshot(snap)
    assert res["ok"] and res["deleted"] == 1
    assert not os.path.exists(os.path.join(d, "new.txt")), "新建文件未被回滚删除"
    print("  ✅ 回滚删除 diff 新建的文件")


def test_apply_with_backup_then_rollback():
    """apply_git_diff(backup_first=True) 返回 snapshot，可回滚到 apply 前。"""
    from swarm.project.diff_apply import apply_git_diff, restore_snapshot

    d = tempfile.mkdtemp(prefix="swarm_rb_")
    _git_init(d)
    fp = os.path.join(d, "code.txt")
    with open(fp, "w") as f:
        f.write("line1\nline2\n")
    subprocess.run(["git", "add", "."], cwd=d, capture_output=True)
    subprocess.run(["git", "commit", "-m", "init"], cwd=d, capture_output=True)

    diff = (
        "--- a/code.txt\n"
        "+++ b/code.txt\n"
        "@@ -1,2 +1,2 @@\n"
        " line1\n"
        "-line2\n"
        "+line2-CHANGED\n"
    )
    res = apply_git_diff(d, diff, backup_first=True)
    assert res["ok"], f"apply 失败: {res}"
    assert "snapshot" in res, "未返回快照句柄"
    assert "CHANGED" in open(fp).read(), "diff 未应用"

    # 回滚
    rb = restore_snapshot(res["snapshot"])
    assert rb["ok"]
    assert open(fp).read() == "line1\nline2\n", "回滚未恢复原内容"
    print("  ✅ apply(backup_first) + restore 回滚到 apply 前")


def test_discard_snapshot_cleanup():
    """discard_snapshot 清理临时备份目录。"""
    from swarm.project.diff_apply import discard_snapshot, snapshot_files

    d = tempfile.mkdtemp(prefix="swarm_rb_")
    with open(os.path.join(d, "a.txt"), "w") as f:
        f.write("x\n")
    snap = snapshot_files(d, ["a.txt"])
    bd = snap["backup_dir"]
    assert os.path.isdir(bd)
    discard_snapshot(snap)
    assert not os.path.isdir(bd), "备份目录未清理"
    print("  ✅ discard_snapshot 清理备份目录")


def main() -> int:
    print("=== test_diff_rollback ===")
    failed = 0
    for fn in (
        test_snapshot_restore_modified_file,
        test_snapshot_restore_deletes_new_file,
        test_apply_with_backup_then_rollback,
        test_discard_snapshot_cleanup,
    ):
        try:
            fn()
        except Exception as exc:
            failed += 1
            print(f"  ❌ {fn.__name__}: {exc}")
            import traceback

            traceback.print_exc()
    if failed:
        print(f"\n{failed} failed")
        return 1
    print("\nAll passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
