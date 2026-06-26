#!/usr/bin/env python3
"""Wave 0 fail-closed 反向闸门测试 —— 根治 §D「测试理论」。

钉死 fail-closed 契约（TD2606-A1/A2/A4/C8）：「验证没跑成」绝不静默当 PASS。
覆盖 run_l1_pipeline 的 not_run_kind 产出与 BLOCKED 透传：
  - 真空 diff + 无 harness        → BENIGN（合法 no-op，可回退弱信号）
  - 非空 diff 解析到 0 文件        → BLOCKED（malformed diff，TD2606-C8/H4）
  - 构建真失败（编译真错误）        → FAIL（绝不被吞，§D 核心反向断言）
  - 构建命中 infra 瞬时故障        → BLOCKED（转 transient，不误判 capability）
  - 期望构建但工程清单缺失          → BLOCKED（TD2606-B7）

这些是历史上缺失的「喂坏输入断言 FAIL/BLOCKED」测试——旧套件只验真值表、从不把坏构建
跑过真实流水线断言其不通过，于是 silent-success 一类 bug 长期不被发现。
"""
from __future__ import annotations

import importlib.util
import tempfile
from pathlib import Path

_bs = Path(__file__).resolve().parent / "swarm_bootstrap.py"
_spec = importlib.util.spec_from_file_location("swarm_bootstrap", _bs)
_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_mod)


def _subtask(writable, harness=None):
    from swarm.types import FileScope, SubTask, SubTaskDifficulty, TaskHarness

    return SubTask(
        id="sub-1",
        description="wave0 fail-closed test",
        difficulty=SubTaskDifficulty.MEDIUM,
        scope=FileScope(writable=writable, readable=writable),
        harness=harness or TaskHarness(language="go"),
    )


def _go_diff(filename="main.go"):
    return (
        f"--- /dev/null\n+++ b/{filename}\n@@ -0,0 +1,2 @@\n"
        "+package main\n+func main() {}\n"
    )


# ── BENIGN vs BLOCKED：空 diff / malformed diff ──

def test_empty_diff_is_benign():
    """真空 diff + 无 harness → BENIGN（合法 no-op）。"""
    from swarm.types import NotRunKind
    from swarm.worker.l1_pipeline import run_l1_pipeline

    with tempfile.TemporaryDirectory() as d:
        st = _subtask(["x.go"])  # 默认 harness 无 build/test/verify
        ok, details = run_l1_pipeline(d, st, "", timeout=30)
        assert ok is True
        assert details.get("not_run_kind") == NotRunKind.BENIGN.value
        assert "pipeline_blocked" not in details
    print("  ✅ 真空 diff → BENIGN（无 pipeline_blocked）")


def test_malformed_nonempty_diff_is_blocked():
    """非空 diff 却解析到 0 文件（malformed）→ BLOCKED，绝不当 no-op PASS（TD2606-C8/H4）。"""
    from swarm.types import NotRunKind
    from swarm.worker.l1_pipeline import run_l1_pipeline

    with tempfile.TemporaryDirectory() as d:
        st = _subtask(["x.go"])
        ok, details = run_l1_pipeline(d, st, "this is garbage\nnot a real diff\n", timeout=30)
        # ok 仍为 True（无 harness 可跑），但必须标 BLOCKED 让裁决器降为 None(BLOCKED)。
        assert details.get("pipeline_blocked") == "malformed_diff_zero_files"
        assert details.get("not_run_kind") == NotRunKind.BLOCKED.value
    print("  ✅ malformed 非空 diff → BLOCKED")


# ── 构建闸门：真失败 FAIL / infra 故障 BLOCKED / 清单缺失 BLOCKED ──

def _patch(l1, *, run_ret=None, applicable=True):
    """monkeypatch _run_l1_command / _build_cmd_applicable，返回还原器。"""
    orig_run = l1._run_l1_command
    orig_app = l1._build_cmd_applicable
    l1._run_l1_command = lambda cmd, pp, timeout=120: run_ret
    l1._build_cmd_applicable = lambda cmd, pp: applicable

    def restore():
        l1._run_l1_command = orig_run
        l1._build_cmd_applicable = orig_app
    return restore


def test_real_build_failure_fails_not_swallowed():
    """§D 核心反向断言：构建真失败（编译真错误）→ L1 FAIL，绝不被吞成 PASS。"""
    import swarm.worker.l1_pipeline as l1
    from swarm.types import TaskHarness

    restore = _patch(l1, run_ret=(1, "main.go:3:5: error: undefined: Foo"), applicable=True)
    try:
        with tempfile.TemporaryDirectory() as d:
            st = _subtask(["main.go"], TaskHarness(language="go", build_command="go build ./..."))
            ok, details = l1.run_l1_pipeline(d, st, _go_diff(), timeout=30)
            assert ok is False, f"真编译错误必须 FAIL, details={details}"
            assert details.get("build_failed")
            assert "pipeline_blocked" not in details, "真失败不是 BLOCKED"
    finally:
        restore()
    print("  ✅ 真坏构建 → FAIL（未被吞）")


def test_build_infra_failure_is_blocked():
    """构建命中网络/工具 infra 瞬时故障 → BLOCKED（转 transient），不误判 capability FAIL。"""
    import swarm.worker.l1_pipeline as l1
    from swarm.types import NotRunKind, TaskHarness

    restore = _patch(
        l1, run_ret=(1, "go: downloading github.com/x/y: dial tcp: i/o timeout"), applicable=True,
    )
    try:
        with tempfile.TemporaryDirectory() as d:
            st = _subtask(["main.go"], TaskHarness(language="go", build_command="go build ./..."))
            ok, details = l1.run_l1_pipeline(d, st, _go_diff(), timeout=30)
            assert details.get("pipeline_blocked") == "build_infra_failure"
            assert details.get("not_run_kind") == NotRunKind.BLOCKED.value
            assert not details.get("build_failed"), "infra 故障不应记为 build_failed(capability)"
    finally:
        restore()
    print("  ✅ 构建 infra 故障 → BLOCKED（非 capability FAIL）")


def test_build_manifest_missing_is_blocked():
    """期望构建但工程清单缺失 → BLOCKED，不再静默当「跳过=通过」（TD2606-B7）。"""
    import swarm.worker.l1_pipeline as l1
    from swarm.types import NotRunKind, TaskHarness

    restore = _patch(l1, run_ret=(0, ""), applicable=False)  # 清单缺失 → 不适用
    try:
        with tempfile.TemporaryDirectory() as d:
            st = _subtask(["main.go"], TaskHarness(language="go", build_command="go build ./..."))
            ok, details = l1.run_l1_pipeline(d, st, _go_diff(), timeout=30)
            assert details.get("pipeline_blocked") == "build_manifest_missing"
            assert details.get("not_run_kind") == NotRunKind.BLOCKED.value
    finally:
        restore()
    print("  ✅ 期望构建但清单缺失 → BLOCKED")


if __name__ == "__main__":
    import sys
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    fails = 0
    for fn in fns:
        try:
            fn()
        except Exception as e:  # noqa: BLE001
            print(f"  ❌ {fn.__name__}: {e}")
            fails += 1
    sys.exit(1 if fails else 0)
