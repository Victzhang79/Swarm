"""RUN13(预算)+RUN14(契约漂移)双治本回归：按【实体】拆分,绝不拆穿一个实体的全栈。

- RUN13：9 文件子任务 CODING 560s 撞预算 → 治法 A=900s 兜底 + 大子任务拆小。
- RUN14：按【层】拆把 service 接口与调用它的 controller 拆进不同子任务 → 两个 worker 各自臆测
  方法签名 → 跨子任务契约漂移 → 整模块编译失败 → st-3 死循环。治法：拆分边界改【实体词干】,
  同一实体全栈(entity+mapper+xml+service+impl+controller)留在一个子任务,签名自洽。

核心不变量：
1. 单实体子任务【绝不拆】(返回 [st]),哪怕文件数超上限 —— 靠 A=900s 容纳,杜绝契约漂移。
2. 多实体子任务按实体拆,每批是【完整实体全栈】,不出现"接口在 A 批、控制器在 B 批"。
3. _entity_stem：同实体各层文件归一到同词干。
"""

from __future__ import annotations

from swarm.brain.planning_nodes import (
    MAX_FILES_PER_SUBTASK,
    _entity_stem,
    _needs_resplit,
    _oversized_by_files,
    _split_oversized_by_files,
)
from swarm.types import FileScope, SubTask, SubTaskDifficulty, SubTaskModality


def _entity_files(entity: str):
    """一个实体的典型 RuoYi 全栈 6 文件。"""
    j = f"ruoyi-alarm/src/main/java/com/ruoyi/alarm"
    return [
        f"{j}/domain/{entity}.java",
        f"{j}/mapper/{entity}Mapper.java",
        f"ruoyi-alarm/src/main/resources/mapper/alarm/{entity}Mapper.xml",
        f"{j}/service/I{entity}Service.java",
        f"{j}/service/impl/{entity}ServiceImpl.java",
        f"{j}/controller/{entity}Controller.java",
    ]


def _single_entity_slice(sid="st-2", entity="AlarmApp"):
    """单实体 6 文件垂直切片(RUN14 st-2 真实形态：全是 AlarmApp)。"""
    return SubTask(
        id=sid, description=f"实现 {entity} 应用秘钥管理完整垂直切片",
        difficulty=SubTaskDifficulty.COMPLEX, modality=SubTaskModality.TEXT,
        scope=FileScope(create_files=_entity_files(entity)),
        depends_on=["st-1"], acceptance_criteria=["可用"], est_context_tokens=40_000,
    )


def _multi_entity_slice(sid="st-20"):
    """多实体子任务(预警任务管理：AlarmTask + AlarmTaskChannel,12 文件)。"""
    return SubTask(
        id=sid, description="实现预警任务管理完整垂直切片",
        difficulty=SubTaskDifficulty.COMPLEX, modality=SubTaskModality.TEXT,
        scope=FileScope(create_files=_entity_files("AlarmTask") + _entity_files("AlarmTaskChannel")),
        depends_on=["st-1"], acceptance_criteria=["可用"], est_context_tokens=60_000,
    )


# ── _entity_stem：同实体各层归一到同词干（RUN14 漂移根因的修复点）──
def test_entity_stem_unifies_all_layers():
    stems = {_entity_stem(f) for f in _entity_files("AlarmApp")}
    assert stems == {"AlarmApp"}, f"AlarmApp 全栈应归一到单一词干,实得 {stems}"


def test_entity_stem_distinguishes_entities():
    assert _entity_stem("a/AlarmTaskController.java") == "AlarmTask"
    assert _entity_stem("a/AlarmTaskChannelController.java") == "AlarmTaskChannel"
    # "AlarmTask" 实体名含 Task,不可被误剥(只剥纯分层后缀)
    assert _entity_stem("a/domain/AlarmTask.java") == "AlarmTask"
    assert _entity_stem("a/service/IAlarmTaskService.java") == "AlarmTask"


# ── 不变量 1：单实体绝不拆(契约自洽优先,靠 A=900 兜底)──
def test_single_entity_never_split():
    st = _single_entity_slice()
    assert _oversized_by_files(st) is True, "6 文件 > 4,触发评估"
    out = _split_oversized_by_files(st)
    assert out == [st], "单实体即使超文件数也不拆(否则接口/控制器分家→契约漂移→死循环)"


def test_single_entity_9_files_still_not_split():
    """单实体哪怕 9 文件(加 vo/dto/额外 controller)也不拆,靠 900s 预算(实测 560s)。"""
    files = _entity_files("AlarmRule") + [
        "ruoyi-alarm/src/main/java/com/ruoyi/alarm/domain/vo/AlarmRuleVo.java",
        "ruoyi-alarm/src/main/java/com/ruoyi/alarm/domain/dto/AlarmRuleDto.java",
        "ruoyi-alarm/src/main/java/com/ruoyi/alarm/domain/vo/AlarmRuleListVo.java",
    ]
    st = SubTask(id="st-6", description="告警规则全栈", scope=FileScope(create_files=files))
    assert _split_oversized_by_files(st) == [st], "单实体 9 文件不拆"


# ── 不变量 2：多实体按实体拆,每批是完整实体全栈 ──
def test_multi_entity_splits_by_entity():
    st = _multi_entity_slice()
    children = _split_oversized_by_files(st)
    assert len(children) >= 2, "双实体应拆开"
    # 每个 child 的文件必须同属一个实体(全栈内聚,不跨实体混)
    for c in children:
        stems = {_entity_stem(f) for f in c.scope.create_files}
        assert len(stems) == 1, f"{c.id} 应只含单一实体全栈,实得 {stems}"
    # 两个实体各自的【接口+控制器】必须落在同一 child(契约自洽)
    for c in children:
        names = [f.split("/")[-1] for f in c.scope.create_files]
        has_iface = any(n.startswith("I") and "Service" in n for n in names)
        has_ctrl = any("Controller" in n for n in names)
        if has_iface or has_ctrl:
            assert has_iface and has_ctrl, \
                f"{c.id} 接口与控制器必须同批(RUN14 漂移点),实得 {names}"


def test_no_split_when_within_cap():
    st = SubTask(id="st-z", description="3 文件", difficulty=SubTaskDifficulty.MEDIUM,
                 scope=FileScope(create_files=["a/A.java", "a/B.java", "a/C.java"]))
    assert _split_oversized_by_files(st) == [st]


def test_split_serial_chain_and_scope_independent():
    st = _multi_entity_slice()
    children = _split_oversized_by_files(st)
    assert children[0].depends_on == ["st-1"]
    for i in range(1, len(children)):
        assert f"st-20-{i}" in children[i].depends_on, "后批串行依赖前批"
    # scope 独立深拷贝(防别名污染)
    children[0].scope.create_files = []
    assert children[1].scope.create_files, "改一个子 scope 不应影响兄弟"


def test_all_files_preserved_no_loss():
    st = _multi_entity_slice()
    children = _split_oversized_by_files(st)
    got = sorted(f for c in children for f in c.scope.create_files)
    assert got == sorted(st.scope.create_files), "拆分不得丢/重文件"


def test_writables_go_to_last_batch():
    """多实体 + writable(注册/pom)：writable 垫最后批。"""
    st = SubTask(id="st-7", description="双实体 + 注册",
                 scope=FileScope(create_files=_entity_files("Foo") + _entity_files("Bar"),
                                 writable=["pom.xml"]))
    children = _split_oversized_by_files(st)
    assert "pom.xml" in children[-1].scope.writable
    for c in children[:-1]:
        assert "pom.xml" not in (c.scope.writable or [])


# ── 触发器：单文件守卫不误伤 ──
def test_single_file_guard_still_holds():
    st = SubTask(id="st-1", description="改一个大文件",
                 scope=FileScope(writable=["Big.java"]), est_context_tokens=999_999)
    assert _needs_resplit(st, budget=150_000) is False


def test_oversized_orthogonal_to_token_budget():
    st = _multi_entity_slice()
    assert _needs_resplit(st, budget=150_000) is True, "文件数超标即使 token 预算够也应评估拆分"


if __name__ == "__main__":
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for fn in fns:
        fn()
        print(f"  ✅ {fn.__name__}")
    print(f"\n=== 按实体拆分(契约不漂移): {len(fns)}/{len(fns)} passed ===")
