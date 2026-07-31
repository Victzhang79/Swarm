#!/usr/bin/env python3
"""X-C3-A（治法 B）突变 harness：先证基线全绿，再逐条突变证"指名的每条都红"。

判据与 `xc3_mutation_check.py` 同源（那两条自伤已在上一批修掉，这里一开始就带上）：
  · **先验基线全绿** —— 只验"突变→红"会让修得不全的整改蒙过去。
  · **逐条**跑 should_red，每条都必须红（"整组任一条红"会让零区分力的名字永不被发现）。
  · `rc=5`（`-k` 一条都没选到，如测试被重命名）**判失败**，不是"红了"。
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PY = str(ROOT / ".venv" / "bin" / "python")
TESTS = ["test/test_xc3a_path_namespace.py", "test/test_xc3_error_drivers.py"]

DRV = ROOT / "worker" / "l1_error_drivers.py"
PIPE = ROOT / "worker" / "l1_pipeline.py"
REC = ROOT / "brain" / "nodes" / "recovery.py"
FAIL = ROOT / "brain" / "nodes" / "failure.py"

MUTATIONS = [
    (
        "worker 不再产路径口径（brain 侧回落 Java 点分 → 四栈全灭）",
        PIPE,
        "            _paths = ref_path_stems(language_key, driver_refs or list(blocked_pkgs),\n"
        "                                    project_path, timeout, run)",
        "            _paths = {}",
        # 不含 test_worker_emits_path_stems：它直调 `ref_path_stems`，看不见**管线侧**这处突变。
        ["test_verdict_details_carry_path_keys_consumable_by_brain"],
    ),
    (
        "栈键不再由裁决层写（新调用点漏写即静默丢栈 → 自愈取不到扩展名）",
        PIPE,
        '        details["blocked_via_error_driver"] = language_key',
        "        pass",
        ["test_verdict_details_carry_path_keys_consumable_by_brain"],
    ),
    (
        "消费者1 _producers_of 忽略 paths（退回点分口径）",
        REC,
        '    _use_paths = ([str(p).strip("/") for p in paths] if paths is not None else [])',
        "    _use_paths = []",
        ["test_producers_of_resolves_with_paths"],
    ),
    (
        "消费者1 词干只当目录段比（python/TS 的**文件**词干失配）",
        REC,
        '        return _has_paths and fn.rsplit(".", 1)[0] == pp',
        "        return False",
        ["test_producers_of_resolves_with_paths"],
    ),
    (
        "消费者2 _package_in_baseline 忽略 path_stems（树里有也判 False）",
        REC,
        '    _stems = ([str(s).strip("/") for s in path_stems] if path_stems is not None else [])',
        "    _stems = []",
        ["test_package_in_baseline_sees_existing_tree",
         "test_futile_no_longer_true_for_existing_tree"],
    ),
    (
        "消费者2 拧成恒真（护栏失效：臆造 ref 也说在树里 → #10 幽灵生产者无人拦）",
        REC,
        "        return False\n    rel = pkg.replace(\".\", \"/\").strip(\"/\")",
        "        return True\n    rel = pkg.replace(\".\", \"/\").strip(\"/\")",
        ["test_package_in_baseline_still_negative_when_truly_absent",
         "test_futile_still_true_when_truly_hallucinated"],
    ),
    (
        "消费者3 _derive_missing_type_files 忽略 path_stems（非 JVM 恒返 []）",
        FAIL,
        "    _by_ref: dict = (path_stems if isinstance(path_stems, dict)\n"
        "                     else {s: [s] for s in (path_stems or [])})",
        "    _by_ref: dict = {}",
        ["test_derive_missing_files_for_non_jvm"],
    ),
    (
        "消费者3 Go 包当成文件（建出与包同名的 .go，编译仍缺包）",
        FAIL,
        "                out.append(f\"{pick}/{pick.rsplit('/', 1)[-1]}{exts[0]}\")",
        "                out.append(f\"{pick}{exts[0]}\")",
        ["test_derive_go_package_is_a_directory",
         "test_derive_missing_files_for_non_jvm"],
    ),
    (
        "消费者3 输出不再去重（同一落点重复进 create_files）",
        FAIL,
        "        return list(dict.fromkeys(out))",
        "        return out + out",
        ["test_derive_keeps_every_independent_ref"],
    ),
    (
        "消费者3 未收录栈猜扩展名（建出 x.unknown）",
        FAIL,
        "        if not exts:\n            return []",
        "        if not exts:\n            exts = (\".txt\",)",
        ["test_derive_returns_empty_for_unregistered_stack"],
    ),
    # ── 以下为 reviewer 复核整改新增（每条都有实测复现的 finding）──
    (
        "CRITICAL-1: 类级臂又抢先 return（符号级通道整块绕过路径口径）",
        REC,
        "        _sym_paths = [s for c in _cls for s in (_pbr.get(c) or [])]\n"
        "        if _sym_paths:",
        "        _sym_paths = []\n        if _sym_paths:",
        ["test_symbol_level_arm_uses_path_namespace"],
    ),
    (
        "CRITICAL-1 配套: worker 不给符号 FQN 发路径口径（brain 类级臂恒空）",
        PIPE,
        "                        if _cs == _ref or _cs.startswith(_ref + _sym_sep):\n"
        "                            _by_ref.setdefault(_cs, sorted(_stems))",
        "                        if False:\n"
        "                            _by_ref.setdefault(_cs, sorted(_stems))",
        # 不含 test_symbol_level_arm_uses_path_namespace：它直接**手传** paths_by_ref，
        # 看不见 worker 侧这处突变（零区分力，严格粒度如实报出）。
        ["test_worker_emits_path_stems_for_symbol_fqns"],
    ),
    (
        "HIGH-2: Go 根包词干 `\"\"` 又被过滤成键缺席（回落点分 → 连坐，零留痕）",
        DRV,
        "        if stems:\n            out[ref] = list(stems)",
        "        if stems:\n            out[ref] = [s for s in stems if s]",
        ["test_go_root_stem_survives_to_all_consumers"],
    ),
    (
        "HIGH-2 配套: 根词干在 _producers_of 里被滤掉",
        REC,
        "    _use_paths = ([str(p).strip(\"/\") for p in paths] if paths is not None else [])",
        "    _use_paths = [str(p).strip(\"/\") for p in (paths or []) if str(p).strip()]",
        ["test_go_root_stem_survives_to_all_consumers"],
    ),
    (
        "HIGH-2 配套: 根词干吃掉子目录（整棵树都算自产）",
        REC,
        "            if _root_stem and \"/\" not in fn:",
        "            if _root_stem:",
        ["test_root_stem_does_not_swallow_subdirs"],
    ),
    (
        "HIGH-2 账: 『判了 BLOCKED 却无路径口径』不再留痕",
        PIPE,
        '                details["blocked_on_paths_absent"] = language_key',
        "                pass",
        ["test_blocked_without_path_namespace_leaves_account"],
    ),
    (
        "HIGH-3: 多候选二选一又跨 ref 施加（缺 N 个只建 1 个）",
        FAIL,
        "    _by_ref: dict = (path_stems if isinstance(path_stems, dict)\n"
        "                     else {s: [s] for s in (path_stems or [])})",
        "    _by_ref: dict = {\"_\": [s for v in path_stems.values() for s in v]} \\\n"
        "        if isinstance(path_stems, dict) else {\"_\": list(path_stems or [])}",
        ["test_derive_keeps_every_independent_ref"],
    ),
    (
        "HIGH-4: 布局二选一又回去猜（取最短 → src-layout 落点错+种影子包）",
        FAIL,
        "            pick = _pick_stem_by_evidence(stems, scope_files, project_path)",
        "            pick = min(stems, key=lambda p: (p.count(\"/\"), len(p)))",
        ["test_derive_picks_layout_by_evidence_not_by_guess"],
    ),
    (
        "HIGH-4: 定不了档时不再 fail-honest（硬猜一个落点）",
        FAIL,
        "            if pick is None:",
        "            if False:",
        ["test_derive_fails_honest_when_layout_undecidable"],
    ),
    (
        "HIGH-5: 越界词干校验被拆（../ 能写到工程树外）",
        FAIL,
        '        if not fn or fn.startswith("/") or ".." in fn.split("/"):\n'
        "            continue",
        "        if not fn:\n            continue",
        ["test_derive_rejects_path_traversal_stems"],
    ),
    (
        "HIGH-5: 已存在文件过滤被拆（自愈指着 300 行既有实现说『该新建』）",
        FAIL,
        "        out = [s for s in out\n"
        "               if not any(os.path.isfile(os.path.join(project_path, s + e))\n"
        "                          for e in (\".go\", \".ts\", \".tsx\", \".js\", \".rs\", \".py\", \"\"))]",
        "        pass",
        ["test_derive_skips_already_existing_file"],
    ),
]

# ── 一条**刻意不做**的突变（诚实记账，别让下一个人以为漏了）──
#
# "`ref_path_stems` 对 java 不再返空"：这条突变**天然无害**，因为 JVM 有**三重**独立
#    护栏，去掉任一条都不会让 java 走上新路：
#      ① `_xc3_lang` 只在 `if not _blocked_pkgs` 分支里赋值 ⇒ JVM 专用链命中时它恒 None；
#      ② 路径块门控 `if language_key and …` ⇒ None 即整块跳过；
#      ③ `ref_path_stems` 里 `drv.key in _SELF_HANDLED_KEYS` 在**调用 `ref_tree_paths` 之前**
#         就返 `{}`（★这是真正的短路点★；`JvmErrorDriver.ref_tree_paths` 的
#         NotImplementedError 只是更外一层保险，要等 ③ 被拆掉才轮得到它——原注释把两者
#         写颠倒了，reviewer 勘误，已改）。
#    单独拧坏 ③ 后 java 仍返 {}，故**任何**测试都不该因它变红。这不是"锁不住"，是"这条
#    突变与被测命题不等价"（上一批的第 4 类假绿）。JVM 零回归由
#    `test_jvm_verdict_writes_no_path_keys`（走裁决本体，证 ①②）钉住。


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
        path.write_text(src.replace(old, new, 1))
        try:
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
