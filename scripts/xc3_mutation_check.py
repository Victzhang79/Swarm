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
        "                refs_out=_c_refs, disarm_out=_c_disarm)",
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
        '    details[{"build": "l1_2_1_build_ok", "compile": "l1_2_compile_ok",\n             "test": "l1_3_test_ok"}[stage]] = None',
        '    details[{"build": "l1_2_1_build_ok", "compile": "l1_2_compile_ok",\n             "test": "l1_3_test_ok"}[stage]] = False',
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
        # ★不含 test_produced_in_scope_detects_own_container★ 它直调 driver 本体，与 PIPE 侧
        # 这处突变无关（hunter 实测：该条在此突变下全绿）。混进零区分力的名字会让整条突变
        # 变成"看起来锁住了"——严格粒度下它会如实报失败，故这里如实收窄。
        ["test_step4_shared_layer_consults_driver_half",
         "test_step4_symbol_inherits_container_ownership",
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
        '            return _disarm("third_party", r.ref)      # 有第三方 → 全盘不标',
        "            continue",
        # ★只留真有区分力的那条★ 两条 wiring 测试在此突变下**仍绿，但原因不同**：泄漏出的
        # `./routes/users` 会被**第二道闸**（`express` 非相对导入 → 归属 UNKNOWN → 落 FAIL）
        # 接住。它们证不了"是全或无拦住的"（"通用并行写闸替漏判的根聚合闸背书"同型）。
        # 严格粒度把这件事如实暴露出来了，故按事实收窄。
        ["test_solver_all_or_nothing_third_party_present"],
    ),
    (
        "求解器：已在树里不再全盘不标（真编译错被当成未就绪）",
        DRV,
        '            return _disarm("already_in_tree", r.ref)  # 有已在树里的 → 真编译错，全盘不标',
        "            pass",
        ["test_solver_all_or_nothing_already_in_tree",
         "test_wiring_ts_already_in_tree_stays_compile_fail"],
    ),
    (
        "求解器：未收录栈不再 fail-closed（臆造 BLOCKED）",
        DRV,
        "    drv = driver_for(language_key)\n"
        "    if drv is None:\n"
        '        return _disarm("unregistered_stack", str(language_key or ""))',
        "    drv = driver_for(language_key) or GoErrorDriver()\n"
        "    if drv is None:\n        pass",
        # java 那条对本突变零区分力（突变只改 `drv is None` 分支，java 仍走
        # `_SELF_HANDLED_KEYS` 那条）——留着会让整条突变"看起来锁住了"。
        ["test_solver_unregistered_stack_fail_closed"],
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
    # ── hunter 复核整改新增 ──
    (
        "C-1: 步骤3 不再与步骤4 同口径（present_in_tree 自己按工程根解）",
        DRV,
        "    stems = drv.ref_tree_paths(ref, src, project_path, timeout, run)\n"
        "    if not stems:\n        return False",
        "    stems = [ref[2:] if ref.startswith('./') else ref]\n"
        "    if not stems:\n        return False",
        ["test_present_in_tree_uses_same_path_convention_as_ref_tree_paths"],
    ),
    (
        "C-2: 删掉 L1.3 test 闸的 X-C3 归因（python 退回死代码）",
        PIPE,
        "                _t_pkgs, _t_syms = blocked_on_unbuilt_internal(\n"
        "                    _t_lang, t_out, project_path, timeout, _run_check_split,\n"
        "                    refs_out=_t_refs, disarm_out=_t_disarm)",
        "                _t_pkgs, _t_syms = (set(), [])",
        ["test_wiring_python_test_gate_reaches_blocked"],
    ),
    (
        "H-1: 解除武装不再留机读账（返空与『真没有』不可分）",
        DRV,
        '        if disarm_out is not None:\n'
        '            disarm_out["reason"] = reason',
        "        if False:\n            disarm_out[\"reason\"] = reason",
        ["test_disarm_reason_is_machine_readable",
         "test_wiring_ts_already_in_tree_stays_compile_fail"],
    ),
    (
        "H-2: Rust E0433 裸模块名不再归一成 crate::（调用形清盘同批）",
        DRV,
        '                ref=name if "::" in name else f"crate::{name}", symbol=None, src=None))',
        "                ref=name, symbol=None, src=None))",
        ["test_rust_e0433_call_forms_normalize_to_crate_path",
         "test_rust_mixed_use_and_call_forms_not_cleared"],
    ),
    (
        "H-4: ref_tree_paths 返 [] 又塌进『确定不自产』（CRITICAL-2 复发种子）",
        DRV,
        "        if not stems:\n"
        "            # ★复核 H-4★ `None`（UNKNOWN）与 `[]` 一律记为\"归属未知\"",
        "        if stems is None:\n"
        "            # ★复核 H-4★ `None`（UNKNOWN）与 `[]` 一律记为\"归属未知\"",
        ["test_produced_in_scope_treats_empty_stems_as_unresolved"],
    ),
    (
        "M-3: 符号级探测递归整树（跨包同名短名被当成已建出）",
        DRV,
        "    cmd = f\"grep -lE {_sh_quote(pat)} {files} 2>/dev/null | head -1\"",
        "    cmd = f\"grep -rlE {_sh_quote(pat)} . 2>/dev/null | head -1\"",
        ["test_symbol_probe_does_not_cross_package_boundary"],
    ),
    (
        "M-4: 裁决翻转成 FAIL 后仍留 blocked_via_error_driver 粘滞键",
        PIPE,
        '        details.pop("blocked_via_error_driver", None)   # M-4：裁决翻转成 FAIL → 不留粘滞键',
        "        pass",
        ["test_wiring_ts_own_scope_producer_falls_to_fail"],
    ),
    (
        "H-3: 步骤4 异常退回 fail-open（判 BLOCKED 去等自己）",
        PIPE,
        "            if unresolved_out is not None:\n"
        "                unresolved_out.update(str(p) for p in blocked_pkgs)",
        "            pass",
        ["test_step4_driver_exception_falls_to_fail_not_blocked"],
    ),
    # ── 以下为 reviewer 复核整改新增（每条对应一个已实测复现的 finding）──
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


def run_one(name: str) -> int:
    """跑**单条**测试名，返回 pytest rc。rc=5（没选到）由调用方判为失败。"""
    p = subprocess.run(
        [PY, "-m", "pytest", TEST, "-p", "no:warnings", "-q", "--tb=no", "-k", name],
        cwd=ROOT, capture_output=True, text=True)
    return p.returncode


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
            # ★M-2（hunter 实测两条自伤）★
            # (a) 粒度必须是"**指名的每一条**都红"，不是"整组任一条红"——否则 should_red 里
            #     混进零区分力的测试名永远不会被发现（实测：F2 突变下 4 条里有 1 条全绿，
            #     harness 仍打印 ✓）。
            # (b) rc=5（-k 一条都没选到，如测试被重命名/typo）**不是红**，原判据 rc!=0
            #     会把它读成"该红的红了"⇒ 那条突变永久假绿。
            per: list[tuple[str, int]] = [(n, run_one(n)) for n in should_red]
            bad = [(n, rc) for n, rc in per if rc == 5]
            green = [(n, rc) for n, rc in per if rc == 0]
            ok = not bad and not green
            print(f"[{i}/{len(MUTATIONS)}] {name}")
            if ok:
                print(f"    ✓ 指名的 {len(per)} 条全红  (锁定 {should_red})")
            else:
                if bad:
                    print(f"    ✗ 测试名选不到（重命名/typo，rc=5）: {[n for n, _ in bad]}")
                    failures.append((name, "测试名选不到"))
                if green:
                    print(f"    ✗ 突变后仍绿 = 对该机制零区分力: {[n for n, _ in green]}")
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
