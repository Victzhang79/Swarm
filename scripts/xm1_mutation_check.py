#!/usr/bin/env python3
"""X-M1（非 Maven 验收命令归一栈驱动层）突变 harness（判据与 b7/xh3 harness 同源）：

  · 先验基线全绿；· 逐条跑 should_red；· rc=5 判失败；· 落点唯一性；
  · 突变后 ast.parse；· 绝不进超时循环、绝不与全量并发；· 突变/还原后清 pyc。

★锁的命题★：gradle 驱动接线 · gradle 注册表证据（非注册目录绝不猜工程路径）·
gradle 纯任务名守卫（选项不臆改）· npm 根 script 检查（根有就不改）·
npm 锚点包 script 检查（锚点没有就不改）· go 根 go.mod 守卫（单模块不动）·
入口真分派（机制存在≠接线覆盖）。
"""
from __future__ import annotations

import ast
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PY = str(ROOT / ".venv" / "bin" / "python")
TESTS = ["test/test_xm1_verify_normalize.py",
         "test/test_r65e8_t1_verify_reactor_scope.py"]

VD = ROOT / "worker" / "l1_verify_drivers.py"
L1 = ROOT / "worker" / "l1_pipeline.py"

MUTATIONS = [
    (
        "XM1-a：gradle 驱动摘出注册表（cd+wrapper 127 假阴性回归=机制存在但接线缺席）",
        VD,
        "    _GradleCdWrapperDriver(),\n",
        "",
        ["test_gradle_cd_wrapper_rewritten", "test_driver_registry_is_single_source"],
    ),
    (
        "XM1-b：gradle 注册表证据摘除（非注册目录也猜工程路径改写=血规 2 复发，"
        "独立工程被改烂）",
        VD,
        "        proj_token = next(\n"
        "            (tok for tok, probe in probes if probe == d), None)",
        "        proj_token = d  # 突变：非注册目录照猜",
        ["test_gradle_untouched_shapes"],
    ),
    (
        "XM1-c：npm 根 script 检查摘除（根有的 script 也被 --prefix 改写="
        "把能跑的命令改成语义不同的命令）",
        VD,
        "        if script in root_scripts:\n"
        "            return None  # 根本来就能跑 → 原样",
        "        if False:  # 突变：根有 script 也改写\n"
        "            return None",
        ["test_npm_untouched_shapes"],
    ),
    (
        "XM1-d：npm 锚点包 script 检查摘除（锚点包没有的 script 也 --prefix 过去="
        "Missing script 假阴性换目录复发）",
        VD,
        "        if not member_scripts or script not in member_scripts:",
        "        if False:  # 突变：锚点包没有也改写",
        ["test_npm_bare_script_rewritten"],
    ),
    (
        "XM1-e：go 根 go.mod 守卫摘除（根是模块的单模块工程也被 cd 进子模块="
        "覆盖面从全工程缩到一个模块）",
        VD,
        '        if io.file_exists("go.mod", project_path):',
        '        if False:  # 突变：根有 go.mod 也改写',
        ["test_go_root_has_gomod_untouched"],
    ),
    (
        "XM1-f：gradle 纯任务名守卫摘除（带选项的命令被挂 :proj: 前缀="
        "./gradlew :app:-q 语法错误）",
        VD,
        "        if not tasks or any(not _GRADLE_TASK_RE.match(t) for t in tasks):",
        "        if not tasks:  # 突变：选项也挂前缀",
        ["test_gradle_untouched_shapes"],
    ),
    (
        "XM1-g：入口分派摘除（非 Maven 命令直接原样=驱动层整模块死代码，"
        "硬检查①接线覆盖）",
        L1,
        "    if not is_maven_family_command(command):\n"
        "        # X-M1（R1 hunter F1）：非 Maven 系直进驱动层",
        "    if False:  # 突变：分派摘除\n"
        "        # X-M1（R1 hunter F1）：非 Maven 系直进驱动层",
        ["test_entry_dispatches_non_maven_to_drivers",
         "test_entry_really_delegates_to_driver_registry"],
    ),
    # ── R1 双复核整改锁（XM1-h/i）──────────────────────────────────────────
    (
        "XM1-h：go 取值 flag 消费摘除（-run/-timeout 等生产常见形态回落原样="
        "reviewer F-1 复发：go 栈归一覆盖大面积失效）",
        VD,
        "        if t in _GO_VALUE_FLAGS:\n"
        "            i += 2",
        "        if False:  # 突变：取值 flag 不消费\n"
        "            i += 2",
        ["test_go_value_flags_rewritten_verbatim"],
    ),
    (
        "XM1-i：go 未知 bare flag 放行（fail-closed 反转：取值 flag 被当布尔→"
        "它的值被当包模式错位=臆改命令）",
        VD,
        "        return False      # 未知 bare flag → fail-closed",
        "        i += 1  # 突变：未知 bare flag 当布尔放行",
        ["test_go_unidentifiable_flags_untouched"],
    ),
    # ── R2 复核整改锁（XM1-j）──────────────────────────────────────────────
    (
        "XM1-j：shlex 分词退化回 str.split（带引号空格取值被切碎→"
        "fail-closed 漏改=reviewer F-5 复发）",
        VD,
        "            tokens = shlex.split(m.group(\"tail\"))",
        "            tokens = m.group(\"tail\").split()  # 突变：引号不识",
        ["test_go_quoted_value_flags_rewritten_verbatim"],
    ),
]


def _pytest(args: list[str]) -> int:
    p = subprocess.run([PY, "-m", "pytest", *TESTS, "-p", "no:warnings", "-q",
                        "--tb=no", *args], cwd=ROOT, capture_output=True, text=True)
    return p.returncode


def _clear_pyc(path: Path) -> None:
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
