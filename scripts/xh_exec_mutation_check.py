#!/usr/bin/env python3
"""X-H2/H5/H6/H7/H8 突变 harness（判据与前三批同源，那些自伤一开始就带上）：

  · **先验基线全绿**；· **逐条**跑 should_red，每条都必须红；
  · `rc=5`（`-k` 选不到，如测试被重命名）**判失败**；· 落点唯一性检查。
"""
from __future__ import annotations

import ast
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PY = str(ROOT / ".venv" / "bin" / "python")
TESTS = ["test/test_xh_exec_stack_gates.py", "test/test_intent_harness_matrix.py",
         "test/test_mixed_stack_planning.py", "test/test_l1_pipeline.py",
         "test/test_w24_test_cmd_priority.py"]

PIPE = ROOT / "worker" / "l1_pipeline.py"
SET = ROOT / "config" / "settings.py"
SPE = ROOT / "stacks" / "spec.py"
DRV = ROOT / "worker" / "stack_drivers.py"

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
        "    _base = _norm_rel(tool.rsplit(\"/\", 1)[-1])",
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
        # ★#29-3 T-1：机制被**重构**，落点必须对新单一事实源重写★（W-24 二次重写：写死
        # 元组又改成了 `test_drivers_by_priority()` 派生遍历）。原意图逐字保留：go 不在
        # 遍历集里 ⇒ go 工程零测试面。
        "X-H2: go 工程级兜底删掉（无测试文件的工程零测试面）",
        PIPE,
        '    for drv in _test_drivers_by_priority():\n',
        '    for drv in (d for d in _test_drivers_by_priority() if d.stack_key != "go"):  # 突变\n',
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
    # ★W-24（#29-5 挂账）后本条**复活**★ 撤销原因（两处独立编码 ⇒ 单点突变压不动）已随
    # W-24 治愈：写死元组删除，栈遍历改从 `test_drivers_by_priority()` 派生。★复活首跑
    # 实测仍绿 ⇒ 逮到第三处编码★：`_ext_for_lang` 手写小表没有 "java"，maven 拿到
    # test_cmd 也过不了语言守卫——已一并改为从 STACK_SPEC.source_exts 派生。此后「JVM
    # 刻意不猜」只剩 `STACK_SPEC["maven"].test_cmd=""` **一处**承重，单点突变「给它赋值」
    # ⇒ maven 进 TEST_DRIVERS ⇒ 两条 JVM 锁必须恰红。
    # （历史撤销记录：#29-3 T-1 曾试过 ①元组加 maven→driver None→continue；②给
    # test_cmd 赋值→写死元组从不问。两落点都实测仍绿，根因=冗余编码互相兜底，非测试没牙。）
    (
        "X-H2: JVM 不再刻意不猜（maven.test_cmd 赋值 → pom 工程被强猜 mvn test，绕过 S1 决定）",
        SPE,
        '        whole_project_build_cmd="mvn -q -DskipTests compile",\n'
        '        test_cmd="",\n',
        '        whole_project_build_cmd="mvn -q -DskipTests compile",\n'
        '        test_cmd="mvn -q test",\n',
        ["test_xh2_jvm_deliberately_not_guessed",
         "test_jvm_still_deliberately_not_guessed"],
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
        '    if ext(".py") and (build in ("pip", "poetry", "uv", "python")\n                       or has("pyproject.toml", "setup.py", "requirements.txt", "Pipfile")):',
        "    if False:",
        ["test_xh1_derive_covers_every_stack"],
    ),
    (
        "N-2: go.work 不再优先（多模块仓锚错目录）",
        PIPE,
        '        if has("go.work"):',
        "        if False:",
        ["test_xh1_derive_covers_every_stack"],
    ),
    (
        "X-H1: npm 的 .js/.vue 分支删掉（前端整类零构建闸）",
        PIPE,
        '        if _npm_has_build_script(project_path):',
        "        if False:",
        ["test_xh1_derive_covers_every_stack"],
    ),
    (
        "X-H1: C#/Elixir/Dart 分支删掉",
        PIPE,
        '    if ext(".cs") and has("*.csproj", "*.sln"):',
        "    if False:",
        ["test_xh1_derive_covers_every_stack"],
    ),
    (
        "X-H1: 命令不再锚到清单目录（跨栈污染复发：根下跑 go build）",
        PIPE,
        '            return ""\n        return f"cd {shlex.quote(d)} && {cmd}"',
        '            return ""\n        return cmd',
        ["test_xh1_cross_stack_pollution_anchors_to_manifest_dir"],
    ),
    (
        "X-H1: 目录名白名单被拆（外部输入拼进 shell）",
        PIPE,
        '            return cmd\n        if not _SAFE_REL_DIR_RE.match(d):\n            # 目录名来自工程树（外部输入）→ 形态不安全就不拼进命令（S-5 同源判据）',
        '            return cmd\n        if False:\n            # 目录名来自工程树（外部输入）→ 形态不安全就不拼进命令（S-5 同源判据）',
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
    # ── reviewer 复核整改新增 ──
    (
        'C-1: PHP/Ruby 退回 git ls-files（必然假过：非 git 仓 + 零参数读 stdin）',
        PIPE,
        '        return _per_file("ruby -c", (".rb",))',
        '        return at(("Gemfile",), "ruby -c $(git ls-files \'*.rb\' | head -200)")',
        ['test_c1_php_ruby_gate_actually_checks_every_modified_file'],
    ),
    (
        'C-1: 逐文件循环退回一次性多参（ruby -c 只查第一个）',
        PIPE,
        '        return f"for f in {quoted}; do {cmd} \\"$f\\" || exit 1; done"',
        '        return f"{cmd} {quoted}"',
        ['test_c1_php_ruby_gate_actually_checks_every_modified_file'],
    ),
    (
        # ★#29-3 T-1 落点重写（同 d76f954 重构）★ 原落点是 python 臂自己的 `any(.py)` 守卫；
        # 重构后语言守卫成了循环内**通用**的一条（`_ext_for_lang(drv.lang)`）。拆掉它 ⇒ 任何
        # 深度≤3 的 `pyproject.toml` 劫持所有栈（原 C-2 缺陷），意图逐字保留。
        'C-2: 测试兜底的语言守卫被拆（嵌套 pyproject 劫持所有栈）',
        PIPE,
        '        if not any(f.endswith(_ext_for_lang(drv.lang)) for f in mods):\n            continue\n',
        '        if False:  # 突变：语言守卫拆掉\n            continue\n',
        ['test_c2_nested_pyproject_does_not_hijack_other_stacks'],
    ),
    (
        'H-3: .ts 臂又 return None（掐死后续兜底 → 跳过即通过）',
        PIPE,
        '                        return "npm test --silent"\n                    break',
        '                        return "npm test --silent"\n                    return None',
        ['test_h3_ts_arm_does_not_kill_later_fallbacks'],
    ),
    (
        "H-4: 复合写法不搜族键本身（'Java 17' 归不出族 → 拿不到 JDK 镜像）",
        SET,
        '            for a in (fam, *aliases):',
        '            for a in aliases:',
        ['test_h4_composite_form_matches_family_key_itself'],
    ),
    # ★M-2 的突变随决定 1 删除★ 病灶（整树 compileall 钻进 .venv）与治法（`-x` 排除表）
    # 都已被『只编译改动文件』取代 —— 排除表整个不存在，无从突变。现由
    # `test_m2_per_file_needs_no_exclusion_table` 正向钉住『别退回整树』。
    (
        'M-3: _manifest_present 不再排除依赖树（两探针分叉 → at() 退回根 → 127）',
        PIPE,
        '        return not any(seg in _SRC_EXCLUDE_DIRS_FOR_DERIVE for seg in rel.parts)',
        '        return True',
        ['test_m3_both_probes_exclude_dependency_trees'],
    ),
    (
        'M-7: 工具词元退回 tokens[0]（cd/sh 前缀绕过清单闸）',
        PIPE,
        '    tool = _effective_tool_token(tokens)',
        '    tool = tokens[0]',
        ['test_m7_shell_wrapper_prefix_does_not_bypass_gate'],
    ),
    (
        "M-4: 清单探测深度上界收窄到 2（深度 3 的清单漏判，与沙箱 maxdepth 3 不一致）",
        PIPE,
        '        for _d in range(1, 3):                # 深度 2..3，与沙箱 maxdepth 3 对齐',
        '        for _d in range(1, 2):                # 深度 2..3，与沙箱 maxdepth 3 对齐',
        ["test_xh1_manifest_present_local_matches_sandbox_semantics"],
    ),
    # ── hunter 复核整改新增 ──
    (
        'CRITICAL-1: 锚点退回全树最短清单（编译无关子树、rc=0 静默假过）',
        PIPE,
        '        d = _manifest_dir_for(mods, names, project_path, evidence=_ev)',
        '        d = _manifest_dir(names, project_path)',
        # python 走决定 1 的逐文件形态、不再锚定 ⇒ 只有 go 那条真在测锚点
        ['test_hc1_go_anchor_follows_the_changed_module'],
    ),
    (
        'CRITICAL-1: 改动落在锚点外时不再退根（闸覆盖不到改动文件）',
        PIPE,
        '        if _ev.get("uncovered"):',
        '        if False:',
        ['test_hc1_uncovered_changes_fall_back_to_root'],
    ),
    (
        'CRITICAL-1: 跨多清单时硬挑一个（静默只覆盖一半）',
        PIPE,
        '        return ""                              # 跨多个清单 → 根级（并已落 spans 账）',
        '        return sorted(dirs)[0]',
        ['test_hc1_changes_spanning_two_manifests_fall_back_to_root'],
    ),
    (
        # ★#29-3 T-1 落点重写（同 d76f954 重构；W-24 二次重写：return 改为候选收集）★
        # 命令字面量已不在本函数里（改从 driver 取），锚定这一步现在是候选收集处的
        # `_anchor(...)` 调用。
        'CRITICAL-2: 测试兜底不再锚定（根级 pytest → rc=5 → sticky 硬 FAIL）',
        PIPE,
        '        candidates.append((drv.stack_key, _anchor(drv.anchor_manifests, drv.test_cmd)))\n',
        '        candidates.append((drv.stack_key, drv.test_cmd))  # 突变：不锚定\n',
        ['test_hc2_test_fallback_is_anchored'],
    ),
    (
        'CRITICAL-2: rc=5 又被判成失败',
        PIPE,
        '        if (not test_ok) and t_ec == 5 and "pytest" in (test_cmd or ""):',
        '        if False:',
        ['test_hc2_pytest_no_tests_collected_is_not_a_failure'],
    ),
    (
        "HIGH-1: 新增判死面的 infra 标记被删（cargo offline/缺 chromium 判成代码错）",
        PIPE,
        '    "but --offline was specified",      # cargo --offline 在冷沙箱（未预热）必失败',
        '    "__never_matches_offline__",',
        ["test_h1_new_death_surface_classified_as_infra"],
    ),
    (
        'HIGH-2: 沙箱 find 不再剪依赖树（node_modules 里的清单算数 → cd 进依赖树）',
        PIPE,
        '_FIND_PRUNE = "\\\\( -name node_modules -o -name vendor -o -name third_party -o -name target -o -name build -o -name dist -o -name .git -o -name .venv -o -name venv -o -name __pycache__ -o -name .tox -o -name site-packages -o -name .mypy_cache -o -name .pytest_cache -o -name .eggs \\\\) -prune -o "',
        '_FIND_PRUNE = ""',
        ['test_h2_sandbox_find_prunes_dependency_trees'],
    ),
    (
        'HIGH-3: _build_cmd_applicable 本地兜底又自成一套（无深度上限无排除）',
        PIPE,
        '    return _manifest_present(tuple(manifests), project_path)',
        '    return any(any(Path(project_path).rglob(m)) for m in manifests)',
        ['test_h3_build_cmd_applicable_shares_one_implementation'],
    ),
    (
        'MED-1: 循环头不再跳过（返回循环变量 f → 刷假 WARNING）',
        PIPE,
        '        if base in ("for", "while", "until", "select"):',
        '        if False:',
        ['test_med123_effective_tool_token'],
    ),
    (
        "MED-2: 又'宁选表里有的词元'（cd mvn && npm ci → tool=mvn → 跳过即通过）",
        PIPE,
        '        if skip_arg:',
        '        if skip_arg and t in _BUILD_TOOL_MANIFESTS:',
        ['test_med123_effective_tool_token'],
    ),
    (
        'MED-3: 不再按连接符切开（cd sub&&dotnet build → tool=build → 未知放行）',
        PIPE,
        '        raw = _re.sub(r"(&&|\\|\\||;|\\|)", r" \\1 ", raw)',
        '        pass',
        ['test_med123_effective_tool_token'],
    ),
    (
        '整改自伤: sh -c 的实参被当路径跳过（mvn 被跳 → 未知放行）',
        PIPE,
        '        if base in _CMD_EXEC_PREFIX:',
        '        if base in _CMD_PATH_ARG_PREFIX:',
        ['test_med123_effective_tool_token'],
    ),
    (
        "整改自伤: node 错误码退回裸子串（enotfound 命中 ModuleNotFoundError → X-C3 归因失效）",
        PIPE,
        '    "econnrefused\\n", "getaddrinfo enotfound", "eai_again",',
        '    "econnrefused", "enotfound", "eai_again",',
        ["test_infra_markers_do_not_swallow_missing_internal_symbols"],
    ),
    # ── 用户拍板的四条决定 ──
    (
        "决定 1：python 闸退回整树 compileall（linter 仓的坏语法夹具永久判死）",
        PIPE,
        '        return _per_file("python3 -m compileall -q", (".py",))',
        '        return "python3 -m compileall -q ."',
        ["test_d1_python_gate_only_compiles_changed_files"],
    ),
    # ── W-24（#29-5 挂账）新增：派生遍历/显式优先序/候选机读键 三机制的反向锁 ──
    (
        "W-24: 优先序被改（python 沉底 → 多栈仓胜出者易主=未申报的行为变更）",
        SPE,
        '        test_priority=10,\n',
        '        test_priority=50,\n',
        ["test_priority_matches_legacy_tuple_order"],
    ),
    (
        "W-24: 遍历又写死四栈元组（新栈加 test_cmd 进不了循环=该栈测试闸静默零覆盖）",
        DRV,
        '    return sorted(TEST_DRIVERS.values(), key=lambda d: (d.test_priority, d.stack_key))',
        '    return sorted((TEST_DRIVERS[k] for k in ("python", "go", "cargo", "npm")),'
        ' key=lambda d: (d.test_priority, d.stack_key))  # 突变：回写死',
        ["test_new_stack_with_test_cmd_enters_iteration"],
    ),
    (
        "W-24: 多栈候选不落机读键（「没测」与「测过」又不可分，硬检查④复发）",
        PIPE,
        '    if len(candidates) > 1:\n'
        '        details["test_cmd_candidates"] = list(candidates)',
        '    if False:\n'
        '        details["test_cmd_candidates"] = list(candidates)',
        ["test_note_helper_writes_key_only_when_multi"],
    ),
]


def _pytest(args: list[str]) -> int:
    p = subprocess.run([PY, "-m", "pytest", *TESTS, "-p", "no:warnings", "-q",
                        "--tb=no", *args], cwd=ROOT, capture_output=True, text=True)
    return p.returncode



def _clear_pyc(path: Path) -> None:
    """删被突变模块的 pyc（T-2，#29-3 统一补齐）。

    CPython 判 pyc 是否有效看的是源码 **mtime（整秒粒度）+ 字节数**。故当「等长突变
    （len(old)==len(new)）」与「同秒写入」同时成立时，第二条突变写完，pyc 仍被判有效
    ⇒ 子进程加载的是【上一条】的字节码。双向危害：既造"突变后仍绿"（冤报测试没牙），
    也造"红的是上一条"（假背书——这条锁其实没被验证）。
    每条突变前与还原后都必须清。
    """
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
        mutated = src.replace(old, new, 1)
        # ★复核 H-5 的治法★ 突变后的源码必须**仍能编译**。否则 pytest 拿到的是 collection
        # error，rc≠0 被本 harness 当成"该红的红了"—— 那什么也没证明（实测本批 6/22 条落点因
        # 丢了缩进而产生 IndentationError，整个 X-H1/N-2/N-4 组的"全锁"结论都是虚的）。
        # 判据放在这里而不是靠人肉检查：它是"突变落点与被测命题不等价"这类假绿的通用闸。
        try:
            ast.parse(mutated)
        except SyntaxError as _e:
            print(f"[{i}/{len(MUTATIONS)}] {name}\n"
                  f"    ✗ 突变后源码无法编译（{_e.msg} @line {_e.lineno}）⇒ pytest 只会报 "
                  f"collection error，rc≠0 是假信号。落点多半丢了缩进/截断了多行语句。")
            failures.append((name, "突变产生语法错"))
            continue
        path.write_text(mutated)
        _clear_pyc(path)
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
