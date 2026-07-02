#!/usr/bin/env python3
"""merge_engine pom 多写者 / 新模块 pom 组装治本单测（round17 Fix ①②）。

round17 实测：MERGE 三次 apply_ok=False，VERIFY_L2 报
  `pom.xml:215 补丁未应用` + `ruoyi-alarm/pom.xml: No such file or directory`。
四路取证 + 亲验代码定位两个确定性组装缺陷：
- Fix ①：union 成功分支(merge_engine.py:655-663)在 append resolved_diff(已含全量插入)后
  又 append 一次 non_conflicting → 同一插入进两次 → 累积 apply 错位。
- Fix ②：_format_file_patch:203-206 不判 `--- /dev/null` 就重写成 `--- a/` → 新模块 pom
  退化成"改已存在文件" → git apply 报 No such file。

本测同时用作【复现夹具】（跑 __main__ 打印 merge_diffs 实际输出，坐实 worker 头分叉）。
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
    dump_merged_diff_for_diagnosis,
    merge_diffs,
    verify_merged_patch_applies,
)

# ── 基线根 pom.xml（含 <modules> 段，6 模块简化为 2）──
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
# 1:<?xml ...  2:<project>  3:groupId  4:artifactId  5:<properties>  6:java.version
# 7:</properties>  8:<modules>  9:ruoyi-admin  10:ruoyi-common  11:</modules>
# 12:<packaging>  13:</project>

# st-1: 在 </modules>(行11)前插 ruoyi-alarm（锚点 hunk），另在 <properties> 内插一行（非冲突 hunk）
ST1_ROOT_DIFF = (
    "--- a/pom.xml\n"
    "+++ b/pom.xml\n"
    "@@ -5,2 +5,3 @@\n"
    "     <properties>\n"
    "+        <maven.compiler.source>1.8</maven.compiler.source>\n"
    "         <java.version>1.8</java.version>\n"
    "@@ -9,3 +10,4 @@\n"
    "         <module>ruoyi-admin</module>\n"
    "         <module>ruoyi-common</module>\n"
    "+        <module>ruoyi-alarm</module>\n"
    "     </modules>\n"
)
# st-23-2: 同锚点(行9-11)插 ruoyi-alarm-sdk → 与 st-1 的第二个 hunk 重叠 → 冲突 → union
ST23_ROOT_DIFF = (
    "--- a/pom.xml\n"
    "+++ b/pom.xml\n"
    "@@ -9,3 +9,4 @@\n"
    "         <module>ruoyi-admin</module>\n"
    "         <module>ruoyi-common</module>\n"
    "+        <module>ruoyi-alarm-sdk</module>\n"
    "     </modules>\n"
)

# ruoyi-alarm/pom.xml 新文件——两个 worker 头版本（分叉）
NEW_POM_BODY_HUNK = (
    "@@ -0,0 +1,4 @@\n"
    "+<project>\n"
    "+    <artifactId>ruoyi-alarm</artifactId>\n"
    "+    <packaging>jar</packaging>\n"
    "+</project>\n"
)
NEW_POM_DEVNULL = "--- /dev/null\n+++ b/ruoyi-alarm/pom.xml\n" + NEW_POM_BODY_HUNK
NEW_POM_MODIFY = "--- a/ruoyi-alarm/pom.xml\n+++ b/ruoyi-alarm/pom.xml\n" + NEW_POM_BODY_HUNK


def _base_reader(path: str):
    """基线只有根 pom.xml；ruoyi-alarm/pom.xml 不存在（新模块）→ None。"""
    p = path.lstrip("/")
    if p.startswith(("a/", "b/")):
        p = p[2:]
    if p == "pom.xml":
        return BASE_ROOT_POM
    return None  # ruoyi-alarm/pom.xml 及其它 → 新文件


def _temp_git_repo_with_base() -> str:
    """建含【基线根 pom.xml】但【无 ruoyi-alarm/】的临时 git 仓库，模拟真实 merge base。"""
    d = tempfile.mkdtemp(prefix="pommerge_")
    Path(d, "pom.xml").write_text(BASE_ROOT_POM)
    subprocess.run(["git", "init", "-q"], cwd=d, check=True)
    subprocess.run(["git", "config", "user.email", "t@t"], cwd=d, check=True)
    subprocess.run(["git", "config", "user.name", "t"], cwd=d, check=True)
    subprocess.run(["git", "add", "-A"], cwd=d, check=True)
    subprocess.run(["git", "commit", "-qm", "base"], cwd=d, check=True)
    return d


def _run(new_pom_diff: str, label: str):
    subtask_diffs = [
        ("st-1", ST1_ROOT_DIFF + new_pom_diff),
        ("st-23-2", ST23_ROOT_DIFF),
    ]
    result = merge_diffs(subtask_diffs, base_reader=_base_reader, auto_resolve=True)
    repo = _temp_git_repo_with_base()
    ok, err = verify_merged_patch_applies(repo, result.merged_diff)
    return result, ok, err


# ── Fix ①：union 后 root pom 只应有一个块、无重复插入、git apply 通过 ──
def test_root_pom_union_no_double_emit():
    result, ok, err = _run(NEW_POM_DEVNULL, "devnull")
    md = result.merged_diff
    # 非冲突插入(maven.compiler.source)只应出现一次（双块 emit 会出现两次）
    assert md.count("<maven.compiler.source>") == 1, (
        f"non_conflicting 插入被双块 emit 了 {md.count('<maven.compiler.source>')} 次:\n{md}"
    )
    # root pom.xml 只应有一个 `--- a/pom.xml`/`+++ b/pom.xml` 块
    assert md.count("+++ b/pom.xml") == 1, f"root pom 出现多个块:\n{md}"
    print("  ✅ Fix①：root pom union 单块、无重复插入")


# ── Fix ②：新模块 pom（base 缺）必须输出新文件补丁，两种 worker 头都要能 apply ──
def test_newmodule_pom_devnull_applies():
    result, ok, err = _run(NEW_POM_DEVNULL, "devnull")
    md = result.merged_diff
    assert "ruoyi-alarm/pom.xml" in md
    assert ok, f"新模块 pom(worker 发 /dev/null)合并后应能 git apply，但失败: {err}\n---\n{md}"
    print("  ✅ Fix②：worker /dev/null 版 → 新文件补丁 apply 通过")


def test_newmodule_pom_modify_head_applies():
    result, ok, err = _run(NEW_POM_MODIFY, "modify")
    md = result.merged_diff
    assert "ruoyi-alarm/pom.xml" in md
    assert ok, f"新模块 pom(worker 发 --- a/)合并后应能 git apply，但失败: {err}\n---\n{md}"
    print("  ✅ Fix②：worker --- a/ 版 → 按 base 权威判新文件、apply 通过")


def test_newmodule_pom_modify_style_hunk_applies():
    """对抗案例：新文件(base 缺)但 worker 发 modify 风格 hunk `@@ -1,N`+context（沙箱材化致）。
    Fix② 须把新侧(context+addition)重建成纯新增 `@@ -0,0`，否则 git apply 报'新文件依赖旧内容'。"""
    modify_hunk_new = (
        "--- a/ruoyi-alarm/pom.xml\n"
        "+++ b/ruoyi-alarm/pom.xml\n"
        "@@ -1,2 +1,4 @@\n"
        " <project>\n"
        "+    <artifactId>ruoyi-alarm</artifactId>\n"
        "+    <packaging>jar</packaging>\n"
        " </project>\n"
    )
    result = merge_diffs([("st-1", modify_hunk_new)], base_reader=_base_reader, auto_resolve=True)
    md = result.merged_diff
    assert "@@ -0,0 +1,4 @@" in md, f"新文件 hunk 应归一为 @@ -0,0：\n{md}"
    assert "--- /dev/null" in md and "new file mode" in md
    repo = _temp_git_repo_with_base()
    ok, err = verify_merged_patch_applies(repo, md)
    assert ok, f"modify 风格新文件应重建后 apply 通过，但失败: {err}\n---\n{md}"
    print("  ✅ Fix②硬化：modify 风格 hunk(@@ -1,N) 新文件 → 重建 @@ -0,0 apply 通过")


def test_newfile_multiwriter_identical_dedup():
    """多写者建【同一新文件】且内容一致（round17 sdk pom 3 沙箱各建）→ 去重取一，不 emit 冲突标记。"""
    same = NEW_POM_DEVNULL  # ruoyi-alarm/pom.xml，base 缺
    result = merge_diffs([("st-a", same), ("st-b", same)], base_reader=_base_reader, auto_resolve=True)
    md = result.merged_diff
    assert len(result.conflicts) == 0, f"新文件多写者不应产生冲突标记:\n{md}"
    assert "<<<<<<<" not in md, f"不应有冲突标记:\n{md}"
    assert md.count("+++ b/ruoyi-alarm/pom.xml") == 1, f"应去重为单块:\n{md}"
    repo = _temp_git_repo_with_base()
    ok, err = verify_merged_patch_applies(repo, md)
    assert ok, f"去重后应能 apply，但失败: {err}\n---\n{md}"
    print("  ✅ 新文件多写者·一致 → 去重取一、apply 通过")


def test_newfile_multiwriter_divergent_topological():
    """多写者建同一新文件但内容【不一致】→ 确定性取拓扑最上游写者，其余丢弃，仍可 apply。"""
    a = "--- /dev/null\n+++ b/ruoyi-alarm/pom.xml\n@@ -0,0 +1,1 @@\n+<project>A</project>\n"
    b = "--- /dev/null\n+++ b/ruoyi-alarm/pom.xml\n@@ -0,0 +1,1 @@\n+<project>B</project>\n"
    result = merge_diffs(
        [("st-b", b), ("st-a", a)], base_reader=_base_reader,
        auto_resolve=True, subtask_order=["st-a", "st-b"],  # st-a 拓扑最上游
    )
    md = result.merged_diff
    assert len(result.conflicts) == 0 and "<<<<<<<" not in md, f"不应有冲突标记:\n{md}"
    assert "<project>A</project>" in md and "<project>B</project>" not in md, (
        f"应取拓扑最上游 st-a 的版本:\n{md}"
    )
    repo = _temp_git_repo_with_base()
    ok, err = verify_merged_patch_applies(repo, md)
    assert ok, f"取一后应能 apply，但失败: {err}\n---\n{md}"
    print("  ✅ 新文件多写者·不一致 → 拓扑取 st-a、apply 通过")


def test_whole_merged_patch_applies_clean():
    """端到端：多写者 root pom + 新模块 pom 一起，merged patch 整体 git apply 干净（复现 round17 修复）。"""
    result, ok, err = _run(NEW_POM_DEVNULL, "e2e")
    assert ok, f"round17 同形 merged patch 应能干净 apply，但失败: {err}\n---\n{result.merged_diff}"
    print("  ✅ e2e：round17 同形 merged patch 干净落盘")


# ── Fix 0：apply 失败时 merged_diff 落盘诊断（fail-safe）──
def test_dump_merged_diff_for_diagnosis():
    d = tempfile.mkdtemp(prefix="dumptest_")
    path = dump_merged_diff_for_diagnosis("996db614-abc", "--- a/x\n+y\n", dump_dir=d, ts=123)
    assert path is not None and Path(path).is_file()
    assert Path(path).name == "merged_diff_996db614_123.diff"
    assert Path(path).read_text() == "--- a/x\n+y\n"
    # fail-safe：不可写目录 → 返回 None 不抛
    assert dump_merged_diff_for_diagnosis("t", "diff", dump_dir="/nonexistent/\x00/bad") is None
    print("  ✅ Fix0：merged_diff 落盘 + fail-safe")


if __name__ == "__main__":
    # 复现观察：打印两种 worker 头下 merge_diffs 的实际输出，坐实 ② 分叉
    for label, npom in [("worker=/dev/null", NEW_POM_DEVNULL), ("worker=--- a/", NEW_POM_MODIFY)]:
        result, ok, err = _run(npom, label)
        print(f"\n{'='*70}\n[{label}] apply_ok={ok} err={err!r}")
        print(f"success={result.success} conflicts={len(result.conflicts)} "
              f"auto_resolved={result.auto_resolved_files} rebase={result.rebase_subtask_ids}")
        print(f"--- merged_diff ---\n{result.merged_diff}")
