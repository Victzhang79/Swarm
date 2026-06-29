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


# ── 治本(st-10 npm 误判空转)：纯静态资源被误派 node 构建 → 跳过放行，非 BLOCKED 空转 ──

def _html_diff(path="src/main/resources/templates/alarm/x.html"):
    return f"--- /dev/null\n+++ b/{path}\n@@ -0,0 +1,2 @@\n+<html>\n+</html>\n"


def _ts_diff(path="src/main/resources/static/app.ts"):
    return f"--- /dev/null\n+++ b/{path}\n@@ -0,0 +1,1 @@\n+export const x = 1;\n"


def test_node_build_on_maven_static_resource_skipped_not_blocked():
    """治本(st-10)：Brain 给【纯静态资源子任务】误派 npm 构建 + Maven 项目(有 pom)无 package.json
    → 跳过放行(走 scope+lint)，绝不 BLOCKED 空转(代码本没问题，旧逻辑每轮重试再撞、永远不过)。"""
    import swarm.worker.l1_pipeline as l1
    from swarm.types import TaskHarness

    restore = _patch(l1, run_ret=(0, ""), applicable=False)  # npm 不适用(无 package.json)
    try:
        with tempfile.TemporaryDirectory() as d:
            Path(d, "pom.xml").write_text("<project/>", encoding="utf-8")  # Maven 项目
            # 故意不建 package.json
            st = _subtask(["src/main/resources/templates/alarm/x.html"],
                          TaskHarness(language="java", build_command="npm run build --if-present"))
            ok, details = l1.run_l1_pipeline(d, st, _html_diff(), timeout=30)
            assert details.get("build_command_skipped_reason") == "node_build_on_maven_static_resource", details
            assert details.get("l1_2_1_build_ok") is True
            assert "pipeline_blocked" not in details, "纯静态资源不该 BLOCKED 空转"
    finally:
        restore()
    print("  ✅ 误派 node 构建+Maven 静态资源 → 跳过放行(非 BLOCKED)")


def test_node_build_with_compilable_source_still_blocked():
    """对照：有【可编译前端源(.ts)】+ 无 package.json → 仍 BLOCKED(真前端构建在等 upstream 建清单)，
    不被新跳过逻辑误放(跳过只针对纯资源)。"""
    import swarm.worker.l1_pipeline as l1
    from swarm.types import NotRunKind, TaskHarness

    restore = _patch(l1, run_ret=(0, ""), applicable=False)
    try:
        with tempfile.TemporaryDirectory() as d:
            Path(d, "pom.xml").write_text("<project/>", encoding="utf-8")
            st = _subtask(["src/main/resources/static/app.ts"],
                          TaskHarness(language="node", build_command="npm run build --if-present"))
            ok, details = l1.run_l1_pipeline(d, st, _ts_diff(), timeout=30)
            assert details.get("pipeline_blocked") == "build_manifest_missing", details
            assert details.get("not_run_kind") == NotRunKind.BLOCKED.value
    finally:
        restore()
    print("  ✅ 真前端源(.ts)无清单 → 仍 BLOCKED(未被误放)")


# ── 根因#③：确定性 repair 幂等收敛循环（吃下编译器错误掩蔽的级联 typo）──
# 旧实现【单发单重跑】只纠第一层可见 typo，rerun 暴露的下一批级联 typo 漏到慢 LLM
# 循环 → 模型反复写回 → 撞 900s 超时 → FAILED（996db614 实证 531 只纠 17）。
# 治本：修→重跑→再修，直到通过或零新增。下列三测钉死收敛/早停/有界三性质。

def _patch_repair_loop(l1, *, run_seq, repair_seq):
    """monkeypatch _run_l1_command(返回序列) + _attempt_build_repair(返回序列) + 闸门可用。"""
    orig_run = l1._run_l1_command
    orig_app = l1._build_cmd_applicable
    orig_rep = l1._attempt_build_repair
    rc = {"build": 0, "repair": 0}

    def fake_run(cmd, pp, timeout=120):
        i = rc["build"]; rc["build"] += 1
        return run_seq[min(i, len(run_seq) - 1)]

    def fake_repair(pp, out, mods, timeout, stack=None):
        i = rc["repair"]; rc["repair"] += 1
        return repair_seq[min(i, len(repair_seq) - 1)]

    l1._run_l1_command = fake_run
    l1._build_cmd_applicable = lambda cmd, pp: True
    l1._attempt_build_repair = fake_repair

    def restore():
        l1._run_l1_command = orig_run
        l1._build_cmd_applicable = orig_app
        l1._attempt_build_repair = orig_rep
    return rc, restore


def test_build_repair_loop_absorbs_cascade():
    """级联收敛：构建前 3 次失败（每次暴露下一层 typo），repair 每轮纠 1 文件，
    第 4 次构建通过 → L1 PASS。证明【不再单发单重跑】，整条级联当场吃完。"""
    import swarm.worker.l1_pipeline as l1
    from swarm.types import TaskHarness

    FAIL = (1, "main.go:3:5: cannot find symbol: undefined: isEmtpy")
    OK = (0, "")
    # 初始构建(call0)失败 + 3 次重跑(call1,2)失败 + 第4次(call3)通过
    rc, restore = _patch_repair_loop(
        l1, run_seq=[FAIL, FAIL, FAIL, OK],
        repair_seq=[(1, ["f1.go"]), (1, ["f2.go"]), (1, ["f3.go"])],
    )
    try:
        with tempfile.TemporaryDirectory() as d:
            st = _subtask(["main.go"], TaskHarness(language="go", build_command="go build ./..."))
            ok, details = l1.run_l1_pipeline(d, st, _go_diff(), timeout=30)
            assert ok is True, f"级联应被收敛吃完 → PASS, details={details}"
            assert rc["repair"] == 3, f"应跑 3 轮收敛 repair，实际 {rc['repair']}"
            assert rc["build"] == 4, f"初始1+重跑3=4 次构建，实际 {rc['build']}"
            assert details.get("import_repaired_files") == 3
            assert details.get("repaired_file_paths") == ["f1.go", "f2.go", "f3.go"]
            assert "build_failed" not in details
    finally:
        restore()
    print("  ✅ 级联 typo → 多轮确定性收敛 → PASS（不再单发漏级联）")


def test_build_repair_loop_stops_on_no_progress():
    """零进展早停：repair 立刻返回 0（修不动）→ 不空转重跑，构建仍失败 → FAIL。"""
    import swarm.worker.l1_pipeline as l1
    from swarm.types import TaskHarness

    FAIL = (1, "main.go:3:5: cannot find symbol: undefined: HttpServletException")
    rc, restore = _patch_repair_loop(l1, run_seq=[FAIL], repair_seq=[(0, [])])
    try:
        with tempfile.TemporaryDirectory() as d:
            st = _subtask(["main.go"], TaskHarness(language="go", build_command="go build ./..."))
            ok, details = l1.run_l1_pipeline(d, st, _go_diff(), timeout=30)
            assert ok is False, f"修不动应 FAIL, details={details}"
            assert rc["repair"] == 1, "零进展应在第 1 轮就 break"
            assert rc["build"] == 1, "零进展不应触发任何重跑构建"
            assert details.get("build_failed")
            assert "import_repaired_files" not in details
    finally:
        restore()
    print("  ✅ repair 零进展 → 立即停（不空转）→ FAIL")


def test_build_repair_loop_bounded():
    """有界不死循环：repair 每轮都报修了文件但构建始终不过 → 至多 N 轮后停（默认 4）。"""
    import os
    import swarm.worker.l1_pipeline as l1
    from swarm.types import TaskHarness

    FAIL = (1, "main.go:3:5: cannot find symbol")
    rc, restore = _patch_repair_loop(l1, run_seq=[FAIL], repair_seq=[(1, ["x.go"])])
    old = os.environ.get("SWARM_WORKER_BUILD_REPAIR_ROUNDS")
    os.environ["SWARM_WORKER_BUILD_REPAIR_ROUNDS"] = "4"
    try:
        with tempfile.TemporaryDirectory() as d:
            st = _subtask(["main.go"], TaskHarness(language="go", build_command="go build ./..."))
            ok, details = l1.run_l1_pipeline(d, st, _go_diff(), timeout=30)
            assert ok is False, "始终修不动应 FAIL"
            assert rc["repair"] == 4, f"应至多 4 轮，实际 {rc['repair']}（死循环风险！）"
            assert details.get("build_failed")
    finally:
        if old is None:
            os.environ.pop("SWARM_WORKER_BUILD_REPAIR_ROUNDS", None)
        else:
            os.environ["SWARM_WORKER_BUILD_REPAIR_ROUNDS"] = old
        restore()
    print("  ✅ 始终修不动 → 有界 4 轮后停（非死循环）→ FAIL")


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
