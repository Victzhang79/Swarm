#!/usr/bin/env python3
"""round21 全节点假绿/落盘门 — 两个确定性 HIGH 治本回归。

H1(空 scope 假绿门)：tech_design/plan 返回空但合法(file_plan=[])→所有子任务写 scope 皆空→只能产空
diff→"DONE 零放弃"下沿 tech_design→plan→validate→confirm→dispatch 直穿判成功交付(空交付假 DONE)。
plan_validator 加确定性下限：整 plan 零写 scope → 硬失败。

H-exec2(revert 误删兄弟产物)：放弃子任务清 footprint 时，若文件被【其它已完成子任务】拥有为有效产物
→ 跳过删除(窄加性守卫，不碰 round15 红线恢复层)。
"""
from __future__ import annotations

import importlib.util
import subprocess
import tempfile
from pathlib import Path

_bs = Path(__file__).resolve().parent / "swarm_bootstrap.py"
_spec = importlib.util.spec_from_file_location("swarm_bootstrap", _bs)
_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_mod)

from swarm.brain.plan_validator import validate_plan_structure  # noqa: E402
from swarm.brain import nodes as bn  # noqa: E402
from swarm.types import FileScope, SubTask, TaskPlan, WorkerOutput  # noqa: E402


# ── H1：空写 scope 的 plan 必须 fail-closed ──

def test_h1_empty_write_scope_plan_rejected():
    plan = TaskPlan(subtasks=[
        SubTask(id="st-1", description="空实现", scope=FileScope(readable=["x.java"])),
        SubTask(id="st-2", description="也空", scope=FileScope()),
    ])
    res = validate_plan_structure(plan)
    assert res.valid is False, "零写 scope 的空计划必须拒绝(防空 diff 假 DONE)"
    assert any("空计划" in i or "可产出改动" in i for i in res.issues), res.issues
    print("  ✅ H1 整 plan 零写 scope → fail-closed")


def test_h1_plan_with_writer_passes_floor():
    # 至少一个子任务有 writable/create → 过 H1 下限(其余结构校验另说)
    plan = TaskPlan(subtasks=[
        SubTask(id="st-1", description="建实体",
                scope=FileScope(create_files=["src/A.java"])),
    ])
    res = validate_plan_structure(plan)
    # 不应因 H1 下限被拒(可能因别的结构规则,但不含"空计划"issue)
    assert not any("空计划" in i or "可产出改动" in i for i in res.issues), res.issues
    print("  ✅ H1 有写者的 plan 不被空计划下限误拒")


# ── H-exec2：revert 窄守卫护住兄弟产物 ──

def _init_repo():
    d = tempfile.mkdtemp()
    subprocess.run(["git", "init", "-q"], cwd=d, check=True, capture_output=True)
    subprocess.run(["git", "-C", d, "config", "user.email", "t@t"], capture_output=True)
    subprocess.run(["git", "-C", d, "config", "user.name", "t"], capture_output=True)
    return d


def test_hexec2_protected_sibling_product_not_deleted():
    d = _init_repo()
    root = Path(d)
    # 兄弟(已完成)的有效产物 + 失败子任务自己的新建文件（footprint 均含）
    (root / "src").mkdir()
    (root / "src/Shared.java").write_text("class Shared {}\n")   # 兄弟 st-ok 拥有
    (root / "src/Own.java").write_text("class Own {}\n")          # 失败 st-bad 独有
    st_bad = SubTask(id="st-bad", description="失败者",
                     scope=FileScope(create_files=["src/Shared.java", "src/Own.java"]))
    st_ok = SubTask(id="st-ok", description="已完成兄弟",
                    scope=FileScope(create_files=["src/Shared.java"]))
    subtasks = [st_bad, st_ok]
    results = {"st-ok": WorkerOutput(subtask_id="st-ok", diff="d", summary="ok", l1_passed=True)}
    protected = bn._files_owned_by_completed(subtasks, results, exclude_ids={"st-bad"})
    assert "src/Shared.java" in protected and "src/Own.java" not in protected
    rev = bn._local_tree_revert_subtask(d, st_bad, protected_files=protected)
    # Shared.java(兄弟产物)被护住不删；Own.java(失败者独有)被清
    assert (root / "src/Shared.java").exists(), "兄弟已完成产物绝不能被放弃 revert 误删"
    assert "src/Shared.java" in rev.get("skipped_protected", [])
    assert not (root / "src/Own.java").exists(), "失败者独有 footprint 应被清"
    print("  ✅ H-exec2 revert 护住兄弟产物、只清失败者独有足迹")


def test_hexec2_no_protection_reverts_all_footprint():
    d = _init_repo()
    root = Path(d)
    (root / "src").mkdir()
    (root / "src/Own.java").write_text("class Own {}\n")
    st_bad = SubTask(id="st-bad", description="失败者",
                     scope=FileScope(create_files=["src/Own.java"]))
    rev = bn._local_tree_revert_subtask(d, st_bad, protected_files=set())
    assert not (root / "src/Own.java").exists()
    assert not rev.get("skipped_protected")
    print("  ✅ H-exec2 无保护集时按原行为清足迹(不回归)")


if __name__ == "__main__":
    test_h1_empty_write_scope_plan_rejected()
    test_h1_plan_with_writer_passes_floor()
    test_hexec2_protected_sibling_product_not_deleted()
    test_hexec2_no_protection_reverts_all_footprint()
    print("\n✅ 全部通过：round21 假绿/落盘门 H1 + H-exec2 治本")
