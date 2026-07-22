"""plan_batch 分批拆解纯函数单测（M-1/M-3/M-4）。"""
import math

from swarm.brain.plan_batch import (
    batch_progress_line,
    group_file_plan,
    merge_subtask_batches,
)


def _fp(path, module=None, depends_on=None, action="create"):
    d = {"path": path, "action": action, "responsibility": "x"}
    if module:
        d["module"] = module
    if depends_on:
        d["depends_on"] = depends_on
    return d


def test_group_by_module_field_priority():
    """module 字段优先分组。"""
    fp = [_fp("a/X.java", module="alarm"), _fp("b/Y.java", module="alarm"),
          _fp("c/Z.java", module="schedule")]
    g = group_file_plan(fp)
    assert set(g.keys()) == {"alarm", "schedule"}
    assert len(g["alarm"]) == 2 and len(g["schedule"]) == 1


def test_group_path_prefix_fallback():
    """无 module 字段时按路径前缀推断业务分组。"""
    fp = [_fp("ruoyi-system/src/main/java/com/ruoyi/alarm/task/AlarmTask.java"),
          _fp("ruoyi-system/src/main/java/com/ruoyi/alarm/channel/Channel.java")]
    g = group_file_plan(fp)
    # 两文件都在 alarm 下的不同子模块，应分到 alarm/task 和 alarm/channel
    assert all("alarm" in k for k in g.keys()), g.keys()
    total = sum(len(v) for v in g.values())
    assert total == 2


def test_grouping_no_loss_no_dup():
    """分组无遗漏无重复：所有文件都落到某组，总数守恒。"""
    fp = [_fp(f"mod{i % 3}/F{i}.java", module=f"mod{i % 3}") for i in range(50)]
    g = group_file_plan(fp)
    total = sum(len(v) for v in g.values())
    assert total == 50, f"分组后文件数应守恒: {total}"
    # 无重复：每个 path 只出现一次
    seen = set()
    for items in g.values():
        for x in items:
            assert x["path"] not in seen
            seen.add(x["path"])


def test_merge_global_unique_ids_and_serial_dep():
    """合并多批：id 全局唯一 + 后批首子任务依赖前批末子任务(串行门控)。"""
    b1 = [{"id": "st-1", "description": "a"}, {"id": "st-2", "description": "b"}]
    b2 = [{"id": "st-1", "description": "c", "depends_on": []}]  # 批内 id 会冲突,需重编
    merged = merge_subtask_batches([b1, b2])
    ids = [m["id"] for m in merged]
    assert len(ids) == len(set(ids)) == 3, f"id 应全局唯一: {ids}"
    # 第3个(批2首)应依赖批1末尾(st-2 重编后)
    last_b1 = merged[1]["id"]
    assert last_b1 in merged[2]["depends_on"], f"批间串行依赖缺失: {merged[2]}"


def test_merge_remaps_intra_batch_depends():
    """批内 depends_on 旧 id 应重映射到新 id。"""
    b1 = [{"id": "x1", "description": "a"},
          {"id": "x2", "description": "b", "depends_on": ["x1"]}]
    merged = merge_subtask_batches([b1])
    # x2→st-2 应依赖 x1→st-1
    assert merged[1]["depends_on"] == ["st-1"], merged[1]


def test_progress_line_format():
    line = batch_progress_line(3, 11, 12, llm_seconds=43.2)
    assert "批 3/11" in line and "27%" in line and "文件数=12" in line and "43.2s" in line


def test_dedupe_same_basename():
    """P5（R67-1 收权）：不同路径同名文件【不再】静默剪——"保留首个"是无证据挑边
    （round67 duty 域剪错方向实锤）。重复裁决交 #110/#101/T1b 有证据闸。"""
    from swarm.brain.plan_batch import dedupe_file_plan
    fp = [_fp("channel/INotifyService.java", module="channel"),
          _fp("engine/INotifyService.java", module="engine"),
          _fp("channel/Foo.java", module="channel")]
    d = dedupe_file_plan(fp)
    paths = [x["path"] for x in d]
    assert len(d) == 3, f"跨路径同名不得静默剪: {paths}"


def test_dedupe_exact_path():
    """完全同路径也去重。"""
    from swarm.brain.plan_batch import dedupe_file_plan
    fp = [_fp("a/X.java"), _fp("a/X.java"), _fp("b/Y.java")]
    assert len(dedupe_file_plan(fp)) == 2


def test_module_batches_one_per_module():
    """P1：按模块分批——每模块一批（不再 10% 机械切）。"""
    from swarm.brain.plan_batch import group_into_module_batches
    fp = [_fp(f"m1/F{i}.java", module="m1") for i in range(15)] + \
         [_fp(f"m2/G{i}.java", module="m2") for i in range(5)]
    batches = group_into_module_batches(fp)
    assert len(batches) == 2, "2 模块应 2 批"
    by_mod = dict(batches)
    assert len(by_mod["m1"]) == 15 and len(by_mod["m2"]) == 5


def test_module_batches_dep_order():
    """P2：批间按 tech_design 模块 depends_on 排序。"""
    from swarm.brain.plan_batch import group_into_module_batches
    fp = [_fp("svc/S.java", module="service"), _fp("ent/E.java", module="entity")]
    deps = {"service": ["entity"], "entity": []}
    batches = group_into_module_batches(fp, deps)
    names = [n for n, _ in batches]
    assert names.index("entity") < names.index("service"), f"entity 应先于 service: {names}"


# ── RUN6 复盘(task f3f85f3d)：分批分解把"建 ruoyi-alarm 模块脚手架"拆了两遍
#    (st-1 无依赖 / st-7 依赖倒置依赖填充该模块的 st-6) → 模型对已完工活反复拒答
#    → Brain 循环撞 recursion_limit 崩。merge 后须去重 + 断环治本。 ──
def test_dedupe_drops_duplicate_scaffold_keeps_foundational():
    from swarm.brain.plan_batch import dedupe_subtasks
    subs = [
        {"id": "st-1", "scope": {"create_files": ["ruoyi-alarm/pom.xml"], "writable": ["pom.xml"]}, "depends_on": []},
        {"id": "st-6", "scope": {"create_files": ["ruoyi-alarm/src/X.java"]}, "depends_on": ["st-5"]},
        {"id": "st-7", "scope": {"writable": ["ruoyi-alarm/pom.xml", "pom.xml"]}, "depends_on": ["st-6"]},
        {"id": "st-8", "scope": {"create_files": ["ruoyi-alarm/src/Y.java"]}, "depends_on": ["st-7"]},
    ]
    out = dedupe_subtasks(subs)
    ids = [s["id"] for s in out]
    assert "st-7" not in ids, f"重复脚手架 st-7 应去重: {ids}"
    assert "st-1" in ids, "无依赖的地基 st-1 应保留"
    st8 = next(s for s in out if s["id"] == "st-8")
    assert st8["depends_on"] == ["st-1"], f"依赖被去重者应改指保留者: {st8['depends_on']}"


def test_dedupe_ignores_shared_existing_file_no_special_casing():
    """仅共写既存文件（各模块都合法注册进根 pom）不构成重复，不得误去重。

    判据零生态特判：根 pom 不在任何 create_files → 不入 global_creates → 自动排除。
    三个真实模块各建各的 pom + 都改根 pom，签名 {a/pom}≠{b/pom}≠{c/pom}，全保留。
    """
    from swarm.brain.plan_batch import dedupe_subtasks
    subs = [
        {"id": "st-1", "scope": {"writable": ["pom.xml"], "create_files": ["a/pom.xml"]}, "depends_on": []},
        {"id": "st-2", "scope": {"writable": ["pom.xml"], "create_files": ["b/pom.xml"]}, "depends_on": []},
        {"id": "st-3", "scope": {"writable": ["pom.xml"], "create_files": ["c/pom.xml"]}, "depends_on": []},
    ]
    out = dedupe_subtasks(subs)
    assert {s["id"] for s in out} == {"st-1", "st-2", "st-3"}, "不同模块脚手架不应被共享根文件误判重复"


def test_dedupe_is_ecosystem_agnostic():
    """去重判据内生于计划，对任意构建生态一视同仁（非 Maven/RuoYi 特判）。

    Go：go.mod 是既存共享文件（只被 modify，不在 create_files），两子任务重复建同一新包
    pkg/alarm/alarm.go（一个 create 一个 writable 口径分歧）→ 仍判重，与 Maven 同理。
    """
    from swarm.brain.plan_batch import dedupe_subtasks
    go = [
        {"id": "g1", "scope": {"create_files": ["pkg/alarm/alarm.go"], "writable": ["go.mod"]}, "depends_on": []},
        {"id": "g2", "scope": {"writable": ["pkg/alarm/alarm.go", "go.mod"]}, "depends_on": ["g1"]},
    ]
    out = dedupe_subtasks(go)
    assert {s["id"] for s in out} == {"g1"}, "go.mod 生态同样判重，证明零生态特判"


def test_dedupe_merges_covers_and_criteria_into_survivor():
    """S2 复核 F1：keep-first 去重的守恒面——被丢弃者的 covers（需求覆盖声明）与
    acceptance_criteria（含机器写入的依赖/登记声明）并集并入 survivor；丢了 covers
    覆盖矩阵会误判"未覆盖"白烧 plan 重试，丢了 criteria 机器约定随副本蒸发。"""
    from swarm.brain.plan_batch import dedupe_subtasks
    a = {"id": "st-1", "scope": {"create_files": ["m/App.java"]}, "depends_on": [],
         "covers": ["req-aaaa1111"],
         "acceptance_criteria": ["模块可编译", "pom 必须声明依赖 [x]"]}
    b = {"id": "st-7", "scope": {"create_files": ["m/App.java"]}, "depends_on": ["st-6"],
         "covers": ["req-bbbb2222", "req-aaaa1111"],
         "acceptance_criteria": ["模块可编译", "在根 pom.xml <modules> 登记全部新模块"]}
    out = dedupe_subtasks([a, b])
    assert [s["id"] for s in out] == ["st-1"], "同签名去重保留更地基者（既有行为）"
    surv = out[0]
    assert surv["covers"] == ["req-aaaa1111", "req-bbbb2222"], "covers 并集去重、survivor 在前"
    assert surv["acceptance_criteria"] == [
        "模块可编译", "pom 必须声明依赖 [x]", "在根 pom.xml <modules> 登记全部新模块",
    ], "criteria 并集去重，机器写入声明不蒸发"


def test_dedupe_merges_when_later_subtask_is_more_foundational():
    """F1 对称面：后到者更地基（顶替 prev）方向同样并入被丢弃者的 covers/criteria。"""
    from swarm.brain.plan_batch import dedupe_subtasks
    first = {"id": "st-2", "scope": {"create_files": ["m/App.java"]},
             "depends_on": ["st-1"], "covers": ["req-cccc3333"],
             "acceptance_criteria": ["约定甲"]}
    later_foundational = {"id": "st-5", "scope": {"create_files": ["m/App.java"]},
                          "depends_on": [], "covers": ["req-dddd4444"],
                          "acceptance_criteria": ["约定乙"]}
    out = dedupe_subtasks([first, later_foundational])
    assert [s["id"] for s in out] == ["st-5"], "依赖更少者顶替（既有行为）"
    assert out[0]["covers"] == ["req-dddd4444", "req-cccc3333"]
    assert out[0]["acceptance_criteria"] == ["约定乙", "约定甲"]


def test_merge_subtask_batches_end_to_end_preserves_dropped_covers():
    """F1 端到端：跨批同签名去重穿过 merge_subtask_batches（重编号+串行门控）后，
    survivor 兼具两批的覆盖声明。"""
    from swarm.brain.plan_batch import merge_subtask_batches
    batch1 = [{"id": "st-1", "scope": {"create_files": ["m/App.java"]},
               "depends_on": [], "covers": ["req-aaaa1111"]}]
    batch2 = [{"id": "st-1", "scope": {"create_files": ["m/App.java"]},
               "depends_on": [], "covers": ["req-bbbb2222"]}]
    merged = merge_subtask_batches([batch1, batch2])
    assert len(merged) == 1, "跨批重复脚手架应去重"
    assert merged[0]["covers"] == ["req-aaaa1111", "req-bbbb2222"]


def test_break_dependency_cycles_and_dangling():
    from swarm.brain.plan_batch import break_dependency_cycles
    subs = [
        {"id": "a", "depends_on": ["b"]},
        {"id": "b", "depends_on": ["a"]},          # 与 a 成环
        {"id": "c", "depends_on": ["ghost"]},      # 悬空依赖
        {"id": "d", "depends_on": ["d"]},          # 自指
    ]
    out = break_dependency_cycles(subs)
    deps = {s["id"]: s["depends_on"] for s in out}
    assert not (deps["a"] and deps["b"]), f"环应断开一条边: {deps}"
    assert deps["c"] == [], "悬空依赖应剔除"
    assert deps["d"] == [], "自指应剔除"


def test_merge_applies_dedupe_and_cycle_break():
    """merge_subtask_batches 端到端：跨批重复脚手架被去重。"""
    from swarm.brain.plan_batch import merge_subtask_batches
    batch_a = [{"id": "s1", "scope": {"create_files": ["mod/pom.xml"]}, "depends_on": []}]
    batch_b = [{"id": "s1", "scope": {"writable": ["mod/pom.xml"]}, "depends_on": []}]
    merged = merge_subtask_batches([batch_a, batch_b])
    sigs = [s for s in merged if "mod/pom.xml" in (
        (s.get("scope") or {}).get("create_files", []) + (s.get("scope") or {}).get("writable", []))]
    assert len(sigs) == 1, f"跨批重复的 mod/pom.xml 脚手架应只剩一个: {[s['id'] for s in merged]}"


def test_resolve_brain_recursion_limit_scales():
    from swarm.tracing import resolve_brain_recursion_limit as R, BRAIN_RECURSION_LIMIT
    assert R(None, 45) == 45 * 4 + 40, "按子任务数 4×+40"
    assert R("ultra") == 300, "ultra 档兜底 300"
    assert R("complex") == 150
    assert R(None, 1) == BRAIN_RECURSION_LIMIT, "小计划不低于 floor"
    assert R("trivial") == BRAIN_RECURSION_LIMIT
    assert R(None, 200) == 200 * 4 + 40, "超大计划继续放大"
