"""★32 号文 M1+M2★ merge_engine 三路合并 region-grouping 重写 + git 拒绝可观测 — 行为锁。

M1：_python_merge3 旧拉链按整条 opcode 消费，两侧 base 区间粒度不同时假冲突+行错位
    （最平凡的「不同位置各改一行」100% 命中）；沙箱无 git 时本函数是实际主路径
    （test_merge3_conflict_23 注释实证）。重写为 diff3 经典 region-grouping：
    变更区=至少一侧非稳定的最大连续行段；区内一侧保持 base 原样则取另一侧；
    插入点 p∈[s,e] 并入相邻变更区（git 实测口径）；冲突块共同前后缀提出标记外。
    本文件钉死：复现案例、git 边界语义探针、以及与 git merge-file 的差分一致性——
    分两个语料层（★不得混写★）：base 行唯一语料=硬锁（git 干净⇒逐字一致；
    git 冲突⇒必冲突）；重复行密集语料=可达性质锁（不崩溃+标记行零丢失），方向锁
    在该维度不成立（slide-down 单方向规范化 vs git 跨双侧 hunk 对齐，残余发散已
    量化登记，详见 merge_engine._python_merge3 docstring 诚实边界段）。
M2/HIGH-1：_git_merge_file 退出码=冲突块个数（0=干净，1..127=冲突数）——rc∈[1,127]
    按冲突透传；rc∉[0,127]（典型 git<2.35 不识 --zdiff3 恒 129）才 WARNING+降级。
"""

from __future__ import annotations

import random
import shutil
import subprocess
import tempfile
from pathlib import Path
from unittest.mock import patch

import pytest

from swarm.brain.merge_engine import _git_merge_file, _python_merge3

BASE10 = "".join(f"L{i}\n" for i in range(1, 11))


# ───────────── M1 复现案例（旧拉链必假冲突）─────────────

def test_nonoverlapping_line_edits_clean_merge():
    """M1 原复现：ours 改 L2 / theirs 改 L8 → 干净合并且逐字等于 git 结果。"""
    ours = BASE10.replace("L2\n", "OURS2\n")
    theirs = BASE10.replace("L8\n", "THEIRS8\n")
    merged, ok = _python_merge3(BASE10, ours, theirs)
    assert ok, f"不同位置各改一行必须干净合并，实际:\n{merged}"
    assert merged == BASE10.replace("L2\n", "OURS2\n").replace("L8\n", "THEIRS8\n")


def test_tail_insert_and_mid_replace_clean():
    """异侧：中部替换 + 尾部追加 → 干净合并，两者俱在且无重复行。"""
    ours = BASE10.replace("L5\n", "X5\n")
    theirs = BASE10 + "TAIL\n"
    merged, ok = _python_merge3(BASE10, ours, theirs)
    assert ok, f"中部替换+尾部追加必须干净合并，实际:\n{merged}"
    assert "X5\n" in merged and merged.endswith("TAIL\n")
    assert merged.count("L9") == 1 and merged.count("L10") == 1


# ───────────── git 边界语义探针（插入点与变更区相邻的归属口径）─────────────

def test_boundary_insert_glues_to_adjacent_edit_conflict():
    """git 探针(a)：ours 在 L1 后插入 / theirs 改 L1 → 插入点与编辑行相邻=同一 hunk=冲突。"""
    base = "L1\nL2\nL3\n"
    merged, ok = _python_merge3(base, "L1\nINS\nL2\nL3\n", "X\nL2\nL3\n")
    assert not ok
    assert "INS" in merged and "X" in merged and "<<<<<<< ours" in merged


def test_insert_separated_by_stable_line_clean():
    """git 探针(b)：插入点与编辑行之间隔一行未修改行 → 干净合并。"""
    base = "L1\nL2\nL3\n"
    merged, ok = _python_merge3(base, "L1\nL2\nINS\nL3\n", "X\nL2\nL3\n")
    assert ok, f"隔一行的插入+编辑必须干净合并，实际:\n{merged}"
    assert merged == "X\nL2\nINS\nL3\n"


def test_both_edit_same_line_and_one_inserts_conflict():
    """git 探针(c)：双侧改同一行 + 一侧相邻插入 → 冲突，插入并入该侧冲突块。"""
    base = "L1\nL2\nL3\n"
    merged, ok = _python_merge3(base, "X\nINS\nL2\nL3\n", "Y\nL2\nL3\n")
    assert not ok
    assert "INS" in merged and "X" in merged and "Y" in merged


def test_common_suffix_trimmed_out_of_conflict_block():
    """git 探针(g)：双侧同址插入同内容 NEW 但同区编辑不同 → NEW 提出冲突块只出现一次。"""
    base = "L1\nL2\n"
    merged, ok = _python_merge3(base, "X\nNEW\nL2\n", "Y\nNEW\nL2\n")
    assert not ok
    assert merged.count("NEW") == 1, f"共同后缀必须提出冲突块，实际:\n{merged}"
    assert "X" in merged and "Y" in merged


def test_common_prefix_trimmed_out_of_conflict_block():
    """git 探针(g)镜像：双侧同址插入同内容 NEW（区首）但编辑不同 → NEW 提出冲突块只一次。"""
    base = "L1\nL2\n"
    merged, ok = _python_merge3(base, "NEW\nX\nL2\n", "NEW\nY\nL2\n")
    assert not ok
    assert merged.count("NEW") == 1, f"共同前缀必须提出冲突块，实际:\n{merged}"
    assert merged.index("NEW") < merged.index("<<<<<<<"), "共同前缀必须在冲突块之前"


def test_replace_plus_divergent_insert_at_same_point_conflicts():
    """git 探针(d)：ours 纯插入 / theirs 改+插（同址异内容）→ 冲突，绝不静默拼接。"""
    base = "a0\n"
    merged, ok = _python_merge3(base, "a0\nNEW0\n", "ED2\nNEW3\n")
    assert not ok
    assert "NEW0" in merged and "NEW3" in merged and "ED2" in merged


# ───────────── 与 git merge-file 的差分一致性锁（有 git 时）─────────────

def _git_merge(base: str, ours: str, theirs: str) -> tuple[str, bool]:
    with tempfile.TemporaryDirectory() as d:
        for name, content in (("ours", ours), ("base", base), ("theirs", theirs)):
            Path(d, name).write_text(content, encoding="utf-8")
        p = subprocess.run(
            ["git", "merge-file", "-p",
             str(Path(d, "ours")), str(Path(d, "base")), str(Path(d, "theirs"))],
            capture_output=True, text=True)
        return p.stdout, p.returncode == 0


@pytest.mark.skipif(shutil.which("git") is None, reason="沙箱无 git——差分锁只在有 git 环境跑")
def test_differential_vs_git_merge_file_seeded_corpus():
    """差分硬锁（base 行【唯一】语料）：git 干净 ⇒ 逐字一致；git 冲突 ⇒ 必冲突。
    行值带位置序号 i ⇒ 每行唯一 ⇒ 无对齐歧义 ⇒ 硬不变量成立。
    重复行密集维度见下方 test_differential_duplicate_dense_corpus——该维度方向锁
    不成立（已量化诚实边界），只钉可达性质，两个语料层不得混写。"""
    rng = random.Random(20260819)
    exact = both_conflict = 0
    for _ in range(150):
        n = rng.randint(0, 12)
        base = "".join(f"{rng.choice('abcd')}{i}\n" for i in range(n))

        def _mutate(s: str) -> str:
            lines = s.splitlines(keepends=True)
            for _ in range(rng.randint(1, 3)):
                if not lines or rng.random() < 0.35:
                    lines.insert(rng.randint(0, len(lines)), f"NEW{rng.randint(0, 3)}\n")
                else:
                    pos = rng.randrange(len(lines))
                    if rng.random() < 0.5:
                        lines[pos] = f"ED{rng.randint(0, 3)}\n"
                    else:
                        lines.pop(pos)
            return "".join(lines)

        ours, theirs = _mutate(base), _mutate(base)
        py_merged, py_ok = _python_merge3(base, ours, theirs)
        git_merged, git_ok = _git_merge(base, ours, theirs)
        if git_ok:
            assert py_ok, f"git 干净合并我们却冲突: base={base!r} ours={ours!r} theirs={theirs!r}"
            assert py_merged == git_merged, (
                f"干净合并必须逐字一致: base={base!r} ours={ours!r} theirs={theirs!r}\n"
                f"py={py_merged!r}\ngit={git_merged!r}")
            exact += 1
        else:
            assert not py_ok, f"git 冲突我们却干净放行: base={base!r} ours={ours!r} theirs={theirs!r}"
            both_conflict += 1
    assert exact + both_conflict == 150


def test_duplicate_line_delete_adjacent_to_edit_conflicts():
    """slide-down 规范化实证锁：base 含重复行时，删除经规范化下移后与对侧编辑
    落入同一变更区=冲突（git 口径；规范化前本例被拆到两区干净放行=fail-open）。"""
    merged, ok = _python_merge3("a3\na3\na1\n", "a3\na3\nE0\n", "a3\na1\n")
    assert not ok, "重复行删除与对侧行编辑相邻必须冲突（git merge-file 同判）"


# ───────────── canon 承重硬锁（R2 复核 LOW：单条锁钉滑窗算法偏薄）─────────────
# 以下三例由差分探针打捞（py带canon==git 而 py无canon!=git，4000 种子语料），
# 逐例经真 git merge-file 复核：删 _canon（MUT-I）即红，两个方向各至少一例。

def test_canon_bearing_conflict_dup_delete_vs_insert():
    """canon 承重·冲突方向：ours 删尾部 a3+替换 / theirs 删一个重复 a1——
    无 canon 时两侧变更区被重复行岔开而干净放行（fail-open），git 判冲突。"""
    base = "a1\na1\na1\na2\na3\n"
    merged, ok = _python_merge3(base, "a1\na1\nN1\na2\n", "a1\na1\na2\na3\n")
    assert not ok, "git 判冲突（探针实证），无 canon 时本例被干净放行"
    assert merged == "a1\na1\n<<<<<<< ours\nN1\n=======\n>>>>>>> theirs\na2\n"


def test_canon_bearing_clean_crossing_edits():
    """canon 承重·干净方向：ours 改 a2→E1 / theirs 插 N1+删 a2——重复行锚定下
    无 canon 会把两处干净变更误并为冲突（误杀），git 干净合并。"""
    base = "a2\na3\na2\na0\na3\na2\na0\n"
    ours = "a2\na3\nE1\na3\na2\na0\n"
    theirs = "a2\nN1\na3\na2\na0\na3\na2\na0\n"
    merged, ok = _python_merge3(base, ours, theirs)
    assert ok, "git 干净合并（探针实证），无 canon 时本例假冲突"
    assert merged == "a2\nN1\na3\nE1\na3\na2\na0\n"


def test_canon_bearing_conflict_dup_delete_vs_edit():
    """canon 承重·冲突方向：ours 删重复 a0 实例 / theirs 改相邻 a0→E1——
    无 canon 时删除被锚定到异位实例而干净放行（fail-open），git 判冲突。"""
    base = "a0\na0\na1\na1\na0\n"
    merged, ok = _python_merge3(base, "a0\na1\na1\na0\n", "a0\na0\nE1\na1\na1\na0\n")
    assert not ok, "git 判冲突（探针实证），无 canon 时本例被干净放行"
    assert merged == "a0\n<<<<<<< ours\n=======\na0\nE1\n>>>>>>> theirs\na1\na1\na0\n"


@pytest.mark.skipif(shutil.which("git") is None, reason="沙箱无 git——差分锁只在有 git 环境跑")
def test_differential_duplicate_dense_corpus_reachable_properties():
    """重复行密集语料（对齐歧义高发）：方向锁在此维度【不成立】——slide-down 是
    单方向规范化，git 在歧义形态跨双侧 hunk 对齐（xdiff ZEALOUS），残余发散已量化
    （三份各自独立 6000 例：假干净 8~9 / 假冲突 8~41 / 干净但内容不一致 0~2，
    数字随语料配方漂移，见 merge_engine._python_merge3 docstring 诚实边界段）。
    本锁钉【可达性质】：不崩溃 + 双侧各自引入的标记行在输出中零丢失且零重复
    （hunter R2 在 6000 例上实证 count==1 全称成立，从 in 升级为计数锁）。"""
    rng = random.Random(20260819)
    for _ in range(400):
        n = rng.randint(0, 25)
        base = "".join(f"{rng.choice('ab')}{i % 3}\n" for i in range(n))

        def _mutate(s: str, tag: str) -> str:
            lines = s.splitlines(keepends=True)
            for _ in range(rng.randint(0, 4)):
                if not lines or rng.random() < 0.35:
                    lines.insert(rng.randint(0, len(lines)), f"NEW{rng.randint(0, 3)}\n")
                else:
                    pos = rng.randrange(len(lines))
                    if rng.random() < 0.4:
                        lines[pos] = f"ED{rng.randint(0, 3)}\n"
                    else:
                        lines.pop(pos)
            lines.insert(rng.randint(0, len(lines)), f"MARKER_{tag}\n")
            return "".join(lines)

        ours, theirs = _mutate(base, "O"), _mutate(base, "T")
        merged, ok = _python_merge3(base, ours, theirs)
        assert isinstance(merged, str) and isinstance(ok, bool), "不得崩溃/返回异型"
        assert merged.count("MARKER_O\n") == 1 and merged.count("MARKER_T\n") == 1, (
            f"双侧引入的标记行必须零丢失且零重复（任何合并结果）: "
            f"base={base!r} ours={ours!r} theirs={theirs!r} merged={merged!r}")


@pytest.mark.xfail(strict=True, reason=(
    "已量化诚实边界：周期/重复块语料下 git 跨双侧 hunk 对齐（ZEALOUS），单方向 "
    "slide-down 规范化不可全对齐——本例 git 判冲突、我们干净放行（6000 例残余 "
    "千分位 fail-open 之一）。收编为文档化案例，算法对齐 xdiff 后方可转硬锁。"))
@pytest.mark.skipif(shutil.which("git") is None,
                    reason="沙箱无 git——无法对照 git 方向（hunter R2：strict 锁在无 git "
                           "环境会 vacuous，而那正是 fallback 主战场，干脆不跑而非假过）")
def test_documented_residual_failopen_periodic_block():
    """文档化残余 fail-open：theirs 删 f1、ours 在 f1 前插 f1 c1——重复行锚定岔开。"""
    merged, ok = _python_merge3(
        "d1\nb2\nf0\nf1\na2\nd1\nf0\n",
        "d1\nb2\nf1\nc1\nf0\na2\nd1\nf0\n",
        "d1\nb2\nf0\na2\nd1\nf0\n")
    _git_merged, git_ok = _git_merge(
        "d1\nb2\nf0\nf1\na2\nd1\nf0\n",
        "d1\nb2\nf1\nc1\nf0\na2\nd1\nf0\n",
        "d1\nb2\nf0\na2\nd1\nf0\n")
    assert ok == git_ok, "与 git 方向一致（当前已知发散：git 冲突我们干净）"


@pytest.mark.xfail(strict=True, reason=(
    "★hunter R2 坐实的内容不一致形态★「双侧各删一个重复行实例的归属歧义」：base 含 "
    "3×a5，ours 删一个 / theirs 插 N0 同时删一个——git 对齐后只留 1×a5，py 保守多留一行 "
    "（2×a5）。双侧各自引入的行零丢失，发散只在 base 重复实例的存活数。py 方向=保守侧"
    "（宁多勿丢），可辩护但与 git 不一致必须登记。算法对齐 xdiff 后方可转硬锁。"))
@pytest.mark.skipif(shutil.which("git") is None,
                    reason="沙箱无 git——内容对照锁只在有 git 环境跑")
def test_documented_residual_content_divergence_dup_delete_attribution():
    """双侧各删一个重复行实例的最小复现（hunter R2 语料坐实的两个实例之一）。"""
    base = "b2\na5\na5\na5\n"
    ours = "b2\na5\na5\n"
    theirs = "b2\nN0\na5\na5\n"
    py_merged, py_ok = _python_merge3(base, ours, theirs)
    git_merged, git_ok = _git_merge(base, ours, theirs)
    assert py_ok and git_ok, "双侧都判干净（发散不在方向在内容）"
    assert "N0\n" in py_merged and "N0\n" in git_merged, "theirs 引入行两侧都必须零丢失"
    assert py_merged == git_merged, (
        f"当前已知发散：py 多留一个重复实例 a5（保守侧）: py={py_merged!r} git={git_merged!r}")



# ───────────── M2：git 拒绝（rc∉{0,1}）必须 WARNING + 降级可观测 ─────────────

class _Proc:
    def __init__(self, returncode, stdout="", stderr=""):
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


def test_git_merge_file_rc_not_in_01_warns_and_returns_none(caplog):
    """M2：git<2.35 不识 --zdiff3 恒 rc=129——原静默降级，现必须 WARNING + 返回 None。"""
    with patch("swarm.brain.merge_engine.subprocess.run",
               return_value=_Proc(129, "", "error: unknown option `zdiff3'")):
        with caplog.at_level("WARNING", logger="swarm.brain.merge_engine"):
            res = _git_merge_file("a\n", "a\nb\n", "a\nc\n")
    assert res is None
    assert any("git merge-file 拒绝" in r.message and "129" in r.message
               for r in caplog.records), "rc∉{0,1} 必须有带 rc 的 WARNING（降级可观测）"


def test_git_merge_file_rc0_clean_and_rc1_conflict_still_parsed():
    """回归护栏：rc=0（干净）/rc=1（一个冲突块）仍按原语义解析。"""
    with patch("swarm.brain.merge_engine.subprocess.run",
               return_value=_Proc(0, "merged\n", "")):
        assert _git_merge_file("a\n", "a\nb\n", "a\nc\n") == ("merged\n", True)
    with patch("swarm.brain.merge_engine.subprocess.run",
               return_value=_Proc(1, "conflict\n", "")):
        assert _git_merge_file("a\n", "a\nb\n", "a\nc\n") == ("conflict\n", False)


def test_git_merge_file_rc_ge_2_is_conflict_not_rejection():
    """★32 号文 HIGH-1（双复核逮出）★ git merge-file 退出码=冲突块个数（0=干净，
    1..127=冲突数）——`in (0,1)` 判别把 rc≥2 的多冲突块合并误诊为「git 拒绝」降级
    python merge3，恰好把最难的多冲突文件交给语义最弱的 fallback。rc∈[1,127] 必须
    按冲突透传，且不打「拒绝」WARNING（不误导 ops 去查幻影 git 版本问题）。"""
    with patch("swarm.brain.merge_engine.subprocess.run",
               return_value=_Proc(2, "c1\n<<<<<<<\n=======\n>>>>>>>\nc2\n", "")) as _run:
        with patch("swarm.brain.merge_engine.logger.warning") as warn:
            res = _git_merge_file("a\nb\nc\n", "A1\nb\nC1\n", "A2\nb\nC2\n")
    assert res == ("c1\n<<<<<<<\n=======\n>>>>>>>\nc2\n", False), "rc=2=两个冲突块，必须透传"
    warn.assert_not_called()
