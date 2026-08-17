#!/usr/bin/env python3
"""批 G 突变实验 harness（31 号文 A3 残余 8 条）。

条目形状 = (说明, 路径常量, old, new)，供 `scripts/harness_landing_audit.py` 静态审计。

纪律：绝不与全量并发；绝不放进带超时的循环；每次突变前后清 pyc；锚点缺失即报错；
先验基线全绿；执行面覆盖新锁所在全部文件 + 同域既有锁。
"""

from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
L1 = ROOT / "worker" / "l1_pipeline.py"
WM = ROOT / "worker" / "workspace_manifest.py"
SP = ROOT / "stacks" / "spec.py"
TY = ROOT / "types.py"

LOCK_FILES = [
    "test/test_31_batch_g_l1_coverage_and_attribution.py",
    # 同域既有锁：本批动了 L1 闸门/栈 spec/清单对账三处承重结构
    "test/test_a2_tsc_failclosed.py",
    "test/test_p2_14_d57_scans.py",
    "test/test_l1_pipeline.py",
    "test/test_w24_test_cmd_priority.py",
    "test/test_r46_manifest_prune.py",
    "test/test_31_batch_c_contract_and_l1_coverage.py",
]

MUTATIONS = [
    # ── A3-M1 / A3-L3：后缀集派生化 ──
    ("g1 A3-M1：权威表删回 .mts/.cts（派生也覆盖不到）", SP,
     'source_exts=(".ts", ".tsx", ".js", ".jsx", ".mjs", ".cjs", ".mts", ".cts", ".vue"),',
     'source_exts=(".ts", ".tsx", ".js", ".jsx", ".mjs", ".cjs", ".vue"),'),
    ("g2 A3-M1：compile 触发集退回手抄小表", L1,
     '    js_ts = [f for f in files if f.endswith(_ext_for_lang("node"))]',
     '    js_ts = [f for f in files if f.endswith((".ts", ".tsx", ".js", ".jsx", ".vue"))]'),
    ("g3 A3-L3：lint 分组退回手抄（.vue 零 lint 覆盖）", L1,
     '        elif f.endswith(_ext_for_lang("node")):\n            lang_groups["js_ts"].append(f)',
     '        elif f.endswith((".ts", ".tsx", ".js", ".jsx")):\n            lang_groups["js_ts"].append(f)'),
    ("g4 A3-M1：.d.mts/.d.cts 排除面撤销（纯声明算成源码）", SP,
     'source_exclude_suffixes=(".d.ts", ".d.mts", ".d.cts"),   # A3-M1：随新后缀同步',
     'source_exclude_suffixes=(".d.ts",),'),
    ("g5 A3-M1：npx 缺 tsc 横幅不判 infra（合法 JS 被冤杀）", L1,
     '    "is not the tsc command you are looking for",',
     '    # MUTANT: marker 摘掉'),
    ("g6 A3-M1：ts_only 集混进纯 JS（ts_gate_unavailable 误报）", L1,
     '    return tuple(e for e in _ext_for_lang("node") if e not in _JS_SYNTAX_EXTS)',
     '    return _ext_for_lang("node")'),
    # ── A3-M2：_per_file 截断记账 ──
    ("g7 A3-M2：_per_file 退回写死 100 且不记账", L1,
     '        if isinstance(details, dict):\n            files = _cap_files(files, cmd, details=details)',
     '        if False:\n            files = _cap_files(files, cmd, details=details)'),
    ("g8 A3-M2：details 不透传（截断静默）", L1,
     '        _derived = _derive_full_build_command(\n            project_path, modified, project_stack, details=details)',
     '        _derived = _derive_full_build_command(project_path, modified, project_stack)'),
    ("g9 A3-M2：上限退回写死 100（env 调了也不生效）", L1,
     '            _cap = _max_files_per_check()',
     '            _cap = 100'),
    # ── A3-M3：清单探针失败可辨 ──
    ("g10 A3-M3：不看 cr.success（探针失败塌成清单不存在）", L1,
     '            if getattr(cr, "success", True) is False:\n                _note_manifest_probe_error(',
     '            if False:\n                _note_manifest_probe_error('),
    ("g11 A3-M3：探针失败账不留 WARNING（降级无痕）", L1,
     '        logger.warning(\n            "[L1] A3-M3 清单探针失败（manifests=%s，原因=%s）',
     '        logger.debug(\n            "[L1] A3-M3 清单探针失败（manifests=%s，原因=%s）'),
    ("g12 A3-M3：needs_review 枚举摘掉（写侧消费侧不同源）", TY,
     '    "manifest_probe_failed",',
     '    # MUTANT: 枚举摘掉'),
    # ── A3-M4：infra 归因 ──
    # ★g13 原写成 `return {} or {` —— 空 dict 为假 ⇒ `or` 返回原 dict ⇒ **突变从未施加**
    # （批 D 的 d4 同一个坑：我自己的突变不等价）。改为真的不成账。
    ("g13 A3-M4：归因账不成（BLOCKED 凭什么判 infra 机读面消失）", L1,
     '    mk = _infra_marker_of(text)\n    if mk is None:\n        return None',
     '    mk = _infra_marker_of(text)\n    if mk is None or True:\n        return None'),
    ("g14 A3-M4：回环判据失效（最强确定性信号丢失）", L1,
     '        "loopback_target": bool(_is_net and _LOOPBACK_TARGET_RE.search(text or "")),',
     '        "loopback_target": False,'),
    ("g15 A3-M4：网络族子集掺入主表外条目（恒不命中）", L1,
     '_NETWORK_INFRA_MARKERS: tuple[str, ...] = (\n    "econnrefused ",',
     '_NETWORK_INFRA_MARKERS: tuple[str, ...] = (\n    "this_marker_is_not_in_main_table",\n    "econnrefused ",'),
    # ── A3-M5 / A3-L1 / A3-L2 ──
    ("g16 A3-M5：逐生态异常账不回传", WM,
     '            reconcile_errors[fn.__name__] = f"{type(exc).__name__}: {exc}"[:200]',
     '            pass  # MUTANT'),
    ("g17 A3-M5：异常降回 debug（生产不可见）", WM,
     '            logger.warning(\n                "[workspace-manifest] A3-M5 %s 对账**异常跳过**',
     '            logger.debug(\n                "[workspace-manifest] A3-M5 %s 对账**异常跳过**'),
    ("g18 A3-M5：返回值不带 reconcile_errors", WM,
     '    return {"modified_manifests": modified_manifests, "added": added, "removed": removed,\n            "reconcile_errors": reconcile_errors}',
     '    return {"modified_manifests": modified_manifests, "added": added, "removed": removed}'),
    ("g19 A3-L1：module-reg 调用点不传 status_out", L1,
     '                _pushed = _push_manifests_to_sandbox(\n                    project_path, _manifests, status_out=_push_status)',
     '                _pushed = _push_manifests_to_sandbox(project_path, _manifests)'),
    ("g20 A3-L2：包声明对账异常不成账", L1,
     '        _PKG_DECL_CHECK_ERROR["error"] = f"{type(exc).__name__}: {exc}"[:200]',
     '        pass  # MUTANT'),
    ("g21 A3-L2：异常降回 debug", L1,
     '        logger.warning(\n            "[L1.1b] A3-L2 包声明对账**异常中断**',
     '        logger.debug(\n            "[L1.1b] A3-L2 包声明对账**异常中断**'),
]


def _clear_pyc() -> None:
    for d in (ROOT / "worker" / "__pycache__", ROOT / "stacks" / "__pycache__",
              ROOT / "__pycache__", ROOT / "brain" / "__pycache__"):
        if d.exists():
            shutil.rmtree(d, ignore_errors=True)


def _run_locks() -> tuple[int, str]:
    _clear_pyc()
    proc = subprocess.run(
        [str(ROOT / ".venv/bin/python"), "-m", "pytest", *LOCK_FILES,
         "-p", "no:warnings", "-q", "--tb=no"],
        cwd=ROOT, capture_output=True, text=True, timeout=2400)
    return proc.returncode, proc.stdout[-2500:]


def main() -> int:
    originals = {p: p.read_text(encoding="utf-8") for p in {m[1] for m in MUTATIONS}}
    print("=== 基线（必须全绿）===")
    rc, out = _run_locks()
    print(out.strip()[-400:])
    if rc != 0:
        print("!! 基线不绿，停止")
        return 1
    print("BASELINE_GREEN\n")

    only = sys.argv[1] if len(sys.argv) > 1 else None
    results = []
    for desc, path, anchor, repl in MUTATIONS:
        mid = desc.split()[0]
        if only and mid != only:
            continue
        orig = originals[path]
        if anchor not in orig:
            print(f"[{mid}] ANCHOR_MISSING —— 锚点漂移，突变未施加！\n    {anchor[:110]}")
            results.append((mid, "ANCHOR_MISSING", desc))
            continue
        try:
            path.write_text(orig.replace(anchor, repl, 1), encoding="utf-8")
            rc, out = _run_locks()
            tail = [ln for ln in out.strip().splitlines() if ln.strip()][-1:]
            verdict = "RED" if rc != 0 else "GREEN(!!)"
            print(f"[{mid}] {verdict:10s} {desc}\n    {tail[0] if tail else ''}")
            results.append((mid, verdict, desc))
        finally:
            path.write_text(orig, encoding="utf-8")
            _clear_pyc()

    print("\n=== 汇总 ===")
    bad = [r for r in results if r[1] != "RED"]
    for mid, v, desc in results:
        print(f"  {mid:5s} {v:12s} {desc}")
    print(f"\n{len(results) - len(bad)}/{len(results)} 红")
    if bad:
        print("!! 未被逮到（锁没牙 / 锚点漂移 / 突变本身不等价）:")
        for mid, v, desc in bad:
            print(f"   {mid} [{v}] {desc}")
    for p, orig in originals.items():
        assert p.read_text(encoding="utf-8") == orig, f"源码未还原: {p}"
    print("全部源码已还原（逐字节相等）")
    return 1 if bad else 0


if __name__ == "__main__":
    raise SystemExit(main())
