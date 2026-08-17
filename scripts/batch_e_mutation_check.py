#!/usr/bin/env python3
"""批 E 突变实验 harness（31 号文 A1-M1/M2/M3/L1）。

条目形状 = (说明, 路径常量, old, new)——`scripts/harness_landing_audit.py` 静态取
`elts[1]` 当路径、`elts[2]` 当 old。别改成别的顺序（批 D 实测：一次就把仓内
undecidable 从 6 顶到 17，被 test_harness_landing_locks 拦下）。

纪律：绝不与全量并发；绝不放进带超时的循环；每次突变前后清 pyc；锚点缺失即报错；
先验基线全绿；执行面必须覆盖新锁所在的全部文件。
"""

from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PF = ROOT / "brain" / "plan_finisher.py"
PI = ROOT / "brain" / "plan_inject.py"
PV = ROOT / "brain" / "plan_validator.py"
PN = ROOT / "brain" / "planning_nodes.py"
ND = ROOT / "brain" / "nodes" / "__init__.py"
RN = ROOT / "brain" / "runner.py"
ST = ROOT / "brain" / "state.py"
SS = ROOT / "brain" / "symbol_surgery.py"

LOCK_FILES = [
    "test/test_31_batch_e_plan_gate_residuals.py",
    # 执行面必须含同域既有锁——本批动了 B3④/B3⑥/C1 三处承重结构
    "test/test_round67l_plan_exam_truth.py",
    "test/test_h3b_symbol_cycle_pairs.py",
    "test/test_b3_stack_spec_single_source.py",
    "test/test_r67_brain_exit_gates.py",
]

MUTATIONS = [
    # ── A1-M3 ──
    ("e1 A1-M3：锚定形退回闭合标签（同族坐标被放行）", PF,
     '    return f"<artifactId>{artifact_word}"',
     '    return f"<artifactId>{artifact_word}</artifactId>"'),
    ("e2 A1-M3：pom 臂不用单一事实源（退回内联闭合标签）", PF,
     '                new_pat = pom_dep_ban_pattern(pat)',
     '                new_pat = f"<artifactId>{pat}</artifactId>"'),
    ("e3 A1-M3：pom 臂整块摘掉（禁令改写失效=注释散文冤杀复发）", PF,
     '            if all(_is_pom_manifest_path(t) for t in targets) and _WORD.fullmatch(pat):',
     '            if False and all(_is_pom_manifest_path(t) for t in targets) and _WORD.fullmatch(pat):'),
    # ── A1-M2 ──
    ("e4 A1-M2：剔除账不落 plan（账退回只活在日志里）", PF,
     '                    plan.symbol_exam_dropped = {k: list(v) for k, v in _exam_dropped.items()}',
     '                    pass  # MUTANT: 账不落 plan'),
    ("e5 A1-M2：归零账不落 plan（剔到零与剔一部分不可分）", PF,
     '                    plan.symbol_exam_zeroed = sorted(set(_exam_zeroed))',
     '                    pass  # MUTANT: 归零账不落'),
    ("e6 A1-M2：不再识别归零（_zeroed 恒空）", PF,
     '                    if not kept:\n                        _zeroed.append(c)',
     '                    if False:\n                        _zeroed.append(c)'),
    ("e7 A1-M2：finisher 中间那一跳摘掉（out 里没有账 ⇒ state 恒空）", PF,
     '        out["symbol_exam_dropped"] = {\n            k: list(v) for k, v in\n            (getattr(plan, "symbol_exam_dropped", None) or {}).items()}',
     '        pass  # MUTANT: 中间跳摘掉'),
    ("e8 A1-M2：归零账不进 out", PF,
     '        out["symbol_exam_zeroed"] = list(\n            getattr(plan, "symbol_exam_zeroed", None) or [])',
     '        pass  # MUTANT'),
    ("e9 A1-M2：progress 出口不消费（新账没有消费者）", RN,
     '        "symbol_exam_dropped": state.get("symbol_exam_dropped") or {},',
     '        # MUTANT: 消费者摘掉'),
    ("e10 A1-M2：validate 侧不折进 warnings（人读文案面消失）", ND,
     '    _exam_dropped_acct = state.get("symbol_exam_dropped") or {}',
     '    _exam_dropped_acct = {}  # MUTANT'),
    ("e11 A1-M2：归零独立文案摘掉（响铃塌进同一条）", ND,
     '    if _exam_zeroed_acct:\n        _vp_warnings.append(',
     '    if False and _exam_zeroed_acct:\n        _vp_warnings.append('),
    ("e12 A1-M2：BrainState 声明摘掉（LangGraph 静默丢弃）", ST,
     '    symbol_exam_dropped: dict          # ★31 号文 A1-M2★',
     '    _mutant_symbol_exam_dropped: dict  # ★31 号文 A1-M2★'),
    ("e13 A1-M2：round 语义注册摘掉（跨轮粘滞）", ST,
     '    "symbol_exam_dropped": "round",  # 31 号文 A1-M2',
     '    "_mutant_exam_dropped": "round",  # 31 号文 A1-M2'),
    ("e14 A1-M2：_rebuild_plan 不携带新账（重建一轮即丢，B-1 同族）", PN,
     '        symbol_exam_dropped={k: list(v) for k, v in\n                             (getattr(plan_obj, "symbol_exam_dropped", None) or {}).items()},',
     '        # MUTANT: 重建不携带'),
    ("e15 A1-M2：out 里的 always-emit 改成条件发射（无命中即缺席→粘滞）", PF,
     '        out["symbol_exam_zeroed"] = list(\n            getattr(plan, "symbol_exam_zeroed", None) or [])',
     '        if getattr(plan, "symbol_exam_zeroed", None):\n            out["symbol_exam_zeroed"] = list(plan.symbol_exam_zeroed)'),
    # ── A1-M1 ──
    ("e16 A1-M1：注入通道漏传 layout_punted（布局闸硬打回结构性失效）", PI,
     '        _cres = validate_contract_ownership(\n            plan, shared_contract, project_path=project_path,\n            layout_punted=finish_out.get("contract_symbols_layout_punted") or [])',
     '        _cres = validate_contract_ownership(\n            plan, shared_contract, project_path=project_path)'),
    ("e17 A1-M1：外科通道漏传（口径不同源）", SS,
     '    verdict = validate_contract_ownership(\n        candidate, sc, project_path=project_path,\n        layout_punted=state.get("contract_symbols_layout_punted") or [])',
     '    verdict = validate_contract_ownership(candidate, sc, project_path=project_path)'),
    # ── A1-L1 ──
    ("e18 A1-L1：签名退回 dict（与真实返回值不符）", PV,
     'def _cross_cluster_route_double_claims(\n        plan) -> tuple[dict[str, list[str]], dict[str, list[str]]]:',
     'def _cross_cluster_route_double_claims(plan) -> dict[str, list[str]]:'),
]


def _clear_pyc() -> None:
    for d in (ROOT / "brain" / "__pycache__", ROOT / "brain" / "nodes" / "__pycache__",
              ROOT / "__pycache__"):
        if d.exists():
            shutil.rmtree(d, ignore_errors=True)


def _run_locks() -> tuple[int, str]:
    _clear_pyc()
    proc = subprocess.run(
        [str(ROOT / ".venv/bin/python"), "-m", "pytest", *LOCK_FILES,
         "-p", "no:warnings", "-q", "--tb=no"],
        cwd=ROOT, capture_output=True, text=True, timeout=1800)
    return proc.returncode, proc.stdout[-2500:]


def main() -> int:
    originals = {p: p.read_text(encoding="utf-8") for p in
                 {m[1] for m in MUTATIONS}}
    print("=== 基线（必须全绿）===")
    rc, out = _run_locks()
    print(out.strip()[-500:])
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
