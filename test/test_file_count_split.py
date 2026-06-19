"""RUN13 治本回归：单子任务文件数上限 → 确定性按层拆分。

9 文件垂直切片(entity+vo+dto+mapper+xml+service+impl+controller+...)的 CODING 单阶段就
~560s 撞预算 → VERIFY 超时 → 重试死循环。治本：PLAN 端按层把它拆成 ≤MAX_FILES_PER_SUBTASK
文件的子任务，每个只面对一层，编码确定性高、稳进预算。

验证：
- _oversized_by_files：文件数(create+writable)超上限才判超标，与上下文预算正交。
- _split_oversized_by_files：确定性按层切批、每批≤上限、串行链、scope 独立深拷贝、create/write 归位。
- _needs_resplit：文件数超标即使 est_tokens 在预算内也触发拆分；单文件守卫不误伤。
- _layer_rank：耦合层(mapper+xml、service+impl)排序相邻；下游层秩大于上游层。
"""

from __future__ import annotations

from swarm.brain.planning_nodes import (
    MAX_FILES_PER_SUBTASK,
    _layer_rank,
    _needs_resplit,
    _oversized_by_files,
    _split_oversized_by_files,
)
from swarm.types import FileScope, SubTask, SubTaskDifficulty, SubTaskModality


def _ruoyi_slice(sid="st-6"):
    """典型 RuoYi 垂直切片：9 个新建文件横跨 entity→vo→dto→mapper→xml→service→impl→controller。"""
    base = "ruoyi-modules/alarm/src/main"
    j = f"{base}/java/com/ruoyi/alarm"
    return SubTask(
        id=sid,
        description="实现告警规则 CRUD（含实体、Mapper、Service、Controller 全链路）",
        difficulty=SubTaskDifficulty.COMPLEX,
        modality=SubTaskModality.TEXT,
        scope=FileScope(create_files=[
            f"{j}/domain/AlarmRule.java",
            f"{j}/domain/vo/AlarmRuleVo.java",
            f"{j}/domain/dto/AlarmRuleDto.java",
            f"{j}/mapper/AlarmRuleMapper.java",
            f"{base}/resources/mapper/alarm/AlarmRuleMapper.xml",
            f"{j}/service/IAlarmRuleService.java",
            f"{j}/service/impl/AlarmRuleServiceImpl.java",
            f"{j}/controller/AlarmRuleController.java",
            f"{j}/domain/vo/AlarmRuleListVo.java",
        ]),
        depends_on=["st-1"],
        acceptance_criteria=["告警规则 CRUD 可用"],
        est_context_tokens=40_000,  # 9 小文件，未超 150k 上下文预算——但工作量超时间预算
    )


def test_oversized_by_files_orthogonal_to_token_budget():
    st = _ruoyi_slice()
    # est_tokens 40k 远低于 150k 预算，但 9 文件 > 4 → 仍判超标
    assert _oversized_by_files(st) is True
    assert _needs_resplit(st, budget=150_000) is True, "文件数超标即使预算够也应拆"


def test_small_subtask_not_oversized():
    st = SubTask(
        id="st-x", description="改一处", difficulty=SubTaskDifficulty.MEDIUM,
        scope=FileScope(create_files=["A.java", "B.java"], writable=["C.java"]),
    )
    assert _oversized_by_files(st) is False
    assert _needs_resplit(st, budget=150_000) is False


def test_single_file_guard_still_holds():
    """单文件修改子任务绝不拆(拆了多子任务改同一文件→坏 patch)。"""
    st = SubTask(
        id="st-1", description="改一个大文件", difficulty=SubTaskDifficulty.MEDIUM,
        scope=FileScope(writable=["Big.java"]), est_context_tokens=999_999,
    )
    assert _needs_resplit(st, budget=150_000) is False


def test_split_produces_capped_children():
    st = _ruoyi_slice()
    children = _split_oversized_by_files(st, max_files=MAX_FILES_PER_SUBTASK)
    # 9 文件 / 4 = 3 批
    assert len(children) == 3, f"9 文件应拆 3 批，实际 {len(children)}"
    for c in children:
        n = len(c.scope.create_files) + len(c.scope.writable)
        assert n <= MAX_FILES_PER_SUBTASK, f"{c.id} 文件数 {n} 超上限"
        assert n >= 1, f"{c.id} 不应空批"
    # 全部 create 文件都被分配，无遗漏无重复
    all_files = [f for c in children for f in c.scope.create_files]
    assert sorted(all_files) == sorted(st.scope.create_files), "拆分不得丢/重文件"


def test_split_serial_chain_and_ids():
    st = _ruoyi_slice()
    children = _split_oversized_by_files(st)
    ids = [c.id for c in children]
    assert ids == ["st-6-1", "st-6-2", "st-6-3"]
    # 首批继承父依赖；后续批串行依赖前一批
    assert children[0].depends_on == ["st-1"]
    assert "st-6-1" in children[1].depends_on
    assert "st-6-2" in children[2].depends_on


def test_split_scope_independent_no_aliasing():
    st = _ruoyi_slice()
    children = _split_oversized_by_files(st)
    assert children[0].scope is not children[1].scope
    children[0].scope.create_files = []
    assert children[1].scope.create_files, "改一个子 scope 不应影响兄弟(深拷贝)"


def test_split_layer_ordering():
    """按层排序：entity(domain) 批在 controller 批之前。"""
    st = _ruoyi_slice()
    children = _split_oversized_by_files(st)
    flat = [f for c in children for f in c.scope.create_files]
    # domain/AlarmRule 应排在 controller 之前
    i_entity = next(i for i, f in enumerate(flat) if f.endswith("domain/AlarmRule.java"))
    i_ctrl = next(i for i, f in enumerate(flat) if "controller" in f)
    assert i_entity < i_ctrl, "数据层应排在 Web 层之前(串行链方向=编译依赖方向)"


def test_writables_go_to_last_batch():
    """对既有文件的修改(writable，如注册/pom)垫到最后批。"""
    st = SubTask(
        id="st-7", description="新增 5 文件并注册",
        difficulty=SubTaskDifficulty.COMPLEX,
        scope=FileScope(
            create_files=[f"com/x/A{i}.java" for i in range(5)],
            writable=["pom.xml"],
        ),
    )
    children = _split_oversized_by_files(st, max_files=MAX_FILES_PER_SUBTASK)
    assert "pom.xml" in children[-1].scope.writable, "writable 应在最后批"
    for c in children[:-1]:
        assert not c.scope.writable, "非末批不应含 writable"


def test_layer_rank_couples_adjacent():
    """耦合层排序相邻：mapper 紧邻 mapperxml，service 紧邻 serviceimpl。"""
    assert _layer_rank("a/mapper/X.java")[0] < _layer_rank("a/resources/mapper/X.xml")[0]
    assert _layer_rank("a/resources/mapper/X.xml")[0] < _layer_rank("a/service/IX.java")[0]
    assert _layer_rank("a/service/IX.java")[0] < _layer_rank("a/service/impl/X.java")[0]
    assert _layer_rank("a/service/impl/X.java")[0] < _layer_rank("a/controller/X.java")[0]
    # 未知层垫后
    assert _layer_rank("a/random/Weird.txt")[0] > _layer_rank("a/controller/X.java")[0]


def test_no_split_when_within_cap():
    st = SubTask(
        id="st-z", description="3 文件", difficulty=SubTaskDifficulty.MEDIUM,
        scope=FileScope(create_files=["A.java", "B.java", "C.java"]),
    )
    out = _split_oversized_by_files(st)
    assert out == [st], "未超上限应原样返回"


if __name__ == "__main__":
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for fn in fns:
        fn()
        print(f"  ✅ {fn.__name__}")
    print(f"\n=== 文件数上限按层拆分: {len(fns)}/{len(fns)} passed ===")
