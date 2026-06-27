"""996db614 #4 回归：合并补丁损坏(git apply 第N行损坏 malformed)——@@ 头行数与 body 不符。
治本：_format_file_patch 逐 hunk 据实际 body 重算 @@ 头，保证永远 well-formed。"""
import re
from swarm.brain.merge_engine import _recount_hunk_header, _format_file_patch, _Hunk, _HUNK_RE


def test_recount_matches_body():
    # 头声称 -1,5 +1,5，但 body 实际只有 1 context+2 增 → 应重算为 -1,1 +1,3
    assert _recount_hunk_header("@@ -1,5 +1,5 @@", [" ctx", "+a", "+b"]) == "@@ -1,1 +1,3 @@"
    # 保留尾部 section heading
    assert _recount_hunk_header("@@ -10,9 +10,9 @@ class Foo", ["-x", " c"]) == "@@ -10,2 +10,1 @@ class Foo"


def test_format_file_patch_header_always_matches_body():
    """规范化后(空行 context 漂移) @@ 头仍与 body 严格一致——杜绝 malformed。"""
    # 构造一个尾部 context 被 strip 成 "" 的 hunk（旧逻辑会让头多算 1 行 → 损坏）
    hunk = _Hunk(old_start=1, old_count=3, new_start=1, new_count=4,
                 lines=["@@ -1,3 +1,4 @@", " line1", "+added", " line3", ""], subtask_id="st-1")
    out = _format_file_patch("a/X.java", ["--- a/X.java", "+++ b/X.java"], [hunk])
    # 取出 @@ 头，核其声明行数 == body 实际行数
    for hl in out.splitlines():
        m = _HUNK_RE.match(hl)
        if m:
            oc, nc = int(m.group(2)), int(m.group(4))
            body = out.split(hl, 1)[1].splitlines()
            real_old = sum(1 for l in body if l[:1] in (" ", "-"))
            real_new = sum(1 for l in body if l[:1] in (" ", "+"))
            assert oc == real_old and nc == real_new, (hl, real_old, real_new)
            break
    else:
        raise AssertionError("no hunk header found")
