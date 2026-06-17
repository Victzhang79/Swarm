"""plan_batch 分批拆解纯函数单测（M-1/M-3/M-4）。"""
import math

from swarm.brain.plan_batch import (
    batch_progress_line,
    compute_batches,
    group_file_plan,
    merge_subtask_batches,
    order_groups_flatten,
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


def test_compute_batches_10pct():
    """125 文件按 10% 分批 → 每批 13(ceil(12.5)) → 约 10 批。"""
    fp = [_fp(f"m{i % 5}/F{i}.java", module=f"m{i % 5}") for i in range(125)]
    batches = compute_batches(fp, ratio=0.1)
    bs = math.ceil(125 * 0.1)  # 13
    assert all(len(b) <= bs for b in batches), [len(b) for b in batches]
    # 总文件守恒
    assert sum(len(b) for b in batches) == 125
    # 批数 ≈ ceil(125/13) = 10
    assert len(batches) == math.ceil(125 / bs)


def test_compute_batches_small_single():
    """小 file_plan(如 5 文件) → 单批即全部(等价原路径)。"""
    fp = [_fp(f"m/F{i}.java", module="m") for i in range(5)]
    batches = compute_batches(fp, ratio=0.1)
    assert sum(len(b) for b in batches) == 5


def test_order_groups_depends_on_topo():
    """depends_on 跨组依赖 → 被依赖组排前。"""
    fp = [
        _fp("svc/AlarmService.java", module="service", depends_on=["dao/AlarmMapper.java"]),
        _fp("dao/AlarmMapper.java", module="dao", depends_on=["entity/Alarm.java"]),
        _fp("entity/Alarm.java", module="entity"),
    ]
    flat = order_groups_flatten(fp)
    paths = [x["path"] for x in flat]
    # entity 必须在 dao 之前，dao 在 service 之前
    assert paths.index("entity/Alarm.java") < paths.index("dao/AlarmMapper.java")
    assert paths.index("dao/AlarmMapper.java") < paths.index("svc/AlarmService.java")


def test_order_groups_layer_fallback():
    """无 depends_on → 按分层序(entity 先于 controller)。"""
    fp = [_fp("c/FooController.java", module="controller"),
          _fp("e/Foo.java", module="entity")]
    flat = order_groups_flatten(fp)
    paths = [x["path"] for x in flat]
    assert paths.index("e/Foo.java") < paths.index("c/FooController.java")


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
