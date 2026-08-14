#!/usr/bin/env python3
"""30 号文批14 F-2 锁：diff_apply 快照/回滚机制整链拆除（死代码+假安全网比没有更坏）。

存废拍板（自动模式下按登记册判据落地，可经 git 回退）：
- 8 个生产 apply 点无人传 `backup_first=True`（grep 机器复算=0 调用点）；
- `apply_git_diff` 走 `git apply --check` + 原子 `git apply`——失败即全不落地，
  快照回滚在该路径【本就无对象可回】；
- `apply_git_diff_resilient` 的设计哲学是【分文件部分落盘+failed 账交 owner 重修】
  （round18 P0-C 治本），与「整体回滚」语义互斥——接线即推翻该设计；
- 机制还带分支洞（`existed=True, backup=None` 时 restore 静默跳过却报 ok=True，
  部分回滚伪装完全成功）——4 条全绿测试=假安全网，真要用第一跤就踩。
⇒ 删除整套机制（snapshot_files/restore_snapshot/discard_snapshot/backup_first），
不接线。P0-3 边界防护保留在 apply 前预检（diff_paths_escape_root），不随机制删除。

本文件的锁=「假安全网入口已拆」的契约断言：未来任何人重新引入 backup_first 形参或
snapshot_* 公开 API 而不接线完整回滚语义，本文件红。
"""
from __future__ import annotations

import tempfile

import pytest

from swarm.project import diff_apply as da


def test_apply_git_diff_rejects_backup_first_kwarg():
    """backup_first 形参已随机制删除——传它必须 TypeError（fail-loud），
    绝不静默接受一个「看似有回滚保护」的假承诺。"""
    diff = "--- a/x.txt\n+++ b/x.txt\n@@ -1 +1 @@\n-a\n+b\n"
    with tempfile.TemporaryDirectory() as d:
        with pytest.raises(TypeError):
            da.apply_git_diff(d, diff, backup_first=True)


def test_snapshot_api_surface_removed():
    """snapshot_files/restore_snapshot/discard_snapshot 三个公开 API 全部拆除。"""
    for name in ("snapshot_files", "restore_snapshot", "discard_snapshot"):
        assert not hasattr(da, name), \
            f"{name} 应已随 F-2 删除——复活它=复活分支洞与假安全网"


def test_boundary_defense_survives_mechanism_removal():
    """P0-3 越界预检不随快照机制删除：含 ../ 逃逸的 diff 仍 fail-closed 拒绝
    （防御从「快照链边界校验」收敛到「apply 前预检」单点，该点必须在）。"""
    evil_diff = (
        "diff --git a/../../tmp/pwned b/../../tmp/pwned\n"
        "--- /dev/null\n"
        "+++ b/../../tmp/pwned\n"
        "@@ -0,0 +1 @@\n"
        "+pwned\n"
    )
    with tempfile.TemporaryDirectory() as d:
        res = da.apply_git_diff(d, evil_diff)
    assert res["ok"] is False and res.get("stage") == "boundary", \
        "快照机制删除后 apply 前越界预检必须在（P0-3 防线不塌）"
