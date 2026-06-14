"""audit #23 修复回归测试：_python_merge3 双插冲突不再静默拼接。

问题：双方对同一 base 位置插入【不同】内容时，原实现静默"先 a 后 b"拼接，产生顺序
任意、两段都塞进去的语义错乱结果，且 conflict=False（上层以为合并成功）。
修复：标记为冲突（<<<<<<< / >>>>>>>）+ 返回 success=False，交上层 rebase 重新生成。

注：我们的沙箱环境无 git，three_way_merge_text 会回退到 _python_merge3，故这条 fallback
是实际主路径，非"很少触发"。
"""

from __future__ import annotations

from swarm.brain.merge_engine import _python_merge3


def test_identical_insert_not_conflict():
    """双方在同一位置插入【相同】内容 → 非冲突，取其一。"""
    base = "line1\nline3\n"
    ours = "line1\nINSERTED\nline3\n"
    theirs = "line1\nINSERTED\nline3\n"
    merged, ok = _python_merge3(base, ours, theirs)
    assert ok is True
    assert merged.count("INSERTED") == 1


def test_divergent_insert_is_conflict():
    """双方在同一位置插入【不同】内容 → 冲突，不静默拼接。"""
    base = "line1\nline3\n"
    ours = "line1\nFROM_OURS\nline3\n"
    theirs = "line1\nFROM_THEIRS\nline3\n"
    merged, ok = _python_merge3(base, ours, theirs)
    assert ok is False, "不同插入必须判冲突，而非静默成功"
    assert "<<<<<<<" in merged and ">>>>>>>" in merged
    # 两段内容都在冲突块里（让人/上层能看到），但带冲突标记而非裸拼接
    assert "FROM_OURS" in merged and "FROM_THEIRS" in merged


def test_one_side_unchanged_takes_other():
    """一方等于 base → 取另一方，非冲突。"""
    base = "a\nb\n"
    ours = "a\nb\n"          # 未改
    theirs = "a\nX\nb\n"     # 插入
    merged, ok = _python_merge3(base, ours, theirs)
    assert ok is True
    assert "X" in merged


def test_both_identical_full_text():
    base = "a\n"
    merged, ok = _python_merge3(base, "a\nb\n", "a\nb\n")
    assert ok is True


if __name__ == "__main__":
    import sys
    fns = [v for k, v in list(globals().items()) if k.startswith("test_") and callable(v)]
    failed = 0
    for fn in fns:
        try:
            fn()
            print(f"  ✅ {fn.__name__}")
        except AssertionError as e:
            failed += 1
            print(f"  ❌ {fn.__name__}: {e}")
    print(f"\n=== #23 merge3 conflict: {len(fns) - failed}/{len(fns)} passed ===")
    sys.exit(1 if failed else 0)
