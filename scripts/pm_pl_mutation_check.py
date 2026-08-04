#!/usr/bin/env python3
"""P-M/P-L 批（P-M2/P-M3/P-M4/P-L1~3）突变 harness（判据与前批同源）：

  · **先验基线全绿**；· **逐条**跑 should_red；· `rc=5` **判失败**；
  · 落点唯一性检查；· 突变后源码必须仍能 `ast.parse`（try/except SyntaxError）；
  · harness 改磁盘源码——**绝不进带超时的循环、绝不与全量并发、跑完看 git status**；
  · 突变写入后与还原后都清被突变模块的 pyc（CPython 整秒粒度 mtime 陈旧坑）。

★锁的命题★
- P-M2：snake/kebab↔camel 词序列归一进 tier 1（另立 tier 3 会把蛇形正名路由进
  rename-reconcile；词序/单复数绝不放宽）。
- P-M3：符号安置小写排除按栈门控——JVM 主导 plan 排除逐字节保留；非 JVM 主导
  plan 的小写契约符号确定性安置；JVM 集派生自 STACK_SPEC 非手抄。
- P-M4：布局段派生并集逐元素等于旧 12 段；workspace 容器段（position-0）三机制
  （_code_module_root/_evidence_class/_common_module_prefix）缺一即塌模块。
- P-L1~3：同目录兄弟源码 readable 扩栈（同扩展名保守匹配）+ _entity_stem 通用
  末段剥离（白名单外的扩展名不再碎裂实体聚簇）。
"""
from __future__ import annotations

import ast
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PY = str(ROOT / ".venv" / "bin" / "python")
TESTS = ["test/test_pm2_symbol_form_equivalence.py",
         "test/test_pm3_lowercase_symbol_domicile.py",
         "test/test_pm4_workspace_container_layout.py",
         "test/test_pm_l_entity_cluster_readable.py"]

PV = ROOT / "brain" / "plan_validator.py"
PF = ROOT / "brain" / "plan_finisher.py"
CU = ROOT / "brain" / "contract_utils.py"
PN = ROOT / "brain" / "planning_nodes.py"

MUTATIONS = [
    # ── P-M2：词序列归一 ─────────────────────────────────────
    (
        "P-M2-a：词序列归一分支整删（snake/kebab 文件通道回到恒 -1，机制删掉必须红）",
        PV,
        "    sw, yw = _symbol_words(s), _symbol_words(y)\n"
        "    if sw and sw == yw:\n"
        "        return 1\n",
        "",
        ["test_snake_stem_matches_camel_symbol",
         "test_kebab_stem_matches_camel_symbol",
         "test_reverse_direction_symbol_snake_stem_camel"],
    ),
    (
        "P-M2-b：归一结果降成 tier 3（symbol_provenance 把 t>1 当装饰弱通道走改名 "
        "reconcile——蛇形正名 user_service.py 被「修」成 UserService.py，消费契约误判）",
        PV,
        "    if sw and sw == yw:\n"
        "        return 1\n",
        "    if sw and sw == yw:\n"
        "        return 3  # 突变：另立 tier 3\n",
        ["test_snake_stem_matches_camel_symbol"],
    ),
    (
        "P-M2-c：词序不敏感（sorted 归一=service_user↔UserService 互认，"
        "豁免半径失控 F3 族方向）",
        PV,
        "    if sw and sw == yw:\n"
        "        return 1\n",
        "    if sw and sorted(sw) == sorted(yw):\n"
        "        return 1  # 突变：词序不敏感\n",
        ["test_word_order_and_plural_still_distinguish"],
    ),
    # ── P-M3：小写排除按栈门控 ───────────────────────────────
    (
        "P-M3-a：门控回退无条件排除（治前形状——py/go 小写契约符号重新被挡在确定性"
        "安置门外）",
        PF,
        "            and (_dominant_ext not in _JVM_CLASS_FILE_EXTS\n"
        "                 or not e[\"symbol\"][0].islower())\n",
        "            and not e[\"symbol\"][0].islower()  # 突变：无条件排除\n",
        ["test_python_plan_lowercase_symbol_domiciled",
         "test_go_plan_lowercase_symbol_domiciled"],
    ),
    (
        "P-M3-b：排除整删（JVM 主导 plan 的方法名形状符号也被安置=Java 幻影类文件，"
        "回归锁必须红）",
        PF,
        "            and (_dominant_ext not in _JVM_CLASS_FILE_EXTS\n"
        "                 or not e[\"symbol\"][0].islower())\n",
        "            and True  # 突变：排除整删\n",
        ["test_java_plan_still_excludes_lowercase_symbol"],
    ),
    (
        "P-M3-c：JVM 集手抄退化为全栈并集（py/go/ts 全算「类文件栈」=门控对非 JVM "
        "恒排除，单一事实源派生被换成等效手抄宽表）",
        PF,
        "_JVM_CLASS_FILE_EXTS: frozenset[str] = frozenset(\n"
        "    e.lstrip(\".\") for s in STACK_SPEC.values()\n"
        "    if s.shares_classpath_namespace for e in s.source_exts)",
        "_JVM_CLASS_FILE_EXTS: frozenset[str] = frozenset(\n"
        "    e.lstrip(\".\") for s in STACK_SPEC.values()\n"
        "    for e in s.source_exts)  # 突变：全栈并集",
        ["test_python_plan_lowercase_symbol_domiciled",
         "test_jvm_exts_derived_from_stack_spec"],
    ),
    # ── P-M4：布局段派生 + 容器机制 ──────────────────────────
    (
        "P-M4-a：_code_module_root 容器规则整删（packages/api/index.ts 回到 None→"
        "WEAK 档塌首段假模块，主治机制删掉必须红）",
        CU,
        "    if (len(parts) >= 3 and parts[0] in _WORKSPACE_CONTAINER_SEGMENTS\n"
        "            and not any(seg in _SRC_EXCLUDE_DIRS_ALL for seg in parts[2:-1])\n"
        "            and not (_SRC_EXCLUDE_SUFFIXES_ALL\n"
        "                     and p.lower().endswith(_SRC_EXCLUDE_SUFFIXES_ALL))):\n"
        "        return f\"{parts[0]}/{parts[1]}\"\n",
        "",
        ["test_module_root_workspace_package_without_layout_segment"],
    ),
    (
        "P-M4-b：容器规则丢 position-0 判据（任意深处 packages 都触发=Java 包名 "
        "com.x.packages 被当 workspace，反误杀锁必须红）",
        CU,
        "    if (len(parts) >= 3 and parts[0] in _WORKSPACE_CONTAINER_SEGMENTS\n"
        "            and not any(seg in _SRC_EXCLUDE_DIRS_ALL for seg in parts[2:-1])\n"
        "            and not (_SRC_EXCLUDE_SUFFIXES_ALL\n"
        "                     and p.lower().endswith(_SRC_EXCLUDE_SUFFIXES_ALL))):\n"
        "        return f\"{parts[0]}/{parts[1]}\"\n",
        "    if (\"packages\" in parts[:-1] and len(parts) >= 3):\n"
        "        _ci = parts.index(\"packages\")\n"
        "        return f\"{parts[_ci]}/{parts[_ci + 1]}\"  # 突变：position-0 丢失\n",
        ["test_module_root_deep_packages_segment_not_container"],
    ),
    (
        "P-M4-c：_evidence_class 容器档整删（workspace 包源码塌回 WEAK=首段假模块 "
        "「packages」主张物理根）",
        CU,
        "    if (len(_dirs) >= 2 and _dirs[0] in _WORKSPACE_CONTAINER_SEGMENTS\n"
        "            and not any(seg in _SRC_EXCLUDE_DIRS_ALL for seg in _dirs[2:])\n"
        "            and not (_SRC_EXCLUDE_SUFFIXES_ALL\n"
        "                     and p.lower().endswith(_SRC_EXCLUDE_SUFFIXES_ALL))):\n"
        "        return _EV_STRONG\n",
        "",
        ["test_evidence_class_workspace_package_is_strong"],
    ),
    (
        "P-M4-d：_common_module_prefix 容器收尾整删（公共前缀恰止 packages 被判成"
        "合法模块根=塌模块假根复辟）",
        CU,
        "    if common and len(common) == 1 and common[0] in _WORKSPACE_CONTAINER_SEGMENTS:\n"
        "        return None\n",
        "",
        ["test_common_prefix_ending_at_container_is_none"],
    ),
    (
        "P-M4-e：npm 条目容器字段删除（新栈加表行机制断——字段缺席=容器段空集="
        "全部容器机制静默失效，相等锁必须红）",
        ROOT / "stacks" / "spec.py",
        "        # pnpm/turborepo workspace 容器（P-M4 主治：packages 布局塌模块）\n"
        "        workspace_container_segments=(\"packages\", \"apps\"),",
        "",
        ["test_workspace_container_segments_npm_only",
         "test_module_root_workspace_package_without_layout_segment"],
    ),
    # ── P-L1~3：readable 扩栈 + stem 通用剥离 ────────────────
    (
        "P-L-a：enrich 回退只认 .java（Go/Python 同目录兄弟重新零 readable="
        "异栈同死法复辟）",
        CU,
        "            _e = \".\" + _b.rsplit(\".\", 1)[-1].lower()\n"
        "            if _e in _SRC_EXTS:",
        "            _e = \".\" + _b.rsplit(\".\", 1)[-1].lower()\n"
        "            if _e == \".java\":  # 突变：回退只认 .java",
        ["test_go_siblings_enriched", "test_python_siblings_enriched"],
    ),
    (
        "P-L-b：同扩展名约束放宽成任意源码扩展名（.ts 目标拉 .tsx/.java 兄弟="
        "保守同构边界失守，readable 膨胀方向）",
        CU,
        "                if (\".\" + name.rsplit(\".\", 1)[-1].lower()) not in exts:\n"
        "                    continue",
        "                if (\".\" + name.rsplit(\".\", 1)[-1].lower()) not in _SRC_EXTS:\n"
        "                    continue  # 突变：任意源码扩展名都拉",
        ["test_cross_ext_not_pulled"],
    ),
    (
        "P-L-c：_entity_stem 退回白名单（.tsx/.kt/.rs/.php 剥不掉=非 JVM 实体聚簇"
        "按扩展名碎裂复辟）",
        PN,
        "    if \".\" in name and not name.startswith(\".\"):\n"
        "        name = name.rsplit(\".\", 1)[0]",
        "    import re as _re2\n"
        "    name = _re2.sub(r\"\\.(java|xml|sql|vue|js|ts|go|py)$\", \"\", name)"
        "  # 突变：退回白名单",
        ["test_entity_stem_new_stack_extensions"],
    ),
    # ── R2 双透镜整改批（hunter F1/F2/F3）──
    (
        "P-M3-d：平票确定性回退 most_common（插入序=LLM 输出序 → 同一逻辑 plan 门控"
        "结论随文件顺序抖动，hunter F2）",
        PF,
        "    _dominant_ext = (sorted(exts.items(), key=lambda kv: (-kv[1], kv[0]))[0][0]\n"
        "                     if exts else \"\")",
        "    _dominant_ext = exts.most_common(1)[0][0] if exts else \"\""
        "  # 突变：平票回退插入序",
        ["test_tie_break_is_deterministic"],
    ),
    (
        "P-M4-f：容器档排除守卫整删（node_modules/dist/.d.ts 提升 STRONG 主张模块根，"
        "hunter F3；_evidence_class 臂）",
        CU,
        "            and not any(seg in _SRC_EXCLUDE_DIRS_ALL for seg in _dirs[2:])\n"
        "            and not (_SRC_EXCLUDE_SUFFIXES_ALL\n"
        "                     and p.lower().endswith(_SRC_EXCLUDE_SUFFIXES_ALL))):\n"
        "        return _EV_STRONG\n",
        "            ):\n"
        "        return _EV_STRONG  # 突变：排除守卫整删\n",
        ["test_container_rule_excludes_artifact_dirs_and_declaration_suffix"],
    ),
    (
        "P-M4-g：容器档排除守卫整删（_code_module_root 臂——两臂同源，各自独立锁）",
        CU,
        "            and not any(seg in _SRC_EXCLUDE_DIRS_ALL for seg in parts[2:-1])\n"
        "            and not (_SRC_EXCLUDE_SUFFIXES_ALL\n"
        "                     and p.lower().endswith(_SRC_EXCLUDE_SUFFIXES_ALL))):\n"
        "        return f\"{parts[0]}/{parts[1]}\"\n",
        "            ):\n"
        "        return f\"{parts[0]}/{parts[1]}\"  # 突变：排除守卫整删\n",
        ["test_container_rule_excludes_artifact_dirs_and_declaration_suffix"],
    ),
    (
        "P-L-d：兄弟拉取上限闸整删（Go 大 package 60+ 兄弟全进 readable=上下文爆炸，"
        "hunter F1）",
        CU,
        "            if len(cands) > _MAX_SIBLINGS_PER_DIR:",
        "            if False:  # 突变：上限闸整删",
        ["test_sibling_pull_capped_with_warning"],
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
