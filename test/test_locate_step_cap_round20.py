#!/usr/bin/env python3
"""#14 迭代上限分配 → LOCATING 预算按 scope 文件数弹性放宽（round20 治本）回归测试。

背景（round19 KICKOFF:31）：126 次撞迭代上限，LOCATING cap-20 编码前烧光。根因之一 = 定位预算
flat-20 而 CODING 按 scope 文件数弹性（executor:480 base+15/file）——多文件子任务勘察不全全部落点
→ CODING 欠信息空烧其预算。治本 = LOCATING 也按文件数弹性放宽，但【单文件/trivial 恒 20】不回归
RUN12 墙钟保护，多文件 +4/file、硬顶 40、且永不超过整体 max_iterations（双重封顶不失控）。

注：CODING 阶段的 empty_diff（st-28/29）是【模型产出能力】非代码 bug（reviewer 定性），不在此治本。
本套只验证确定性的预算分配函数 _locate_step_cap 的边界。
"""
from __future__ import annotations

import importlib.util
from pathlib import Path

_bs = Path(__file__).resolve().parent / "swarm_bootstrap.py"
_spec = importlib.util.spec_from_file_location("swarm_bootstrap", _bs)
_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_mod)

from swarm.worker.executor import _LOCATE_STEP_CAP, _LOCATE_STEP_CAP_MAX, _locate_step_cap  # noqa: E402


def test_single_or_trivial_unchanged():
    # n≤1 → 恒 20，与 RUN12 原行为逐字节一致（不回归墙钟保护）
    assert _locate_step_cap(0, 100) == _LOCATE_STEP_CAP == 20
    assert _locate_step_cap(1, 100) == 20
    print("  ✅ ① 单文件/trivial(n≤1) → 恒 20（不回归 RUN12）")


def test_multifile_scales():
    assert _locate_step_cap(4, 100) == 20 + 3 * 4  # 32
    assert _locate_step_cap(2, 100) == 24
    print("  ✅ ② 多文件按 +4/file 弹性放宽（4 文件→32）")


def test_hard_ceiling():
    # 大量文件 → 撞硬顶 40，不失控
    assert _locate_step_cap(20, 100) == _LOCATE_STEP_CAP_MAX == 40
    assert _locate_step_cap(100, 100) == 40
    print("  ✅ ③ 大量文件 → 撞硬顶 40（不失控）")


def test_never_exceeds_max_iterations():
    # 整体 max_iterations 更紧时以它为准（双重封顶）
    assert _locate_step_cap(4, 25) == 25   # 32 被 25 压住
    assert _locate_step_cap(1, 10) == 10   # 20 被 10 压住
    assert _locate_step_cap(4, 0) == 32    # max_iter=0(未知)→不据此压
    print("  ✅ ④ 永不超过 max_iterations（双重封顶）")


def test_negative_or_none_safe():
    assert _locate_step_cap(-3, 100) == 20
    assert _locate_step_cap(None, 100) == 20  # type: ignore[arg-type]
    print("  ✅ ⑤ 负数/None 安全退化为 base 20")


if __name__ == "__main__":
    test_single_or_trivial_unchanged()
    test_multifile_scales()
    test_hard_ceiling()
    test_never_exceeds_max_iterations()
    test_negative_or_none_safe()
    print("\n✅ 全部通过：#14 LOCATING 预算弹性分配（round20 治本）")
