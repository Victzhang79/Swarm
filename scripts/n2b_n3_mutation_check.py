#!/usr/bin/env python3
"""N-2b / N-3 突变 harness（判据与前四批同源，那些自伤一开始就带上）：

  · **先验基线全绿**（只验"突变→红"会让**修得不全**的整改全绿通过：B-4b I-1 实证）；
  · **逐条**跑 should_red，每条都必须红；· `rc=5`（`-k` 选不到，如测试被重命名）**判失败**；
  · 落点唯一性检查；· 突变后源码必须仍能 `ast.parse`（否则 rc≠0 只是 collection error）。

★与 `decisions_mutation_check.py` 刻意各一份★：TESTS 集合不同（那批锁 L2/沙箱，本批锁
规划期脚手架与规则5）。合成一份会让任一批的失败都要跑全部测试，且落点表混在一起读不出批次。
"""
from __future__ import annotations

import ast
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PY = str(ROOT / ".venv" / "bin" / "python")
TESTS = ["test/test_n2b_n3_go_prefix_and_rule5_stack.py",
         "test/test_b0_non_maven_cassette_replay.py",
         "test/test_ctodebt_maven_module_scope.py",
         "test/test_contract_dependencies_b.py",
         "test/test_r39_build_scaffold_inject.py",
         "test/test_sandbox_spec.py",
         "test/test_decisions_l2_share_and_skipdirs.py"]

CU = ROOT / "brain" / "contract_utils.py"
PV = ROOT / "brain" / "plan_validator.py"
SBX = ROOT / "project" / "sandbox_spec.py"
SPEC = ROOT / "stacks" / "spec.py"

MUTATIONS = [
    # ── N-2b：go module 前缀取证 ──
    (
        'N-2b：成员反推整块失效（退回"只认根 go.mod" ⇒ go.work 仓整栈零脚手架）',
        CU,
        '    root_mod = _go_root_module_path(project_path)\n    if root_mod:\n        return root_mod',
        '    return _go_root_module_path(project_path)',
        ['test_n2b_prefix_derived_from_workspace_members_when_no_root_go_mod',
         'test_n2b_go_work_repo_actually_gets_scaffolds_end_to_end',
         'test_go_work_modules_get_manifest_scaffolds'],
    ),
    (
        'N-2b：歧义不再 fail-closed（互斥前缀时挑第一个 ⇒ 臆造 module 路径）',
        CU,
        '    if len(prefixes) == 1:\n        prefix = next(iter(prefixes))',
        '    if len(prefixes) >= 1:\n        prefix = next(iter(prefixes))',
        ['test_n2b_conflicting_member_prefixes_are_ambiguous_not_a_guess'],
    ),
    (
        'N-2b：前缀按"只去尾段"算（嵌套成员 svc/auth ⇒ 前缀多一层）',
        CU,
        '        prefixes.setdefault(mp[: -(len(rel_n) + 1)], rel_n)',
        '        prefixes.setdefault(mp.rsplit("/", 1)[0], rel_n)',
        ['test_n2b_nested_member_dir_strips_full_reldir'],
    ),
    (
        'N-2b：依赖树目录也产前缀证据（第三方 module 路径推兄弟前缀 ⇒ 歧义 ⇒ 整栈零脚手架）。'
        '★唯一落点★：两条路径（go.work 显式 use / 无 go.work 扫一层）都过这一处过滤——'
        '此前另有一处冗余过滤，两处互相兜底 ⇒ 两条突变都不可证伪，已删掉冗余的那处。',
        CU,
        '                or any(seg in DEPENDENCY_TREE_DIRS for seg in rel_n.split("/"))):',
        '                or False):',
        ['test_n2b_go_work_declared_vendored_member_gives_no_prefix_evidence',
         'test_n2b_vendored_go_mod_is_not_prefix_evidence',
         'test_n2b_dependency_tree_segment_anywhere_in_path_is_rejected'],
    ),
    (
        'N-2b：★误杀面★ 退回读整张 `_SKIP_DIRS`（产物目录里的**本仓自己的**模块被剔 ⇒ '
        '唯一证据没了 ⇒ 前缀推不出 ⇒ 整栈零脚手架）',
        CU,
        '                or any(seg in DEPENDENCY_TREE_DIRS for seg in rel_n.split("/"))):',
        '                or any(seg in __import__("swarm.project.sandbox_spec", fromlist=["x"])._SKIP_DIRS for seg in rel_n.split("/"))):',
        ['test_n2b_product_dir_member_is_still_valid_prefix_evidence'],
    ),
    (
        'N-2b：go.work 的 `use ../外部目录` 被拿去取证（读工程外文件这条边界破了）',
        CU,
        '        if (not rel_n or rel_n.startswith("..")',
        '        if (not rel_n',
        ['test_n2b_go_work_member_outside_project_is_never_read'],
    ),
    (
        'N-2b：拆表时顺手改了 `_SKIP_DIRS` 的作用域（"行为不变"的幌子下扩闸）',
        SBX,
        '    "target", "build", "dist", ".git", ".idea", ".vscode",',
        '    "target", "build", "dist", "out", ".git", ".idea", ".vscode",',
        ['test_skip_dirs_split_is_element_equal_to_the_pre_split_set'],
    ),
    (
        'N-2b：产物目录混进依赖树表（分表的意义消失，误杀面复活）',
        SPEC,
        '    ".tox", ".eggs", "site-packages", ".venv", "venv",',
        '    ".tox", ".eggs", "site-packages", ".venv", "venv", "build", "target",\n    "dist", ".gradle", ".mvn",',
        ['test_skip_dirs_split_is_element_equal_to_the_pre_split_set',
         'test_n2b_product_dir_member_is_still_valid_prefix_evidence'],
    ),
    (
        'N-2b：module 路径与落点无关的成员被当歧义证据（一个怪成员毒死整仓）',
        CU,
        '        if mp == rel_n or not mp.endswith("/" + rel_n):',
        '        if mp == rel_n:',
        ['test_n2b_member_whose_module_path_ignores_its_dir_gives_no_evidence'],
    ),
    (
        'N-2b：`use` 块只捕获首成员（C4 病灶形状复现）',
        CU,
        '        for line in blk.group(1).splitlines():\n            e = _norm(line)\n            if e:\n                out.append(e)',
        '        e = _norm(blk.group(1).splitlines()[0])\n        if e:\n            out.append(e)',
        ['test_n2b_go_work_use_parsing_covers_both_forms'],
    ),
    (
        'N-2b：go 指令不读 go.work（go.work 仓恒落 1.21 ⇒ 低于工作区要求）',
        CU,
        '        for name in ("go.mod", "go.work"):',
        '        for name in ("go.mod",):',
        ['test_n2b_go_directive_reads_go_work_when_no_root_go_mod',
         'test_n2b_go_work_repo_actually_gets_scaffolds_end_to_end'],
    ),
    (
        'N-2b：注入点不用新前缀（原语造对了但没接线 ⇒ 机制不存在）',
        CU,
        '    mod_prefix = _go_module_path_prefix(project_path)',
        '    mod_prefix = _go_root_module_path(project_path)',
        ['test_n2b_go_work_repo_actually_gets_scaffolds_end_to_end',
         'test_go_work_modules_get_manifest_scaffolds'],
    ),
    # ── N-3：规则5 栈驱动化 ──
    (
        'N-3：清单名退回写死 pom.xml（异栈 owner 恒 None）',
        CU,
        '        names = module_manifests_of_stack(stack)\n        if names:\n            return names',
        '        pass',
        ['test_n3_rule5_manifests_are_stack_driven_with_maven_backcompat',
         'test_n3_npm_plan_no_longer_reports_false_unclaimed',
         'test_n3_rule5_acceptance_note_uses_the_real_manifest',
         'test_unclaimed_contract_deps_is_stack_aware'],
    ),
    (
        'N-3：gradle 只取单数字段（`.kts` 别名整列落空，F-1 形态复现）',
        CU,
        '        names = module_manifests_of_stack(stack)',
        '        names = (module_manifests_of_stack(stack) or ("",))[:1]',
        ['test_n3_rule5_manifests_are_stack_driven_with_maven_backcompat'],
    ),
    (
        'N-3：候选路径丢掉物理落点（npm 契约标签恒 miss 真身）',
        CU,
        '    for d in ([base] + ([dirs[mod]] if dirs and mod in dirs else [])):',
        '    for d in [base]:',
        ['test_n3_candidates_cover_both_label_and_physical_dir',
         'test_n3_npm_plan_no_longer_reports_false_unclaimed',
         'test_n3_rule5_acceptance_note_uses_the_real_manifest',
         'test_unclaimed_contract_deps_is_stack_aware'],
    ),
    (
        'N-3：候选路径丢掉标签源（Maven 扁平惯例回归）',
        CU,
        '    for d in ([base] + ([dirs[mod]] if dirs and mod in dirs else [])):',
        '    for d in ([dirs[mod]] if dirs and mod in dirs else [base]):',
        ['test_n3_candidates_cover_both_label_and_physical_dir'],
    ),
    (
        'N-3：owner 表按裸清单名匹配（**根**清单也算模块 owner ⇒ A5 单 owner 判据被污染）。'
        '注：落点是【条件+取名】两行一起换——"排除根清单"由 `/`+切片长度**共同**编码，'
        '只换其中一行会被另一行兜住（那是不可证伪的冗余，不是两道闸）。',
        CU,
        '                suffix = f"/{name}"\n                if ff.endswith(suffix):   # 模块清单（有目录前缀），排除根清单\n                    modname = ff[: -len(suffix)].rsplit("/", 1)[-1]',
        '                suffix = f"/{name}"\n                if ff.endswith(name):\n                    modname = ff[: -len(name)].rsplit("/", 1)[-1] or "ROOT"',
        ['test_n3_manifest_owners_are_stack_aware_and_skip_root_manifest'],
    ),
    (
        'N-3：验收行/告警点名的不是 owner 真写的那条（落点错误再传播一次）',
        CU,
        '                    owner, mod_manifest = st, _hit[0]',
        '                    owner = st',
        ['test_n3_note_names_the_path_the_owner_actually_writes'],
    ),
    (
        'N-3：告警面不再传 stack（plan_validator 退回 pom 口径 ⇒ 假警报复发）',
        PV,
        '    for entry in unclaimed_contract_deps(plan, stack=_r5_stack, dirs=_r5_dirs):',
        '    for entry in unclaimed_contract_deps(plan):',
        ['test_n3_validator_warns_are_stack_aware'],
    ),
    (
        'N-3：unclaimed 被拧成 fail-open（真落空也不报 ⇒ 注入面漏建构建文件）',
        CU,
        '        if owner is None:\n            out.append({"module": mod, "artifacts": arts})',
        '        if owner is None and False:\n            out.append({"module": mod, "artifacts": arts})',
        ['test_n3_truly_unclaimed_module_is_still_reported_under_its_own_stack',
         'test_n3_maven_call_is_byte_identical_to_the_old_hardcoded_behavior',
         'test_unclaimed_contract_deps_is_stack_aware'],
    ),
    (
        'N-3：规则5 验收注入面不再栈驱动（npm owner 一条验收都拿不到）',
        CU,
        '        _r5_manifests = _rule5_manifests(_stk)',
        '        _r5_manifests = ("pom.xml",)',
        ['test_n3_rule5_acceptance_note_uses_the_real_manifest'],
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
