#!/usr/bin/env python3
"""★32 号文批1 突变锁★ M1（merge3 region-grouping）/ M2（git 拒绝可观测）/ S1（AUDIT
劫持链源头）/ D1-上半（topup 解析裸抛）四把锁的区分力验证。

每条突变必须让指名测试变红；仍绿=锁没牙（或 pyc 陈旧）。纪律同 xh_exec_mutation_check：
进程内字节快照还原（绝不 git checkout）、突变前后清 pyc、基线先绿、逐条串行、
落点唯一（count==1）、突变后源码必须仍可编译。
"""
from __future__ import annotations

import ast
import hashlib
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PY = ROOT / ".venv" / "bin" / "python"

MERGE = ROOT / "brain" / "merge_engine.py"
SHARED = ROOT / "brain" / "nodes" / "shared.py"
NODES = ROOT / "brain" / "nodes" / "__init__.py"

TESTS = [
    "test/test_merge3_region_grouping_32.py",
    "test/test_intent_audit_hijack_32s1.py",
    "test/test_coverage_topup_p1.py",
]

MUTATIONS = [
    # ── M1：region-grouping 三不变量 ──
    (
        "MUT-A 去掉「一侧保持 base 原样取另一侧」兜底（异侧各改一处必假冲突）",
        MERGE,
        "        if chunk_a == chunk_b:\n"
        "            merged.extend(chunk_a)\n"
        "        elif chunk_a == chunk_base:\n"
        "            merged.extend(chunk_b)\n"
        "        elif chunk_b == chunk_base:\n"
        "            merged.extend(chunk_a)\n"
        "        else:\n"
        "            _emit_conflict(chunk_a, chunk_b)",
        "        if chunk_a == chunk_b:\n"
        "            merged.extend(chunk_a)\n"
        "        else:\n"
        "            _emit_conflict(chunk_a, chunk_b)",
        ["test_nonoverlapping_line_edits_clean_merge",
         "test_insert_separated_by_stable_line_clean"],
    ),
    (
        "MUT-B 插入不并入区末（s<=i1<e）——插入点与编辑行相邻被拆成两个独立变更",
        MERGE,
        "                if s <= i1 <= e:",
        "                if s <= i1 < e:",
        ["test_boundary_insert_glues_to_adjacent_edit_conflict",
         "test_differential_vs_git_merge_file_seeded_corpus"],
    ),
    (
        "MUT-C1 冲突块不修剪共同前缀——同内容双插在标记里重复出现",
        MERGE,
        "        while pre < len(chunk_a) and pre < len(chunk_b) and chunk_a[pre] == chunk_b[pre]:",
        "        while False:",
        ["test_common_prefix_trimmed_out_of_conflict_block"],
    ),
    (
        "MUT-C2 冲突块不修剪共同后缀——同内容双插在标记里重复出现",
        MERGE,
        "        while (suf < len(chunk_a) - pre and suf < len(chunk_b) - pre",
        "        while (False and suf < len(chunk_a) - pre and suf < len(chunk_b) - pre",
        ["test_common_suffix_trimmed_out_of_conflict_block"],
    ),
    (
        "MUT-D 区界条件反转（or）——单侧编辑被切成碎片，编辑内容静默丢失",
        MERGE,
        "        if a_st[i] and b_st[i]:",
        "        if a_st[i] or b_st[i]:",
        ["test_nonoverlapping_line_edits_clean_merge",
         "test_differential_vs_git_merge_file_seeded_corpus"],
    ),
    # ── M2/HIGH-1：git 退出码语义 ──
    (
        "MUT-E git merge-file 拒绝时删 WARNING——老 git 部署无痕降级 python merge3",
        MERGE,
        '            logger.warning(\n'
        '                "git merge-file 拒绝（rc=%s, stderr=%.200s）→ 降级 python merge3"\n'
        '                "（行级三路合并，语义不同；典型=git<2.35 不识 --zdiff3）",\n'
        '                proc.returncode, proc.stderr or "")',
        '            pass  # 突变：静默降级',
        ["test_git_merge_file_rc_not_in_01_warns_and_returns_none"],
    ),
    (
        "MUT-J rc 判别退回 in (0,1)——rc≥2 的多冲突块合并被误诊为「git 拒绝」降级",
        MERGE,
        "            if proc.returncode == 0:\n"
        "                return proc.stdout, True\n"
        "            if 1 <= proc.returncode < 128:\n"
        "                return proc.stdout, False",
        "            if proc.returncode in (0, 1):\n"
        "                return proc.stdout, proc.returncode == 0",
        ["test_git_merge_file_rc_ge_2_is_conflict_not_rejection"],
    ),
    # ── slide-down 规范化 ──
    (
        "MUT-I 删 slide-down 规范化——重复行语料对齐歧义复活（fail-open 回潮）",
        MERGE,
        "    a_edits = _canon(a_edits, a_lines)\n"
        "    b_edits = _canon(b_edits, b_lines)",
        "    a_edits = [tuple(op) for op in a_edits]\n"
        "    b_edits = [tuple(op) for op in b_edits]",
        ["test_duplicate_line_delete_adjacent_to_edit_conflicts",
         "test_canon_bearing_conflict_dup_delete_vs_insert",
         "test_canon_bearing_clean_crossing_edits",
         "test_canon_bearing_conflict_dup_delete_vs_edit"],
    ),
    # ── S1：AUDIT 劫持链 ──
    (
        "MUT-F 删建造/修复/改造词共现守卫——「实现 X 并通过安全审计」整任务翻 AUDIT",
        SHARED,
        "        if not has(*_INTENT_DEBUG_KEYWORDS, *_INTENT_REFACTOR_KEYWORDS,\n"
        "                   *_INTENT_CREATE_KEYWORDS, *_INTENT_CHANGE_VERBS_ZH) \\\n"
        "                and not _INTENT_CHANGE_VERBS_EN_RE.search(t):",
        "        if True:  # 突变：建造词共现也翻 AUDIT",
        ["test_audit_keyword_with_build_words_never_flips_audit",
         "test_every_sibling_family_word_blocks_audit_flip",
         "test_every_english_change_verb_blocks_audit_flip",
         "test_non_audit_inference_no_audit_warning"],
    ),
    (
        "MUT-G 删 AUDIT 翻转 WARNING——劫持链第一环恢复静默",
        SHARED,
        '            import logging\n'
        '            logging.getLogger(__name__).warning(\n'
        '                "[INTENT] intent_audit_inferred: 启发式判定 AUDIT（审计关键词命中且无"\n'
        '                "建造/修复词共现）；该意图走安全审计短路不产 diff，请核对任务描述意图: %.80s",\n'
        '                task_description or "")',
        '            pass  # 突变：静默翻转',
        ["test_audit_flip_emits_machine_readable_warning"],
    ),
    # ── D1-上半：解析裸抛 ──
    (
        "MUT-H topup 解析失去 try 包裹——LLM 输出不可解析时异常裸抛炸链 FAILED@PLAN",
        NODES,
        "        try:\n"
        "            _result = _parse_json_from_llm(_resp.content)\n"
        "        except Exception as exc:  # noqa: BLE001 — 解析失败=补齐无效→回退全量重拆，绝不炸链\n"
        '            logger.warning("[PLAN] P1 外科补齐 LLM 输出解析失败(%s)→回退全量重拆", exc)\n'
        "            return None",
        "        _result = _parse_json_from_llm(_resp.content)",
        ["test_topup_returns_none_on_unparseable_llm_output"],
    ),
]


def _pytest(args: list[str]) -> int:
    p = subprocess.run([PY, "-m", "pytest", *TESTS, "-p", "no:warnings", "-q",
                        "--tb=no", *args], cwd=ROOT, capture_output=True, text=True)
    return p.returncode


def _clear_pyc(path: Path) -> None:
    """删被突变模块的 pyc（整秒粒度 mtime 判旧 ⇒ 相邻突变可能跑上一条字节码）。"""
    cache = path.parent / "__pycache__"
    if cache.is_dir():
        for f in cache.glob(path.stem + ".*.pyc"):
            try:
                f.unlink()
            except OSError:
                pass


def main() -> int:
    md5_before = {p: hashlib.md5(p.read_bytes()).hexdigest() for p in {MERGE, SHARED, NODES}}
    print("═" * 70)
    print("步骤 0：基线必须全绿")
    print("═" * 70)
    rc = _pytest([])
    if rc != 0:
        print(f"✗ 基线是红的 (exit={rc}) —— 突变结果全部无意义。先修基线。")
        return 1
    print(f"✓ 基线全绿 (exit={rc})\n")

    failures = []
    for i, (name, path, old, new, should_red) in enumerate(MUTATIONS, 1):
        src = path.read_text()
        if old not in src:
            print(f"[{i}/{len(MUTATIONS)}] {name}\n    ✗ 落点未命中（代码已漂移）")
            failures.append((name, "落点未命中"))
            continue
        if src.count(old) != 1:
            print(f"[{i}/{len(MUTATIONS)}] {name}\n"
                  f"    ✗ 落点出现 {src.count(old)} 次（非唯一，突变不等价）")
            failures.append((name, "落点非唯一"))
            continue
        mutated = src.replace(old, new, 1)
        try:
            ast.parse(mutated)
        except SyntaxError as _e:
            print(f"[{i}/{len(MUTATIONS)}] {name}\n"
                  f"    ✗ 突变后源码无法编译（{_e.msg} @line {_e.lineno}）⇒ rc≠0 是假信号")
            failures.append((name, "突变产生语法错"))
            continue
        path.write_text(mutated)
        _clear_pyc(path)
        try:
            per = [(n, _pytest(["-k", n])) for n in should_red]
            missing = [n for n, r in per if r == 5]
            green = [n for n, r in per if r == 0]
            print(f"[{i}/{len(MUTATIONS)}] {name}")
            if not missing and not green:
                print(f"    ✓ 指名的 {len(per)} 条全红")
            else:
                if missing:
                    print(f"    ✗ 测试名选不到（rc=5）: {missing}")
                    failures.append((name, "测试名选不到"))
                if green:
                    print(f"    ✗ 突变后仍绿 = 零区分力: {green}")
                    failures.append((name, "突变后仍绿"))
        finally:
            path.write_text(src)
            _clear_pyc(path)

    print("\n" + "═" * 70)
    rc_r = _pytest([])
    print(f"步骤 N：还原后基线复验 exit={rc_r}")
    if rc_r != 0:
        print("✗ 还原后基线不绿 —— harness 污染了工作树")
        return 1
    md5_after = {p: hashlib.md5(p.read_bytes()).hexdigest() for p in md5_before}
    if md5_before != md5_after:
        print("✗ 文件 md5 与起跑时不一致 —— 还原不完整")
        return 1
    if failures:
        print(f"\n✗ {len(failures)} 条未达标：")
        for n, why in failures:
            print(f"  · [{why}] {n}")
        return 1
    print(f"\n✓ 全部 {len(MUTATIONS)} 条突变都被锁住，基线前后皆绿，md5 还原一致")
    return 0


if __name__ == "__main__":
    sys.exit(main())
