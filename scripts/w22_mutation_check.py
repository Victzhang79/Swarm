#!/usr/bin/env python3
"""W-22 突变 harness（判据与 xh_exec 同源）：

  · **先验基线全绿**；· **逐条**跑 should_red，每条都必须红；
  · `rc=5`（`-k` 选不到）**判失败**；· 落点唯一性检查；· 突变后必须仍能编译；
  · 每条突变前与还原后清被突变模块的 pyc（T-2，#29-3 统一补齐）。

四环各配一条反向锁——任一环节断（推送状态不分两态/不落机读记录/不抄进 details/
裁决不标 transient），对应测试必须恰红。
"""
from __future__ import annotations

import ast
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PY = str(ROOT / ".venv" / "bin" / "python")
TESTS = ["test/test_w22_push_undelivered.py"]

PIPE = ROOT / "worker" / "l1_pipeline.py"
VER = ROOT / "worker" / "l1_verdict.py"
OC = ROOT / "worker" / "output_compress.py"

MUTATIONS = [
    (
        "环①: 推送状态不再分两态（有沙箱未达又被记成无沙箱=transient 判据死）",
        PIPE,
        '        status_out["sandbox_present"] = True\n',
        '        status_out["sandbox_present"] = False  # 突变：两态合并\n',
        ["test_sandbox_without_sync_api_marks_undelivered",
         "test_successful_push_marks_uploaded"],
    ),
    (
        "环②: A2 推送未达不落机读记录（失败照旧走 capability 冤杀好活）",
        PIPE,
        '                    if evidence_out is not None:\n'
        '                        evidence_out["a2_push_undelivered"] = _rec\n',
        '                    if False:  # 突变\n'
        '                        evidence_out["a2_push_undelivered"] = _rec\n',
        ["test_undelivered_records_stacks_and_coords"],
    ),
    (
        "环②: 缺依赖坐标不收集（记录空坐标 → 抄写丢键 → 裁决永 False）",
        PIPE,
        '                    for _c in missing_deps_for(build_output, stack_key):\n',
        '                    for _c in ():  # 突变\n',
        ["test_undelivered_records_stacks_and_coords"],
    ),
    (
        "环③: evidence 不抄进 details（记录到不了 l1_verdict=机制零效力）",
        PIPE,
        '    if isinstance(a2u, dict) and a2u.get("coords"):\n'
        '        details["a2_push_undelivered"] = {\n',
        '    if False:  # 突变\n'
        '        details["a2_push_undelivered"] = {\n',
        ["test_copies_record_into_details"],
    ),
    (
        "环④: 裁决永不标 transient（记录有了没人用=新账没有消费者）",
        VER,
        '        _w22_transient = (source == "compile"\n'
        '                          and _a2_push_undelivered_still_failing(details))\n',
        '        _w22_transient = False  # 突变\n',
        ["test_same_coord_still_reported_marks_transient"],
    ),
    (
        "环④: 坐标匹配被拆（「同一坐标仍报错」判据失效=瞎标或永不标）",
        VER,
        '    return any(c in blob for c in coords)\n',
        '    return False  # 突变\n',
        ["test_same_coord_still_reported_marks_transient",
         "test_helper_truth_table_fail_closed"],
    ),
    # ── 双复核整改（R1）三条反向锁 ──
    (
        "复核M1: 判据未命中不落机读账（transient 通道被静默关回 capability 无从分辨）",
        VER,
        '            details["a2_push_undelivered_checked"] = False\n',
        '            pass  # 突变：不留账\n',
        ["test_checked_marker_distinguishes_miss_from_no_record"],
    ),
    (
        "复核M2: 坐标解析异常冒出循环（被调用方一把 break 吞成 debug=推送连带丢）",
        PIPE,
        '                    try:\n'
        '                        from swarm.worker.sibling_dep_repair import missing_deps_for\n'
        '                        for _c in missing_deps_for(build_output, stack_key):\n'
        '                            if _c not in _a2_coords:\n'
        '                                _a2_coords.append(_c)\n'
        '                    except Exception as _cde:  # noqa: BLE001\n',
        '                    if True:  # 突变：异常冒出循环\n'
        '                        from swarm.worker.sibling_dep_repair import missing_deps_for\n'
        '                        for _c in missing_deps_for(build_output, stack_key):\n'
        '                            if _c not in _a2_coords:\n'
        '                                _a2_coords.append(_c)\n'
        '                    if False:  # 突变\n',
        ["test_coords_parse_error_does_not_kill_push"],
    ),
    (
        "复核MEDIUM: go 缺模块信号摘除（build_error_lines 对 Go 恒空=判据单条命）",
        OC,
        '    r"no required module provides package",\n',
        '    # 突变：go 信号摘除\n',
        ["test_go_missing_module_line_is_extracted"],
    ),
]


def _pytest(args: list[str]) -> int:
    p = subprocess.run([PY, "-m", "pytest", *TESTS, "-p", "no:warnings", "-q",
                        "--tb=no", *args], cwd=ROOT, capture_output=True, text=True)
    return p.returncode


def _clear_pyc(path: Path) -> None:
    """删被突变模块的 pyc（T-2，#29-3 统一补齐）——整秒 mtime 判旧字节码有效=假绿/假背书。"""
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
        mutated = src.replace(old, new, 1)
        try:
            ast.parse(mutated)
        except SyntaxError as _e:
            print(f"[{i}/{len(MUTATIONS)}] {name}\n"
                  f"    ✗ 突变后源码无法编译（{_e.msg} @line {_e.lineno}）⇒ pytest 只会报 "
                  f"collection error，rc≠0 是假信号。")
            failures.append((name, "突变产生语法错"))
            continue
        path.write_text(mutated)
        _clear_pyc(path)
        try:
            per = [(n, _pytest(["-k", n])) for n in should_red]
            missing = [n for n, r in per if r == 5]
            green = [n for n, r in per if r == 0]
            print(f"[{i}/{len(MUTATIONS)}] {name}")
            if not missing and not green:
                print(f"    ✓ 指名的 {len(per)} 条全红")
            else:
                if missing:
                    print(f"    ✗ 测试名选不到（rc=5）: {missing}")
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
