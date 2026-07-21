"""B1 静态深读修复红测试：#47 drop_remap 链展平 / #48 _module_of 归一 / #52 收养容量约束。"""
from __future__ import annotations

from swarm.brain.nodes.planning_core import _module_of
from swarm.brain.plan_batch import dedupe_subtasks
from swarm.brain.plan_finisher import _synthesize_orphan_subtasks
from swarm.brain.plan_validator import MAX_WRITABLE_FILES_PER_SUBTASK
from swarm.types import FileScope, SubTask, TaskHarness, TaskPlan


# ── #47 drop_remap 链展平（3 同签名脚手架 → 消费者依赖解析到最终 survivor）──────────
def test_47_drop_remap_chain_resolves_to_terminal_survivor():
    # 3 个同签名脚手架（都 create 同一 pom），依赖数递减 → 链式顶替 {st1:st2, st2:st3}。
    def _d(sid, deps):
        # 用普通源文件（pom.xml 等构建清单被 _norm_paths 排除出新建交付物签名，不参与去重）。
        return {"id": sid, "scope": {"create_files": ["m/Base.java"], "writable": []},
                "depends_on": deps}
    st1 = _d("st1", ["x1", "x2"])
    st2 = _d("st2", ["x1"])
    st3 = _d("st3", [])
    consumer = {"id": "cons", "scope": {"create_files": ["m/Foo.java"], "writable": []},
                "depends_on": ["st1"]}
    out = dedupe_subtasks([st1, st2, st3, consumer])
    ids = {s["id"] for s in out}
    assert "st3" in ids and "st1" not in ids and "st2" not in ids, ids
    cons_out = next(s for s in out if s["id"] == "cons")
    assert cons_out["depends_on"] == ["st3"], (
        f"消费者依赖未展平到最终 survivor（单跳会得到已丢弃的 st2）：{cons_out['depends_on']}")


# ── #48 _module_of 归一 './mod' → 'mod'（不返回 '.'）─────────────────────────────
def test_48_module_of_normalizes_dot_slash_prefix():
    assert _module_of(["./ruoyi-alarm/X.java"]) == "ruoyi-alarm"
    assert _module_of(["ruoyi-alarm/X.java"]) == "ruoyi-alarm"
    assert _module_of(["/ruoyi-alarm/X.java"]) == "ruoyi-alarm"


def test_48_module_of_excludes_dot_and_layout():
    assert _module_of(["src/main/java/X.java"]) is None
    assert _module_of(["./X.java"]) is None   # 归一后无 '/'，不是模块


# ── #52 收养分支容量约束（溢出落新分片，host 不超硬上限）────────────────────────
def _st(sid, *, create=None, writable=None):
    return SubTask(
        id=sid, description="d",
        scope=FileScope(writable=writable or [], create_files=create or [], readable=[]),
        harness=TaskHarness(language="java"),
    )


def test_52_adopt_respects_capacity_overflows_to_new_chunks():
    mod = "ruoyi-alarm"
    # host 已含 10 个 create_files；再来 8 个同模块孤儿 → 收养会撑爆（>12）→ 必须溢出分片。
    host = _st(f"st-fileplan-{mod}", create=[f"{mod}/A{i}.java" for i in range(10)])
    plan = TaskPlan(subtasks=[host])
    orphans = [f"{mod}/B{i}.java" for i in range(8)]
    created = _synthesize_orphan_subtasks(plan, orphans, file_plan=[], project_path=None)
    assert created, "溢出孤儿未被安置"
    for st in plan.subtasks:
        n = len(st.scope.writable or [])
        assert n <= MAX_WRITABLE_FILES_PER_SUBTASK, f"{st.id} writable={n} 超硬上限"
    # 所有孤儿都被安置（host 收养一部分 + 新分片承接其余）
    placed = set()
    for st in plan.subtasks:
        placed |= set(st.scope.create_files or []) | set(st.scope.writable or [])
    assert set(orphans) <= placed, f"孤儿丢失: {set(orphans) - placed}"
