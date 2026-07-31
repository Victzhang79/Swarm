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
PIPE = ROOT / "worker" / "l1_pipeline.py"

MUTATIONS = [
    # ── X-C2：安装/自测同读 registry ──
    (
        "X-C2: java 分支退回无条件只装 maven（Gradle 工程镜像没 gradle → 127 死循环）",
        IMG,
        '        _want_gradle = _bt in ("gradle", "")',
        "        _want_gradle = False",
        ["test_gradle_project_image_installs_gradle",
         "test_selftest_and_install_read_the_same_registry",
         "test_mixed_maven_and_gradle_installs_both"],
    ),
    (
        "X-C2: build_tool 缺失时不再保守两个都装（探测不出即 127）",
        IMG,
        '        _want_maven = _bt in ("maven", "")',
        '        _want_maven = _bt == "maven"',
        # 不含 test_undetermined_java_still_gets_warmup_and_settings：`_has_build_tool` 仍把
        # 未定 build_tool 的 java 当"两个都有"⇒ warmup/settings 照旧注入，它看不见这处突变。
        ["test_java_without_build_tool_installs_both"],
    ),
    (
        "X-C2 复核 H-3: 退回用 apt 的 gradle（Debian 4.x 跑不了 Java 17＝装了但不可用）",
        IMG,
        'f"ENV GRADLE_VERSION={_GRADLE_DEFAULT}\\n"',
        '                f"# apt gradle (mutated)\\n"',
        ["test_gradle_project_image_installs_gradle"],
    ),
    (
        "X-C2 复核 H-2: 去掉 gradle 镜像源（构建机网络受限 → warmup 静默空转）",
        IMG,
        '"COPY warmup/init.gradle /root/.gradle/init.gradle\\n"',
        '                "# no init.gradle (mutated)\\n"',
        ["test_gradle_project_image_installs_gradle",
         "test_gradle_init_script_uploaded_when_gradle_present"],
    ),
    (
        "X-C2 复核 H-4: 自测命令改回手写分派（两张表又分叉）",
        IMG,
        "        entry = stack_entry(tc.name, tc.build_tool)",
        '        entry = stack_entry(tc.name, "maven") if tc.name == "java" else stack_entry(tc.name, tc.build_tool)',
        ["test_selftest_and_install_read_the_same_registry",
         "test_new_build_tool_in_registry_is_installed_and_selftested"],
    ),
    (
        "X-C2 复核 C-1: _BUILDER_VERSION 不递增（复用老镜像 → 修复不落地）",
        IMG,
        '_BUILDER_VERSION = "8"',
        '_BUILDER_VERSION = "7"',
        ["test_builder_version_bumped_so_old_images_are_invalidated"],
    ),
    (
        "X-C2 复核 C-2: wrapper jar 又被 tarball 剥掉（./gradlew 必 ClassNotFound）",
        IMG,
        'return any(p == s or p.endswith("/" + s) for s in _SRC_KEEP_PATH_SUFFIXES)',
        "    return False",
        ["test_wrapper_jars_survive_source_tarball"],
    ),
    (
        "X-C2 复核 C-2: _is_wrapper_jar 退回 lstrip('./')（.mvn 被剥成 mvn ⇒ mvnw 仍崩）",
        IMG,
        '    p = str(rel_path or "").replace("\\\\", "/")\n    while p.startswith("./"):\n        p = p[2:]\n    p = p.lstrip("/")',
        '    p = str(rel_path or "").replace("\\", "/").lstrip("./")',
        ["test_wrapper_jars_survive_source_tarball"],
    ),
    (
        "X-C2 复核 H-1: 判成败的臂后面又接管道（兜底臂成死代码，且静默）",
        IMG,
        'f"RUN cd /workspace && ((test -x ./gradlew && ./gradlew --no-daemon classes "\n'
        '                f"> {_log} 2>&1) || (gradle --no-daemon classes > {_log} 2>&1) "',
        'f"RUN cd /workspace && ((test -x ./gradlew && ./gradlew --no-daemon classes "\n'
        '                f"2>&1 | tail -5) || (gradle --no-daemon classes 2>&1 | tail -5) "',
        ["test_gradle_warmup_present_and_wrapper_first"],
    ),
    (
        "X-C2 复核 H-5: gradle warmup 不清 build/（root 所有 → 非 root 编译 Permission denied）",
        IMG,
        '                "; find /workspace -type d -name build -prune -exec rm -rf {} + 2>/dev/null "',
        '                "; true "',
        ["test_gradle_warmup_cleans_build_dir"],
    ),
    (
        "X-C2 复核 L-1: has_maven 退回精确 == 比较（同字段两种归一）",
        IMG,
        '        if not bt and (t.name or "").lower() == "java" and want in ("maven", "gradle"):',
        "        if False:",
        ["test_has_build_tool_single_normalization",
         "test_undetermined_java_still_gets_warmup_and_settings"],
    ),
    # ── hunter 复核整改新增 ──
    (
        "HIGH-1: .mvn 下 wrapper properties/config 又被剥（mvnw 读不到 distributionUrl）",
        IMG,
        '    ".mvn/wrapper/maven-wrapper.jar",\n'
        '    ".mvn/wrapper/maven-wrapper.properties",',
        '    ".mvn/wrapper/maven-wrapper.jar",',
        ["test_wrapper_jars_survive_source_tarball"],
    ),
    (
        "MED-3: 去掉构建期 `gradle -v` 硬闸（下载失败仍发布 → 运行时才 127）",
        IMG,
        '"RUN gradle -v\\n"',
        '                "# no build-time gradle check\\n"',
        ["test_gradle_build_time_verification"],
    ),
    (
        "MED-4: warmup 日志尾退回 5 行 + 去掉机读键",
        IMG,
        'f"; tail -40 {_log} 2>/dev/null || true")',
        'f"; tail -5 {_log} 2>/dev/null || true")',
        ["test_gradle_warmup_observability"],
    ),
    (
        "HIGH-3 配套: notes 又进指纹（诊断文本变化即触发多分钟重建）",
        SPEC,
        '{k: v for k, v in self.to_dict().items() if k not in ("notes", "project_id")},',
        "            self.to_dict(),",
        ["test_notes_do_not_affect_deps_hash"],
    ),
    (
        "HIGH-4: 深度上限 hint 又不排序（同树不同机结论不同）",
        SPEC,
        'for p in sorted(root.rglob("package.json")):',
        '        for p in root.rglob("package.json"):',
        ["test_depth_ceiling_hint_is_deterministic"],
    ),
    (
        "MED-2: 装 node 的正常路径又零留痕（分不清根有 build vs 只有子包有）",
        SPEC,
        '_note(notes, f"node 工具链据**子包**脚本判定（根 package.json 无 build/test/start）："',
        '        _note(notes, "")',
        ["test_child_package_decision_is_noted"],
    ),
    (
        "CRITICAL-1: 判据退回『harness 没给才 derive』（gradle 工程零构建闸）",
        PIPE,
        'if not build_cmd or not _build_cmd_applicable(build_cmd, project_path):',
        "    if not build_cmd:",
        ["test_harness_maven_command_overridden_on_gradle_project"],
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
