#!/usr/bin/env python3
"""P-H4b（cargo 脚手架 driver + cargo_registry）突变 harness（判据与前批同源）：

  · **先验基线全绿**；· **逐条**跑 should_red；· `rc=5` **判失败**；
  · 落点唯一性检查；· 突变后源码必须仍能 `ast.parse`；
  · harness 改磁盘源码——**绝不进带超时的循环、绝不与全量并发、跑完看 git status**；
  · 突变写入后与还原后都清被突变模块的 pyc（CPython 整秒粒度 mtime 陈旧坑）。

★锁的命题★ P-H4b：cargo 工程从「认出来了却零脚手架出口」（矩阵格期望 None 如实记录）
到有确定性 driver。突变压：证据层门控/yanked 剔除/cargo semver 语义（bare=caret 与 npm
正面冲突）/显式版本核验三态（P-C2 平移）/workspace=true 反解/Cargo.lock 证据层/
内部 crate path 物化与 held 分流/edition 不猜/分派接线。
"""
from __future__ import annotations

import ast
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PY = str(ROOT / ".venv" / "bin" / "python")
TESTS = ["test/test_cargo_registry_ph4b.py", "test/test_b3_stack_spec_single_source.py",
         "test/test_scaffold_npm_go_driver_p2.py", "test/test_b0_workspace_fixture_matrix.py"]

CR = ROOT / "brain" / "cargo_registry.py"
CU = ROOT / "brain" / "contract_utils.py"
SP = ROOT / "stacks" / "spec.py"

MUTATIONS = [
    (
        "P-H4b-a：清单证据层 LOOKUP 门控删除（开关契约「关闭后=解析不到」被静默打破）",
        CR,
        "    if not project_path or not _lookup_enabled():\n        return {}\n    root = Path(project_path)\n"
        "    try:\n        if not root.is_dir():",
        "    if not project_path:\n        return {}\n    root = Path(project_path)\n"
        "    try:\n        if not root.is_dir():",
        ["test_manifest_specs_gated_by_lookup"],
    ),
    (
        "P-H4b-b：yanked 剔除删除（yanked 版本被判「可用」——cargo 对新需求拒选 yanked，"
        "留下=推荐一个装不上的版本）",
        CR,
        '        v["num"] for v in vers\n'
        '        if isinstance(v, dict) and isinstance(v.get("num"), str) and not v.get("yanked"))',
        '        v["num"] for v in vers\n'
        '        if isinstance(v, dict) and isinstance(v.get("num"), str))',
        ["test_registry_versions_excludes_yanked"],
    ),
    (
        "P-H4b-c：caret 上界错（`1.2.3` 只放 1.2.x——bare=caret 兼容语义塌成 tilde，"
        "真兼容版被冤杀成不可满足）",
        CR,
        "    if major > 0:\n        return lower, (major + 1, 0, 0)",
        "    if major > 0:\n        return lower, (major, minor + 1, 0)  # 突变：caret 塌成 tilde",
        ["test_range_is_satisfiable_cargo_semantics"],
    ),
    (
        "P-H4b-d：0.x 特例删除（`^0.2.3` 上界 <1.0.0——semver 0.x 最左非零段在 minor，"
        "`0.3.0` 被误判兼容=放过破坏性升级）",
        CR,
        "    if declared >= 2 and minor > 0:\n        return lower, (0, minor + 1, 0)\n"
        "    if declared >= 3:",
        "    if declared >= 3:",
        ["test_range_is_satisfiable_cargo_semantics"],
    ),
    (
        "P-H4b-e：预发布同核心闸删除（`>=1.0` 放进 `1.5.0-rc.1`——项目未显式允许预发布"
        "却被塞进不稳定版）",
        CR,
        "        if vpre and nums not in pre_cores:\n            continue",
        "        if False:  # 突变：预发布闸删除\n            continue",
        ["test_range_is_satisfiable_cargo_semantics"],
    ),
    (
        "P-H4b-f：通配 `1.2.*` 只比 major（`1.3.0` 被判匹配——通配位语义错=放过越界版本）",
        CR,
        "        if declared == 2:\n            return (major, minor) == vnum[:2]\n"
        "        if declared == 1:",
        "        if declared == 2:\n            return major == vnum[0]  # 突变：只比 major\n"
        "        if declared == 1:",
        ["test_range_is_satisfiable_cargo_semantics"],
    ),
    (
        "P-H4b-g：显式不可满足校正删除（幻觉区间直接丢弃而非校正最新稳定版——npm 臂同律"
        "的「能救则救」被砍成真依赖误杀）",
        CR,
        "            latest = registry_latest_version(name)\n            if latest:\n"
        "                logger.warning(\"[cargo-registry] 契约显式依赖 %s@%s 无任何可满足版本\"",
        "            latest = None  # 突变：校正臂删除\n            if latest:\n"
        "                logger.warning(\"[cargo-registry] 契约显式依赖 %s@%s 无任何可满足版本\"",
        ["test_resolve_explicit_unsatisfiable_corrects_to_latest",
         "test_ph4b_cargo_explicit_hallucinated_version_never_in_template"],
    ),
    (
        "P-H4b-h：不可达 fail-open 却标 verified（dep_versions_unverified 账对降级失明——"
        "账在但内容说谎=没记）",
        CR,
        '                kept.append(ResolvedCargoDep(name=name, spec=req, source="explicit",\n'
        '                                             verified="registry_unreachable", features=feats))\n'
        '                continue\n            if _range_is_satisfiable(req, versions):',
        '                kept.append(ResolvedCargoDep(name=name, spec=req, source="explicit",\n'
        '                                             verified="verified", features=feats))\n'
        '                continue\n            if _range_is_satisfiable(req, versions):',
        ["test_resolve_explicit_unreachable_failopen_kept"],
    ),
    (
        "P-H4b-i：`*` 不可复现臂删除（wildcard_any 落「不判」原样保留——不可复现声明"
        "烤进权威模板）",
        CR,
        '    if r == "*":\n        return "wildcard_any"',
        '    if r == "*":\n        return "complex"  # 突变：不可复现臂删除',
        ["test_resolve_wildcard_star_resolves_concrete",
         "test_range_kind_classification"],
    ),
    (
        "P-H4b-j：成员清单读取删除（成员 Cargo.toml 的普通声明整层蒸发——版本证据只剩根档）",
        CR,
        '            _collect(sub_data.get("dependencies"), origin=f"{e.name}/Cargo.toml")',
        '            _collect({}, origin=f"{e.name}/Cargo.toml")  # 突变：成员清单读取删除',
        ["test_manifest_specs_member_plain_deps_read",
         "test_manifest_specs_member_declaration_beats_workspace_default",
         "test_manifest_specs_conflict_warns_on_differing_declaration"],
    ),
    (
        "P-H4b-k：Cargo.lock 证据层删除（「曾经真装上过的版本」零网络臂消失——registry "
        "抖动时本可由 lock 答的依赖被如实丢弃）",
        CR,
        "        if _lock_vers is None:\n            _lock_vers = cargo_lock_versions(project_path)\n"
        "        locked = _lock_vers.get(name)\n        if locked:",
        "        if _lock_vers is None:\n            _lock_vers = {}  # 突变：lock 层删除\n"
        "        locked = _lock_vers.get(name)\n        if locked:",
        ["test_resolve_bare_lock_then_registry"],
    ),
    (
        "P-H4b-l：内部 crate 判定删除（内部包送 crates.io 误解析同名公网 crate——"
        "cr#2/hunter#1 同型）",
        CR,
        "        if name in internal:\n            seen.add(name)\n            internal_hit.append(name)\n"
        "            continue\n",
        "",
        ["test_resolve_internal_never_hits_registry",
         "test_ph4b_cargo_internal_dep_materialized_as_path"],
    ),
    (
        "P-H4b-m：模板渲染静默丢 features（`tokio[full]` 写成无 features——换语义不是"
        "换写法，extras 同律；R1 后实现收编进 _cargo_dep_line 单一事实源）",
        CU,
        '    if k.features:\n'
        '        feats = ", ".join(f\'"{_toml_escape(f)}"\' for f in k.features)\n'
        '        parts.append(f"features = [{feats}]")',
        '    if False:  # 突变：features 臂删除\n'
        '        feats = ", ".join(f\'"{_toml_escape(f)}"\' for f in k.features)\n'
        '        parts.append(f"features = [{feats}]")',
        ["test_render_cargo_toml_escapes_and_roundtrips",
         "test_render_cargo_toml_default_features_false_roundtrips",
         "test_ph4b_cargo_manifest_spec_reaches_scaffold_via_real_caller"],
    ),
    (
        "P-H4b-n：cargo driver 从分派表除名（机制全对但接线断——cargo 工程回到零出口，"
        "矩阵格与真调用方双红）",
        CU,
        # ★#29-3 T-1：落点已死，同「表长大」族★ 原落点写的是 `"cargo": …}`（cargo 是**末项**、
        # 自带右花括号）。`_P2_SCAFFOLD_DRIVERS` 后来加了 `gradle` ⇒ cargo 不再是末项、右括号
        # 移走 ⇒ 该突变自那次扩表起落点未命中＝零覆盖（与 xm 的 `DRIVERS` 死锁同一形状）。
        # 改为只摘 cargo **自己那一行**（逗号形），表继续长也不会再漂。
        '                        "cargo": _inject_cargo_scaffolds,\n',
        '',
        ["test_ph4b_cargo_manifest_spec_reaches_scaffold_via_real_caller",
         "test_scaffold_driver_dispatch_matrix"],
    ),
    (
        "P-H4b-o：held pre-split 删除（无物理落点的内部模块送 crates.io/臆造 path——"
        "hunter R2 H-1 cargo 臂复活）",
        CU,
        # ★#29-3 T-1：落点原为非唯一（出现 2 次）⇒ `replace(...,1)` 只改第一处 ⇒ 突变语义
        # 不等价，该锁实际从未被验证过。两处分别是 **cargo 臂**(:3976) 与 **gradle 臂**(:4174)，
        # 代码逐字相同。本条锁的是 cargo（测试名 …cargo_unresolved_internal_label…），故向上
        # 扩一行带 `crates.io` 的注释来唯一定位——扩上下文比改代码去迁就 harness 更安全。
        "        # 先扣下不送 resolve（否则被当第三方送 crates.io）。\n"
        "        held = [a for a in arts if a in _labels and a not in dirs]",
        "        # 先扣下不送 resolve（否则被当第三方送 crates.io）。\n"
        "        held = []  # 突变：held pre-split 删除",
        ["test_ph4b_cargo_unresolved_internal_label_held_from_registry"],
    ),
    (
        "P-H4b-p：内部标签归一删除（裸标签送 resolve——磁盘名≠标签时 path 物化丢失、"
        "含空格标签被判死成 dropped=真内部依赖当幻觉丢）",
        CU,
        "        _norm_arts = [crate_by_label.get(a, a) for a in arts if a not in held]",
        "        _norm_arts = [a for a in arts if a not in held]  # 突变：归一删除",
        ["test_ph4b_cargo_label_vs_disk_name_internal_stays_internal"],
    ),
    (
        "P-H4b-q：cargo 模块 driver 事实字段翻回 False（模块清单 demote 又刷「无兜底网」"
        "+ 对账锁红——M-3 同型复活）",
        SP,
        "        # 物化 path 相对引用）→ 模块 Cargo.toml demote 安全（P-H4b）。\n"
        "        has_module_scaffold_driver=True,",
        "        # 物化 path 相对引用）→ 模块 Cargo.toml demote 安全（P-H4b）。\n"
        "        has_module_scaffold_driver=False,  # 突变",
        ["test_scaffold_driver_facts_match_reality",
         "test_demote_observability_is_tiered_not_one_boolean"],
    ),
    (
        "P-H4b-r：内部依赖 path 物化删除（内部 crate 留契约但清单里没有——cargo 构建"
        "找不到 crate，L1 必然炸）",
        CU,
        "        path_deps = [(ic, _go_relpath(mdir, dir_by_label[norm_to_label[ic]]))\n"
        "                     for ic in internal_crates\n"
        "                     if ic in norm_to_label and norm_to_label[ic] in dir_by_label]",
        "        path_deps = []  # 突变：path 物化删除",
        ["test_ph4b_cargo_internal_dep_materialized_as_path"],
    ),
    (
        "P-H4b-s：edition 缺席时猜一个默认（血规 2：工具链版本只能来自磁盘真值——猜的 "
        "edition 写进权威清单）",
        CU,
        '                       "缺席（模板省略该字段，血规 2 不猜）", ct, exc)\n    return ""',
        '                       "缺席（模板省略该字段，血规 2 不猜）", ct, exc)\n'
        '    return "2021"  # 突变：猜默认',
        ["test_ph4b_cargo_manifest_spec_reaches_scaffold_via_real_caller"],
    ),
    (
        "P-H4b-t：根 workspace.dependencies 证据通道删除（workspace 继承的唯一证据通道"
        "兼继承默认兜底——删掉后 workspace 工程版本证据整层缺席）",
        CR,
        '    _collect(root_ws, origin="根[workspace.dependencies]")   # 继承默认兜底，最后收',
        '    _collect({}, origin="根[workspace.dependencies]")   # 继承默认兜底，最后收',
        ["test_manifest_specs_workspace_inheritance_via_root_channel",
         "test_manifest_specs_member_plain_deps_read",
         "test_ph4b_cargo_manifest_spec_reaches_scaffold_via_real_caller"],
    ),
    (
        "P-H4b-u：清单解析失败 WARNING 降 DEBUG（「解析失败」与「真没有声明」塌成一个"
        "值——硬检查④）",
        CR,
        '            logger.warning("[cargo-registry] %s 解析失败（%s），该清单声明证据缺席", ct, exc)',
        '            logger.debug("[cargo-registry] %s 解析失败（%s），该清单声明证据缺席", ct, exc)',
        ["test_manifest_specs_malformed_toml_warns_not_silent"],
    ),
    # ── 以下为对抗双复核 R1 整改的突变（每条对应一条已独立复现的 finding）──
    (
        "P-H4b-v：lock 多版本取先见者（最高稳定版选择删除——把 lock 文件顺序巧合当语义，"
        "hunter R1 F-1 复活）",
        CR,
        "        best = max(stable, key=lambda t: t[0])",
        "        best = stable[0]  # 突变：取先见者=顺序巧合",
        ["test_cargo_lock_multiversion_picks_highest_stable"],
    ),
    (
        "P-H4b-w：default-features=false 渲染臂删除（静默重新打开默认特性——编译产物/"
        "传递依赖全变=换语义，hunter R1 F-2 复活）",
        CU,
        '    if not k.default_features:\n        parts.append("default-features = false")',
        '    if False:  # 突变：default-features 臂删除\n'
        '        parts.append("default-features = false")',
        ["test_render_cargo_toml_default_features_false_roundtrips"],
    ),
    (
        "P-H4b-x：预发布段不参与比较（_ver_cmp 退化为纯数字三元组——`>1.5.0-alpha` 对 "
        "`1.5.0-beta` 判不出，两个方向都错，cr R1 #1 复活）",
        CR,
        "    if an != bn:\n        return -1 if an < bn else 1\n",
        "    if an != bn:\n        return -1 if an < bn else 1\n"
        "    return 0  # 突变：预发布段不参与比较\n",
        ["test_range_is_satisfiable_cargo_semantics"],
    ),
    (
        "P-H4b-y：证据优先级倒置（workspace 继承默认先收=盖过成员真实声明，cr R1 #2 复活）",
        CR,
        '    if isinstance(root_data, dict):\n'
        '        _collect(root_data.get("dependencies"), origin="根[dependencies]")',
        '    _collect(root_ws, origin="根[workspace.dependencies]")  # 突变：继承默认先收\n'
        '    if isinstance(root_data, dict):\n'
        '        _collect(root_data.get("dependencies"), origin="根[dependencies]")',
        ["test_manifest_specs_member_declaration_beats_workspace_default"],
    ),
    (
        "P-H4b-z：crate 名降级 WARNING 降 DEBUG（清单解析失败与没有 name 不可辨——path "
        "依赖名与磁盘真名错位零信号，hunter R1 F-3 复活）",
        CU,
        '            logger.warning("[SCAFFOLD-INJECT] #31-P2e %s 读取/解析失败（%s）→ crate 名"',
        '            logger.debug("[SCAFFOLD-INJECT] #31-P2e %s 读取/解析失败（%s）→ crate 名"',
        ["test_cargo_crate_name_warns_on_malformed_manifest"],
    ),
    (
        "P-H4b-aa：清单声明冲突 WARNING 降 DEBUG（确定性选择≠静默选择——被盖零信号="
        "降级无痕，hunter R1 F-4 复活）",
        CR,
        '                    logger.warning("[cargo-registry] %s 对 %s 的声明 %r 与已收录的 %r 冲突"',
        '                    logger.debug("[cargo-registry] %s 对 %s 的声明 %r 与已收录的 %r 冲突"',
        ["test_manifest_specs_conflict_warns_on_differing_declaration"],
    ),
]


def _pytest(args: list[str]) -> int:
    p = subprocess.run([PY, "-m", "pytest", *TESTS, "-p", "no:warnings", "-q",
                        "--tb=no", *args], cwd=ROOT, capture_output=True, text=True)
    return p.returncode


def _clear_pyc(path: Path) -> None:
    """删被突变模块的 pyc——CPython 的 pyc 失效判据是【整秒】粒度 mtime：相邻两条突变
    落在同一秒时，第二条突变写完 pyc 仍被判有效 ⇒ 子进程跑的是【上一条】的代码。
    每条突变前与还原后都必须清。"""
    cache = path.parent / "__pycache__"
    if cache.is_dir():
        for f in cache.glob(path.stem + ".*.pyc"):
            try:
                f.unlink()
            except OSError:
                pass


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
        path.write_text(src.replace(old, new, 1))
        _clear_pyc(path)
        try:
            # ★cr R1 #3★ ast.parse 失败是【抛 SyntaxError】而非返回 None——判空是死代码
            try:
                ast.parse(path.read_text())
            except SyntaxError:
                print(f"[{i}/{len(MUTATIONS)}] {name}\n    ✗ 突变后 ast.parse 失败")
                failures.append((name, "突变后不可解析"))
                continue
            per = [(n, _pytest(["-k", n])) for n in should_red]
            missing = [n for n, r in per if r == 5]
            green = [n for n, r in per if r == 0]
            print(f"[{i}/{len(MUTATIONS)}] {name}")
            if not missing and not green:
                print(f"    ✓ 指名的 {len(per)} 条全红")
            else:
                if missing:
                    print(f"    ✗ 测试名选不到（rc=5，重命名/typo）: {missing}")
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
    if failures:
        print(f"\n✗ {len(failures)} 条未达标：")
        for n, why in failures:
            print(f"  · [{why}] {n}")
        return 1
    print(f"\n✓ 全部 {len(MUTATIONS)} 条突变都被锁住，且基线前后皆绿")
    return 0


if __name__ == "__main__":
    sys.exit(main())
