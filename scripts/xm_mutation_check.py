#!/usr/bin/env python3
"""X-M 簇突变 harness（判据与前九批同源，那些自伤一开始就带上）：

  · **先验基线全绿**（只验"突变→红"会让修得不全的整改全绿通过：B-4b I-1 实证）；
  · **逐条**跑 should_red，每条都必须红；· `rc=5`（`-k` 选不到）**判失败**；
  · 落点唯一性检查；· 突变后源码必须仍能 `ast.parse`（否则 rc≠0 只是 collection error）。

★锁的命题★ X-M 批一（27 号文 §3.2 X-M2/M5/M7）：多栈覆盖的「快赢」三条——
.kt 包声明对账 / 混编逐栈自测 / 未知工具链降级可观测。
X-M 批二（X-M8）：.vue 进类型闸触发集 + vue-tsc 优选 + 缺 vue-tsc 降级 WARNING。
"""
from __future__ import annotations

import ast
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PY = str(ROOT / ".venv" / "bin" / "python")
TESTS = ["test/test_image_builder.py",
         "test/test_e_theme_supply_gates_batch1.py",
         "test/test_xm8_vue_type_gate.py"]

IMG = ROOT / "worker" / "image_builder.py"
PIPE = ROOT / "worker" / "l1_pipeline.py"

MUTATIONS = [
    (
        "X-M2：.kt 规则从分派表删除（Kotlin 包声明对账回退到「抽不到声明 ⇒ 静默跳过」——"
        "与没接上同效：表驱动机制加了栈但没进表＝接线覆盖 ≠ 机制存在）",
        PIPE,
        '    ".kt": (re.compile(r"(?:^|/)(?:src/main/kotlin|src/test/kotlin|src/main/java|src/test/java)"\n'
        '                       r"/(?P<rel>.+\\.kt)$"),\n'
        '            re.compile(r"^\\+\\s*package\\s+([A-Za-z_][\\w.]*?)\\s*;?\\s*(?://.*)?$")),\n',
        '',
        ['test_e6_kotlin_package_decl_mismatch_caught'],
    ),
    (
        "X-M5：混编自测退回首个命中即 return（java+npm 只自测 maven，npm 侧坏掉等运行时炸）",
        IMG,
        '    return " && ".join(cmds) if cmds else None',
        '    return cmds[0] if cmds else None',
        # 配对守卫也锁得住：混编 selftest 输出变了 ⇒ 摘要变 ⇒ (10, 摘要) 配对破。
        ['test_selftest_covers_every_toolchain_in_a_mixed_spec',
         'test_builder_version_bumped_so_old_images_are_invalidated'],
    ),
    (
        "X-M7：未知工具链降级 WARNING 降成 DEBUG（回退到「只在 Dockerfile 里留一行注释」——"
        "降级不可观测，血规 3）",
        IMG,
        '    logger.warning("[IMAGE-BUILD] X-M7 未知工具链 %r（build_tool=%r）：镜像不装它的构建"',
        '    logger.debug("[IMAGE-BUILD] X-M7 未知工具链 %r（build_tool=%r）：镜像不装它的构建"',
        ['test_unknown_toolchain_emits_warning'],
    ),
    (
        "X-M8a：触发集删掉 .vue（.vue 改动回退到「js_ts 为空 → 类型闸整段跳过」＝零覆盖）",
        PIPE,
        'js_ts = [f for f in files if f.endswith((".ts", ".tsx", ".js", ".jsx", ".vue"))]',
        'js_ts = [f for f in files if f.endswith((".ts", ".tsx", ".js", ".jsx"))]',
        ['test_vue_change_is_type_checked_by_vue_tsc'],
    ),
    (
        "X-M8b：vue-tsc 优选被摘（.vue 直接喂 tsc —— tsc 解析不了 SFC，要么假红要么靠"
        " infra 豁免假绿，两种都是错答案）",
        PIPE,
        'if any(f.endswith(".vue") for f in js_ts):',
        'if False:',
        ['test_vue_change_is_type_checked_by_vue_tsc'],
    ),
    (
        "X-M8c：缺 vue-tsc 的降级 WARNING 降成 DEBUG（.vue 无类型覆盖这一降级不可观测，血规 3）",
        PIPE,
        '''logger.warning(
                        "[L1.2] X-M8 项目缺 vue-tsc''',
        '''logger.debug(
                        "[L1.2] X-M8 项目缺 vue-tsc''',
        ['test_missing_vue_tsc_falls_back_to_tsc_with_warning'],
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
        path.write_text(src.replace(old, new, 1))
        try:
            if path.suffix == ".py" and ast.parse(path.read_text()) is None:
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
