#!/usr/bin/env python3
"""P0-3 round22：diff_apply 的路径边界校验（复现 ../ 逃逸）。

根因：files_from_unified_diff 提取的相对路径直接 `root / rel` 参与落盘链路，
从不校验落在 project_path 内 → diff 含 `../` 可写工作区外文件。

治本：_rel_within_root 边界校验 + apply 前预检越界即 fail-closed 拒绝。

★30 号文批14 F-2★：原自研快照/回滚链（snapshot_files/restore_snapshot/discard_snapshot）
已整链删除（零生产调用点+分支洞=假安全网），本文件原两条快照链边界测试随之退役
（防御对象已不存在）；apply 前预检两条边界锁保留——P0-3 防线现收敛于该单点。
"""
from __future__ import annotations

import importlib.util
import tempfile
from pathlib import Path

_bs = Path(__file__).resolve().parent / "swarm_bootstrap.py"
_spec = importlib.util.spec_from_file_location("swarm_bootstrap", _bs)
_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_mod)

from swarm.project import diff_apply as da  # noqa: E402


def test_rel_within_root():
    root = Path(tempfile.mkdtemp())
    assert da._rel_within_root(root, "src/A.java") is True
    assert da._rel_within_root(root, "../../etc/hosts") is False
    assert da._rel_within_root(root, "../outside.txt") is False
    print("  ✅ _rel_within_root 边界判定")


def test_apply_rejects_escaping_diff():
    proj = Path(tempfile.mkdtemp())
    evil_diff = (
        "diff --git a/../../tmp/pwned b/../../tmp/pwned\n"
        "--- /dev/null\n"
        "+++ b/../../tmp/pwned\n"
        "@@ -0,0 +1 @@\n"
        "+pwned\n"
    )
    res = da.apply_git_diff(str(proj), evil_diff)
    assert res["ok"] is False, "含越界路径的 diff 必须 fail-closed 拒绝"
    assert res.get("stage") == "boundary", f"应标注 boundary 拒绝，得到 {res.get('stage')}"
    print("  ✅ apply 越界 diff → fail-closed 拒绝")


def test_apply_resilient_rejects_escaping_diff():
    proj = Path(tempfile.mkdtemp())
    evil_diff = "--- a/../../tmp/x\n+++ b/../../tmp/x\n@@ -1 +1 @@\n-a\n+b\n"
    res = da.apply_git_diff_resilient(str(proj), evil_diff)
    assert res["ok"] is False and res.get("stage") == "boundary"
    print("  ✅ apply_resilient 越界 → fail-closed 拒绝")


def test_apply_rejects_pure_delete_source_escape():
    """30 号文批14 F-2 hunter 建议：纯删除段（+++ /dev/null）的逃逸路径只存在于
    `--- a/` 源端——源端采集若漏，预检对该形态完全失明（P0-3 收敛单点的覆盖缺口）。"""
    proj = Path(tempfile.mkdtemp())
    evil_diff = (
        "diff --git a/../../tmp/victim b/../../tmp/victim\n"
        "--- a/../../tmp/victim\n"
        "+++ /dev/null\n"
        "@@ -1 +0,0 @@\n"
        "-secret\n"
    )
    res = da.apply_git_diff(str(proj), evil_diff)
    assert res["ok"] is False and res.get("stage") == "boundary", \
        "纯删除段源端越界必须 fail-closed 拒绝"
    print("  ✅ 纯删除段源端越界 → 拒绝")


def test_apply_rejects_rename_from_escape():
    """rename 旧名只出现在 `rename from`——旧名逃逸必须被预检逮到。"""
    proj = Path(tempfile.mkdtemp())
    evil_diff = (
        "diff --git a/../../tmp/old b/ok.txt\n"
        "similarity index 90%\n"
        "rename from ../../tmp/old\n"
        "rename to ok.txt\n"
    )
    res = da.apply_git_diff(str(proj), evil_diff)
    assert res["ok"] is False and res.get("stage") == "boundary", \
        "rename 旧名越界必须 fail-closed 拒绝"
    print("  ✅ rename 旧名越界 → 拒绝")


if __name__ == "__main__":
    test_rel_within_root()
    test_apply_rejects_escaping_diff()
    test_apply_resilient_rejects_escaping_diff()
    test_apply_rejects_pure_delete_source_escape()
    test_apply_rejects_rename_from_escape()
    print("\n✅ P0-3 diff_apply 路径边界全部通过")
