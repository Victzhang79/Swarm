#!/usr/bin/env python3
"""P-C1 突变 harness（判据与前六批同源，那些自伤一开始就带上）：

  · **先验基线全绿**（只验"突变→红"会让**修得不全**的整改全绿通过：B-4b I-1 实证）；
  · **逐条**跑 should_red，每条都必须红；· `rc=5`（`-k` 选不到，如测试被重命名）**判失败**；
  · 落点唯一性检查；· 突变后源码必须仍能 `ast.parse`（否则 rc≠0 只是 collection error）。

★锁的命题★ P-C1＝"栈识别不得有第二事实源，且 unknown 回退必须响"。三条独立面：
识别覆盖面（含大小写档）· 伪造闸行为翻转 · 降级可观测（含反向粘滞锁）。
"""
from __future__ import annotations

import ast
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PY = str(ROOT / ".venv" / "bin" / "python")
TESTS = ["test/test_b3_stack_spec_single_source.py",
         "test/test_scaffold_npm_go_driver_p2.py",
         "test/test_b0_non_maven_cassette_replay.py",
         "test/test_r39_build_scaffold_inject.py",
         "test/test_n2b_n3_go_prefix_and_rule5_stack.py"]

CU = ROOT / "brain" / "contract_utils.py"
PF = ROOT / "brain" / "plan_finisher.py"
SPEC = ROOT / "stacks" / "spec.py"

MUTATIONS = [
    # ── 识别覆盖面 ──
    (
        'P-C1：派生视图只出 maven（模拟"新栈没接进路由表"⇒ 纯 python 仓落回 Maven 兜底）',
        SPEC,
        '    for key in sorted(STACK_SPEC):\n        for name in STACK_SPEC[key].root_manifests:\n            out.append((name, key))',
        '    for key in ["maven"]:\n        for name in STACK_SPEC[key].root_manifests:\n            out.append((name, key))',
        ['test_every_spec_root_manifest_is_recognized_on_disk',
         'test_pure_python_repo_is_never_given_fabricated_pom',
         'test_root_manifests_by_stack_covers_every_spec_entry'],
    ),
    (
        'P-C1：派生视图小写化清单名（Linux 上 `Gemfile`/`Pipfile` 恒探不到 ⇒ 判 unknown ⇒ 塞 pom）。'
        '★本机 macOS 大小写不敏感，故落点必须选平台无关的纯函数属性——用"造 Pipfile 探 pipfile"'
        '的夹具在本机零区分力（harness 第一轮实测逮到）★',
        SPEC,
        '    for key in sorted(STACK_SPEC):\n        for name in STACK_SPEC[key].root_manifests:\n            out.append((name, key))',
        '    for key in sorted(STACK_SPEC):\n        for name in STACK_SPEC[key].root_manifests:\n            out.append((name.lower(), key))',
        ['test_root_manifests_by_stack_preserves_canonical_case'],
    ),
    (
        'P-C1：plan 路径档丢掉 root 兜底（plan 里建 requirements.txt 不再算 python 证据）',
        CU,
        '        _stk_hit = stack_of_structural_manifest(base) or stack_of_manifest(base)',
        '        _stk_hit = stack_of_structural_manifest(base)',
        ['test_plan_path_root_only_manifest_is_python_evidence'],
    ),
    # ── 伪造闸行为翻转 ──
    (
        'P-C1：伪造闸把已知非 Maven 栈也放行（回到"unknown 或任何栈都塞 pom"）',
        CU,
        '    _should = (stk == "unknown" or stk in _AGGREGATOR_SCAFFOLD_STACKS)',
        '    _should = True',
        ['test_pure_python_repo_is_never_given_fabricated_pom'],
    ),
    (
        'P-C1：第三个消费者（plan_finisher 裸奔闸）退回窄表口径 ⇒ python 基线判不出栈',
        PF,
        '            _bstk = {stk for name, stk in root_manifests_by_stack()\n                     if _os.path.exists(_os.path.join(project_path, name))}',
        '            _bstk = {stk for name, stk in [("pom.xml", "maven")]\n                     if _os.path.exists(_os.path.join(project_path, name))}',
        ['test_pc1_bare_pom_gate_recognizes_python_baseline'],
    ),
    # ── 降级可观测（血规 3）──
    (
        'P-C1：unknown 回退不再告警（降级静默 ⇒ php/ruby 被塞 pom 无声）',
        CU,
        '    if stk == "unknown":\n        # ★两因并列，不猜是哪个★',
        '    if stk == "unknown" and False:\n        # ★两因并列，不猜是哪个★',
        ['test_unknown_stack_fallback_is_loud'],
    ),
    (
        'P-C1 自查：歧义混栈不再有独占机读键（两种 unknown 塌成一个信号 ⇒ 读者被指向 php/ruby '
        '而真因是"plan 同时像两个栈"）',
        CU,
        '        logger.info("[SCAFFOLD-INJECT] G9 stack_unknown_cause=ambiguous_mixed：异栈清单证据 %s "',
        '        logger.info("[SCAFFOLD-INJECT] G9 异栈清单证据 %s "',
        ['test_ambiguous_mixed_stack_unknown_is_distinguishable_from_no_evidence'],
    ),
    (
        'P-C1 自查：机读键改成无条件打（粘滞 ⇒ 零证据也报歧义混栈）。★这条锁的是反向锁本身'
        '有区分力——第一版用散文子串"歧义混栈"判，因外层 WARNING 也含该四字而当场假绿★',
        CU,
        # ★落点必须选"零证据路径也挂上那个键"★ 不能改 `if _has_jvm_src` → `if True`：
        # `if not seen: return "unknown"` 在它**之前**早返，零证据输入根本到不了那一行
        # ⇒ 突变不等价（harness 第一轮实测存活，正是它该抓的"落点不等价"）。
        '        return "unknown"\n    if "maven" in seen:',
        '        logger.info("[SCAFFOLD-INJECT] G9 stack_unknown_cause=ambiguous_mixed（粘滞突变）")\n'
        '        return "unknown"\n    if "maven" in seen:',
        ['test_no_evidence_unknown_does_not_claim_mixed_stack'],
    ),
    (
        'P-C1：告警变无条件（粘滞告警＝等于没有告警，always-emit 一族）',
        CU,
        '    if stk == "unknown":\n        # ★两因并列，不猜是哪个★',
        '    if True:\n        # ★两因并列，不猜是哪个★',
        ['test_known_stack_does_not_emit_unknown_warning'],
    ),
]


def _pytest(args: list[str]) -> int:
    p = subprocess.run([PY, "-m", "pytest", *TESTS, "-p", "no:warnings", "-q",
                        "--tb=no", *args], cwd=ROOT, capture_output=True, text=True)
    return p.returncode


def main() -> int:
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
                  f"    ✗ 突变后源码无法编译（{_e.msg} @line {_e.lineno}）⇒ pytest 只会报 "
                  f"collection error，rc≠0 是假信号。")
            failures.append((name, "突变产生语法错"))
            continue
        path.write_text(mutated)
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

    print("\n" + "═" * 70)
    rc_r = _pytest([])
    print(f"步骤 N：还原后基线复验 exit={rc_r}")
    if rc_r != 0:
        print("✗ 还原后基线不绿 —— harness 污染了工作树")
        return 1
    if failures:
        print(f"\n✗ {len(failures)} 条未达标：")
        for n, why in failures:
            print(f"  · [{why}] {n}")
        return 1
    print(f"\n✓ 全部 {len(MUTATIONS)} 条突变都被锁住，且基线前后皆绿")
    return 0


if __name__ == "__main__":
    sys.exit(main())
