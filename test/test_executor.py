#!/usr/bin/env python3
"""worker/executor.py 真实单测（纯逻辑 + 本地模式 diff 快照）。

不跑完整 Worker 生命周期（需 LLM/沙箱），聚焦可确定性验证的部分：
scope 文件收集、路径归一化、LLM L1 自报解析、以及之前修复的「本地执行模式
diff 快照」——_snapshot_scope_local + _get_git_diff 在无沙箱时也能正确产出 diff。
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

_bs = Path(__file__).resolve().parent / "swarm_bootstrap.py"
_spec = importlib.util.spec_from_file_location("swarm_bootstrap", _bs)
_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_mod)

from swarm.types import FileScope, SubTask, SubTaskDifficulty
from swarm.worker.executor import WorkerExecutor


def _executor(tmp_path, writable=None, readable=None):
    st = SubTask(
        id="sub-1",
        description="测试子任务",
        difficulty=SubTaskDifficulty.MEDIUM,
        scope=FileScope(
            writable=writable if writable is not None else ["a.py"],
            readable=readable if readable is not None else ["a.py"],
        ),
    )
    return WorkerExecutor(st, project_path=str(tmp_path), project_id="p1", task_id="t1")


# ── __init__ 初始化快照属性（修复回归保护）─────


def test_init_snapshot_attrs(tmp_path):
    ex = _executor(tmp_path)
    # 修复点：必须在 __init__ 初始化，否则本地模式 _get_git_diff 拿不到基线
    assert ex._pre_sync_contents == {}
    assert ex._post_sync_contents == {}
    print("  ✅ __init__ 初始化 _pre/_post_sync_contents")


# ── scope 文件收集 ───────────────────────────────


def test_scope_files_union_dedup(tmp_path):
    ex = _executor(tmp_path, writable=["w.py"], readable=["r.py", "w.py"])
    files = ex._scope_files()
    # readable ∪ writable 去重保序
    assert set(files) == {"r.py", "w.py"}
    print("  ✅ _scope_files readable∪writable 去重")


def test_writable_files_only(tmp_path):
    ex = _executor(tmp_path, writable=["w1.py", "w2.py"], readable=["r.py"])
    assert ex._writable_files() == ["w1.py", "w2.py"]
    print("  ✅ _writable_files 只取 writable")


def test_norm_rel_relative(tmp_path):
    assert WorkerExecutor._norm_rel(tmp_path, "sub/x.py") == "sub/x.py"
    # 前导斜杠的相对路径被 lstrip
    assert WorkerExecutor._norm_rel(tmp_path, "x.py") == "x.py"
    print("  ✅ _norm_rel 相对路径归一化")


def test_norm_rel_absolute_inside(tmp_path):
    abs_path = str(tmp_path / "pkg" / "m.py")
    assert WorkerExecutor._norm_rel(tmp_path, abs_path) == "pkg/m.py"
    print("  ✅ _norm_rel 绝对路径(root 内)→相对")


def test_norm_rel_absolute_outside(tmp_path):
    # root 外的绝对路径退化为文件名
    assert WorkerExecutor._norm_rel(tmp_path, "/etc/passwd") == "passwd"
    print("  ✅ _norm_rel root 外绝对路径退化文件名")


# ── LLM L1 自报解析 ──────────────────────────────


def test_parse_l1_explicit_pass(tmp_path):
    ex = _executor(tmp_path)
    ok, details = ex._parse_l1_result("一切正常\nL1_RESULT: PASS\n")
    assert ok is True
    assert details["llm_self_report"] == "pass"
    print("  ✅ _parse_l1_result 显式 PASS 标记")


def test_parse_l1_explicit_fail(tmp_path):
    ex = _executor(tmp_path)
    ok, _ = ex._parse_l1_result("L1_RESULT: FAIL")
    assert ok is False
    print("  ✅ _parse_l1_result 显式 FAIL 标记")


def test_parse_l1_fail_signal_overrides(tmp_path):
    ex = _executor(tmp_path)
    # 无显式标记，出现失败信号即判未通过（保守）
    ok, _ = ex._parse_l1_result("测试通过了一部分，但有 error")
    assert ok is False
    print("  ✅ _parse_l1_result 失败信号保守判未通过")


def test_parse_l1_pass_only(tmp_path):
    ex = _executor(tmp_path)
    ok, _ = ex._parse_l1_result("全部测试通过 ✅")
    assert ok is True
    print("  ✅ _parse_l1_result 仅通过信号")


# ── 本地模式快照 + diff（修复验证）─────────────


def test_snapshot_scope_local_reads_files(tmp_path):
    (tmp_path / "a.py").write_text("print('hello')\n", encoding="utf-8")
    ex = _executor(tmp_path, writable=["a.py"], readable=["a.py"])
    snap = ex._snapshot_scope_local(tmp_path)
    assert snap["a.py"] == "print('hello')\n"
    print("  ✅ _snapshot_scope_local 读取本地文件内容")


def test_snapshot_scope_local_missing_file(tmp_path):
    ex = _executor(tmp_path, writable=["ghost.py"], readable=["ghost.py"])
    snap = ex._snapshot_scope_local(tmp_path)
    # 不存在的文件记为空串（新增基线）
    assert snap["ghost.py"] == ""
    print("  ✅ _snapshot_scope_local 缺失文件记空串")


def test_local_mode_diff_end_to_end(tmp_path):
    """复现并验证修复：无沙箱本地模式应产出真实 diff，而非'(无变更)'。"""
    import asyncio

    f = tmp_path / "a.py"
    f.write_text("x = 1\n", encoding="utf-8")
    ex = _executor(tmp_path, writable=["a.py"], readable=["a.py"])
    # 无沙箱 → 本地模式：pre 快照
    asyncio.run(ex._sync_to_sandbox("bootstrap"))
    assert ex._pre_sync_contents.get("a.py") == "x = 1\n"

    # 模拟 agent 就地改文件
    f.write_text("x = 2\n", encoding="utf-8")

    # 无沙箱 → 本地模式：post 快照
    asyncio.run(ex._sync_from_sandbox("产出"))
    assert ex._post_sync_contents.get("a.py") == "x = 2\n"

    diff = ex._get_git_diff()
    assert diff != "(无变更)", "本地模式应产出真实 diff（修复回归）"
    assert "-x = 1" in diff and "+x = 2" in diff
    print("  ✅ 本地模式 end-to-end 产出真实 diff（修复验证）")


def test_local_mode_no_change_diff(tmp_path):
    """文件未改时本地模式应正确报告无变更。"""
    import asyncio

    f = tmp_path / "a.py"
    f.write_text("x = 1\n", encoding="utf-8")
    ex = _executor(tmp_path, writable=["a.py"], readable=["a.py"])
    asyncio.run(ex._sync_to_sandbox("bootstrap"))
    asyncio.run(ex._sync_from_sandbox("产出"))
    assert ex._get_git_diff() == "(无变更)"
    print("  ✅ 本地模式 文件未改→(无变更)")


if __name__ == "__main__":
    import tempfile

    for name, fn in list(globals().items()):
        if name.startswith("test_") and callable(fn):
            with tempfile.TemporaryDirectory() as d:
                fn(Path(d))
    print("\nexecutor 单测通过。")
