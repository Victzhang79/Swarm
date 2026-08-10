#!/usr/bin/env python3
"""B-7（未支持栈 fail-closed + 新栈准入闸 + X-M6 + verification_coverage）突变 harness
（判据与 pm_pl/xm9 harness 同源）：

  · 先验基线全绿；· 逐条跑 should_red；· rc=5 判失败；· 落点唯一性；
  · 突变后 ast.parse；· 绝不进超时循环、绝不与全量并发；· 突变/还原后清 pyc。

★锁的命题★：准入闸（_MANIFEST_BACKEND 每清单有主）· test_cmd 通过出口携未支持族 ·
三 producer 覆盖格 · gates 旧前缀 checkpoint 兼容 · X-M6 发现面（php）· unsupported
机读 note（静默化=发现面撒谎）· deliver payload 明示。
"""
from __future__ import annotations

import ast
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PY = str(ROOT / ".venv" / "bin" / "python")
TESTS = ["test/test_b7_verification_coverage.py",
         "test/test_b7_stack_admission_gate.py",
         "test/test_b4a_build_surface_tristate.py",
         "test/test_sandbox_spec.py",
         "test/test_f3_l3_skip_reason.py"]   # F3：l3 格分档+always-emit 兜底（B7-g 落点）

SPEC = ROOT / "stacks" / "spec.py"
VERIFY = ROOT / "brain" / "nodes" / "verify.py"
GATES = ROOT / "brain" / "gates.py"
SANDBOX = ROOT / "project" / "sandbox_spec.py"
RUNNER = ROOT / "brain" / "runner.py"

MUTATIONS = [
    (
        "B7-a：manage.py 摘出 root_manifests（准入闸当场抓到无清单——"
        "检测侧认得而 driver 侧无人认领的第三态复活）",
        SPEC,
        '                        "manage.py"),',
        '                        ),  # 突变：manage.py 摘除',
        ["test_manifest_backend_every_entry_has_driver_or_unsupported_registration"],
    ),
    (
        "B7-b：test_cmd 通过出口吞留痕（gates 拒 auto_accept 臂不可达，"
        "B-4a「闸造好了接在没人走的路上」复活）",
        VERIFY,
        '            return {"l2_passed": sandbox_result, **_fp_reset, **_deg_carry}',
        '            return {"l2_passed": sandbox_result, **_fp_reset}'
        "  # 突变：通过出口吞留痕",
        ["test_unsupported_family_carried_on_test_cmd_pass_exit"],
    ),
    (
        "B7-c：verify_l2 覆盖格删除（l2 验没验过回到不可机读）",
        VERIFY,
        '    return {**result, "verification_coverage": {"l2": _cell}}',
        "    return result  # 突变：l2 格删除",
        ["test_verify_l2_wrapper_cell_unsupported_stack",
         "test_verify_l2_wrapper_cell_passed_and_failed",
         "test_verify_l2_end_to_end_cell_on_unsupported_pass"],
    ),
    (
        "B7-d：gates 旧前缀兼容删除（升级前 checkpoint 的存量条目失效="
        "硬闸对存量任务静默拆掉）",
        GATES,
        '              or str(d).startswith("l2_unsupported_stack:")]',
        '              or False]  # 突变：旧前缀兼容删除',
        ["test_auto_accept_refuses_on_legacy_l2_prefix_from_old_checkpoint"],
    ),
    (
        "B7-e：php 发现面删除（X-M6 回退——composer.json 工程回到「发现不了」）",
        SANDBOX,
        '        elif name == "composer.json":\n'
        '            _add("php", rel)       # B-7/X-M6：发现面扩三栈（无工具链 driver，显式登记）',
        '        elif False:  # 突变：php 发现面删除\n'
        '            _add("php", rel)',
        ["test_find_build_files_discovers_unsupported_stacks"],
    ),
    (
        "B7-f：unsupported 机读 note 静默化（发现得了却零信号=发现面撒谎，"
        "「缺席必须机读可辨」失守）",
        SANDBOX,
        "    for _kind, _hint in _UNSUPPORTED_TOOLCHAIN_KINDS.items():",
        "    for _kind, _hint in {}.items():  # 突变：unsupported 登记静默化",
        ["test_infer_env_spec_registers_unsupported_kinds_machine_readably",
         "test_find_build_files_kinds_all_dispatched_or_registered"],
    ),
    (
        "B7-g：verify_l3 覆盖格删除",
        VERIFY,
        '    return {**result, "l3_skip_reason": _reason,\n'
        '            "verification_coverage": {"l3": _cell}}',
        "    return result  # 突变：l3 格删除",
        ["test_verify_l3_wrapper_cell_from_three_state",
         "test_verify_l3_real_node_writes_skipped_cell",
         "test_wrapper_always_emit_backfill_when_impl_omits_reason"],
    ),
    (
        "B7-h：deliver payload 覆盖账明示删除（消费者被摘=账白造，"
        "「新账必须有人消费」失守）",
        RUNNER,
        '"verification_failure", "verification_coverage"):',
        '"verification_failure"):  # 突变：payload 明示删除',
        ["test_deliver_payload_surfaces_coverage"],
    ),
    # ── R2 双复核整改锁 ──────────────────────────────────────────────────
    (
        "B7-i：infra_degrade 出口吞族留痕（R2 reviewer HIGH——未支持栈与 infra "
        "降级同现时族事实静默丢失、覆盖账被误推成 failed）",
        VERIFY,
        '                    **({"degraded_reasons": _l2_unverified_degraded}\n'
        '                       if _l2_unverified_degraded else {}),\n'
        '                }\n'
        '            if any("契约" in i for i in ir_issues):',
        '                }  # 突变：infra 出口吞留痕\n'
        '            if any("契约" in i for i in ir_issues):',
        ["test_unsupported_family_carried_on_infra_degrade_exit",
         "test_infra_degrade_exit_cell_is_unsupported_not_failed"],
    ),
    (
        "B7-j：gates 覆盖账优先删除（R2 hunter H-1——回退纯 degraded 扫描，"
        "旧轮粘滞条目复活误拦本轮真验过的交付）",
        GATES,
        '    _l2_cell = str(_cov.get("l2") or "")',
        '    _l2_cell = ""  # 突变：覆盖账优先删除',
        ["test_gates_prefers_current_round_cell_over_stale_degraded",
         "test_gates_blocks_on_cell_alone_without_degraded_entry"],
    ),
    (
        "B7-k：passed:unverified 分档删除（R2 hunter M-1——「放行但未验」"
        "混回 passed，消费者谎称「验过」）",
        VERIFY,
        '        _cell = ("passed:unverified" if any(d.startswith(',
        '        _cell = ("passed" if any(d.startswith(  # 突变：分档删除',
        ["test_verify_l2_wrapper_cell_passed_unverified_tiering"],
    ),
    (
        "B7-l：manage.py 摘出沙箱发现面（R2 hunter M-2——纯 Django 工程镜像 "
        "base_only 无 python 工具链，与 STACK_SPEC/构建面口径漂移复活）",
        SANDBOX,
        '_PY_REQ = ("requirements.txt", "pyproject.toml", "setup.py", "Pipfile", "manage.py")',
        '_PY_REQ = ("requirements.txt", "pyproject.toml", "setup.py", "Pipfile")'
        '  # 突变：manage.py 摘除',
        ["test_manage_py_discovers_python_toolchain"],
    ),
]


def _pytest(args: list[str]) -> int:
    p = subprocess.run([PY, "-m", "pytest", *TESTS, "-p", "no:warnings", "-q",
                        "--tb=no", *args], cwd=ROOT, capture_output=True, text=True)
    return p.returncode


def _clear_pyc(path: Path) -> None:
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
        path.write_text(src.replace(old, new, 1))
        _clear_pyc(path)
        try:
            try:
                ast.parse(path.read_text())
            except SyntaxError:
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
