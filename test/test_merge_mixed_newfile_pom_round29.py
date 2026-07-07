#!/usr/bin/env python3
"""round29 B 治本单测：merged_diff 混排【git 格式 new-file 段 + 裸传统 pom modify 段】必须可 apply。

task d37a52a3 实测：merged_diff 有 77 个 `diff --git` 头、却有 79 个文件段——两个 pom MODIFY 段
（root pom union 出自 _lines_to_unified_diff；ruoyi-admin/pom.xml 单写者出自 _format_file_patch
非新建分支）是裸传统补丁（无 `diff --git` 头），拼在 git 格式 new-file 段之间。git 进入 git 格式
模式后无法为裸块建立文件上下文 → 解析 desync 消费到 EOF → 报「第 9208 行损坏」（EOF 只是症状）。

治本：两处源头统一前置 `diff --git a/<p> b/<p>` 头（路径派生、无 pom/xml 特判、幂等）。
不变量：merged_diff 中 `diff --git ` 头数 == 文件段数（`+++ b/` 数）。
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
    _format_file_patch,
    _Hunk,
    _lines_to_unified_diff,
    dump_merged_diff_for_diagnosis,
    merge_diffs,
    verify_merged_patch_applies,
)

# ── 基线：root pom.xml + ruoyi-admin/pom.xml 存在；alarm-interface/ 为新模块（base 无）──
BASE_ROOT_POM = """<?xml version="1.0" encoding="UTF-8"?>
<project>
    <groupId>com.ruoyi</groupId>
    <artifactId>ruoyi</artifactId>
    <properties>
        <java.version>1.8</java.version>
    </properties>
    <modules>
        <module>ruoyi-admin</module>
        <module>ruoyi-common</module>
    </modules>
    <packaging>pom</packaging>
</project>
"""
BASE_ADMIN_POM = """<project>
    <artifactId>ruoyi-admin</artifactId>
    <dependencies>
        <dependency>ruoyi-common</dependency>
    </dependencies>
</project>
"""

# st-1: root pom </modules> 前插 alarm-interface（锚点 hunk）+ 新模块 pom（/dev/null 新建）
ST1_DIFF = (
    "--- a/pom.xml\n"
    "+++ b/pom.xml\n"
    "@@ -9,3 +9,4 @@\n"
    "         <module>ruoyi-admin</module>\n"
    "         <module>ruoyi-common</module>\n"
    "+        <module>alarm-interface</module>\n"
    "     </modules>\n"
    "--- /dev/null\n"
    "+++ b/alarm-interface/pom.xml\n"
    "@@ -0,0 +1,4 @@\n"
    "+<project>\n"
    "+    <artifactId>alarm-interface</artifactId>\n"
    "+    <packaging>jar</packaging>\n"
    "+</project>\n"
)
# st-2: root pom 同锚点插另一模块 → 与 st-1 重叠 → 冲突 → union（走 _lines_to_unified_diff）
ST2_DIFF = (
    "--- a/pom.xml\n"
    "+++ b/pom.xml\n"
    "@@ -9,3 +9,4 @@\n"
    "         <module>ruoyi-admin</module>\n"
    "         <module>ruoyi-common</module>\n"
    "+        <module>alarm-sdk</module>\n"
    "     </modules>\n"
)
# st-3: ruoyi-admin/pom.xml 单写者 modify（走 _format_file_patch 非新建分支）
ST3_DIFF = (
    "--- a/ruoyi-admin/pom.xml\n"
    "+++ b/ruoyi-admin/pom.xml\n"
    "@@ -3,3 +3,4 @@\n"
    "     <dependencies>\n"
    "         <dependency>ruoyi-common</dependency>\n"
    "+        <dependency>alarm-interface</dependency>\n"
    "     </dependencies>\n"
)


def _base_reader(path: str):
    p = path.lstrip("/")
    if p.startswith(("a/", "b/")):
        p = p[2:]
    if p == "pom.xml":
        return BASE_ROOT_POM
    if p == "ruoyi-admin/pom.xml":
        return BASE_ADMIN_POM
    return None  # alarm-interface/* → 新文件


def _temp_git_repo_with_base() -> str:
    d = tempfile.mkdtemp(prefix="mixedmerge_")
    Path(d, "pom.xml").write_text(BASE_ROOT_POM)
    Path(d, "ruoyi-admin").mkdir()
    Path(d, "ruoyi-admin", "pom.xml").write_text(BASE_ADMIN_POM)
    subprocess.run(["git", "init", "-q"], cwd=d, check=True)
    subprocess.run(["git", "config", "user.email", "t@t"], cwd=d, check=True)
    subprocess.run(["git", "config", "user.name", "t"], cwd=d, check=True)
    subprocess.run(["git", "add", "-A"], cwd=d, check=True)
    subprocess.run(["git", "commit", "-qm", "base"], cwd=d, check=True)
    return d


def _run_mixed():
    """new-file(git 格式) + root pom union(裸) + 单写者 modify(裸) 的混排 merge。

    sorted(by_file) = [alarm-interface/pom.xml, pom.xml, ruoyi-admin/pom.xml]
    → git 格式段在最前、裸段在其后 = task d37a52a3 的畸形布局。
    """
    subtask_diffs = [
        ("st-1", ST1_DIFF),
        ("st-2", ST2_DIFF),
        ("st-3", ST3_DIFF),
    ]
    return merge_diffs(subtask_diffs, base_reader=_base_reader, auto_resolve=True)


# ── 不变量：`diff --git ` 头数 == 文件段数（修前 1 vs 3 红）──
def test_header_segment_parity():
    result = _run_mixed()
    md = result.merged_diff
    n_headers = md.count("diff --git ")
    n_segments = md.count("+++ b/")
    assert n_headers == n_segments, (
        f"diff --git 头数({n_headers}) != 文件段数({n_segments})，裸传统段会使 git 解析 desync:\n{md}"
    )


# ── 端到端：混排 merged_diff 必须能 git apply --check（真 base 树）──
def test_mixed_merged_diff_git_applies():
    result = _run_mixed()
    assert result.success, f"merge 本身不应报硬冲突: {result.conflicts}"
    md = result.merged_diff
    assert "alarm-interface/pom.xml" in md
    assert "+++ b/pom.xml" in md
    assert "+++ b/ruoyi-admin/pom.xml" in md
    repo = _temp_git_repo_with_base()
    ok, err = verify_merged_patch_applies(repo, md)
    assert ok, f"混排 merged_diff 应能 git apply，但失败: {err}\n---\n{md}"


# ── 单元：_lines_to_unified_diff 必须自带 diff --git 头 ──
def test_lines_to_unified_diff_has_git_header():
    base = "<modules>\n</modules>\n"
    merged = "<modules>\n    <module>m1</module>\n</modules>\n"
    block = _lines_to_unified_diff("pom.xml", base, merged)
    assert block.startswith("diff --git a/pom.xml b/pom.xml\n"), f"缺 diff --git 头:\n{block}"
    # 空 diff 不应输出孤儿头
    assert _lines_to_unified_diff("pom.xml", base, base) == ""


# ── 单元：_format_file_patch 非新建分支必须自带 diff --git 头（且不带 new file mode）──
def test_format_file_patch_modify_has_git_header():
    hunks = [
        _Hunk(
            subtask_id="st-3",
            old_start=3,
            old_count=3,
            new_start=3,
            new_count=4,
            lines=[
                "@@ -3,3 +3,4 @@",
                "     <dependencies>",
                "         <dependency>ruoyi-common</dependency>",
                "+        <dependency>alarm-interface</dependency>",
                "     </dependencies>",
            ],
        )
    ]
    out = _format_file_patch("ruoyi-admin/pom.xml", [], hunks, is_new=False)
    lines = out.split("\n")
    assert lines[0] == "diff --git a/ruoyi-admin/pom.xml b/ruoyi-admin/pom.xml", out
    assert "new file mode" not in out, "modify 分支绝不能带 new file mode"
    assert lines[1] == "--- a/ruoyi-admin/pom.xml"
    assert lines[2] == "+++ b/ruoyi-admin/pom.xml"
    # worker 自带 header_lines 时同样前置 git 头
    out2 = _format_file_patch(
        "ruoyi-admin/pom.xml",
        ["--- a/ruoyi-admin/pom.xml", "+++ b/ruoyi-admin/pom.xml"],
        hunks,
        is_new=False,
    )
    assert out2.split("\n")[0] == "diff --git a/ruoyi-admin/pom.xml b/ruoyi-admin/pom.xml", out2


# ── 双复核 Finding 1 回归：base 里【已存在的空文件】被填充（-0,0 合法 modify 形状）──
# 有 base_reader 权威判定文件存在时，绝不能因 hunk 形状把 modify 提升为 new-file
# （`new file mode` 打已存在文件必报 already exists，会重新引入 apply_ok=False 假失败）。
def test_existing_empty_file_fill_not_promoted_to_newfile():
    def _reader(path: str):
        p = path.lstrip("/")
        if p.startswith(("a/", "b/")):
            p = p[2:]
        return "" if p == "notes.txt" else None  # 空文件【存在】于 base

    fill_diff = (
        "--- a/notes.txt\n"
        "+++ b/notes.txt\n"
        "@@ -0,0 +1,2 @@\n"
        "+line1\n"
        "+line2\n"
    )
    result = merge_diffs([("st-1", fill_diff)], base_reader=_reader, auto_resolve=True)
    md = result.merged_diff
    assert "new file mode" not in md, f"已存在文件绝不能升为 new-file:\n{md}"
    assert md.startswith("diff --git a/notes.txt b/notes.txt\n"), md
    # 真 git 校验：对含【空 notes.txt】的 base 树可干净 apply
    d = tempfile.mkdtemp(prefix="emptyfill_")
    Path(d, "notes.txt").write_text("")
    subprocess.run(["git", "init", "-q"], cwd=d, check=True)
    subprocess.run(["git", "config", "user.email", "t@t"], cwd=d, check=True)
    subprocess.run(["git", "config", "user.name", "t"], cwd=d, check=True)
    subprocess.run(["git", "add", "-A"], cwd=d, check=True)
    subprocess.run(["git", "commit", "-qm", "base"], cwd=d, check=True)
    ok, err = verify_merged_patch_applies(d, md)
    assert ok, f"已存在空文件的填充补丁应能 apply: {err}\n{md}"


# ── 复核 MEDIUM：硬冲突段也带 diff --git 头（头/段配平无条件成立，且仍不可 apply）──
def test_conflict_segment_has_git_header_and_stays_unappliable():
    from swarm.brain.merge_engine import _format_conflict_hunks

    h1 = _Hunk(subtask_id="st-a", old_start=1, old_count=2, new_start=1, new_count=3,
               lines=["@@ -1,2 +1,3 @@", " x", "+a", " y"])
    h2 = _Hunk(subtask_id="st-b", old_start=1, old_count=2, new_start=1, new_count=3,
               lines=["@@ -1,2 +1,3 @@", " x", "+b", " y"])
    out = _format_conflict_hunks("f.txt", [h1, h2])
    assert out.startswith("diff --git a/f.txt b/f.txt\n"), out
    assert "<<<<<<<" in out and ">>>>>>>" in out  # 冲突语义不变（不可 apply）


# ── 反误诊：dump 落盘必须补尾换行（git 要求补丁文件以换行结尾）──
# task d37a52a3 实证：merged_diff 本体不带尾换行，dump 原样落盘 → 离线手工 git apply 在 EOF
# 假报「corrupt patch at line N」→ 被误诊为组装畸形（实际生产 apply 路径补了尾换行、能干净打上）。
def test_dump_appends_trailing_newline():
    d = tempfile.mkdtemp(prefix="dumpnl_")
    path = dump_merged_diff_for_diagnosis("d37a52a3", "--- a/x\n+++ b/x\n@@ -1,1 +1,1 @@\n-a\n+b", dump_dir=d, ts=1)
    assert path is not None
    content = Path(path).read_text()
    assert content.endswith("+b\n"), f"dump 必须补尾换行: {content[-10:]!r}"
    # 已带尾换行的不重复追加
    path2 = dump_merged_diff_for_diagnosis("d37a52a3", "+b\n", dump_dir=d, ts=2)
    assert Path(path2).read_text() == "+b\n"
