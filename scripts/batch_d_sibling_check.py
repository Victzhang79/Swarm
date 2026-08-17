#!/usr/bin/env python3
"""批 D 的 sibling 复核：批 A 立的锁在批 D 改动后是否仍有牙。

纪律「改共享代码必复跑上一批 harness」：批 D 动的 `_reject_endpoint_keys` /
`_persist_env_updates` 正是批 A（A4-C1 提权闸）的承重结构。批 A harness 未入库，
故在此复刻它的三条核心突变——若批 D 把批 A 的锁静默拆成死代码，这里会 GREEN。
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CFG = ROOT / "api" / "routers" / "config.py"
LOCK_FILES = [
    "test/test_a4c1_security_key_admin_gate.py",
    "test/test_s1_endpoint_gate_value_layer.py",
    "test/test_31_batch_d_config_gate_attribution.py",
]

# 形状同 batch_d_mutation_check：(说明, 路径常量, old, new)，供 harness_landing_audit 静态审计。
MUTATIONS = [
    ("a1 批A：安全键枚举表清空（提权闸失效）", CFG,
     'def _is_admin_only_security_key(env_key: str) -> bool:',
     'def _is_admin_only_security_key(env_key: str) -> bool:\n    return False  # MUTANT'),
    ("a2 批A：族兜底正则清空（未来同族新键无防护）", CFG,
     '    return any(p in k for p in _ADMIN_ONLY_SECURITY_PATTERNS)',
     '    return False'),
    ("a3 批A：安全键闸从 chokepoint 摘掉（四 caller + backstop 全失效）", CFG,
     '        if _is_admin_only_security_key(k):',
     '        if False and _is_admin_only_security_key(k):'),
    ("a4 批A：大小写不归一（小写键绕过）", CFG,
     '    k = (env_key or "").strip().upper()',
     '    k = (env_key or "").strip()'),
]


def _clear_pyc() -> None:
    for pat in ("api/routers/__pycache__", "api/__pycache__"):
        d = ROOT / pat
        if d.exists():
            shutil.rmtree(d, ignore_errors=True)


def _run() -> tuple[int, str]:
    _clear_pyc()
    p = subprocess.run([str(ROOT / ".venv/bin/python"), "-m", "pytest", *LOCK_FILES,
                        "-p", "no:warnings", "-q", "--tb=no"],
                       cwd=ROOT, capture_output=True, text=True, timeout=900)
    return p.returncode, p.stdout[-1500:]


def main() -> int:
    orig = CFG.read_text(encoding="utf-8")
    rc, out = _run()
    if rc != 0:
        print("!! 基线不绿，停止\n" + out[-800:])
        return 1
    print("BASELINE_GREEN\n")
    results = []
    for desc, _path, anchor, repl in MUTATIONS:
        mid = desc.split()[0]
        if anchor not in orig:
            print(f"[{mid}] ANCHOR_MISSING {desc}")
            results.append((mid, "ANCHOR_MISSING", desc))
            continue
        try:
            CFG.write_text(orig.replace(anchor, repl, 1), encoding="utf-8")
            rc, out = _run()
            v = "RED" if rc != 0 else "GREEN(!!)"
            print(f"[{mid}] {v:10s} {desc}")
            results.append((mid, v, desc))
        finally:
            CFG.write_text(orig, encoding="utf-8")
            _clear_pyc()
    bad = [r for r in results if r[1] != "RED"]
    print(f"\n{len(results) - len(bad)}/{len(results)} 红")
    for mid, v, desc in bad:
        print(f"!! {mid} [{v}] {desc}")
    assert CFG.read_text(encoding="utf-8") == orig, "源码未还原！"
    print("源码已还原（逐字节相等）")
    return 1 if bad else 0


if __name__ == "__main__":
    raise SystemExit(main())
