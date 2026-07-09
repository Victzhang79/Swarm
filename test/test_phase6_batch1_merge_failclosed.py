"""阶段6 批1（登记册 §五）：合并 fail-closed——D2/D3/D4/D7/D9/D11 行为锁。

D2 新文件多写者不一致：非选中写者从静默丢弃改进 rebase 通道（merge 后文件已在树，
   重派在其上重生成；账面不再假成功）。
D3 rebase 超限 clean-accept：只对聚合/模块清单文件成立（post-pass reconcile 只兜清单
   ground-truth）；超限方碰普通源文件=接受即静默丢源码 → 走 escalate。
D4 rebase 重派注入保留方最新内容（retry_guidance，worker prompt 渲染硬约束块）——
   否则 worker 在同一钉扎 base 重生成同形 diff，3 轮必再撞落 D3。
D7 孤儿模块剔除的 sid 并入 abandoned + pop 完成态（旧行为仅日志，终态仍假 DONE）。
D9 apply_hunk context/删除行与 base 比对，漂移抛 HunkContextMismatch（3-way 输入
   被污染时静默产出语义损坏的"干净"合并）→ 调用方放弃 3-way 落冲突/rebase。
D11 硬冲突标记（<<<<<<<）剥离 merged_diff → conflict_render 字段+诊断件（毒 diff
   不可 apply 且同文件双段互踩）。
"""

from __future__ import annotations

import pytest

from swarm.brain.merge_engine import (
    HunkContextMismatch,
    apply_hunks_to_text,
    merge_diffs,
)


def _diff(path, old_lines, new_lines, sid):
    """构造最小 unified diff（单 hunk 全文件替换形态）。"""
    old_c, new_c = len(old_lines), len(new_lines)
    body = "".join(f"-{l}\n" for l in old_lines) + "".join(f"+{l}\n" for l in new_lines)
    return (f"--- a/{path}\n+++ b/{path}\n"
            f"@@ -1,{old_c} +1,{new_c} @@\n{body}")


def _new_file_diff(path, lines):
    body = "".join(f"+{l}\n" for l in lines)
    return (f"--- /dev/null\n+++ b/{path}\n"
            f"@@ -0,0 +1,{len(lines)} @@\n{body}")


# ─────────────── D9：context 校验 ───────────────

def test_d9_context_mismatch_raises():
    from swarm.brain.merge_engine import _Hunk
    base = "line1\nline2\nline3\n"
    # hunk 声称 base 第 2 行是 "DRIFTED"（实为 line2）——基线漂移
    h = _Hunk(old_start=2, old_count=1, new_start=2, new_count=1,
              lines=["@@ -2,1 +2,1 @@", "-DRIFTED", "+changed"], subtask_id="st-1")
    with pytest.raises(HunkContextMismatch):
        apply_hunks_to_text(base, [h])


def test_d9_matching_context_applies():
    from swarm.brain.merge_engine import _Hunk
    base = "line1\nline2\nline3\n"
    h = _Hunk(old_start=2, old_count=1, new_start=2, new_count=1,
              lines=["@@ -2,1 +2,1 @@", "-line2", "+changed"], subtask_id="st-1")
    assert apply_hunks_to_text(base, [h]) == "line1\nchanged\nline3\n"


def test_d9_drifted_hunk_falls_to_conflict_not_corrupt_merge():
    """两写者重叠 + 其一 hunk 基线漂移 → 绝不产出语义损坏的『干净』合并。"""
    base = "A\nB\nC\nD\n"
    d1 = _diff("x.py", ["B"], ["B1"], "st-1").replace("@@ -1,1 +1,1 @@", "@@ -2,1 +2,1 @@")
    # st-2 的 hunk 声称第 2 行是 "WRONG"（漂移）且与 st-1 同锚点
    d2 = ("--- a/x.py\n+++ b/x.py\n@@ -2,1 +2,1 @@\n-WRONG\n+B2\n")
    result = merge_diffs([("st-1", d1), ("st-2", d2)],
                         base_reader=lambda p: base if p == "x.py" else None)
    # 漂移方绝不被静默并入：要么进冲突要么进 rebase，且 merged_diff 无 WRONG 残留
    assert result.conflicts or result.rebase_subtask_ids, (
        "基线漂移的 3-way 输入被污染时必须 fail-closed（冲突/rebase），"
        "旧行为静默产出错位『干净』合并")
    assert "B2" not in (result.merged_diff or "") or "B1" not in (result.merged_diff or "")


# ─────────────── D2：新文件多写者 → rebase ───────────────

def test_d2_new_file_conflicting_writers_go_rebase():
    d1 = _new_file_diff("new/Svc.java", ["class Svc {", "  int a;", "}"])
    d2 = _new_file_diff("new/Svc.java", ["class Svc {", "  int b;", "}"])
    result = merge_diffs([("st-1", d1), ("st-2", d2)],
                         base_reader=lambda p: None)  # base 无此文件=新文件
    assert "st-2" in result.rebase_subtask_ids or "st-1" in result.rebase_subtask_ids, (
        "非选中写者必须进 rebase 通道重生成——旧行为静默丢弃（不进 conflicts/rebase/"
        "degraded，账面仍成功=跑完白跑）")
    assert "<<<<<<<" not in result.merged_diff, "新文件绝不 emit 冲突标记（毒化整包）"


def test_d2_new_file_identical_writers_dedupe_no_rebase():
    d = _new_file_diff("new/Same.java", ["class Same {}"])
    result = merge_diffs([("st-1", d), ("st-2", d)], base_reader=lambda p: None)
    assert result.rebase_subtask_ids == [], "内容一致=去重取一，无需 rebase（原语义不回归）"


# ─────────────── D11：冲突标记剥离 merged_diff ───────────────

def test_d11_conflict_markers_not_in_merged_diff():
    base = "A\nB\nC\n"
    # 同锚点不同修改 + 无法 3-way（单行重叠改动）→ 硬冲突
    d1 = "--- a/y.py\n+++ b/y.py\n@@ -2,1 +2,1 @@\n-B\n+B-from-1\n"
    d2 = "--- a/y.py\n+++ b/y.py\n@@ -2,1 +2,1 @@\n-B\n+B-from-2\n"
    result = merge_diffs([("st-1", d1), ("st-2", d2)],
                         base_reader=None)  # 无 base_reader=旧文件走硬冲突兜底
    if result.conflicts:
        assert "<<<<<<<" not in (result.merged_diff or ""), (
            "毒标记写入 merged_diff=不可 apply 且同文件双段互踩——必须剥离到 conflict_render")
        assert "<<<<<<<" in (result.conflict_render or ""), "冲突渲染落诊断字段（可观测不丢）"


# ─────────────── D3/D4/D7：merge 节点侧（源码断言级） ───────────────
# merge() 节点需要完整 state/项目路径，行为锁通过关键机制的单元面覆盖：

def test_d3_manifest_discriminators_available():
    from swarm.brain.merge_engine import _is_aggregate_manifest, _is_module_manifest
    assert _is_aggregate_manifest("pom.xml") or _is_module_manifest("pom.xml")
    assert not (_is_aggregate_manifest("src/main/java/App.java")
                or _is_module_manifest("src/main/java/App.java")), (
        "clean-accept 收窄判据=聚合/模块清单判别（普通源文件绝不适用）")


def test_d7_orphan_filter_returns_dropped_sids():
    from swarm.brain.merge_engine import filter_orphan_module_patches
    diffs = [("st-1", _new_file_diff("missing-mod/src/A.java", ["class A {}"]))]
    kept, dropped = filter_orphan_module_patches(
        diffs, base_module_exists=lambda d: False)  # 模块骨架缺失
    assert all(sid != "st-1" for sid, _ in kept) and dropped, (
        "剔除信息必须结构化返回（sid→模块）——merge 节点据此并入 abandoned+pop 完成态，"
        "不再只留日志假 DONE")
