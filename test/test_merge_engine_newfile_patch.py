#!/usr/bin/env python3
"""merge_engine 新文件 diff 组装治本单测（round16 治本 Fix 1a/1b/1c）。

round16b 实测：merge_engine 拼出的 350789 字符合并 patch 对 `git apply --check` 第 2289 行损坏 →
VERIFY_L2 阻断交付 → 全量 replan → 中止。三代理取证定位到两个确定性 bug + 缺护栏：
- Fix 1a：`_recount_hunk_header` 把 `\\ No newline at end of file` 标记误当 context 计数 →
  全部新文件头从 `@@ -0,0` 变 `@@ -0,1`（引用不存在的旧行 0）→ 补丁损坏。
- Fix 1b：`_lines_to_unified_diff` 行尾翻倍 → hunk 体内空行 → 头声明行数与实际不符 → 越界损坏。
- Fix 1c：`verify_merged_patch_applies` fail-closed 护栏——合并 patch 交付前 git apply --check。
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

from swarm.brain.merge_engine import (
    _lines_to_unified_diff,
    _recount_hunk_header,
    merge_diffs,
    verify_merged_patch_applies,
)


# ── Fix 1a：\ No newline 标记不计入 old/new ──
def test_recount_skips_no_newline_marker():
    # 新文件 3 行 + git 的 `\ No newline` 标记 → 头应保持 -0,0 +1,3（标记两侧都不计）
    body = ["+line1", "+line2", "+line3", "\\ No newline at end of file"]
    hdr = _recount_hunk_header("@@ -0,0 +1,3 @@", body)
    assert hdr == "@@ -0,0 +1,3 @@", f"新文件头应 -0,0，实际 {hdr!r}"
    print("  ✅ Fix1a：`\\ No newline` 标记不被误计 → 新文件头 -0,0")


def test_recount_still_counts_real_context():
    # 真正的 context 行(" " 开头)仍两侧各 +1，别改坏正常计数
    body = [" ctx", "+add", "-del", "\\ No newline at end of file"]
    hdr = _recount_hunk_header("@@ -10,2 +10,2 @@", body)
    assert hdr == "@@ -10,2 +10,2 @@", f"old=ctx+del=2 new=ctx+add=2，实际 {hdr!r}"
    print("  ✅ Fix1a：真 context 仍正确计数，marker 跳过")


# ── Fix 1b：无行尾翻倍空行 ──
def test_lines_to_unified_diff_no_doubled_newlines():
    base = "line-a\nline-b\nline-c\n"
    merged = "line-a\nINSERTED\nline-b\nline-c\n"
    out = _lines_to_unified_diff("pom.xml", base, merged)
    assert "\n\n" not in out, f"不应有行尾翻倍空行:\n{out!r}"
    assert out and "+INSERTED" in out
    print("  ✅ Fix1b：_lines_to_unified_diff 无 \\n\\n 翻倍空行")


# ── 端到端：新文件 diff（LLM 常见的无结尾换行）经 merge_diffs 组装后应 git-apply-able ──
def _temp_git_repo(files: dict[str, str]) -> str:
    d = tempfile.mkdtemp(prefix="mergetest_")
    subprocess.run(["git", "init", "-q"], cwd=d, check=True)
    subprocess.run(["git", "config", "user.email", "t@t"], cwd=d, check=True)
    subprocess.run(["git", "config", "user.name", "t"], cwd=d, check=True)
    for p, c in files.items():
        fp = Path(d, p)
        fp.parent.mkdir(parents=True, exist_ok=True)
        fp.write_text(c)
    subprocess.run(["git", "add", "-A"], cwd=d, check=True)
    subprocess.run(["git", "commit", "-qm", "base"], cwd=d, check=True)
    return d


def test_merged_newfile_patch_applies_cleanly():
    # 模拟 worker 对新文件的真实 git diff（--- /dev/null + 无结尾换行 + \ No newline 标记）
    newfile_diff = (
        "--- /dev/null\n"
        "+++ b/src/NewClass.java\n"
        "@@ -0,0 +1,3 @@\n"
        "+package com.x;\n"
        "+public class NewClass {}\n"
        "+// tail\n"
        "\\ No newline at end of file"
    )
    result = merge_diffs([("st-1", newfile_diff)])
    # 组装后的头应是 -0,0（Fix1a），且能干净 apply（Fix1a+1b 合力）
    assert "@@ -0,1" not in result.merged_diff, f"不应出现畸形 -0,1 头:\n{result.merged_diff}"
    repo = _temp_git_repo({"README.md": "x\n"})
    ok, err = verify_merged_patch_applies(repo, result.merged_diff)
    assert ok, f"合并 patch 应能 git apply，但失败: {err}\n---\n{result.merged_diff}"
    print("  ✅ e2e：新文件合并 patch git-apply-able（Fix1a+1b）")


# ── Fix 1c：护栏 ──
def test_guardrail_rejects_corrupt_minus01_patch():
    # 已知畸形 -0,1 头（round16 的病）必须被护栏判失败
    corrupt = (
        "--- a/Bad.java\n"
        "+++ b/Bad.java\n"
        "@@ -0,1 +1,2 @@\n"
        "+package com.x;\n"
        "+public class Bad {}\n"
    )
    repo = _temp_git_repo({"README.md": "x\n"})
    ok, err = verify_merged_patch_applies(repo, corrupt)
    assert not ok, "畸形 -0,1 patch 必须被护栏 git apply --check 判失败"
    assert err
    print("  ✅ Fix1c：护栏拒绝畸形 -0,1 patch")


def test_guardrail_accepts_wellformed_and_noops():
    wellformed = (
        "--- a/New.java\n"
        "+++ b/New.java\n"
        "@@ -0,0 +1,1 @@\n"
        "+class New {}\n"
    )
    repo = _temp_git_repo({"README.md": "x\n"})
    assert verify_merged_patch_applies(repo, wellformed)[0] is True, "合法新文件 patch 应通过"
    # 空 diff / 无 git 工作树 → ok（不误报，不假阻断）
    assert verify_merged_patch_applies(None, "")[0] is True
    assert verify_merged_patch_applies("/nonexistent/path", "some diff")[0] is True
    print("  ✅ Fix1c：合法 patch 通过；空/无仓库不误报")


if __name__ == "__main__":
    test_recount_skips_no_newline_marker()
    test_recount_still_counts_real_context()
    test_lines_to_unified_diff_no_doubled_newlines()
    test_merged_newfile_patch_applies_cleanly()
    test_guardrail_rejects_corrupt_minus01_patch()
    test_guardrail_accepts_wellformed_and_noops()
    print("\n✅ merge_engine 新文件 patch 治本 全部通过")
