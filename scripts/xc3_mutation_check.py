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
        "                _c_lang, _c_text, project_path, timeout, _run_check_split,\n"
        "                refs_out=_c_refs)",
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
        "            _own, _unres = produced_in_scope(\n"
        "                language_key, driver_refs or list(blocked_pkgs), _files,\n"
        "                project_path, timeout, run)",
        "            _own, _unres = set(), set()",
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
    # ── 以下为复核整改新增（每条对应一个已实测复现的 finding）──
    (
        "CRITICAL-2: UNKNOWN 归属不再拦 BLOCKED（解不出就敢断言外部生产者）",
        PIPE,
        '    if _unres:\n'
        '        # ★复核 CRITICAL-2 的裁决半边★ 归属**解不出**时不敢断言"生产者在外部"——',
        '    if False:\n'
        '        # ★复核 CRITICAL-2 的裁决半边★ 归属**解不出**时不敢断言"生产者在外部"——',
        ["test_wiring_ts_unresolved_owner_falls_to_fail"],
    ),
    (
        "CRITICAL-2: TS 相对导入退回『工程根相对』（scope 词干永不匹配 → 等自己）",
        DRV,
        '        base_dir = str(src).replace("\\\\", "/").rsplit("/", 1)[0] if "/" in str(src) else ""',
        '        base_dir = ""',
        ["test_produced_in_scope_detects_own_container",
         "test_wiring_ts_own_scope_producer_falls_to_fail"],
    ),
    (
        "HIGH-1: Rust 主形态退回按段数 rpartition（crate::svc → 容器 crate）",
        DRV,
        "            if leaf in _leaves and container:",
        "            if container:",
        ["test_rust_primary_form_is_whole_container_not_split"],
    ),
    (
        "HIGH-2: Go 包别名不再反解（qualifier 当容器 → 清盘同批裸 undefined）",
        DRV,
        '            _p = self._resolve_qualifier(r.ref, r.src, project_path, timeout, run)\n'
        "            return r._replace(ref=_p) if _p else r",
        "            return r",
        ["test_go_qualified_undefined_resolves_alias"],
    ),
    (
        "MED-2: Go 裸 `package` 分支恢复过宽（噪声行静默关掉整个闸）",
        DRV,
        '    r"|package\\s+([A-Za-z0-9_./\\-]+)(?=\\s+is not in\\b))"',
        '    r"|package\\s+([A-Za-z0-9_./\\-]+))"',
        ["test_go_bare_package_noise_does_not_disarm_gate"],
    ),
    (
        "MED-1: bundler 形第三方看不见（全或无被静默解除武装）",
        DRV,
        "        for m in _NODE_BUNDLER_RESOLVE_RE.finditer(text):\n"
        "            out.append(MissingRef(ref=m.group(1), symbol=None, src=None))",
        "        pass",
        ["test_solver_sees_bundler_third_party"],
    ),
    (
        "MED-1: GOPATH 形第三方看不见（同上，Go 侧）",
        DRV,
        "        for m in _GO_CANNOT_FIND_PKG_RE.finditer(text):\n"
        "            out.append(MissingRef(ref=m.group(1), symbol=None, src=None))",
        "        pass",
        ["test_solver_sees_gopath_third_party"],
    ),
    (
        "LOW-3: _norm_rel 退回 lstrip('./')（吃掉 .github/.mvn 前导点）",
        DRV,
        '    while p.startswith("./"):\n        p = p[2:]\n    return p.lstrip("/")',
        '    return p.lstrip("./")',
        ["test_norm_rel_preserves_dotfile_dirs"],
    ),
    (
        "去重：同一行多正则命中时留了 src=None 那条（步骤4 整批落 UNKNOWN）",
        DRV,
        "        elif best[k].src is None and r.src:\n"
        "            best[k] = r",
        "        elif False:\n            best[k] = r",
        ["test_dedupe_prefers_evidence_with_source_file"],
    ),
    (
        "LOW-2: 符号继承退回裸 startswith（容器 svc 吞 svcutil.Foo）",
        PIPE,
        "                if any(_cs == p or _cs.startswith(p + _sep) for p in own_pkgs):",
        "                if any(_cs.startswith(p) for p in own_pkgs):",
        ["test_step4_symbol_inheritance_rejects_sibling_container"],
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
