#!/usr/bin/env python3
"""P-H4a（python 脚手架 driver + pypi_registry）突变 harness（判据与前批同源）：

  · **先验基线全绿**；· **逐条**跑 should_red；· `rc=5` **判失败**；
  · 落点唯一性检查；· 突变后源码必须仍能 `ast.parse`；
  · harness 改磁盘源码——**绝不进带超时的循环、绝不与全量并发、跑完看 git status**。

★锁的命题★ P-H4a：pyproject 工程从「零脚手架出口」（injected=[] → 派 worker 手写清单
臆造版本，R47/R53 病）到有确定性 driver。突变压：证据层门控/排序/解析形状、显式版本
核验三态（P-C2 平移）、extras 语义、内部模块分流与不物化、requires-python 不猜、
WARNING 可辨（硬检查④）、分派接线。
"""
from __future__ import annotations

import ast
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PY = str(ROOT / ".venv" / "bin" / "python")
TESTS = ["test/test_pypi_registry_ph4.py", "test/test_i6_decouple_subtasks.py",
         "test/test_b3_stack_spec_single_source.py"]

PR = ROOT / "brain" / "pypi_registry.py"
CU = ROOT / "brain" / "contract_utils.py"
SP = ROOT / "stacks" / "spec.py"
# ★#29-3 T-1★ go 脚手架叶簇已从 contract_utils.py 拆到 brain/go_scaffold.py（纪律#9），
# contract_utils 只留顶层 re-export。落点必须跟着**定义模块**走 —— 打 re-export 那个地址
# 的突变会静默零覆盖（本仓已登记「拆函数迁模块后落点随簇漂移」这一类）。
GS = ROOT / "brain" / "go_scaffold.py"

MUTATIONS = [
    (
        "P-H4a-a：清单证据层 LOOKUP 门控删除（开关契约「关闭后=解析不到」被静默打破）",
        PR,
        "    if not project_path or not _lookup_enabled():\n        return {}",
        "    if not project_path:\n        return {}",
        ["test_manifest_specs_gated_by_lookup"],
    ),
    (
        "P-H4a-b：清单解析 tuple 装错字段（extras 装进 specifier 位——声明约束全丢成裸名）",
        PR,
        "                        parsed = _parse_dep_text(d)\n"
        "                        if parsed and parsed[0] not in specs:\n"
        "                            specs[parsed[0]] = (parsed[1], parsed[2])",
        "                        parsed = _parse_dep_text(d)\n"
        "                        if parsed and parsed[0] not in specs:\n"
        "                            specs[parsed[0]] = (parsed[1], parsed[1])",
        ["test_manifest_specs_pyproject_and_requirements",
         "test_manifest_specs_root_wins_over_subdir"],
    ),
    (
        "P-H4a-c：根清单优先序删除（根从不读——根的权威声明被一级子目录盖掉）",
        PR,
        "    _read_manifest(root)\n    try:",
        "    try:",
        ["test_manifest_specs_root_wins_over_subdir"],
    ),
    (
        "P-H4a-d：显式钉版存在性核验删除（幻觉 `==99.0` 无条件烤进权威模板=P-C2 原病复发）",
        PR,
        "                if exists:",
        "                if True:  # 突变：存在性核验删除",
        ["test_resolve_explicit_pin_hallucinated_dropped"],
    ),
    (
        "P-H4a-e：钉版核验收窄到稳定版集（`==1.1.0b1` 真预发布被误杀——P-C2「版本集含"
        "预发布」同律）",
        PR,
        "    return frozenset(rel) if isinstance(rel, dict) else frozenset()",
        "    return frozenset(_stable_versions(data)) if isinstance(rel, dict) else frozenset()",
        ["test_resolve_explicit_pin_prerelease_not_killed",
         "test_registry_versions_includes_prerelease"],
    ),
    (
        "P-H4a-f：区间臂不可达 fail-open 却标 verified（dep_versions_unverified 账对降级"
        "失明——账在但内容说谎=没记）",
        PR,
        "                kept.append(ResolvedPyDep(name=name, spec=spec, source=\"explicit\",\n"
        "                                          verified=\"registry_unreachable\", extras=extras))\n"
        "                continue\n            # ★pip 语义（cr R-5）★",
        "                kept.append(ResolvedPyDep(name=name, spec=spec, source=\"explicit\",\n"
        "                                          verified=\"verified\", extras=extras))\n"
        "                continue\n            # ★pip 语义（cr R-5）★",
        ["test_resolve_explicit_range_unreachable_failopen_kept"],
    ),
    (
        "P-H4a-f2：钉版臂不可达 fail-open 却标 verified（与 f 是两条分支——首轮 harness"
        "实测区间臂突变压不到钉版臂的锁，两臂必须各有突变）",
        PR,
        "                if versions is None:\n"
        "                    seen.add(name)   # 不可达 → fail-open 保留（证据缺失≠否定证据）\n"
        "                    kept.append(ResolvedPyDep(name=name, spec=spec, source=\"explicit\",\n"
        "                                              verified=\"registry_unreachable\", extras=extras))",
        "                if versions is None:\n"
        "                    seen.add(name)   # 不可达 → fail-open 保留（证据缺失≠否定证据）\n"
        "                    kept.append(ResolvedPyDep(name=name, spec=spec, source=\"explicit\",\n"
        "                                              verified=\"verified\", extras=extras))",
        ["test_resolve_explicit_unreachable_failopen_kept"],
    ),
    (
        "P-H4a-g：内部模块分流删除（内部包送 PyPI 误解析同名公网包——cr#2/hunter#1 同型）",
        PR,
        "        if name in internal:\n            seen.add(name)\n"
        "            internal_hit.append(name)\n            continue\n",
        "",
        ["test_resolve_internal_never_hits_registry"],
    ),
    (
        "P-H4a-h：模板渲染静默丢 extras（`uvicorn[standard]` 写成 `uvicorn`——换语义）",
        CU,
        "        deps = \"\\n\".join(f'    \"{_toml_escape(f\"{k.name}{k.extras}{k.spec}\")}\",' for k in kept)\n"
        "        lines.append(f\"dependencies = [\\n{deps}\\n]\")",
        "        deps = \"\\n\".join(f'    \"{_toml_escape(f\"{k.name}{k.spec}\")}\",' for k in kept)\n"
        "        lines.append(f\"dependencies = [\\n{deps}\\n]\")",
        ["test_ph4_python_manifest_spec_reaches_scaffold_via_real_caller"],
    ),
    (
        # ★#29-3 T-1：落点已死，同「表长大」族★ 原落点写的是 `"python": …}`（python 当时是
        # **末项**、自带右花括号）。`_P2_SCAFFOLD_DRIVERS` 后来加了 cargo、gradle ⇒ python 不再
        # 是末项、右括号移走 ⇒ 该突变自那次扩表起零覆盖（与 ph4b #14 / xm 的 `DRIVERS` 同形）。
        # 改为只摘 python **自己那一行**（逗号形），表继续长也不会再漂。
        "P-H4a-i：python driver 从分派表除名（机制全对但接线断——pyproject 工程回到零出口）",
        CU,
        '                        "python": _inject_python_scaffolds,\n',
        '',
        ["test_ph4_python_manifest_spec_reaches_scaffold_via_real_caller"],
    ),
    (
        "P-H4a-j：内部依赖不物化 WARNING 降 INFO（「不物化」与「没有内部依赖」不可辨——"
        "硬检查④）",
        CU,
        "            logger.warning(\n"
        "                \"[SCAFFOLD-INJECT] #31-P2d 模块 %s 的 %d 个内部 python 依赖不物化进 pyproject\"",
        "            logger.info(\n"
        "                \"[SCAFFOLD-INJECT] #31-P2d 模块 %s 的 %d 个内部 python 依赖不物化进 pyproject\"",
        ["test_ph4_python_internal_dep_not_materialized_but_kept_in_contract"],
    ),
    (
        "P-H4a-k：requires-python 缺席时猜一个默认（血规 2：版本下界只能来自磁盘真值——"
        "猜的下界会拦死合法的旧环境）",
        CU,
        # ★#29-3 T-1 落点更新★ 原落点含 `except (OSError, ValueError):\n        pass` —— 那个
        # 裸 `pass` 后来被**刻意改好**了（hunter R1 F-3 硬检查④：根清单存在却读不出必须有信号
        # ⇒ 改成 `as exc` + 一条 WARNING）。旧字面量自那次整改起落点未命中＝零覆盖。
        # 突变**意图不变**（缺席时猜一个默认 vs 如实返空），故重新对着当前的 `return ""` 写，
        # 并带上前面那条 WARNING 作唯一上下文（`return ""` 单独出现多处）。
        '        logger.warning("[SCAFFOLD-INJECT] #31-P2d 根 %s 读取/解析失败（%s）→ "\n'
        '                       "requires-python 证据缺席（模板省略该字段，血规 2 不猜）", pj, exc)\n'
        '    return ""\n',
        '        logger.warning("[SCAFFOLD-INJECT] #31-P2d 根 %s 读取/解析失败（%s）→ "\n'
        '                       "requires-python 证据缺席（模板省略该字段，血规 2 不猜）", pj, exc)\n'
        '    return ">=3.9"  # 突变：猜默认\n',
        ["test_ph4_python_manifest_spec_reaches_scaffold_via_real_caller"],
    ),
    (
        "P-H4a-l：清单解析失败 WARNING 降 DEBUG（「解析失败」与「真没有声明」塌成一个值）",
        PR,
        'logger.warning("[pypi-registry] %s 解析失败（%s），该清单声明证据缺席", pj, exc)',
        'logger.debug("[pypi-registry] %s 解析失败（%s），该清单声明证据缺席", pj, exc)',
        ["test_manifest_specs_malformed_pyproject_warns"],
    ),
    # ── 以下为对抗双复核 R1 整改的突变（每条对应一条已独立复现的 finding）──
    (
        "P-H4a-m：脚手架结构判据非 Maven 补位删除（python 脚手架边又被 decouple 剥——复核 R-1 复活）",
        CU,
        "    return dep_st is not None and (\n"
        "        (_is_scaffold_subtask(dep_st) and _creates_module_pom(dep_st))\n"
        "        or _is_pure_module_manifest_scaffold(dep_st))",
        "    return dep_st is not None and _is_scaffold_subtask(dep_st) "
        "and _creates_module_pom(dep_st)",
        ["test_ph4a_python_scaffold_ordering_edge_never_stripped"],
    ),
    (
        "P-H4a-n：dedupe 分组回到只认 pom.xml（python 重复脚手架不合并——复核 R-2 复活）",
        CU,
        '            if norm.rsplit("/", 1)[-1] in module_manifest_names() and "/" in norm:',
        '            if norm.rsplit("/", 1)[-1] == "pom.xml" and "/" in norm:',
        ["test_dedupe_module_scaffolds_python_pure_merged_mixed_untouched"],
    ),
    (
        "P-H4a-p：区间可满足性收窄回稳定版集（`>=1.0b1` 预发布真包被冤杀——cr R-5 复活）",
        PR,
        '            rel = data.get("releases")\n'
        "            satisfiable = []\n"
        "            for v in (rel if isinstance(rel, dict) else ()):",
        "            satisfiable = []\n"
        "            for v in _stable_versions(data):",
        ["test_resolve_range_prerelease_bound_allows_prerelease"],
    ),
    (
        "P-H4a-q：行内注释剥离删除（requirements 约束被注释污染 → spec_unparsed 假降级——cr R-4）",
        PR,
        '    if " #" in line:\n',
        "    if False:  # 突变：行内注释剥离删除\n",
        ["test_parse_dep_text_inline_comment_stripped"],
    ),
    (
        "P-H4a-r：direct reference 剥 URL 只留名（项目钉死来源被静默换成公网版——cr R-3 本尊复活）",
        PR,
        "    m = _DEP_RE.match(core)",
        '    if " @ " in core:\n        core = core.split(" @ ", 1)[0].strip()\n'
        "    m = _DEP_RE.match(core)",
        ["test_parse_dep_text_direct_ref_preserves_url",
         "test_resolve_direct_ref_passthrough_never_consults_registry"],
    ),
    (
        "P-H4a-s：setup.py install_requires 证据层删除（setuptools 工程声明蒸发——cr R-6 复活）",
        PR,
        '            m_ir = re.search(r"install_requires\\s*=\\s*\\[(.*?)\\]\\s*[,)\\n]", sp_text, re.S)',
        "            m_ir = None",
        ["test_manifest_specs_setup_py_install_requires"],
    ),
    (
        "P-H4a-t：未解析模块 WARNING 降 INFO（「没注入」与「不需要注入」不可辨——hunter#4 复活）",
        CU,
        '                logger.warning(\n'
        '                    "[SCAFFOLD-INJECT] #31-P2 %s 栈 %d 个契约模块无确定物理落点',
        '                logger.info(\n'
        '                    "[SCAFFOLD-INJECT] #31-P2 %s 栈 %d 个契约模块无确定物理落点',
        ["test_ph4_unresolved_contract_module_warns_not_silent"],
    ),
    (
        "P-H4a-u：python 模块 driver 事实字段翻回 False（新建撞车 demote 又刷「无兜底网」——hunter#1 复活）",
        SP,
        "        has_module_scaffold_driver=True,\n        # ★刻意 None（诚实边界）★",
        "        has_module_scaffold_driver=False,\n        # ★刻意 None（诚实边界）★",
        ["test_python_module_manifest_create_collision_demote_is_silent",
         "test_scaffold_driver_facts_match_reality"],
    ),
    # ── 以下为 R2 双复核 findings 整改的突变 ──
    (
        "P-H4a-v：http 前缀闸复活（httpx/httpcore 主流包名族被系统性误杀——cr R2 HIGH-1 复活）",
        PR,
        '    if not line or line.startswith(("#", "-", "git+", ".")):',
        '    if not line or line.startswith(("#", "-", "http", "git+", ".")):',
        ["test_parse_dep_text_forms"],
    ),
    (
        "P-H4a-w：setup.py 闭括号锚定删除（extras 的 `']'` 提前截断，其后条目静默蒸发——cr R2 MEDIUM-1 复活）",
        PR,
        r'm_ir = re.search(r"install_requires\s*=\s*\[(.*?)\]\s*[,)\n]", sp_text, re.S)',
        r'm_ir = re.search(r"install_requires\s*=\s*\[(.*?)\]", sp_text, re.S)',
        ["test_manifest_specs_setup_py_install_requires"],
    ),
    (
        "P-H4a-x：钉版判等退回字面相等（`==2.0.0` 对发布键 `2.0` 冤杀——hunter R2 M-1 复活）",
        PR,
        "                pin = exact.group(1).strip()\n"
        "                try:\n"
        "                    pin_v = Version(pin)\n"
        "                except InvalidVersion:\n"
        "                    pin_v = None",
        "                pin = exact.group(1).strip()\n"
        "                pin_v = None  # 突变：退回字面相等",
        ["test_resolve_explicit_pin_pep440_equivalent_kept"],
    ),
    (
        "P-H4a-y：marker 保留删除（条件依赖被改写成无条件——hunter R2 M-2 复活）",
        PR,
        "    if sep and marker.strip():",
        "    if False:  # 突变：marker 剥离",
        ["test_parse_dep_text_forms",
         "test_resolve_marker_preserved_through_verification",
         "test_resolve_bare_with_marker_keeps_marker_on_floor",
         "test_resolve_manifest_adoption_keeps_contract_marker"],
    ),
    (
        "P-H4a-z：python 契约标签并入内部集删除（未解析模块送 PyPI 物化同名公网包——hunter R2 H-1 本尊复活）",
        CU,
        "    internal_names |= {normalize_name(m) for m in _contract_module_labels(plan)}",
        "    internal_names |= set()  # 突变：契约标签并集删除",
        ["test_ph4_python_unresolved_internal_label_never_hits_pypi",
         "test_ph4_python_contract_label_stays_internal_when_disk_name_differs"],
    ),
    (
        "P-H4a-aa：npm held pre-split 删除（未解析内部模块送公网 registry/物化 workspace:*——H-1 npm 臂复活）",
        CU,
        "        held = [a for a in arts\n"
        "                if _split_name_range(a)[0] in _labels\n"
        "                and _split_name_range(a)[0] not in internal_names]",
        "        held = []  # 突变：held pre-split 删除",
        ["test_ph4_npm_unresolved_internal_label_held_from_registry"],
    ),
    (
        # ★#29-3 T-1：落点死于**模块迁移**，代码逐字未变★ go 脚手架叶簇从 contract_utils.py
        # 拆到 go_scaffold.py，`held` 这行一个字节都没改，但地址变了 ⇒ 打 CU 恒未命中＝零覆盖。
        # 只需把路径从 CU 换成 GS（定义模块），字符串原样。
        "P-H4a-ab：go held pre-split 删除（未解析内部模块送 proxy/臆造 replace——H-1 go 臂复活）",
        GS,
        "        held = [a for a in arts if a in _labels and a not in internal_paths]",
        "        held = []  # 突变：held pre-split 删除",
        ["test_ph4_go_unresolved_internal_label_held_from_proxy"],
    ),
    (
        "P-H4a-ac：TOML 转义删除（双引号 marker/extras 插值=权威模板产出非法 TOML——cr R3 HIGH 复活）",
        CU,
        "    return s.replace(\"\\\\\", \"\\\\\\\\\").replace('\"', '\\\\\"')",
        "    return s  # 突变：转义删除",
        ["test_render_pyproject_escapes_double_quoted_markers"],
    ),
]


def _pytest(args: list[str]) -> int:
    p = subprocess.run([PY, "-m", "pytest", *TESTS, "-p", "no:warnings", "-q",
                        "--tb=no", *args], cwd=ROOT, capture_output=True, text=True)
    return p.returncode


def _clear_pyc(path: Path) -> None:
    """删被突变模块的 pyc——CPython 的 pyc 失效判据是【整秒】粒度 mtime：相邻两条突变
    落在同一秒时，第二条突变写完 pyc 仍被判有效 ⇒ 子进程跑的是【上一条】的代码
    （f/f2 实测：区间臂突变后钉版臂突变在同一秒，钉版臂测试跑的是区间臂代码=假绿）。
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
            # ★P-H4b cr R1 #3 sibling★ ast.parse 失败是【抛 SyntaxError】而非返回
            # None——判空是死代码（P-H4a 期间该检查从未能触发）
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
