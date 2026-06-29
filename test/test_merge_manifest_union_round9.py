"""round9 治本：聚合清单(pom <modules> 等)多写者同锚点不同插入 → 并集而非 rebase/escalate。

背景(round9 实测)：根 pom.xml 有 4 个子任务各加【不同】<module>/<dependency>。MERGE 旧逻辑把
同锚点不同插入判冲突→rebase 重生成→达上限→升级人工→auto_accept fail-fast→整体 FAILED，
丢弃 35 子任务/0 失败/0 冲突的近完整产物。

杠杆A：聚合清单文件同锚点不同插入 = order-independent 且都该保留 → 并集(去重相同块)。
普通代码仍保守(同锚点不同插入=真冲突→rebase)。
"""

from __future__ import annotations

from swarm.brain.merge_engine import (
    _is_aggregate_manifest,
    merge_diffs,
    merge_insert_only_changes,
)

BASE_POM = (
    "<project>\n"
    "  <modules>\n"
    "    <module>existing</module>\n"
    "  </modules>\n"
    "</project>\n"
)


def _pom_reader(path: str) -> str | None:
    return BASE_POM if path == "pom.xml" else None


# 两个子任务各在 </modules> 前插入【不同】<module> —— 同锚点不同插入
DIFF_ADD_ALARM = (
    "--- a/pom.xml\n+++ b/pom.xml\n"
    "@@ -3,1 +3,2 @@\n"
    "     <module>existing</module>\n"
    "+    <module>ruoyi-alarm</module>\n"
)
DIFF_ADD_SDK = (
    "--- a/pom.xml\n+++ b/pom.xml\n"
    "@@ -3,1 +3,2 @@\n"
    "     <module>existing</module>\n"
    "+    <module>ruoyi-alarm-sdk</module>\n"
)


def test_pom_multiwriter_unions_no_rebase():
    """根 pom 多写者各加不同 module → 并集合并(两者都在),不 rebase/不冲突。"""
    result = merge_diffs(
        [("st-1", DIFF_ADD_ALARM), ("st-30", DIFF_ADD_SDK)],
        base_reader=_pom_reader,
        auto_resolve=True,
        subtask_order=["st-1", "st-30"],
    )
    assert result.success is True, result.merged_diff
    assert result.conflicts == [], result.conflicts
    assert result.rebase_subtask_ids == [], result.rebase_subtask_ids
    # 两个 module 都保留(并集)——round9 失败点的根治
    assert "ruoyi-alarm" in result.merged_diff, result.merged_diff
    assert "ruoyi-alarm-sdk" in result.merged_diff, result.merged_diff


def test_non_manifest_same_anchor_still_rebases():
    """普通 .py 文件同锚点不同插入仍走 rebase(保守,不并集)——杠杆A 不放宽普通代码。"""
    base_py = "".join(f"line{i}\n" for i in range(1, 6))

    def _reader(p: str) -> str | None:
        return base_py if p == "f.py" else None

    da = "--- a/f.py\n+++ b/f.py\n@@ -2,1 +2,2 @@\n line2\n+AAA\n"
    db = "--- a/f.py\n+++ b/f.py\n@@ -2,1 +2,2 @@\n line2\n+BBB\n"
    result = merge_diffs(
        [("st-a", da), ("st-b", db)], base_reader=_reader, auto_resolve=True,
        subtask_order=["st-a", "st-b"],
    )
    # 普通代码同锚点不同插入不并集 → rebase 或冲突,绝不静默两版都塞
    assert result.rebase_subtask_ids or result.conflicts, result.merged_diff


def test_merge_insert_only_union_flag():
    """merge_insert_only_changes：allow_anchor_union 控制同锚点不同插入并集 vs 拒绝。"""
    base = "a\nb\n"
    v1 = "a\nX\nb\n"   # 在 b 前插 X
    v2 = "a\nY\nb\n"   # 在 b 前插 Y(同锚点不同内容)
    # 默认(普通代码)：同锚点不同插入 → None
    assert merge_insert_only_changes(base, v1, v2) is None
    # 聚合清单：并集保留 X 和 Y
    out = merge_insert_only_changes(base, v1, v2, allow_anchor_union=True)
    assert out is not None and "X" in out and "Y" in out, out
    # 同内容仍去重(不重复两遍)
    out2 = merge_insert_only_changes(base, v1, v1, allow_anchor_union=True)
    assert out2 is not None and out2.count("X") == 1, out2


def test_is_aggregate_manifest():
    for f in ("pom.xml", "a/b/pom.xml", "settings.gradle", "settings.gradle.kts",
              "Cargo.toml", "go.work", "App.sln", "sub/My.sln"):
        assert _is_aggregate_manifest(f), f
    for f in ("build.gradle", "src/Main.java", "x/pom.xml.bak", "module.py", "package.json"):
        assert not _is_aggregate_manifest(f), f


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn(); print("ok", name)
