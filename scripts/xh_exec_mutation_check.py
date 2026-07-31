#!/usr/bin/env python3
"""X-H2/H5/H6/H7/H8 突变 harness（判据与前三批同源，那些自伤一开始就带上）：

  · **先验基线全绿**；· **逐条**跑 should_red，每条都必须红；
  · `rc=5`（`-k` 选不到，如测试被重命名）**判失败**；· 落点唯一性检查。
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PY = str(ROOT / ".venv" / "bin" / "python")
TESTS = ["test/test_xh_exec_stack_gates.py", "test/test_intent_harness_matrix.py",
         "test/test_mixed_stack_planning.py", "test/test_l1_pipeline.py"]

PIPE = ROOT / "worker" / "l1_pipeline.py"
SET = ROOT / "config" / "settings.py"

MUTATIONS = [
    # ── X-H6 ──
    (
        "X-H6: 未知工具又一律放行（dotnet/sbt 空项目 applicable=True → 127）",
        PIPE,
        '    "dotnet": ("*.csproj", "*.sln", "*.fsproj"),',
        "    # (dotnet removed)",
        ["test_xh6_known_tool_rejected_without_manifest_accepted_with"],
    ),
    (
        "X-H6: wrapper 形态漏出表（./mvnw 整类工程放行明知必失败的命令）",
        PIPE,
        '    "./mvnw": ("pom.xml",),',
        "    # (mvnw removed)",
        ["test_xh6_known_tool_rejected_without_manifest_accepted_with"],
    ),
    (
        "X-H6: 未知工具静默放行（表缺项无人察觉）",
        PIPE,
        "        if _base not in _NO_MANIFEST_TOOLS:",
        "        if False:",
        ["test_xh6_unknown_tool_passes_but_warns"],
    ),
    (
        "X-H6: 白名单被去掉 → 正常路径刷 WARNING（真信号被噪声埋掉）",
        PIPE,
        "    _base = tool.rsplit(\"/\", 1)[-1].lstrip(\"./\")",
        "    _base = \"__never_in_whitelist__\"",
        ["test_xh6_known_no_manifest_tool_does_not_warn"],
    ),
    # ── X-H2 ──
    (
        "X-H2: _guess_test_cmd 退回只认 .py（其余栈跳过＝通过）",
        PIPE,
        '        elif fp.endswith(".go"):',
        "        elif False:",
        ["test_xh2_guess_test_cmd_covers_every_stack"],
    ),
    (
        "X-H2: go/rust 工程级兜底删掉（无测试文件的工程零测试面）",
        PIPE,
        '    if _manifest_present(("go.mod",), project_path) and any(f.endswith(".go") for f in mods):',
        "    if False:",
        ["test_xh2_guess_test_cmd_covers_every_stack"],
    ),
    (
        "X-H2: 不再查 scripts.test（npm test 报 Missing script → 把没测试误判成测试失败）",
        PIPE,
        '    t = str(scripts.get("test") or "").strip()\n'
        '    return bool(t) and "no test specified" not in t',
        "    return True",
        ["test_xh2_never_invents_npm_test_without_script",
         "test_xh2_rejects_npm_init_placeholder_test_script"],
    ),
    (
        "X-H2: 占位 test 脚本不再排除（每个 npm 工程都被判测试失败）",
        PIPE,
        '    return bool(t) and "no test specified" not in t',
        "    return bool(t)",
        ["test_xh2_rejects_npm_init_placeholder_test_script"],
    ),
    (
        "X-H2: JVM 也去猜 mvn test（绕过 S1 的刻意留空，打在唯一跑过 E2E 的栈上）",
        PIPE,
        '    if _manifest_present(("pyproject.toml",), project_path):\n'
        '        return "python -m pytest -q --maxfail=1"',
        '    if _manifest_present(("pom.xml",), project_path):\n'
        '        return "mvn -q test"\n'
        '    if _manifest_present(("pyproject.toml",), project_path):\n'
        '        return "python -m pytest -q --maxfail=1"',
        ["test_xh2_jvm_deliberately_not_guessed"],
    ),
    (
        "X-H2: scoped 探测退回 _manifest_present（子目录测试文件全漏 + 越界不拒）",
        PIPE,
        '    r = str(rel or "").replace("\\\\", "/").lstrip("/")\n'
        '    if not r or ".." in r.split("/"):\n'
        "        return False",
        '    r = str(rel or "").replace("\\\\", "/").lstrip("/")\n'
        "    if not r:\n        return False",
        ["test_xh2_project_file_exists_is_path_exact"],
    ),
    # ── X-H7 ──
    (
        "X-H7: 语言不再归一（Kotlin/Scala 拿不到带 JDK 的镜像）",
        SET,
        "        lang = (language or \"\").strip().lower()\n"
        "        if not lang:\n"
        '            return ""',
        "        lang = (language or \"\").strip().lower()\n"
        "        if True:\n"
        '            return ""',
        ["test_xh7_kotlin_gets_the_same_image_as_java"],
    ),
    (
        "X-H7: java 族别名表被清空（kotlin/scala 归不到 java）",
        SET,
        '    "java": ("kotlin", "kt", "scala", "groovy", "jvm", "maven", "gradle",',
        '    "java": (',
        ["test_xh7_free_text_language_maps_to_toolchain_family"],
    ),
    (
        "X-H7: 未知语言硬塞一个族（csharp 拿到 JDK 镜像却没有 dotnet）",
        SET,
        "        return \"\"\n\n    def template_for_language",
        "        return \"java\"\n\n    def template_for_language",
        ["test_xh7_unknown_language_falls_back_honestly"],
    ),
    # ── X-H8 ──
    (
        "X-H8: java 的 file_signal 退回写死 True（非 JVM 工程每轮联网打 Maven Central）",
        PIPE,
        "    if eligible(\"java\", _jvm_signal):",
        '    if eligible("java", True):',
        # 只锁**接线**那条：`..._requires_real_evidence` 只测正则＝证实现，
        # 把常量改回 True 它照旧绿（零区分力，突变如实报出）。
        ["test_xh8_java_repair_family_not_invoked_on_non_jvm_build"],
    ),
    (
        "X-H8: JVM 判据放宽到任意路径（非 JVM 输出也命中 → 修复族又被无条件唤起）",
        PIPE,
        r'_JVM_SRC_IN_TEXT_RE = re.compile(r"[\w./\\-]+\.(?:java|kt|scala)\b")',
        r'_JVM_SRC_IN_TEXT_RE = re.compile(r"[\w./\\-]+\.\w+\b")',
        ["test_xh8_jvm_repair_signal_requires_real_evidence"],
    ),
    # ── X-H1 / N-4 / N-2 ──
    (
        "N-4: python 分支删掉（python 工程零构建闸）",
        PIPE,
        'if ext(".py") and (build in ("pip", "poetry", "uv", "python")',
        "    if False:",
        ["test_xh1_derive_covers_every_stack"],
    ),
    (
        "N-2: go.work 不再优先（多模块仓锚错目录）",
        PIPE,
        'if has("go.work"):',
        "        if False:",
        ["test_xh1_derive_covers_every_stack"],
    ),
    (
        "X-H1: npm 的 .js/.vue 分支删掉（前端整类零构建闸）",
        PIPE,
        'if _npm_has_build_script(project_path):',
        "        if False:",
        ["test_xh1_derive_covers_every_stack"],
    ),
    (
        "X-H1: C#/Elixir/Dart 分支删掉",
        PIPE,
        'if ext(".cs") and has("*.csproj", "*.sln"):',
        "    if False:",
        ["test_xh1_derive_covers_every_stack"],
    ),
    (
        "X-H1: 命令不再锚到清单目录（跨栈污染复发：根下跑 go build）",
        PIPE,
        'return f"cd {shlex.quote(d)} && {cmd}"',
        "        return cmd",
        ["test_xh1_cross_stack_pollution_anchors_to_manifest_dir"],
    ),
    (
        "X-H1: 目录名白名单被拆（外部输入拼进 shell）",
        PIPE,
        'if not _SAFE_REL_DIR_RE.match(d):',
        "        if False:",
        ["test_xh1_unsafe_manifest_dir_is_refused"],
    ),
    (
        "X-H1: _manifest_present 本地兜底退回只看工程根 + 不 glob（本地绿而生产另一套）",
        PIPE,
        "    _root = Path(project_path)",
        "    return any(os.path.isfile(os.path.join(project_path, m)) for m in manifests)\n"
        "    _root = Path(project_path)",
        ["test_xh1_manifest_present_local_matches_sandbox_semantics"],
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
