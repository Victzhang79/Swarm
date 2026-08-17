#!/usr/bin/env python3
"""批 D 突变实验 harness（31 号文 A4-M2/M3/L1/L2）。

纪律（记忆已立档，逐条遵守）：
- 绝不与全量并发，绝不放进带超时的循环（SIGKILL 跳 finally ⇒ 突变留在磁盘上）。
- 每个突变前后清被突变模块的 pyc（整秒 mtime 粒度会让子进程跑上一条代码 = 假绿）。
- 锚点缺失即 ANCHOR_MISSING 报错，绝不静默零覆盖。
- 必须先验基线全绿（只验"突变→红"会让修得不全的整改全绿通过）。
- 执行面必须覆盖新锁所在的【全部】文件（批 B 的 b10 假信号教训）。
"""

from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CFG = ROOT / "api" / "routers" / "config.py"
LOCK_FILES = [
    "test/test_31_batch_d_config_gate_attribution.py",
    # 执行面必须含既有的同域锁——放宽方向的整改最可能把老锁拆成死代码
    "test/test_s1_endpoint_gate_value_layer.py",
    "test/test_a4c1_security_key_admin_gate.py",
]

# ★条目形状必须是 (说明, 路径常量, old, new)★——`scripts/harness_landing_audit.py` 静态取
# `elts[1]` 当路径、`elts[2]` 当 old。把说明塞在 index 1 会让全部落点变成"静态判不了"，
# 逃出 test_harness_landing_locks 那道闸（实测：一次就把 undecidable 从 6 顶到 17）。
MUTATIONS = [
    ("d1 A4-M2：who 改回字面量（归因丢失）", CFG,
     'update_map = _reject_endpoint_keys(update_map, is_admin, who, rejected_out=_rejected)',
     'update_map = _reject_endpoint_keys(update_map, is_admin, "persist_env", rejected_out=_rejected)'),
    ("d2 A4-M2：不回传 rejected_out（被剔键无法透出）", CFG,
     'update_map = _reject_endpoint_keys(update_map, is_admin, who, rejected_out=_rejected)',
     'update_map = _reject_endpoint_keys(update_map, is_admin, who)'),
    ("d3 A4-M2：persisted_keys 报入参全集（拒绝伪装成成功）", CFG,
     '"persisted_keys": sorted(update_map), "requested_keys": _requested}',
     '"persisted_keys": sorted(_requested), "requested_keys": _requested}'),
    ("d4 A4-M3：档位表失效（全落 full ⇒ 冤杀复发，等价于表被清空）", CFG,
     'return "host" if (env_key or "").strip().upper() in _URL_DIFF_HOST_TIER_KEYS else "full"',
     'return "full"'),
    ("d5 A4-M3：默认档改成 host（notify 外泄通道打开）", CFG,
     'return "host" if (env_key or "").strip().upper() in _URL_DIFF_HOST_TIER_KEYS else "full"',
     'return "host"'),
    ("d6 A4-M3：档位不被闸消费（差集退回整串）", CFG,
     '_added_urls = _diff_outbound(_new_urls, _old_urls, tier=_tier)',
     '_added_urls = _new_urls - _old_urls'),
    ("d7 A4-M3：authority 畸形回退塌成空串（值层闸对畸形载荷失效）", CFG,
     '    return s.lower()',
     '    return ""'),
    ("d8 A4-M3：host 档忽略端口（不同服务被当同端点）", CFG,
     'return f"{parts.hostname}:{parts.port}" if parts.port else parts.hostname',
     'return parts.hostname'),
    ("d9 A4-L2：判据改回读 request.state.user（RBAC 关闭时恒 403）", CFG,
     '    _who, _u = _require_config_admin(request)',
     '''    _require_perm(request, "config:write")
    _u = getattr(request.state, "user", None)
    if not _caller_is_admin(_u):
        raise HTTPException(status_code=403, detail="仅 admin 可写入凭据")
    _who = getattr(_u, "username", "?") if _u else "?"'''),
    ("d10 A4-L1：migrate 退回 config:write（非 admin 可覆盖 provider key）", CFG,
     '    _who, _ = _require_config_admin(request)\n    from swarm.config import secret_store',
     '    _who = "?"\n    _require_perm(request, "config:write")\n    from swarm.config import secret_store'),
    ("d11 A4-L1：migrate 审计整块摘掉", CFG,
     '    if migrated or cleared:\n        try:\n            from swarm.config.config_audit import record_config_changes\n            _audit_changes = {f"secret:{n}": (None, "(migrated to secret_store)")',
     '    if False:\n        try:\n            from swarm.config.config_audit import record_config_changes\n            _audit_changes = {f"secret:{n}": (None, "(migrated to secret_store)")'),
]


def _clear_pyc() -> None:
    for pat in ("api/routers/__pycache__", "api/__pycache__"):
        d = ROOT / pat
        if d.exists():
            shutil.rmtree(d, ignore_errors=True)


def _run_locks() -> tuple[int, str]:
    _clear_pyc()
    proc = subprocess.run(
        [str(ROOT / ".venv/bin/python"), "-m", "pytest", *LOCK_FILES,
         "-p", "no:warnings", "-q", "--tb=no"],
        cwd=ROOT, capture_output=True, text=True, timeout=900)
    return proc.returncode, proc.stdout[-2500:]


def main() -> int:
    orig = CFG.read_text(encoding="utf-8")
    print("=== 基线（必须全绿，否则一切『突变红』都无意义）===")
    rc, out = _run_locks()
    print(out.strip()[-600:])
    if rc != 0:
        print("!! 基线不绿，停止")
        return 1
    print("BASELINE_GREEN\n")

    only = sys.argv[1] if len(sys.argv) > 1 else None
    results = []
    for desc, _path, anchor, repl in MUTATIONS:
        mid = desc.split()[0]
        if only and mid != only:
            continue
        if anchor not in orig:
            print(f"[{mid}] ANCHOR_MISSING —— 锚点已漂移，突变未施加！\n    {anchor[:110]}")
            results.append((mid, "ANCHOR_MISSING", desc))
            continue
        try:
            CFG.write_text(orig.replace(anchor, repl, 1), encoding="utf-8")
            rc, out = _run_locks()
            tail = [ln for ln in out.strip().splitlines() if ln.strip()][-1:]
            verdict = "RED" if rc != 0 else "GREEN(!!)"
            print(f"[{mid}] {verdict:10s} {desc}\n    {tail[0] if tail else ''}")
            results.append((mid, verdict, desc))
        finally:
            CFG.write_text(orig, encoding="utf-8")
            _clear_pyc()

    print("\n=== 汇总 ===")
    bad = [r for r in results if r[1] != "RED"]
    for mid, v, desc in results:
        print(f"  {mid:4s} {v:12s} {desc}")
    print(f"\n{len(results) - len(bad)}/{len(results)} 红")
    if bad:
        print("!! 以下突变未被逮到（锁没牙 / 锚点漂移）:")
        for mid, v, desc in bad:
            print(f"   {mid} [{v}] {desc}")
    # 还原核验
    assert CFG.read_text(encoding="utf-8") == orig, "源码未还原！"
    print("源码已还原（逐字节相等）")
    return 1 if bad else 0


if __name__ == "__main__":
    raise SystemExit(main())
