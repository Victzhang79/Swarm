#!/usr/bin/env python3
"""Blocker B (round21 全流程推演·post-MERGE 确定性缺陷)：learn_success 对 MODIFY 型文件静默丢产物。

缺陷链：VERIFY_L2 的 run_integration_review 编译后 `_reset_worktree_to_head` 把 merged_diff 涉及的
【MODIFY 型文件】checkout 回 HEAD(文件仍在、内容=HEAD 原版)。原 learn_success 只对【磁盘缺失】文件
重 apply→MODIFY 文件不缺失→跳过→按 HEAD 原样 commit→worker 修改静默丢弃。仅因历轮从未越 MERGE
而从未暴露。

治本：learn_success 不再看"是否缺失"，而是【先 reset merged_diff 涉及文件到 HEAD，再 resilient
apply】——HEAD-relative 补丁对干净 HEAD 必 apply，new+modify 全部正确落盘。

本套直接验证治本【机制】(reset→resilient apply)：MODIFY 文件在 HEAD 态(模拟 L2 reset 后)仍能重放出
worker 目标内容；NEW 文件缺失(L2 reset 删)仍能重建。
"""
from __future__ import annotations

import importlib.util
import subprocess
import tempfile
from pathlib import Path

_bs = Path(__file__).resolve().parent / "swarm_bootstrap.py"
_spec = importlib.util.spec_from_file_location("swarm_bootstrap", _bs)
_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_mod)

from swarm.brain.integration_review import _reset_worktree_to_head  # noqa: E402
from swarm.project.diff_apply import apply_git_diff_resilient  # noqa: E402


def _git(args, cwd):
    subprocess.run(["git", *args], cwd=str(cwd), check=True, capture_output=True, text=True)


def _apply_fix_mechanism(proj, merged_diff):
    """治本机制：先 reset 到 HEAD 再 resilient apply（learn_success round21 的做法）。"""
    _reset_worktree_to_head(proj, merged_diff)
    return apply_git_diff_resilient(proj, merged_diff)


def test_modify_file_at_head_gets_reapplied():
    """★核心★ MODIFY 文件被 L2 reset 回 HEAD(文件在、内容=原版)→治本后重放出 worker 目标内容。"""
    with tempfile.TemporaryDirectory() as dd:
        root = Path(dd)
        _git(["init", "-q"], root); _git(["config", "user.email", "t@t"], root)
        _git(["config", "user.name", "t"], root)
        f = root / "src/Svc.java"
        f.parent.mkdir(parents=True)
        f.write_text("class Svc {\n}\n")
        _git(["add", "-A"], root); _git(["commit", "-qm", "base"], root)
        # merged_diff：worker 往 Svc.java 里加了一个方法
        diff = (
            "diff --git a/src/Svc.java b/src/Svc.java\n"
            "--- a/src/Svc.java\n"
            "+++ b/src/Svc.java\n"
            "@@ -1,2 +1,3 @@\n"
            " class Svc {\n"
            "+  void ping() {}\n"
            " }\n"
        )
        # 模拟 VERIFY_L2 reset 后：Svc.java 仍在磁盘，但内容=HEAD 原版(worker 的改动已被 checkout 抹掉)
        assert f.read_text() == "class Svc {\n}\n"
        _ap = _apply_fix_mechanism(str(root), diff)
        assert _ap.get("ok"), _ap
        # 治本后：worker 的 ping() 必须回来(不再静默丢弃)
        assert "void ping()" in f.read_text(), "MODIFY 文件的 worker 修改必须被重放,不能丢"
    print("  ✅ MODIFY 文件(L2 reset 回 HEAD)→治本重放出 worker 改动,不丢产物")


def test_new_file_missing_gets_recreated():
    """NEW 文件被 L2 reset 删除(磁盘缺失)→治本后重建。"""
    with tempfile.TemporaryDirectory() as dd:
        root = Path(dd)
        _git(["init", "-q"], root); _git(["config", "user.email", "t@t"], root)
        _git(["config", "user.name", "t"], root)
        (root / "keep.txt").write_text("x\n")
        _git(["add", "-A"], root); _git(["commit", "-qm", "base"], root)
        diff = (
            "diff --git a/src/New.java b/src/New.java\n"
            "new file mode 100644\n"
            "--- /dev/null\n"
            "+++ b/src/New.java\n"
            "@@ -0,0 +1,1 @@\n"
            "+class New {}\n"
        )
        # 模拟 L2 reset 删了新建文件
        assert not (root / "src/New.java").exists()
        _ap = _apply_fix_mechanism(str(root), diff)
        assert _ap.get("ok"), _ap
        assert (root / "src/New.java").read_text().strip() == "class New {}"
    print("  ✅ NEW 文件(L2 reset 删)→治本重建")


if __name__ == "__main__":
    test_modify_file_at_head_gets_reapplied()
    test_new_file_missing_gets_recreated()
    print("\n✅ 全部通过：#Blocker B learn_success MODIFY 重放(round21 治本)")
