#!/usr/bin/env python3
"""X-C2 + N-1 突变 harness（判据与前两批同源，那两条自伤一开始就带上）：

  · **先验基线全绿** —— 只验"突变→红"会让修得不全的整改蒙过去；
  · **逐条**跑 should_red，每条都必须红（"整组任一条红"会让零区分力的名字永不被发现）；
  · `rc=5`（`-k` 一条都没选到，如测试被重命名）**判失败**，不是"红了"；
  · 落点唯一性检查（出现多次＝突变不等价）。
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PY = str(ROOT / ".venv" / "bin" / "python")
TESTS = ["test/test_image_builder.py", "test/test_b0_workspace_fixture_matrix.py",
         "test/test_sandbox_spec.py"]

IMG = ROOT / "worker" / "image_builder.py"
SPEC = ROOT / "project" / "sandbox_spec.py"

MUTATIONS = [
    # ── X-C2：安装片段按 build_tool 分派 ──
    (
        "X-C2: java 分支退回只装 maven（Gradle 工程镜像没 gradle → 127 死循环）",
        IMG,
        '        _bt = (tc.build_tool or "").strip().lower()',
        '        _bt = "maven"   # 突变：退回无条件只装 maven（X-C2 的真病灶形态）',
        ["test_gradle_project_image_installs_gradle",
         "test_selftest_and_install_dispatch_on_the_same_build_tools",
         "test_mixed_maven_and_gradle_installs_both"],
    ),
    (
        "X-C2: build_tool 缺失时不再保守两个都装（探测不出即 127）",
        IMG,
        '            _pkgs = "maven gradle"',
        '            _pkgs = "maven"',
        ["test_java_without_build_tool_installs_both"],
    ),
    (
        "X-C2: maven 工程被搭上 gradle（JVM 基线镜像变胖/语义漂移）",
        IMG,
        '        elif _bt == "maven":\n            _pkgs = "maven"',
        '        elif False:\n            _pkgs = "maven"',
        ["test_maven_project_image_unchanged"],
    ),
    (
        "X-C2 配套: gradle warmup 整块删掉（--offline classes 必失败/运行时每次联网）",
        IMG,
        '        if any(t.name == "java" and (t.build_tool or "").lower() == "gradle"\n'
        "               for t in spec.toolchains):",
        '        if False:',
        ["test_gradle_warmup_present_and_wrapper_first"],
    ),
    (
        "X-C2 配套: gradle warmup 不再 wrapper 优先（工程钉的版本被绕过）",
        IMG,
        '                "RUN cd /workspace && ((test -x ./gradlew && ./gradlew --no-daemon classes 2>&1 | tail -5) "',
        '                "RUN cd /workspace && ((gradle --no-daemon classes 2>&1 | tail -5) "',
        ["test_gradle_warmup_present_and_wrapper_first"],
    ),
    (
        "X-C2 配套: gradle warmup 漏了离线自检（与 maven 侧不对称）",
        IMG,
        '                "|| (gradle --offline --no-daemon classes -q)) "',
        '                "|| true) "',
        ["test_gradle_warmup_present_and_wrapper_first"],
    ),
    # ★一条刻意不做的突变（诚实记账）★
    # "gradle warmup 在无源码时也注入"：非等价。整个 warmup 段在 `generate_dockerfile` 的
    # `if src_included:` 之内（image_builder.py:372），拧内层的 gradle 门根本影响不到它；
    # 而拧**外层**那道门会同时打掉 maven/npm warmup（已由 `test_node_warmup_skipped_without_src`
    # 锁住）。`test_gradle_warmup_skipped_without_src` 的保护实际来自那道共享门，
    # 本批不重复造锁——但它作为"新栈也守同一口径"的对账断言仍有价值，故测试保留。
    # ── N-1：_infer_npm 扫全部清单 ──
    (
        "N-1: _infer_npm 退回只读根清单（workspaces 根无 scripts → 不装 node → 127）",
        SPEC,
        "    scanned = ordered[:_NPM_SCAN_CAP]",
        "    scanned = ordered[:1]",
        ["test_n1_npm_toolchain_inferred_from_child_package_scripts",
         "test_sandbox_spec_infers_a_toolchain_for_every_stack"],
    ),
    (
        "N-1: 静态资源也装 node（st-10 治法被放宽掉 → 误派 npm 构建空转）",
        SPEC,
        "        return None  # 纯静态资源，无需 node 工具链（st-10 治法，刻意保留）",
        "        return Toolchain(name=\"node\", build_tool=\"npm\", dep_source=root_pkg)",
        ["test_n1_static_resources_still_get_no_node"],
    ),
    (
        "N-1: dep_source 不再指子目录（warmup cd 到工程根装错地方）",
        SPEC,
        "        dep = with_scripts[0]   # 已按深度排序 ⇒ 最浅的那个有脚本的清单",
        "        dep = root_pkg",
        ["test_n1_single_frontend_in_subdir_points_warmup_at_it"],
    ),
    (
        "N-1: workspaces 根不再优先（依赖提升到根，warmup 必须在根跑）",
        SPEC,
        "    if root_declares_workspaces or root_pkg in with_scripts:",
        "    if root_pkg in with_scripts:",
        ["test_n1_npm_toolchain_inferred_from_child_package_scripts"],
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
