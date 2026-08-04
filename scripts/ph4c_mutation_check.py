#!/usr/bin/env python3
"""P-H4c（gradle 脚手架 driver + gradle_registry）突变 harness（判据与前批同源）：

  · **先验基线全绿**；· **逐条**跑 should_red；· `rc=5` **判失败**；
  · 落点唯一性检查；· 突变后源码必须仍能 `ast.parse`（try/except SyntaxError——
    判 None 是死代码，P-H4b cr R1 #3）；
  · harness 改磁盘源码——**绝不进带超时的循环、绝不与全量并发、跑完看 git status**；
  · 突变写入后与还原后都清被突变模块的 pyc（CPython 整秒粒度 mtime 陈旧坑）。

★锁的命题★ P-H4c：gradle 从「认出来了却直接 no-op」（P-H4 最刺眼一栈）到有确定性
driver。突变压：LOOKUP 门控/属性版本剥离/证据优先级/冲突留痕/Boot BOM 门控/受管
省略版本/显式核验三态（R67L-B3 平移）/内部模块分流与物化/held 分流/标签归一/
方言证据链/分派接线/事实字段。
"""
from __future__ import annotations

import ast
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PY = str(ROOT / ".venv" / "bin" / "python")
TESTS = ["test/test_gradle_registry_ph4c.py", "test/test_b3_stack_spec_single_source.py",
         "test/test_scaffold_npm_go_driver_p2.py"]

GR = ROOT / "brain" / "gradle_registry.py"
CU = ROOT / "brain" / "contract_utils.py"
SP = ROOT / "stacks" / "spec.py"

MUTATIONS = [
    (
        "P-H4c-a：清单证据层 LOOKUP 门控删除（开关契约「关闭后=解析不到」被静默打破）",
        GR,
        "    if not project_path or not _mvn._lookup_enabled():\n        return {}\n"
        "    root = Path(project_path)\n    try:\n        if not root.is_dir():",
        "    if not project_path:\n        return {}\n"
        "    root = Path(project_path)\n    try:\n        if not root.is_dir():",
        ["test_manifest_specs_gated_by_lookup"],
    ),
    (
        "P-H4c-b：`${...}` 属性版本剥离删除（属性接管被当字面版本证据——版本链被短路，"
        "解析出的「版本」是属性名）",
        GR,
        '    v = v.strip()\n    return "" if "$" in v else v',
        '    v = v.strip()\n    return v  # 突变：属性剥离删除',
        ["test_manifest_specs_map_form_and_property_version"],
    ),
    (
        "P-H4c-c：证据优先级倒置（版本目录共享默认先收=盖过直接声明，cr R1 #2 同律）",
        GR,
        "    specs: dict[str, tuple[str, str]] = {}\n\n    def _read(ct: Path) -> str | None:",
        "    specs: dict[str, tuple[str, str]] = {}\n"
        "    specs.update(_catalog_specs(root))  # 突变：共享默认先收=优先级倒置\n\n"
        "    def _read(ct: Path) -> str | None:",
        ["test_manifest_specs_priority_root_member_catalog"],
    ),
    (
        "P-H4c-d：清单声明冲突 WARNING 降 DEBUG（确定性选择≠静默选择——被盖零信号，"
        "hunter R1 F-4 同律）",
        GR,
        '                logger.warning("[gradle-registry] %s 对 %s 的声明 %r 与已收录的 %r 冲突"',
        '                logger.debug("[gradle-registry] %s 对 %s 的声明 %r 与已收录的 %r 冲突"',
        ["test_manifest_specs_priority_root_member_catalog"],
    ),
    (
        "P-H4c-e：Boot BOM 自动导入的 DM 插件门控删除（未声明 dependency-management 也"
        "当在场——gradle 无自动受管=幻觉受管，版本被静默省略）",
        GR,
        "        _, dm_applied = _plugin_decl(text, _DM_PLUGIN)\n        if dm_applied:",
        "        _, dm_applied = _plugin_decl(text, _DM_PLUGIN)\n"
        "        if True:  # 突变：DM 插件门控删除",
        ["test_bom_managed_requires_dm_plugin_for_boot_autoload",
         "test_bom_dm_plugin_requires_declaration_form"],
    ),
    (
        "P-H4c-f：BOM 受管省略版本臂删除（受管坐标被写上猜的版本=对抗受管对齐，"
        "R67L-B3 gradle 形态复活）",
        GR,
        '            kept.append(ResolvedGradleDep(group or _managed[artifact], artifact, None,\n'
        '                                          source="bom_managed"))',
        '            kept.append(ResolvedGradleDep(group or _managed[artifact], artifact,'
        ' "99.0-GUESS",  # 突变：受管也写版本\n'
        '                                          source="bom_managed"))',
        ["test_resolve_bare_bom_managed_omits_version"],
    ),
    (
        "P-H4c-g：显式幻觉校正删除（确证查无直接丢弃而非校正最新稳定版——「能救则救」"
        "被砍成真依赖误杀）",
        GR,
        "                    latest, lsrc = _latest_stable(group, artifact)\n"
        "                    if latest:",
        "                    latest, lsrc = None, \"\"  # 突变：校正臂删除\n"
        "                    if latest:",
        ["test_resolve_explicit_hallucinated_corrects_to_latest"],
    ),
    (
        "P-H4c-h：不可达 fail-open 却标 verified（dep_versions_unverified 账对降级失明——"
        "账在但内容说谎=没记）",
        GR,
        '                    kept.append(ResolvedGradleDep(group, artifact, version,'
        ' source="explicit",\n'
        '                                                  verified="registry_unreachable"))\n'
        '                continue\n            # g:a 无版本 → 落入下方版本链（group 已知）',
        '                    kept.append(ResolvedGradleDep(group, artifact, version,'
        ' source="explicit",\n'
        '                                                  verified="verified"))\n'
        '                continue\n            # g:a 无版本 → 落入下方版本链（group 已知）',
        ["test_resolve_explicit_unreachable_failopen_kept"],
    ),
    (
        "P-H4c-i：内部模块判定删除（内部包送 Central 误解析同名公网坐标——cr#2/hunter#1"
        " 同型）",
        GR,
        "        if not explicit and artifact in internal:\n            seen.add(key)\n"
        "            internal_hit.append(artifact)\n            continue\n",
        "",
        ["test_resolve_internal_never_hits_registry",
         "test_ph4c_gradle_internal_dep_materialized_as_project"],
    ),
    (
        "P-H4c-j：非法坐标闸删除（引号/反斜杠坐标进 Groovy/Kotlin 字符串=模板注入面）",
        GR,
        '        if any(c in spec for c in (\'"\', "\'", "\\\\")):',
        "        if False:  # 突变：非法坐标闸删除",
        ["test_resolve_illegal_coord_chars_dropped"],
    ),
    (
        "P-H4c-k：held pre-split 删除（无物理落点的内部模块送仓库/静默不物化——"
        "hunter R2 H-1 gradle 臂复活）",
        CU,
        "        held = [a for a in arts if a in _labels and a not in dirs]\n"
        "        if held:\n            logger.warning(\n"
        '                "[SCAFFOLD-INJECT] #31-P2f 模块 %s 的 %d 个内部 gradle 依赖无物理落点',
        "        held = []  # 突变：held pre-split 删除\n"
        "        if held:\n            logger.warning(\n"
        '                "[SCAFFOLD-INJECT] #31-P2f 模块 %s 的 %d 个内部 gradle 依赖无物理落点',
        ["test_ph4c_gradle_unresolved_internal_label_held"],
    ),
    (
        "P-H4c-l：内部标签归一删除（标签≠目录名时 project 引用静默丢失——留契约但清单"
        "里没有，gradlew 解析必然炸）",
        CU,
        "        _norm_arts = [name_by_label.get(a, a) for a in arts if a not in held]",
        "        _norm_arts = [a for a in arts if a not in held]  # 突变：归一删除",
        ["test_ph4c_gradle_label_vs_dir_name_internal_still_materialized"],
    ),
    (
        "P-H4c-m：受管无版本形态渲染删除（`g:a` 写成 `g:a:None` 字面量——受管对齐被"
        "字面量污染）",
        CU,
        '    coord = d.raw or (f"{d.group}:{d.artifact}" + (f":{d.version}" if d.version else ""))',
        '    coord = d.raw or (f"{d.group}:{d.artifact}" + (f":{d.version}" if True else ""))'
        "  # 突变：受管省略删除",
        ["test_render_build_gradle_groovy_vs_kts"],
    ),
    (
        "P-H4c-n：gradle driver 从分派表除名（机制全对但接线断——gradle 工程回到零出口，"
        "矩阵格与真调用方双红）",
        CU,
        '                        "cargo": _inject_cargo_scaffolds,\n'
        '                        "gradle": _inject_gradle_scaffolds}',
        '                        "cargo": _inject_cargo_scaffolds}',
        ["test_ph4c_gradle_manifest_spec_reaches_scaffold_via_real_caller",
         "test_scaffold_driver_dispatch_matrix"],
    ),
    (
        "P-H4c-o：内部模块 project 物化删除（内部依赖留契约但清单里没有——gradlew 解析"
        "找不到工程，L1 必然炸）",
        CU,
        "        project_paths = [_gradle_project_path(dir_by_name[im]) for im in internal_mods\n"
        "                         if im in dir_by_name]",
        "        project_paths = []  # 突变：project 物化删除",
        ["test_ph4c_gradle_internal_dep_materialized_as_project",
         "test_ph4c_gradle_label_vs_dir_name_internal_still_materialized"],
    ),
    (
        "P-H4c-p：方言证据链断（根 kts 证据删除——kts 工程被塞 Groovy 清单=双清单体系"
        "混乱）",
        CU,
        '        for name in ("settings.gradle.kts", "build.gradle.kts"):\n'
        "            if (Path(project_path) / name).is_file():\n"
        '                return "kts"\n    return "groovy"',
        '        pass  # 突变：根 kts 证据链删除\n    return "groovy"',
        ["test_gradle_dialect_evidence_chain",
         "test_ph4c_gradle_kts_dialect_reaches_scaffold"],
    ),
    (
        "P-H4c-q：gradle 模块 driver 事实字段翻回 False（模块清单 demote 又刷「无兜底网」"
        "+ 对账锁红——M-3 同型复活）",
        SP,
        "        # 模块 build.gradle(.kts) 有 #31-P2f 脚手架 driver（P-H4c：坐标经 maven_registry\n"
        "        # 原语解析——同坐标同仓库，BOM 受管省略版本）→ 模块清单 demote 安全\n"
        "        has_module_scaffold_driver=True,",
        "        # 模块 build.gradle(.kts) 有 #31-P2f 脚手架 driver（P-H4c：坐标经 maven_registry\n"
        "        # 原语解析——同坐标同仓库，BOM 受管省略版本）→ 模块清单 demote 安全\n"
        "        has_module_scaffold_driver=False,  # 突变",
        ["test_scaffold_driver_facts_match_reality",
         "test_demote_observability_is_tiered_not_one_boolean"],
    ),
    (
        "P-H4c-r：版本目录解析失败 WARNING 降 DEBUG（「解析失败」与「真没有目录」塌成"
        "一个值——硬检查④）",
        GR,
        '        logger.warning("[gradle-registry] %s 解析失败（%s），版本目录证据缺席", ct, exc)',
        '        logger.debug("[gradle-registry] %s 解析失败（%s），版本目录证据缺席", ct, exc)',
        ["test_manifest_specs_malformed_catalog_warns_not_silent"],
    ),
    # ── 以下为对抗双复核 R1 整改的突变（每条对应一条已独立复现的 finding）──
    (
        'P-H4c-s：版本目录 version.ref 解析删除（`version = { ref = ... }` 失去版本'
        "=组证据被当成完整证据，版本链被短路）",
        GR,
        '            elif isinstance(ver, dict) and isinstance(ver.get("ref"), str):\n'
        '                ref_v = vers_tbl.get(ver["ref"])\n'
        '                if isinstance(ref_v, str):\n'
        '                    v = ref_v',
        '            elif isinstance(ver, dict) and isinstance(ver.get("ref"), str):\n'
        '                ref_v = None  # 突变：version.ref 解析删除\n'
        '                if isinstance(ref_v, str):\n'
        '                    v = ref_v',
        ["test_manifest_specs_catalog_forms"],
    ),
    (
        "P-H4c-t：apply false 闸删除（未应用的 Boot 插件被当真应用=幻觉受管，版本被"
        "静默省略——reviewer R1 #4 复活）",
        GR,
        "            decls.append((m.group(1), not bool(m.group(2))))",
        "            decls.append((m.group(1), True))  # 突变：apply false 闸删除",
        ["test_bom_boot_apply_false_not_managed"],
    ),
    (
        "P-H4c-u：方言 plan 证据臂删除（根 Groovy 盖过 plan 明确要建的 .kts=生成与 plan"
        "期望不符的文件，reviewer R1 #6 复活）",
        CU,
        '    if plan_files:\n        rel = mdir.strip("/") + "/"\n'
        '        if rel + "build.gradle.kts" in plan_files:\n            return "kts"\n'
        '        if rel + "build.gradle" in plan_files:\n            return "groovy"',
        '    if False:  # 突变：plan 证据臂删除\n        rel = mdir.strip("/") + "/"\n'
        '        if rel + "build.gradle.kts" in plan_files:\n            return "kts"\n'
        '        if rel + "build.gradle" in plan_files:\n            return "groovy"',
        ["test_gradle_dialect_follows_plan_create_files"],
    ),
    (
        "P-H4c-v：词边界删除（自定义配置 `someapi` 尾部被当 `api` 配置=假证据进层，"
        "reviewer R1 CR-1 复活）",
        GR,
        '_DEP_LINE_RE = re.compile(_BOUND + _CONFIGS + r"""\\s*\\(?\\s*["\']""" + _COORD)',
        '_DEP_LINE_RE = re.compile(_CONFIGS + r"""\\s*\\(?\\s*["\']""" + _COORD)'
        "  # 突变：词边界删除",
        ["test_manifest_specs_custom_config_word_boundary"],
    ),
    (
        "P-H4c-w：注释剥离删除（注释里的假声明进证据层——先见先收留注释版旧坐标、"
        "给真实声明打假冲突 WARNING，reviewer R1 CR-1 复活）",
        GR,
        "    text = _strip_comments(text)\n    pairs = [(m.group(1), m.group(2),"
        " _clean_version(m.group(3)))",
        "    text = text  # 突变：注释剥离删除\n    pairs = [(m.group(1), m.group(2),"
        " _clean_version(m.group(3)))",
        ["test_manifest_specs_comments_not_evidence"],
    ),
    (
        "P-H4c-x：repositories 块删除（gradle 零默认仓库——greenfield 缺它=自败脚手架，"
        "验收的 gradlew dependencies 必炸，reviewer R1 #3 复活）",
        CU,
        '    lines += ["", "repositories {", "    mavenCentral()", "}"]',
        '    lines += []  # 突变：repositories 块删除',
        ["test_render_includes_repositories_block"],
    ),
    (
        "P-H4c-y：settings include 嵌套成员通道删除（`services/api/build.gradle` 证据"
        "整层缺席=退回一级扫描盲区，reviewer R1 #5 复活）",
        GR,
        "    for d in sorted(_settings_member_dirs(root)):",
        "    for d in sorted([]):  # 突变：include 通道删除",
        ["test_manifest_specs_nested_member_via_settings_include"],
    ),
    (
        "P-H4c-z：unverified 账 raw 兜底删除（classifier 超集坐标被记成 `?@?`——账在"
        "却认不出是谁，hunter R1 HIGH 复活）",
        CU,
        "                 # gradle raw（classifier 超集等不判形态）：group/artifact 为空，\n"
        "                 # 没这一档就记成 `?@?`——账在、却认不出是哪个依赖＝这笔账没用\n"
        "                 # （hunter R1 HIGH，与 go 侧 `?@?` 同型）\n"
        "                 or getattr(k, \"raw\", None)\n",
        "                 # gradle raw（classifier 超集等不判形态）：group/artifact 为空，\n"
        "                 # 没这一档就记成 `?@?`——账在、却认不出是哪个依赖＝这笔账没用\n"
        "                 # （hunter R1 HIGH，与 go 侧 `?@?` 同型）\n",
        ["test_record_unverified_ledger_consumes_raw_coord"],
    ),
    # ── 以下为对抗双复核 R2 整改的突变（每条对应一条已独立复现的 finding）──
    (
        "P-H4c-aa：Kotlin apply(false) 形态删除（`apply(false)`/`.apply(false)` 被当真"
        "应用=幻觉受管，hunter R2 HIGH 复活）",
        GR,
        '((?:(?:\\s*\\.\\s*)|(?:[ \\t]+))apply[ \\t]*\\(?[ \\t]*false[ \\t]*\\)?)?',
        '([ \\t]+apply[ \\t]+false)?',
        ["test_bom_boot_kotlin_dsl_forms"],
    ),
    (
        "P-H4c-ab：plugins 块限定删除（全文搜索=字符串字面量里的类插件声明被当真声明，"
        "reviewer R2 #3 / hunter R2 MEDIUM 复活）",
        GR,
        "    for body in _plugins_block_bodies(text):",
        "    for body in (text,):  # 突变：plugins 块限定删除",
        ["test_bom_boot_string_literal_not_a_decl"],
    ),
    (
        "P-H4c-ac：插件 id 词边界删除（`myid` 尾部 id 子串当插件声明=假在场，"
        "reviewer R2 HIGH-1 复活）",
        GR,
        '_PLUGIN_DECL_TMPL = (_BOUND + r"""id[ \\t]*\\(?[ \\t]*["\']{pid}["\'](?:[ \\t]*\\))?"""',
        '_PLUGIN_DECL_TMPL = (r"""id[ \\t]*\\(?[ \\t]*["\']{pid}["\'](?:[ \\t]*\\))?"""'
        "  # 突变：id 词边界删除",
        ["test_bom_boot_plugin_decl_word_boundary"],
    ),
    (
        "P-H4c-ad：已应用优先聚合删除（首匹配 apply false 盖真应用行=幻觉不受管，"
        "reviewer R2 #4 复活）",
        GR,
        "    applied_versions = [v for v, ok in decls if ok]",
        "    applied_versions = []  # 突变：已应用优先删除",
        ["test_bom_boot_applied_decl_wins_over_apply_false"],
    ),
    (
        "P-H4c-ae：include 前导冒号可选删除（`include 'services:api'` 无冒号写法整层"
        "失踪，reviewer R2 #5 复活）",
        GR,
        '_INCLUDE_PATH_RE = re.compile(r"""["\'](:?[\\w:.\\-]+)["\']""")',
        '_INCLUDE_PATH_RE = re.compile(r"""["\'](:[\\w:.\\-]+)["\']""")'
        "  # 突变：前导冒号必选",
        ["test_manifest_specs_include_without_leading_colon"],
    ),
    (
        "P-H4c-af：方言 plan 证据 writable 臂删除（清单只声明在 writable 时方言被根"
        "Groovy 盖过，reviewer R2 #6 复活）",
        CU,
        'for key in ("create_files", "writable")',
        'for key in ("create_files",)  # 突变：writable 臂删除',
        ["test_ph4c_gradle_dialect_plan_writable_channel"],
    ),
    (
        "P-H4c-ag：版本连接器横向空白限定删除（非点号跨行=块内下行 version 错挂到"
        "插件头上）",
        GR,
        '(?:(?:\\s*\\.\\s*)|(?:[ \\t]+))version',
        '(?:\\s*\\.?\\s*)version',
        ["test_plugin_decl_no_cross_line_version_attach"],
    ),
    (
        "P-H4c-ah：plugins 块头换行容忍删除（`plugins\\n{` 合法风格整块丢证据=BOM 受管"
        "判定全失效，reviewer R3 MEDIUM 复活）",
        GR,
        '_PLUGINS_HEAD_RE = re.compile(_BOUND + r"plugins\\s*\\{")',
        '_PLUGINS_HEAD_RE = re.compile(_BOUND + r"plugins[ \\t]*\\{")'
        "  # 突变：换行容忍删除",
        ["test_plugins_block_head_accepts_newline"],
    ),
    (
        "P-H4c-ai：跨行点号链删除（Kotlin 链在点前换行合法——`.apply(false)` 漏检="
        "幻觉受管误杀方向，hunter R3 HIGH 复活）",
        GR,
        '(?:(?:\\s*\\.\\s*)|(?:[ \\t]+))apply',
        '(?:[ \\t]+\\.?[ \\t]*)apply',
        ["test_bom_boot_kotlin_chain_multiline"],
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
