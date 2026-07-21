"""B4b-2：NVFP4 量化模型腐坏簇（#103 伪空格 / #114 symbol-repair 震荡 / #113 全角标点）行为级测试。

round66 task=97a56c3d 实证：本地 NVFP4 权重往源码注入伪空格标识符（`is Empty`）+ 全角 CJK 标点
（`，：（）`），逃逸 symbol-repair + 触发复读退化循环烧流。
"""
from __future__ import annotations

from unittest.mock import patch

import swarm.worker.l1_pipeline as L


# ─────────────────────────── #103 伪空格折叠 ───────────────────────────

def test_103_pseudospace_folds_when_high_freq():
    """`.is Empty(` → 折叠为 `.isEmpty(`，当 isEmpty 是项目高频符号（双闸满足）。"""
    freq_out = "  128 isEmpty\n  40 groups\n  5 size\n"
    grep_out = "12:.is Empty(\n"
    perl_calls = []

    def fake_cached_scan(cmd, path, timeout=0):
        if "sort" in cmd and "uniq" in cmd:      # freq 扫描
            return 0, freq_out, ""
        return 0, grep_out, ""                    # 成员调用空格 grep

    def fake_run_l1(cmd, path, timeout=0):
        perl_calls.append(cmd)
        return 0, ""

    with patch.object(L, "_cached_scan", side_effect=fake_cached_scan), \
         patch.object(L, "_run_l1_command", side_effect=fake_run_l1):
        n, files = L._attempt_pseudospace_repair("/p", ["A.java"], 60)

    assert n == 1
    assert "A.java" in files[0]
    assert perl_calls and "isEmpty" in perl_calls[0]  # 折叠命令含 isEmpty


def test_103_no_fold_when_folded_token_not_high_freq():
    """闸②：折叠后 token 非项目高频（<5）→ 不折（fail-safe，交编译闸兜底）。"""
    freq_out = "  128 groups\n  2 isEmpty\n"       # isEmpty 频=2 <5
    grep_out = "12:.is Empty(\n"

    def fake_cached_scan(cmd, path, timeout=0):
        return (0, freq_out, "") if ("uniq" in cmd) else (0, grep_out, "")

    with patch.object(L, "_cached_scan", side_effect=fake_cached_scan), \
         patch.object(L, "_run_l1_command", return_value=(0, "")) as m:
        n, _ = L._attempt_pseudospace_repair("/p", ["A.java"], 60)
    assert n == 0
    m.assert_not_called()   # 未触发折叠


def test_103_perl_anchored_to_reported_line():
    """复核 F1：perl 折叠必须定点到 gate① 报出的行号（`$. == <lineno>`），不 file-wide
    误折字符串/javadoc 里的 `.w1 w2(` 文本。"""
    freq_out = "  128 isEmpty\n"
    grep_out = "42:.is Empty(\n"      # grep -n 报第 42 行
    perl_calls = []

    def fake_cached_scan(cmd, path, timeout=0):
        return (0, freq_out, "") if ("uniq" in cmd) else (0, grep_out, "")

    with patch.object(L, "_cached_scan", side_effect=fake_cached_scan), \
         patch.object(L, "_run_l1_command", side_effect=lambda c, p, timeout=0: perl_calls.append(c) or (0, "")):
        L._attempt_pseudospace_repair("/p", ["A.java"], 60)
    assert perl_calls and "$. == 42" in perl_calls[0]  # 定点到报出行


def test_103_new_keyword_not_folded():
    """复核 F2：`outer.new Inner(`（合法内部类实例化，w1=new 关键字）绝不折叠成 `newInner`。"""
    freq_out = "  50 newInner\n  50 Inner\n"   # 即便 newInner 恰好高频也不折（关键字护栏在前）
    grep_out = "10:.new Inner(\n"

    def fake_cached_scan(cmd, path, timeout=0):
        return (0, freq_out, "") if ("uniq" in cmd) else (0, grep_out, "")

    with patch.object(L, "_cached_scan", side_effect=fake_cached_scan), \
         patch.object(L, "_run_l1_command", return_value=(0, "")) as m:
        n, _ = L._attempt_pseudospace_repair("/p", ["A.java"], 60)
    assert n == 0
    m.assert_not_called()


def test_103_no_candidate_no_fold():
    """无成员调用空格 → 无操作。"""
    def fake_cached_scan(cmd, path, timeout=0):
        return (0, "  128 isEmpty\n", "") if ("uniq" in cmd) else (1, "", "")

    with patch.object(L, "_cached_scan", side_effect=fake_cached_scan), \
         patch.object(L, "_run_l1_command", return_value=(0, "")) as m:
        n, _ = L._attempt_pseudospace_repair("/p", ["A.java"], 60)
    assert n == 0
    m.assert_not_called()


# ─────────────────────────── #114 symbol-repair 震荡护栏 ───────────────────────────

def test_114_high_freq_name_not_globally_renamed():
    """name 本身是项目高频真符号（≥5）→ 非拼写错，绝不 perl 全局改名（防 StringUtils↔SpringUtils 震荡）。"""
    build_out = (
        "src/A.java:[12,20] error: cannot find symbol\n"
        "  symbol:   variable StringUtils\n"
    )
    # StringUtils 与 SpringUtils 都高频，互为编辑距离近邻
    freq_out = "  200 StringUtils\n  180 SpringUtils\n"

    def fake_cached_scan(cmd, path, timeout=0):
        return 0, freq_out, ""

    with patch.object(L, "_cached_scan", side_effect=fake_cached_scan), \
         patch.object(L, "_run_l1_command", return_value=(0, "")) as m:
        n, _ = L._attempt_symbol_repair("/p", build_out, ["src/A.java"], 60)
    assert n == 0
    m.assert_not_called()   # 高频 name 不改名，震荡断链


def test_114_repeated_typo_still_repaired_despite_high_name_freq():
    """复核 F3：同一 typo 被复读复制 ≥5 次（isEmtpy 频=6）——good(isEmpty 128) 频次远高于 name(6)
    → 判 typo→主流纠正，仍修（不被裸 freq(name)≥5 误判成真符号漏修）。"""
    build_out = (
        "src/A.java:[12,20] error: cannot find symbol\n"
        "  symbol:   method isEmtpy\n"
    )
    freq_out = "  128 isEmpty\n  6 isEmtpy\n"   # typo 复读 6 次，仍远低于 isEmpty

    def fake_cached_scan(cmd, path, timeout=0):
        return 0, freq_out, ""

    with patch.object(L, "_cached_scan", side_effect=fake_cached_scan), \
         patch.object(L, "_run_l1_command", return_value=(0, "")) as m:
        n, _ = L._attempt_symbol_repair("/p", build_out, ["src/A.java"], 60)
    assert n == 1   # 仍修复
    assert m.called and "isEmpty" in m.call_args[0][0]


def test_114_low_freq_typo_still_repaired():
    """低频拼写错（isEmtpy 频<5）仍正常纠到高频近邻 isEmpty——护栏不误伤真修复。"""
    build_out = (
        "src/A.java:[12,20] error: cannot find symbol\n"
        "  symbol:   method isEmtpy\n"
    )
    freq_out = "  128 isEmpty\n  1 isEmtpy\n"

    def fake_cached_scan(cmd, path, timeout=0):
        return 0, freq_out, ""

    with patch.object(L, "_cached_scan", side_effect=fake_cached_scan), \
         patch.object(L, "_run_l1_command", return_value=(0, "")) as m:
        n, _ = L._attempt_symbol_repair("/p", build_out, ["src/A.java"], 60)
    assert n == 1
    assert m.called and "isEmpty" in m.call_args[0][0]


# ─────────────────────────── #113 全角标点诊断 ───────────────────────────

def test_113_fullwidth_punct_positions_surfaced():
    """改动源文件含全角 CJK 标点 → 返回精确坐标（只读诊断，不自改）。"""
    grep_out = "45:        method（arg1，arg2）；\n"
    with patch.object(L, "_cached_scan", return_value=(0, grep_out, "")):
        hits = L._scan_fullwidth_punct("/p", ["src/A.java"], 60)
    assert hits
    assert any("A.java" in h and "45" in h for h in hits)


def test_113_no_fullwidth_no_hits():
    with patch.object(L, "_cached_scan", return_value=(1, "", "")):
        assert L._scan_fullwidth_punct("/p", ["src/A.java"], 60) == []


def test_113_non_source_files_skipped():
    """非源文件（.md/.txt）不扫（全角标点在文档里合法）。"""
    with patch.object(L, "_cached_scan", return_value=(0, "1:，\n", "")) as m:
        hits = L._scan_fullwidth_punct("/p", ["README.md", "notes.txt"], 60)
    assert hits == []
    m.assert_not_called()


if __name__ == "__main__":
    import sys

    import pytest
    sys.exit(pytest.main([__file__, "-q"]))
