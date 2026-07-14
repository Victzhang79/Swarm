"""R57-6 治本锁：MERGE 多写者裁决必须认【声明写权的 owner】，不认"碰过"的子任务。

★round57 实锤（MERGE 死循环）★
worker 的**确定性修复**（版本注入 / 依赖合法性闸 / 模块注册）会在自己的沙箱里改**别的模块**
的 pom（实测 st-16 的 `repaired_file_paths` = ['pom.xml', 'alarm-web/pom.xml', 'module/pom.xml']，
而它的 scope 只有 `ruoyi-alarm/alarm-security/` 两个文件）。这些足迹随 pull-back 进了它的 diff
→ **每个 worker 都成了全树 pom 的"写者"**。

旧裁决按"拓扑最上游"选 → 选中一个**碰过但不拥有**该文件的子任务，把真 owner（脚手架的
**确定性权威模板**，R45-2 的全部意义）丢进 rebase 重生成 → 重做 → 修复又碰 → 又多写者
→ **不收敛**（实测两轮 MERGE 的 rebase=10 冲突集完全相同）。
"""
from __future__ import annotations

from swarm.brain.merge_engine import merge_diffs


def _new_file_diff(sid_path: str, body: str) -> str:
    lines = body.splitlines()
    return (f"diff --git a/{sid_path} b/{sid_path}\n--- /dev/null\n+++ b/{sid_path}\n"
            f"@@ -0,0 +1,{len(lines)} @@\n" + "".join(f"+{ln}\n" for ln in lines))


def test_owner_wins_over_repair_footprint_and_non_owner_is_not_rebased():
    """owner（脚手架）的内容胜出；非 owner（只是被确定性修复碰过）→ 丢弃其版本且**不进 rebase**。"""
    f = "alarm-core/pom.xml"
    diffs = [
        ("st-16", _new_file_diff(f, "<project>LLM 手写版</project>")),          # 只是修复碰过
        ("st-scaffold-alarm-core", _new_file_diff(f, "<project>确定性权威模板</project>")),
    ]
    res = merge_diffs(
        diffs, base_reader=lambda _p: None, subtask_order=["st-16", "st-scaffold-alarm-core"],
        file_owner={f: "st-scaffold-alarm-core"}.get,
    )
    assert res.success
    assert "确定性权威模板" in res.merged_diff, "必须取 owner（脚手架的确定性模板）"
    assert "LLM 手写版" not in res.merged_diff
    assert not getattr(res, "rebase_subtask_ids", None), (
        "非 owner 只是'碰过'→ 绝不能标 rebase 重做（重做多少次修复还会碰它 → 这正是死循环的根源）")


def test_without_owner_evidence_falls_back_to_topological_choice():
    """无 owner 证据（谁都没声明写权）→ 退回旧行为（拓扑最上游），不回归。"""
    f = "x/pom.xml"
    diffs = [("st-b", _new_file_diff(f, "B")), ("st-a", _new_file_diff(f, "A"))]
    res = merge_diffs(diffs, base_reader=lambda _p: None,
                      subtask_order=["st-a", "st-b"], file_owner=lambda _f: None)
    assert res.success and "A" in res.merged_diff
