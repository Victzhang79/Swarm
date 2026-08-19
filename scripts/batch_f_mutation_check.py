#!/usr/bin/env python3
"""批 F 突变实验 harness（31 号文 A2-M1/M2/L1/L2/L3）。

条目形状 = (说明, 路径常量, old, new)——供 `scripts/harness_landing_audit.py` 静态审计。

纪律：绝不与全量并发；绝不放进带超时的循环；每次突变前后清 pyc；锚点缺失即报错；
先验基线全绿；执行面覆盖新锁所在全部文件 + 同域既有锁。
"""

from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CU = ROOT / "brain" / "contract_utils.py"
SD = ROOT / "brain" / "smoke_derive.py"
SP = ROOT / "stacks" / "spec.py"

LOCK_FILES = [
    "test/test_31_batch_f_keyspace_and_failclosed.py",
    # 同域既有锁：键空间/脚手架/栈 spec 三处承重结构
    "test/test_b3_stack_spec_single_source.py",
    "test/test_r39_build_scaffold_inject.py",
    "test/test_f5_iter_defined_in_single_source.py",
]

MUTATIONS = [
    # ── A2-M1：规则1.5 键空间 ──
    ("f1 A2-M1：规则1.5 键换回原始拼写（判死名单 ⊅ 收敛名单，熔断烧钱）", CU,
     '            _writers_final.setdefault(_norm_scope_path(f), []).append(st.id)',
     '            _writers_final.setdefault(f, []).append(st.id)'),
    ("f2 A2-M1：规则1.5 只剥反斜杠不剥 ./（半归一同样漏）", CU,
     '            _writers_final.setdefault(_norm_scope_path(f), []).append(st.id)',
     '            _writers_final.setdefault(str(f).replace("\\\\", "/"), []).append(st.id)'),
    ("f3 A2-M1：规则1.5 整块摘掉（RUN9 串序机制失效）", CU,
     '    for f, wids in _writers_final.items():\n        wids = list(dict.fromkeys(wids))\n        if len(wids) < 2:',
     '    for f, wids in _writers_final.items():\n        wids = list(dict.fromkeys(wids))\n        if True or len(wids) < 2:'),
    # ── A2-L2：同族三处 ──
    ("f4 A2-L2(1)：dedupe 分组键换回原串（重复脚手架不合并）", CU,
     '            norm = _norm_scope_path(f)\n            if norm.rsplit("/", 1)[-1] in module_manifest_names()',
     '            norm = str(f).replace("\\\\", "/")\n            if norm.rsplit("/", 1)[-1] in module_manifest_names()'),
    ("f5 A2-L2(2)：all_write_targets 换回原串（mod_pom 查重假阴 → 双写者）", CU,
     '        all_write_targets |= {\n            _norm_scope_path(f) for f in',
     '        all_write_targets |= {\n            str(f) for f in'),
    # ★f6 已移入 KNOWN_EQUIVALENT（见文件末尾）★：mod_pom 侧的归一在当前守卫下**不可达**，
    # 那条突变是等价变换，不是锁没牙。留在 MUTATIONS 里会永远显示 GREEN 并污染"红了几条"。
    ("f7 A2-L2(3)：规则0 _all_creates 换回原串（造双 create）", CU,
     '        _all_creates = {_norm_scope_path(f) for st in subtasks',
     '        _all_creates = {str(f).replace("\\\\", "/") for st in subtasks'),
    # ── A2-M2：冒烟推导 fail-closed ──
    ("f8 A2-M2：fail-closed 整块摘掉（畸形 java -jar <目录>/ 复现）", SD,
     '    if not marker:\n        logger.warning(',
     '    if False:\n        logger.warning('),
    ("f9 A2-M2：fail-closed 改成静默返回（降级无痕，铁律#3）", SD,
     '        logger.warning(\n            "[SMOKE-DERIVE] A2-M2 fail-closed：JVM 臂无产物标记（stack_key=%r，"',
     '        logger.debug(\n            "[SMOKE-DERIVE] A2-M2 fail-closed：JVM 臂无产物标记（stack_key=%r，"'),
    ("f10 A2-M2：JVM_LANGS 常量被内联回去（一致性闸失去派生源）", SD,
     'JVM_LANGS: tuple[str, ...] = ("java", "kotlin", "scala")',
     'JVM_LANGS: tuple[str, ...] = ()'),
    # ── A2-L3：前缀剥离 ──
    ("f11 A2-L3：_norm_manifest_path 换回 lstrip 字符集（隐藏目录被削）", SP,
     '    p = str(path or "").replace("\\\\", "/")\n    while p.startswith("./"):\n        p = p[2:]\n    return p.lstrip("/")',
     '    return str(path or "").replace("\\\\", "/").lstrip("./").lstrip("/")'),
    ("f12 A2-L3：is_root_aggregate_manifest 不走单一事实源", SP,
     '    p = _norm_manifest_path(path)   # A2-L3：字面前缀剥离，绝不用 lstrip 字符集\n    return "/" not in p and p.lower() in _lc(root_aggregate_manifests())',
     '    p = str(path or "").replace("\\\\", "/").lstrip("./").lstrip("/")\n    return "/" not in p and p.lower() in _lc(root_aggregate_manifests())'),
]


# ★已证【等价变换】的突变（不计入红/绿统计，登记以免下轮重跑）★
#
# 「突变仍绿」有三个嫌疑人：① 测试没牙 ② pyc 陈旧 ③ **突变本身不等价（即等价变换）**。
# 第三个最容易被误判成第一个，然后去"加强"一条本来正确的锁——所以证出来的等价变换必须
# 显式登记，而不是留在表里永远显示 GREEN、污染"红了几条"这个数字（批 B 的 b10 教训）。
KNOWN_EQUIVALENT = [
    (
        "f6 A2-L2(2)：mod_pom 侧归一（`_norm_scope_path(f\"{mod}/pom.xml\")` → 裸拼）",
        "mod 来自 `_MVN_PL_RE = -pl\\s+([^\\s,]+)`，随后 `m.lstrip(':').strip()`，"
        "且 `'/' in m` 即被守卫跳过 ⇒ mod 恒不含 '/'、恒无 './' 前缀、恒无首尾空白 "
        "⇒ `_norm_scope_path(f'{mod}/pom.xml') == f'{mod}/pom.xml'` 对**全部可达输入**成立。"
        "已穷举验证 9 组候选（mod/.mod/mod-a/_m/a.b.c/:art/带空白/mod./..mod）全部相等。"
        "★保留该归一的理由★：让'两侧同键空间'这个不变量在两个站点都**语法上可见**——"
        "集合侧归一而这侧裸拼，下一个读者会以为是 bug 并'修'成不一致。"
        "★如实登记★：这一行今天不改变任何行为，故无锁能逮到它（不假称有）。"
    ),
]


def _clear_pyc() -> None:
    for d in (ROOT / "brain" / "__pycache__", ROOT / "brain" / "nodes" / "__pycache__",
              ROOT / "stacks" / "__pycache__", ROOT / "__pycache__"):
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
    if KNOWN_EQUIVALENT:
        print("\n=== 已证等价变换（不计红绿；无锁能逮到，如实登记）===")
        for _name, _why in KNOWN_EQUIVALENT:
            print(f"  · {_name}\n      {_why}")
    for p, orig in originals.items():
        assert p.read_text(encoding="utf-8") == orig, f"源码未还原: {p}"
    print("全部源码已还原（逐字节相等）")
    return 1 if bad else 0


if __name__ == "__main__":
    raise SystemExit(main())
