#!/usr/bin/env python3
"""P0-3 round22：diff_apply 快照/回滚/apply 的路径边界校验（复现 ../ 逃逸）。

根因：files_from_unified_diff 提取的相对路径直接 `root / rel` 参与自研备份/恢复/删除链路，
从不校验落在 project_path 内 → diff 含 `../` 可 备份/覆盖/删除 工作区外文件（git apply 对 ../
有防护，但 snapshot/restore 独立于 git）。

治本：_rel_within_root 边界校验；snapshot/restore 跳过越界；apply 前预检越界即 fail-closed 拒绝。
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


def test_snapshot_skips_external():
    proj = Path(tempfile.mkdtemp())
    # 外部真实文件，snapshot 绝不能备份它
    external = Path(tempfile.gettempdir()) / "p03_external_round22.txt"
    external.write_text("secret")
    try:
        snap = da.snapshot_files(str(proj), ["src/A.java", "../../" + external.name])
        # 越界条目不应进入快照 entries（或至少不产生外部备份）
        ext_rel = "../../" + external.name
        assert ext_rel not in snap["entries"], "越界路径不得进入快照"
        print("  ✅ snapshot 跳过越界路径")
    finally:
        external.unlink(missing_ok=True)


def test_restore_never_deletes_external():
    proj = Path(tempfile.mkdtemp())
    external = Path(tempfile.gettempdir()) / "p03_restore_target_round22.txt"
    external.write_text("must survive")
    try:
        # 手工构造一个恶意 existed=False 越界条目，restore 绝不能 unlink 外部真实文件
        malicious = {
            "project_path": str(proj),
            "entries": {"../../" + external.name: {"existed": False, "backup": None}},
        }
        da.restore_snapshot(malicious)
        assert external.exists(), "回滚绝不能删除工作区外的真实文件（复现 bug）"
        print("  ✅ restore 不删越界外部文件")
    finally:
        external.unlink(missing_ok=True)


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


if __name__ == "__main__":
    test_rel_within_root()
    test_snapshot_skips_external()
    test_restore_never_deletes_external()
    test_apply_rejects_escaping_diff()
    test_apply_resilient_rejects_escaping_diff()
    print("\n✅ P0-3 diff_apply 路径边界全部通过")
