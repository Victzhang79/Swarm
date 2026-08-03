#!/usr/bin/env python3
"""P-H1 突变 harness（判据与前批同源）：

  · **先验基线全绿**；· **逐条**跑 should_red；· `rc=5` **判失败**；
  · 落点唯一性检查；· 突变后源码必须仍能 `ast.parse`；
  · harness 改磁盘源码——**绝不进带超时的循环、绝不与全量并发、跑完看 git status**。

★锁的命题★ P-H1（27 号文 B-6）：npm/go/python 拿到与 JVM 同等级的接地硬约束——
npm 的 ESM/CJS（写错=运行时崩）、go 的 module 前缀与版本（臆造=编译期崩）、python 的
顶层包根与 requires-python（臆造=ModuleNotFoundError）。突变分四类压：
探测被摘 / 判据被换 / WARNING 降级（硬检查④）/ 接线断开（attach 改没、渲染块摘除）。
"""
from __future__ import annotations

import ast
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PY = str(ROOT / ".venv" / "bin" / "python")
TESTS = ["test/test_ph1_nonjvm_grounding.py"]

SD = ROOT / "brain" / "stack_detect.py"

MUTATIONS = [
    (
        "P-H1a：npm 探测器整层被摘（机制造对了却没接——npm 工程回到零硬约束）",
        SD,
        '    t = data.get("type")',
        '    return None  # 突变：探测器失效\n    t = data.get("type")',
        ["test_npm_esm_facts_and_render",
         "test_npm_cjs_default_vs_explicit",
         "test_wiring_npm_profile_reaches_render_via_real_caller"],
    ),
    (
        "P-H1b：ESM/CJS 判定翻转（esm 工程被钉成 cjs——硬约束指反方向，比没有更坏）",
        SD,
        '"module_system": "esm" if t == "module" else "cjs",',
        '"module_system": "cjs" if t == "module" else "esm",',
        ["test_npm_esm_facts_and_render", "test_npm_cjs_default_vs_explicit"],
    ),
    (
        "P-H1c：来源标注翻转（显式声明与 npm 缺省推断不可辨——渲染措辞随之说谎）",
        SD,
        '"module_system_source": "explicit" if t in ("module", "commonjs") else "default",',
        '"module_system_source": "default" if t in ("module", "commonjs") else "explicit",',
        ["test_npm_cjs_default_vs_explicit"],
    ),
    (
        "P-H1d：package.json 解析失败 WARNING 降 DEBUG（自吞=「失败」与「真没有」不可辨）",
        SD,
        'logger.warning("[STACK_DETECT] P-H1 根 package.json 解析失败（%s），npm 接地事实缺席", root_pj)',
        'logger.debug("[STACK_DETECT] P-H1 根 package.json 解析失败（%s），npm 接地事实缺席", root_pj)',
        ["test_npm_malformed_json_warns_not_silent"],
    ),
    (
        "P-H1e：go 探测器整层被摘（module 前缀/版本全丢，臆造 github.com 假路径无闸）",
        SD,
        "    m = _GO_MODULE_LINE.search(text)",
        "    return None  # 突变：探测器失效\n    m = _GO_MODULE_LINE.search(text)",
        ["test_go_facts_case_preserved_and_render",
         "test_wiring_go_profile_reaches_render_via_real_caller"],
    ),
    (
        "P-H1f：go 版本行正则锚错（^go → ^golang 恒不匹配——版本事实静默缺席）",
        SD,
        '_GO_VERSION_LINE = re.compile(r"(?m)^go\\s+([0-9]+(?:\\.[0-9]+){0,2})\\s*(?://.*)?$")',
        '_GO_VERSION_LINE = re.compile(r"(?m)^golang\\s+([0-9]+(?:\\.[0-9]+){0,2})\\s*(?://.*)?$")',
        ["test_go_facts_case_preserved_and_render",
         "test_wiring_go_profile_reaches_render_via_real_caller"],
    ),
    (
        "P-H1g：go.mod 读取失败 WARNING 降 DEBUG",
        SD,
        'logger.warning("[STACK_DETECT] P-H1 根 go.mod 读取失败（%s），go 接地事实缺席", root_mod)',
        'logger.debug("[STACK_DETECT] P-H1 根 go.mod 读取失败（%s），go 接地事实缺席", root_mod)',
        ["test_go_mod_unreadable_warns_not_silent"],
    ),
    (
        "P-H1h：__init__.py 判据被摘（任何目录都算 import 根——非包目录变假事实钉进画像）",
        SD,
        '            and os.path.isfile(os.path.join(e.path, "__init__.py"))',
        "",
        ["test_python_flat_layout_filters_non_packages"],
    ),
    (
        "P-H1i：skip 名单判据被摘（tests/ 等目录混进 import 根——worker 被引导 import 测试包）",
        SD,
        '            if e.is_dir() and not e.name.startswith(".") and e.name not in _PY_ROOT_SKIP_DIRS',
        '            if e.is_dir() and not e.name.startswith(".")',
        ["test_python_flat_layout_filters_non_packages"],
    ),
    (
        "P-H1j：src 布局位探测被摘（src 布局工程不再渲染「import 路径不含 src.」硬约束）",
        SD,
        "            src_layout = True",
        "            pass  # 突变：src_layout 探测失效",
        ["test_python_src_layout_facts_and_render"],
    ),
    (
        "P-H1k：pyproject 读取失败 WARNING 降 DEBUG",
        SD,
        'logger.warning("[STACK_DETECT] P-H1 pyproject.toml 读取失败（%s）", pj_path)',
        'logger.debug("[STACK_DETECT] P-H1 pyproject.toml 读取失败（%s）", pj_path)',
        ["test_python_pyproject_unreadable_warns"],
    ),
    (
        "P-H1l：包根枚举失败 WARNING 降 DEBUG（P-H3 同型：glob 吞 OSError 的教训就在这层）",
        SD,
        'logger.warning("[STACK_DETECT] P-H1 顶层包枚举失败（%s），python 包根事实不完整", base)',
        'logger.debug("[STACK_DETECT] P-H1 顶层包枚举失败（%s），python 包根事实不完整", base)',
        ["test_python_src_enum_failure_warns"],
    ),
    (
        "P-H1m：scandir 结果被换成空表（枚举静默归零=「失败」伪装成「真没有包」）",
        SD,
        "            entries = list(os.scandir(base))",
        "            entries = []",
        ["test_python_src_layout_facts_and_render",
         "test_python_src_enum_failure_warns"],
    ),
    (
        "P-H1n：渲染侧 npm 硬约束块被摘（画像有事实但 prompt 不渲染=机制存在接线断）",
        SD,
        '    if ms == "esm":',
        '    if False and ms == "esm":  # 突变：渲染块失效',
        ["test_npm_esm_facts_and_render",
         "test_wiring_npm_profile_reaches_render_via_real_caller"],
    ),
    (
        "P-H1o：渲染侧 go 硬约束块被摘",
        SD,
        '    if go_f.get("module_path"):',
        '    if False and go_f.get("module_path"):  # 突变：渲染块失效',
        ["test_go_facts_case_preserved_and_render",
         "test_wiring_go_profile_reaches_render_via_real_caller"],
    ),
    (
        "P-H1p：装配点 npm_facts attach 改没（探测/渲染全对，画像键恒空=整条链静默 no-op）",
        SD,
        '        "npm_facts": npm_facts or {},',
        '        "npm_facts": {},  # 突变：接线断开',
        ["test_wiring_npm_profile_reaches_render_via_real_caller"],
    ),
    (
        "P-H1q：装配点 go_facts attach 改没",
        SD,
        '        "go_facts": go_facts or {},',
        '        "go_facts": {},  # 突变：接线断开',
        ["test_wiring_go_profile_reaches_render_via_real_caller"],
    ),
    (
        "P-H1s：装配点 python_facts attach 改没（复核 REV-2：npm/go 两臂有锁、python 臂"
        "曾是静默 no-op 窗口）",
        SD,
        '        "python_facts": python_facts or {},',
        '        "python_facts": {},  # 突变：接线断开',
        ["test_wiring_python_profile_reaches_render_via_real_caller"],
    ),
    (
        "P-H1r：pyproject requires-python 正则打错（TOML 键名匹配不上=版本下界静默缺席）",
        SD,
        'm = re.search(r"requires-python\\s*=\\s*[\\"\']([^\\"\']+)", pyproject)',
        'm = re.search(r"requires_python\\s*=\\s*[\\"\']([^\\"\']+)", pyproject)',
        ["test_python_src_layout_facts_and_render"],
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
