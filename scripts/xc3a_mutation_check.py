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
        '        details.setdefault("blocked_via_error_driver", language_key)',
        "        pass",
        ["test_verdict_details_carry_path_keys_consumable_by_brain"],
    ),
    (
        "消费者1 _producers_of 忽略 paths（退回点分口径）",
        REC,
        "    _use_paths = [str(p).strip(\"/\") for p in (paths or []) if str(p).strip()]",
        "    _use_paths = []",
        ["test_producers_of_resolves_with_paths"],
    ),
    (
        "消费者1 词干只当目录段比（python/TS 的**文件**词干失配）",
        REC,
        "        return bool(_use_paths) and fn.rsplit(\".\", 1)[0] == pp",
        "        return False",
        ["test_producers_of_resolves_with_paths"],
    ),
    (
        "消费者2 _package_in_baseline 忽略 path_stems（树里有也判 False）",
        REC,
        "    _stems = [str(s).strip(\"/\") for s in (path_stems or []) if str(s).strip()]",
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
        "    _stems = [str(s).strip(\"/\") for s in (path_stems or []) if str(s).strip()]",
        "    _stems = []",
        ["test_derive_missing_files_for_non_jvm"],
    ),
    (
        "消费者3 Go 包当成文件（建出与包同名的 .go，编译仍缺包）",
        FAIL,
        "                out.append(f\"{s}/{s.rsplit('/', 1)[-1]}{exts[0]}\")",
        "                out.append(f\"{s}{exts[0]}\")",
        ["test_derive_go_package_is_a_directory",
         "test_derive_missing_files_for_non_jvm"],
    ),
    (
        "消费者3 多候选词干全建（种出两棵真值树）",
        FAIL,
        "            out = [min(out, key=lambda p: (p.count(\"/\"), len(p)))]",
        "            pass",
        ["test_derive_picks_shortest_stem_not_both"],
    ),
    (
        "消费者3 未收录栈猜扩展名（建出 x.unknown）",
        FAIL,
        "        if not exts:\n            return []",
        "        if not exts:\n            exts = (\".txt\",)",
        ["test_derive_returns_empty_for_unregistered_stack"],
    ),
]

# ── 两条**刻意不做**的突变（诚实记账，别让下一个人以为漏了）──
#
# 1. `blocked_on_paths_absent` 降级账：我原先写了它，突变证明锁不住 —— 复查后确认该分支
#    **不可达**（与步骤 4 共用同一个 `ref_tree_paths` + 同门控 ⇒ 解不出的 ref 必先在
#    UNKNOWN 闸早返）。已删除该分支，故无从突变。
#
# 2. "`ref_path_stems` 对 java 不再返空"：这条突变**天然无害**，因为 JVM 有**三重**独立
#    护栏，去掉任一条都不会让 java 走上新路：
#      ① `_xc3_lang` 只在 `if not _blocked_pkgs` 分支里赋值 ⇒ JVM 专用链命中时它恒 None；
#      ② 路径块门控 `if language_key and …` ⇒ None 即整块跳过；
#      ③ `JvmErrorDriver.ref_tree_paths` 抛 NotImplementedError ⇒ 被 except 兜住返 {}。
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
