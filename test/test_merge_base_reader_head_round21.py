#!/usr/bin/env python3
"""P0 (round21 起跑前推演·Agent B 交付链路)：MERGE base_reader 读 git HEAD 而非污染工作区。

round19 真死因 apply_ok=False（史上首次到 MERGE 却死在 git apply）取证：merged diff 88 文件全是
`--- a/` modify 补丁、0 个 `--- /dev/null` create 补丁。根因＝`_make_base_reader`(nodes:2951) 读
【工作区 project_path】，而 pull-back 已把完成子任务产物 materialize 进工作区 → 新模块文件
(ruoyi-alarm/pom.xml 等) 被 base_reader 读到 → `is_new=False` → 发带沙箱相对 base 偏移的 modify
hunk → 纯净 git HEAD 无此文件 → `git apply --check` 必失败。round17 的 is_new→纯新建补丁修法被
pull-back 抵消。worker 的 diff 本就相对 git HEAD 生成，故 merge base 必须同源读 HEAD。

治本：`_make_base_reader` 改读 `git show HEAD:<rel>`；HEAD 无此文件→None→is_new=True→create 补丁。
非 git 仓 / git 异常 → 退回工作区读（不回归）。

本套：① HEAD 有的文件读到 HEAD 版(非工作区污染版)；② HEAD 无但工作区有(pull-back)的新文件→None；
③ 非 git 仓 → 退回工作区读；④ 端到端：新模块文件经 merge_diffs → 出 `--- /dev/null` create 补丁且
`git apply --check` 通过。
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

from swarm.brain import nodes as bn  # noqa: E402
from swarm.brain.merge_engine import merge_diffs  # noqa: E402


def _git(args, cwd):
    subprocess.run(["git", *args], cwd=str(cwd), check=True,
                   capture_output=True, text=True)


def _init_repo(root: Path):
    _git(["init", "-q"], root)
    _git(["config", "user.email", "t@t"], root)
    _git(["config", "user.name", "t"], root)


def test_base_reader_reads_head_not_polluted_worktree(monkeypatch):
    with tempfile.TemporaryDirectory() as dd:
        root = Path(dd)
        _init_repo(root)
        # baseline HEAD：existing.txt = "HEAD-v1"；无 newmod/pom.xml
        (root / "existing.txt").write_text("HEAD-v1\n")
        _git(["add", "-A"], root)
        _git(["commit", "-qm", "base"], root)
        # 工作区污染（模拟 pull-back）：改 existing.txt + 新建 newmod/pom.xml
        (root / "existing.txt").write_text("WORKING-polluted\n")
        (root / "newmod").mkdir()
        (root / "newmod/pom.xml").write_text("<project/>\n")

        monkeypatch.setattr(bn, "_get_project_path", lambda pid: str(root))
        reader = bn._make_base_reader({"project_id": "p"})
        # ① 既有文件读 HEAD 版，不是被污染的工作区版
        assert reader("existing.txt") == "HEAD-v1\n"
        assert reader("a/existing.txt") == "HEAD-v1\n"   # a/ 前缀剥离
        # ② HEAD 无、工作区有的新文件 → None（→ is_new=True）
        assert reader("newmod/pom.xml") is None
    print("  ✅ ① HEAD 版优先(非污染工作区)；② pull-back 新文件→None")


def test_base_reader_binary_file_in_head_no_crash(monkeypatch):
    """对抗审计必修：HEAD 里的二进制文件不得让 base_reader 抛 UnicodeDecodeError 崩 MERGE。"""
    with tempfile.TemporaryDirectory() as dd:
        root = Path(dd)
        _init_repo(root)
        # 提交一个含无效 UTF-8 字节的二进制文件到 HEAD
        (root / "favicon.ico").write_bytes(b"\x89PNG\xff\xfe\x00\x01\x80\x81binary")
        _git(["add", "-A"], root)
        _git(["commit", "-qm", "bin"], root)
        monkeypatch.setattr(bn, "_get_project_path", lambda pid: str(root))
        reader = bn._make_base_reader({"project_id": "p"})
        v = reader("favicon.ico")   # 绝不抛（errors="replace"）
        assert isinstance(v, str) and v != ""   # 返回替换后的字符串，非崩溃/非 None
    print("  ✅ ⑤ HEAD 二进制文件 → errors=replace 返字符串，不崩 MERGE")


def test_base_reader_non_git_falls_back_to_worktree(monkeypatch):
    with tempfile.TemporaryDirectory() as dd:
        root = Path(dd)  # 无 .git
        (root / "f.txt").write_text("disk-content\n")
        monkeypatch.setattr(bn, "_get_project_path", lambda pid: str(root))
        reader = bn._make_base_reader({"project_id": "p"})
        assert reader("f.txt") == "disk-content\n"   # 退回工作区读（不回归）
        assert reader("missing.txt") is None
    print("  ✅ ③ 非 git 仓 → 退回工作区读（不回归）")


def test_new_module_file_emits_create_patch_and_applies(monkeypatch):
    """④ 端到端：新模块文件（HEAD 无）经 merge_diffs → create 补丁 → git apply --check 通过。"""
    with tempfile.TemporaryDirectory() as dd:
        root = Path(dd)
        _init_repo(root)
        (root / "existing.txt").write_text("HEAD-v1\n")
        _git(["add", "-A"], root)
        _git(["commit", "-qm", "base"], root)
        # pull-back 把新模块文件落进工作区
        (root / "newmod").mkdir()
        (root / "newmod/pom.xml").write_text("<project><artifactId>x</artifactId></project>\n")

        monkeypatch.setattr(bn, "_get_project_path", lambda pid: str(root))
        reader = bn._make_base_reader({"project_id": "p"})

        # worker 发的是 modify 风格 hunk（沙箱相对，@@ -1,x 带 context）——正是 round19 的坏形态
        worker_diff = (
            "diff --git a/newmod/pom.xml b/newmod/pom.xml\n"
            "--- a/newmod/pom.xml\n"
            "+++ b/newmod/pom.xml\n"
            "@@ -0,0 +1,1 @@\n"
            "+<project><artifactId>x</artifactId></project>\n"
        )
        res = merge_diffs([("st-1", worker_diff)], base_reader=reader, auto_resolve=True)
        # base_reader 判 HEAD 无 newmod/pom.xml → is_new=True → 纯新建补丁
        assert "--- /dev/null" in res.merged_diff, res.merged_diff
        # 且能被 git apply --check 接受（对纯净 HEAD）——先把工作区新文件移走还原到 HEAD 态
        (root / "newmod/pom.xml").unlink()
        (root / "newmod").rmdir()
        # 与真实交付护栏 verify_merged_patch_applies(merge_engine:995) 同源：补末尾换行再 apply
        _patch = res.merged_diff if res.merged_diff.endswith("\n") else res.merged_diff + "\n"
        r = subprocess.run(["git", "apply", "--check", "-"], cwd=str(root),
                           input=_patch, capture_output=True, text=True)
        assert r.returncode == 0, f"git apply --check 应通过，stderr={r.stderr}"
    print("  ✅ ④ 新模块文件→create 补丁(--- /dev/null)且 git apply --check 通过")


if __name__ == "__main__":
    import pytest
    raise SystemExit(pytest.main([__file__, "-q", "-p", "no:warnings"]))
