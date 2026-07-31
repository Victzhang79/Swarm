#!/usr/bin/env python3
"""X-C3 突变 harness：先证基线全绿，再逐条突变证"会红"。

★为什么必须先验基线★ 上一会话 I-1 的整改 13 突变全红通过，而**基线本身是红的**——
只验"突变→红"会让修得不全的整改全绿蒙过去（见 swarm-mutation-harness-must-check-baseline-green）。

每条突变都指名它该打红哪些测试：突变后**那些测试必须红**，否则说明该测试对被测机制零区分力。
用 .venv/bin/python 跑 pytest（系统 python 会让两个臂都返非零＝假信号双向）。
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PY = str(ROOT / ".venv" / "bin" / "python")
TEST = "test/test_xc3_error_drivers.py"

PIPE = ROOT / "worker" / "l1_pipeline.py"
DRV = ROOT / "worker" / "l1_error_drivers.py"

# (名字, 文件, 原文, 替换, 该打红的测试名片段)
MUTATIONS = [
    (
        "F1: 删掉 L1.2 的 X-C3 归因整块（node/ts 退回死代码）",
        PIPE,
        "            _c_pkgs, _c_syms = blocked_on_unbuilt_internal(\n"
        "                _c_lang, _c_text, project_path, timeout, _run_check_split)",
        "            _c_pkgs, _c_syms = (set(), [])",
        ["test_wiring_ts_compile_gate_reaches_blocked",
         "test_wiring_ts_own_scope_producer_falls_to_fail"],
    ),
    (
        "F1: 分类器改吃截断的 compile_msg（raw_out 分账失效）",
        PIPE,
        '            _c_text = _compile_raw.get("text") or compile_msg or ""',
        '            _c_text = compile_msg or ""',
        ["test_wiring_compile_classifier_eats_untruncated_output"],
    ),
    (
        "F1: BLOCKED 时把 ok 键写成 False（被 l1_verdict 读成 capability 失败）",
        PIPE,
        '    details["l1_2_1_build_ok" if stage == "build" else "l1_2_compile_ok"] = None',
        '    details["l1_2_1_build_ok" if stage == "build" else "l1_2_compile_ok"] = False',
        ["test_wiring_ts_blocked_not_read_as_capability_failure",
         "test_wiring_ts_compile_gate_reaches_blocked"],
    ),
    (
        "F2: 删掉步骤4 的 driver 半边（非 JVM 栈退回 fail-open：去等自己）",
        PIPE,
        "            own_pkgs |= produced_in_scope(\n"
        "                language_key, set(blocked_pkgs), _files, project_path, timeout, run)",
        "            own_pkgs |= set()",
        ["test_step4_shared_layer_consults_driver_half",
         "test_step4_symbol_inherits_container_ownership",
         "test_produced_in_scope_detects_own_container",
         "test_wiring_ts_own_scope_producer_falls_to_fail"],
    ),
    (
        "F2: 词干匹配退化成裸 startswith（吃掉 sibling：svc 吞 svcutil）",
        DRV,
        '    if p.startswith(s + "/"):\n        return True\n'
        '    return p.rsplit(".", 1)[0] == s if "." in p.rsplit("/", 1)[-1] else p == s',
        "    return p.startswith(s)",
        ["test_stem_matches_rejects_sibling_prefix"],
    ),
    (
        "求解器：第三方缺失不再全盘不标（混合形态误标 BLOCKED）",
        DRV,
        "        if not drv.is_internal(r.ref, project_path, timeout, run):\n"
        "            return set(), []      # 有第三方 → 全盘不标",
        "        if not drv.is_internal(r.ref, project_path, timeout, run):\n"
        "            continue",
        ["test_solver_all_or_nothing_third_party_present",
         "test_wiring_ts_third_party_stays_plain_compile_fail",
         "test_wiring_compile_classifier_eats_untruncated_output"],
    ),
    (
        "求解器：已在树里不再全盘不标（真编译错被当成未就绪）",
        DRV,
        "        if drv.present_in_tree(r.ref, r.symbol, project_path, timeout, run):\n"
        "            return set(), []      # 有已在树里的 → 真编译错，全盘不标",
        "        if False:\n            return set(), []",
        ["test_solver_all_or_nothing_already_in_tree",
         "test_wiring_ts_already_in_tree_stays_compile_fail"],
    ),
    (
        "求解器：未收录栈不再 fail-closed（臆造 BLOCKED）",
        DRV,
        "    drv = driver_for(language_key)\n"
        "    if drv is None or drv.key in _SELF_HANDLED_KEYS:\n"
        "        return set(), []",
        "    drv = driver_for(language_key) or GoErrorDriver()\n"
        "    if drv is None:\n        return set(), []",
        ["test_solver_unregistered_stack_fail_closed", "test_solver_java_is_self_handled"],
    ),
    (
        "Go: 裸 undefined 的容器反解删掉（Go 最常见形态恒 fail-closed）",
        DRV,
        "    resolve = getattr(drv, \"resolve_ref\", None)\n"
        "    if resolve is not None:\n"
        "        refs = [resolve(r, project_path, timeout, run) for r in refs]",
        "    resolve = None",
        ["test_solver_wires_go_bare_undefined_through_resolve_ref"],
    ),
    (
        "Rust: 符号分隔符改成 `.`（brain 侧前缀匹配失配）",
        DRV,
        '    symbol_sep = "::"',
        '    symbol_sep = "."',
        ["test_rust_symbol_fqn_uses_stack_separator"],
    ),
]


def run_tests(names: list[str] | None = None) -> tuple[int, str]:
    cmd = [PY, "-m", "pytest", TEST, "-p", "no:warnings", "-q", "--tb=no"]
    if names:
        cmd += ["-k", " or ".join(names)]
    p = subprocess.run(cmd, cwd=ROOT, capture_output=True, text=True)
    return p.returncode, (p.stdout or "") + (p.stderr or "")


def main() -> int:
    print("═" * 70)
    print("步骤 0：基线必须全绿（不验基线的突变 harness 会让修得不全的整改蒙过去）")
    print("═" * 70)
    rc, out = run_tests()
    if rc != 0:
        print(out[-3000:])
        print("\n✗ 基线是红的 —— 突变结果全部无意义。先修基线。")
        return 1
    print(f"✓ 基线全绿 (exit={rc})\n")

    failures = []
    for i, (name, path, old, new, should_red) in enumerate(MUTATIONS, 1):
        src = path.read_text()
        if old not in src:
            print(f"[{i}/{len(MUTATIONS)}] {name}\n    ✗ 突变落点未命中（代码已漂移）")
            failures.append((name, "落点未命中"))
            continue
        if src.count(old) != 1:
            print(f"[{i}/{len(MUTATIONS)}] {name}\n"
                  f"    ✗ 落点出现 {src.count(old)} 次（非唯一，突变不等价）")
            failures.append((name, "落点非唯一"))
            continue
        path.write_text(src.replace(old, new, 1))
        try:
            rc_m, out_m = run_tests(should_red)
            ok = rc_m != 0
            print(f"[{i}/{len(MUTATIONS)}] {name}\n"
                  f"    {'✓ 该红的红了' if ok else '✗ 突变后仍全绿 = 这些测试对该机制零区分力'}"
                  f"  (exit={rc_m}, 锁定 {should_red})")
            if not ok:
                print("    " + out_m.strip().splitlines()[-1][:160])
                failures.append((name, "突变后仍绿"))
        finally:
            path.write_text(src)

    print("\n" + "═" * 70)
    rc_r, _ = run_tests()
    print(f"步骤 N：还原后基线复验 exit={rc_r}")
    if rc_r != 0:
        print("✗ 还原后基线不绿 —— harness 自己污染了工作树，检查 finally 分支")
        return 1
    if failures:
        print(f"\n✗ {len(failures)} 条突变未达标：")
        for n, why in failures:
            print(f"  · [{why}] {n}")
        return 1
    print(f"\n✓ 全部 {len(MUTATIONS)} 条突变都被锁住，且基线前后皆绿")
    return 0


if __name__ == "__main__":
    sys.exit(main())
