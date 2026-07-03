#!/usr/bin/env python3
"""#10 无生产者快失败 → 生产者已终结但阻断包仍缺 = 徒劳等待（round20 治本）回归测试。

治本背景（round19 实测 st-38，log:86-90）：st-38 自身编译过，但 pipeline_blocked
=internal_pkg_not_built 阻在【跨 feature 包】。快失败 `_hallucinated` 要求【完全无生产者】
(not _prods)；但 `_producers_of` 按路径/模块松归属，会把一个【已完成、却产了别的包名】的
子任务算作生产者 → _prods 非空 → _hallucinated=False → 误判 transient 可恢复 → 白磨完整
升级重试阶梯 ~1h 才最终 abandon。

治本（精准、非松紧 _producers_of、不打地鼠）：把"无生产者"泛化为"无 active 生产者"——
生产者已 abandoned 或已成功完成(不再重派)即 settled；仍 pending/在飞/未跑就 active、继续等
(保住合法跨模块等待)。当【全部生产者 settled 且阻断包仍不在工作树】→ 徒劳 → 立即判不可恢复。
与 #12 天然互补：包【在树但没 seed】(_package_in_baseline=True)→ 不 abandon，交 #12 重 seed。

本套验证纯函数 _blocked_pkg_unrecoverable 的判据边界（8 个场景）。
"""
from __future__ import annotations

import importlib.util
import tempfile
from pathlib import Path

_bs = Path(__file__).resolve().parent / "swarm_bootstrap.py"
_spec = importlib.util.spec_from_file_location("swarm_bootstrap", _bs)
_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_mod)

from swarm.brain.nodes import _blocked_pkg_unrecoverable  # noqa: E402

_PKG = "com.ruoyi.alarm.robot.domain"


def _tree_without_pkg():
    """临时工作树，含【别的】包目录但不含 _PKG（模拟生产者产了漂移的扁平包）。"""
    d = tempfile.TemporaryDirectory()
    root = Path(d.name)
    (root / "modA/src/main/java/com/ruoyi/alarm/domain").mkdir(parents=True)  # 扁平，非 robot 嵌套
    return d, str(root)


def _tree_with_pkg():
    d = tempfile.TemporaryDirectory()
    root = Path(d.name)
    (root / "modA/src/main/java" / _PKG.replace(".", "/")).mkdir(parents=True)
    return d, str(root)


def _call(project_path, producers, unsat=(), completed_ok=(), pending=(), self_id="st-38",
          blocked=(_PKG,)):
    return _blocked_pkg_unrecoverable(
        blocked_pkgs=list(blocked), producers=set(producers), unsat=set(unsat),
        completed_ok=set(completed_ok), pending=set(pending),
        project_path=project_path, self_id=self_id,
    )


# ── ① 完全无生产者 + 包不在树 → 不可恢复（旧 _hallucinated 语义，须保住）──
def test_no_producer_pkg_absent_unrecoverable():
    d, root = _tree_without_pkg()
    with d:
        assert _call(root, producers=[]) is True
    print("  ✅ ① 无生产者 + 包缺 → 不可恢复（旧 hallucinated 语义保住）")


# ── ② 无生产者 + 包在树（基线已有/仅漏同步）→ 可恢复，不硬失败 ──
def test_no_producer_pkg_present_recoverable():
    d, root = _tree_with_pkg()
    with d:
        assert _call(root, producers=[]) is False
    print("  ✅ ② 无生产者 + 包在树 → 可恢复（假阳性护栏，不误杀）")


# ── ③ ★核心★ 幽灵生产者已【完成】但产了别的包 → 全 settled + 包缺 → 不可恢复（st-38）──
def test_ghost_producer_completed_pkg_absent_unrecoverable():
    d, root = _tree_without_pkg()
    with d:
        # p-flat 完成了（l1 过、不在 pending），但它产的是扁平包，_PKG 仍缺
        assert _call(root, producers=["p-flat"], completed_ok=["p-flat"]) is True
    print("  ✅ ③ 幽灵生产者已完成但产别的包 + 目标包缺 → 不可恢复（st-38 治本）")


# ── ④ ★反打地鼠★ 生产者仍 pending（该等的合法跨模块等待）→ 可恢复，别误杀 ──
def test_producer_pending_recoverable():
    d, root = _tree_without_pkg()
    with d:
        assert _call(root, producers=["p-nested"], pending=["p-nested"]) is False
    print("  ✅ ④ 生产者仍 pending → 可恢复（保住合法跨模块等待，不打地鼠）")


# ── ⑤ 生产者本轮失败（在重试）→ active → 可恢复，继续等 ──
def test_producer_failing_recoverable():
    d, root = _tree_without_pkg()
    with d:
        # 失败重试 = 既在 pending 也可能未 completed_ok；用 pending 表达"在飞/待重派"
        assert _call(root, producers=["p-nested"], pending=["p-nested"]) is False
    print("  ✅ ⑤ 生产者在重试 → active → 可恢复")


# ── ⑥ 生产者从未跑（无结果、不在任何集合）→ active（该等它跑）→ 可恢复 ──
def test_producer_never_ran_recoverable():
    d, root = _tree_without_pkg()
    with d:
        assert _call(root, producers=["p-future"]) is False
    print("  ✅ ⑥ 生产者从未跑（未 completed/未 pending）→ active → 可恢复")


# ── ⑦ 生产者已 abandoned + 包缺 → settled → 不可恢复 ──
def test_producer_abandoned_unrecoverable():
    d, root = _tree_without_pkg()
    with d:
        assert _call(root, producers=["p-dead"], unsat=["p-dead"]) is True
    print("  ✅ ⑦ 生产者已 abandoned + 包缺 → 不可恢复")


# ── ⑧ self 不算自己的生产者（阻断子任务写了同名路径也不能自证 active）──
def test_self_excluded_from_producers():
    d, root = _tree_without_pkg()
    with d:
        # 生产者集合只含 self（st-38 自己）→ 排除后无 active → 包缺 → 不可恢复
        assert _call(root, producers=["st-38"], self_id="st-38") is True
    print("  ✅ ⑧ self 不算自己生产者 → 排除后按包缺判不可恢复")


# ── ⑨ 混合：一个 settled + 一个仍 pending → 有 active → 可恢复（保守，别误杀）──
def test_mixed_one_active_recoverable():
    d, root = _tree_without_pkg()
    with d:
        assert _call(root, producers=["p-done", "p-live"],
                     completed_ok=["p-done"], pending=["p-live"]) is False
    print("  ✅ ⑨ 混合生产者含 active → 可恢复（保守不误杀）")


if __name__ == "__main__":
    test_no_producer_pkg_absent_unrecoverable()
    test_no_producer_pkg_present_recoverable()
    test_ghost_producer_completed_pkg_absent_unrecoverable()
    test_producer_pending_recoverable()
    test_producer_failing_recoverable()
    test_producer_never_ran_recoverable()
    test_producer_abandoned_unrecoverable()
    test_self_excluded_from_producers()
    test_mixed_one_active_recoverable()
    print("\n✅ 全部通过：#10 生产者徒劳快失败（round20 治本）")
