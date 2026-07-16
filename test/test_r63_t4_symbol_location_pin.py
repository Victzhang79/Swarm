"""T4（round63）：契约钉死共享符号权威落点 + 跨子任务类型引用 provenance 布线。

round63 死因（register T4 调查结论）：plan 自洽（file_plan/scope 给每实体唯一物理落点）但
权威从未下发——契约符号零 FQN/路径，worker 首发 import 全靠臆造（AlarmRobot 三包共存、
AlarmSendLog 10× "package does not exist"）；且 AlarmSendLog 根本不在契约里（纯跨子任务
实体），G2 只在 consumer 已声明 readable 时才补边 → "引用未声明类型"零覆盖。

治本（brain/symbol_provenance.py，确定性/栈中立/plan 期）：
- pin_contract_symbol_paths：从 create/writable 唯一 code 落点给契约条目回填 defined_in
  （不占用 apis 的 path 键）；同符号多落点=计划内漂移 → 不钉+WARNING。
- wire_created_type_references：consumer 语料（description+AC+contract，C1 同语料面）
  区分大小写词边界命中【跨子任务 create 的唯一 code stem】或【已钉契约符号】→ producer
  路径补进 consumer readable+upstream_artifacts；依赖边交既有 G2（复用环守卫）。
"""
from __future__ import annotations

import asyncio
import logging

from swarm.brain.contract_utils import wire_readable_provenance
from swarm.brain.symbol_provenance import (
    pin_contract_symbol_paths,
    wire_created_type_references,
)
from swarm.types import FileScope, SubTask, SubTaskDifficulty, SubTaskModality, TaskPlan

_SENDLOG = "ruoyi-alarm/src/main/java/com/ruoyi/alarm/engine/domain/AlarmSendLog.java"
_ROBOT = "ruoyi-alarm/src/main/java/com/ruoyi/alarm/core/domain/AlarmRobot.java"
_ISVC = "ruoyi-alarm/src/main/java/com/ruoyi/alarm/service/IAlarmTaskService.java"


def _mk(sid, *, desc="", create=None, writable=None, readable=None, deps=None,
        contract=None, acc=None):
    return SubTask(
        id=sid, description=desc or f"sub {sid}",
        difficulty=SubTaskDifficulty.MEDIUM, modality=SubTaskModality.TEXT,
        scope=FileScope(create_files=create or [], writable=writable or [],
                        readable=readable or []),
        depends_on=deps or [], acceptance_criteria=acc or ["ok"],
        contract=contract or {})


def _plan(subs, shared_contract=None):
    return TaskPlan(subtasks=subs, parallel_groups=[[s.id for s in subs]],
                    shared_contract=shared_contract or {})


# ───────────────────────── pin_contract_symbol_paths ─────────────────────────

def test_pin_exact_dto_path():
    """round63 原型：dtos 条目 AlarmRobot，唯一 producer 落点 core/domain → 回填 defined_in。"""
    plan = _plan([_mk("st-6", create=[_ROBOT])],
                 shared_contract={"dtos": [{"name": "AlarmRobot", "module": "ruoyi-alarm",
                                            "fields": ["Long robotId"]}]})
    pinned = pin_contract_symbol_paths(plan)
    assert pinned == 1
    assert plan.shared_contract["dtos"][0]["defined_in"] == _ROBOT


def test_pin_interface_convention_tier1():
    """R42 惯例等价：契约符号 AlarmTaskService ↔ 文件 IAlarmTaskService.java（I 前缀）→ 钉住。"""
    plan = _plan([_mk("st-1", create=[_ISVC])],
                 shared_contract={"interfaces": [{"name": "AlarmTaskService",
                                                  "module": "ruoyi-alarm"}]})
    assert pin_contract_symbol_paths(plan) == 1
    assert plan.shared_contract["interfaces"][0]["defined_in"] == _ISVC


def test_pin_exact_beats_decorated():
    """强度消歧（R43 F1 同理）：精确同名文件在场时，绝不钉到装饰前缀弱匹配的文件。"""
    exact = "mod/src/main/java/com/x/TaskService.java"
    decorated = "mod/src/main/java/com/x/AlarmTaskService.java"
    plan = _plan([_mk("a", create=[exact]), _mk("b", create=[decorated])],
                 shared_contract={"interfaces": [{"name": "TaskService", "module": ""}]})
    assert pin_contract_symbol_paths(plan) == 1
    assert plan.shared_contract["interfaces"][0]["defined_in"] == exact


def test_pin_ambiguous_multi_path_not_pinned(caplog):
    """同符号两个不同落点 = 计划内漂移 → 不钉任何一个 + WARNING（surfaced 不静默）。"""
    p1 = "mod-a/src/main/java/com/x/domain/AlarmRobot.java"
    p2 = "mod-a/src/main/java/com/x/core/domain/AlarmRobot.java"
    plan = _plan([_mk("a", create=[p1]), _mk("b", create=[p2])],
                 shared_contract={"dtos": [{"name": "AlarmRobot", "module": ""}]})
    with caplog.at_level(logging.WARNING):
        assert pin_contract_symbol_paths(plan) == 0
    assert "defined_in" not in plan.shared_contract["dtos"][0]
    assert any("T4" in r.message for r in caplog.records)


def test_pin_module_disambiguates():
    """同 stem 两模块各一份：条目 module 字段能消歧 → 钉到本模块落点。"""
    pa = "mod-a/src/main/java/com/x/AlarmRobot.java"
    pb = "mod-b/src/main/java/com/y/AlarmRobot.java"
    plan = _plan([_mk("a", create=[pa]), _mk("b", create=[pb])],
                 shared_contract={"dtos": [{"name": "AlarmRobot", "module": "mod-b"}]})
    assert pin_contract_symbol_paths(plan) == 1
    assert plan.shared_contract["dtos"][0]["defined_in"] == pb


def test_pin_writable_producer_also_pins():
    """round63 st-6-1-1 形态：实体文件在 writable（上游脚手架已建）→ 同样是权威落点。"""
    plan = _plan([_mk("st-6-1-1", writable=[_ROBOT])],
                 shared_contract={"dtos": [{"name": "AlarmRobot", "module": "ruoyi-alarm"}]})
    assert pin_contract_symbol_paths(plan) == 1
    assert plan.shared_contract["dtos"][0]["defined_in"] == _ROBOT


def test_pin_apis_never_touched():
    """apis 条目已有 path=URL 语义（CONTRACT_MODULE schema）→ 绝不写 defined_in/绝不碰 path。"""
    plan = _plan([_mk("a", create=["mod/src/main/java/com/x/Send.java"])],
                 shared_contract={"apis": [{"path": "/alarm/send", "method": "POST",
                                            "name": "send"}]})
    pin_contract_symbol_paths(plan)
    assert plan.shared_contract["apis"][0]["path"] == "/alarm/send"
    assert "defined_in" not in plan.shared_contract["apis"][0]


def test_pin_non_code_manifest_not_producer():
    """契约符号 Config ↔ 文件 config.xml（非 code 文件）→ 不钉（清单/资源不是类型落点）。"""
    plan = _plan([_mk("a", create=["mod/config.xml"])],
                 shared_contract={"types": [{"name": "Config", "module": ""}]})
    assert pin_contract_symbol_paths(plan) == 0


def test_pin_string_items_and_no_contract_safe():
    """字符串条目/空契约不炸、不改。"""
    plan = _plan([_mk("a", create=["mod/src/A.java"])],
                 shared_contract={"interfaces": ["GET /x/list — 说明"], "dtos": []})
    assert pin_contract_symbol_paths(plan) == 0
    assert pin_contract_symbol_paths(_plan([_mk("a")])) == 0


# ──────────────────────── wire_created_type_references ────────────────────────

def test_wire_round63_alarmsendlog_end_to_end():
    """★round63 真死因复现★：st-7 create engine/domain/AlarmSendLog.java；st-14 语料引用
    AlarmSendLog 但无依赖无 readable → 布线补 readable+upstream_artifacts，既有 G2 补边。"""
    st7 = _mk("st-7", create=[_SENDLOG])
    st14 = _mk("st-14", desc="实现发送日志查询服务，聚合 AlarmSendLog 按渠道统计", deps=["st-1"])
    st1 = _mk("st-1", create=["ruoyi-alarm/pom.xml"])
    plan = _plan([st1, st7, st14])
    res = wire_created_type_references(plan)
    assert ("st-14", _SENDLOG) in res["wired"]
    assert _SENDLOG in st14.scope.readable
    assert _SENDLOG in st14.scope.upstream_artifacts
    added, _cyc = wire_readable_provenance(plan)
    assert ("st-14", "st-7") in added, "布线后既有 G2 必须能补出依赖边"


def test_wire_contract_symbol_via_pinned_path():
    """契约符号通道：consumer 语料写的是符号名 AlarmTaskService（≠文件 stem IAlarmTaskService）
    → 经①钉住的 defined_in 布线。"""
    prod = _mk("p", create=[_ISVC])
    cons = _mk("c", desc="Controller 调用 AlarmTaskService 完成任务下发")
    plan = _plan([prod, cons],
                 shared_contract={"interfaces": [{"name": "AlarmTaskService",
                                                  "module": "ruoyi-alarm"}]})
    pin_contract_symbol_paths(plan)
    wire_created_type_references(plan)
    assert _ISVC in cons.scope.readable


def test_wire_case_sensitive_no_false_hit():
    """区分大小写：语料只有小写 alarmsendlog（普通词/表名）→ 不布线（加边必须强判据）。"""
    st7 = _mk("st-7", create=[_SENDLOG])
    st14 = _mk("st-14", desc="迁移 alarmsendlog 表结构")
    plan = _plan([st7, st14])
    res = wire_created_type_references(plan)
    assert not res["wired"]
    assert _SENDLOG not in st14.scope.readable


def test_wire_noise_and_short_stems_skipped():
    """噪音表/短 stem：main.py、App.java 即使被语料词边界命中也绝不布线。"""
    p = _mk("p", create=["svc/main.py", "web/src/App.java"])
    c = _mk("c", desc="入口 main 调用 App 初始化")
    plan = _plan([p, c])
    assert not wire_created_type_references(plan)["wired"]


def test_wire_ambiguous_stem_skipped(caplog):
    """同 stem 两个不同落点（计划内漂移）→ 跳过布线 + WARNING。"""
    p1 = _mk("p1", create=["mod-a/src/com/x/domain/AlarmRobot.java"])
    p2 = _mk("p2", create=["mod-a/src/com/x/core/domain/AlarmRobot.java"])
    c = _mk("c", desc="mapper 返回 AlarmRobot 列表")
    plan = _plan([p1, p2, c])
    with caplog.at_level(logging.WARNING):
        res = wire_created_type_references(plan)
    assert not res["wired"]
    assert any("T4" in r.message for r in caplog.records)


def test_wire_producer_and_own_scope_untouched():
    """producer 自己、以及 scope 已含该文件的 consumer（readable/writable/create）不重复布线。"""
    prod = _mk("p", create=[_SENDLOG], desc="创建 AlarmSendLog 实体")
    has_r = _mk("r", desc="用 AlarmSendLog", readable=[_SENDLOG])
    plan = _plan([prod, has_r])
    res = wire_created_type_references(plan)
    assert not res["wired"]
    assert has_r.scope.readable.count(_SENDLOG) == 1, "不得重复追加"


def test_wire_idempotent():
    """幂等：连跑两遍不重复追加（replan/elaborate 多轮安全）。"""
    st7 = _mk("st-7", create=[_SENDLOG])
    st14 = _mk("st-14", desc="聚合 AlarmSendLog 统计")
    plan = _plan([st7, st14])
    wire_created_type_references(plan)
    wire_created_type_references(plan)
    assert st14.scope.readable.count(_SENDLOG) == 1
    assert st14.scope.upstream_artifacts.count(_SENDLOG) == 1


def test_wire_fanout_cap(caplog):
    """通用名爆炸守卫：stem 命中 consumer 数超帽 → 跳过 + WARNING（fail-open 可观测）。"""
    prod = _mk("p", create=["mod/src/com/x/Result.java"])
    consumers = [_mk(f"c{i}", desc="所有接口统一返回 Result 包装") for i in range(30)]
    plan = _plan([prod] + consumers)
    with caplog.at_level(logging.WARNING):
        res = wire_created_type_references(plan)
    assert not res["wired"]
    assert any("T4" in r.message for r in caplog.records)


def test_wire_stack_neutral_python():
    """栈中立：Python snake_case 模块同样布线（schedule_service.py ← 语料引用）。"""
    prod = _mk("p", create=["svc/services/schedule_service.py"])
    cons = _mk("c", desc="定时任务调用 schedule_service 生成快照")
    plan = _plan([prod, cons])
    res = wire_created_type_references(plan)
    assert ("c", "svc/services/schedule_service.py") in res["wired"]


# ───────────────────────────── elaborate 接线 ─────────────────────────────

def test_elaborate_wires_and_pins(monkeypatch):
    """接线回归（revert-check 锚点）：elaborate 跑完后 ①契约条目已钉 defined_in 且回写
    state["shared_contract"]（dispatch.py:528 优先读 state，不回写=白钉）；②consumer readable
    已布线且 G2 补了依赖边。"""
    from swarm.brain import planning_nodes as P
    st7 = _mk("st-7", create=[_SENDLOG])
    # st-14 须有写 scope（真 round63 形态），否则被 G3 空 scope 剪除，测的就不是布线了
    st14 = _mk("st-14", desc="实现发送日志查询，聚合 AlarmSendLog 统计",
               create=["ruoyi-alarm/src/main/java/com/ruoyi/alarm/engine/service/AlarmSendLogService.java"])
    plan = _plan([st7, st14],
                 shared_contract={"dtos": [{"name": "AlarmSendLog", "module": "ruoyi-alarm"}]})
    out = asyncio.run(P.elaborate({"plan": plan, "task_id": "", "project_id": ""}))
    new_plan = out.get("plan") or plan
    sc_out = out.get("shared_contract")
    assert sc_out and sc_out["dtos"][0]["defined_in"] == _SENDLOG, \
        "钉住的契约必须随 state 键回写，否则 dispatch 读到旧契约=白钉"
    st14n = next(s for s in new_plan.subtasks if s.id == "st-14")
    assert _SENDLOG in st14n.scope.readable
    assert "st-7" in st14n.depends_on, "布线后 elaborate 内的 G2 必须补出依赖边"


# ───────────────────────────── worker prompt 面 ─────────────────────────────

def test_prompt_directive_present_when_pinned():
    """契约带 defined_in 时，worker prompt 必须含权威落点指令（import 由该路径推导勿臆造）。"""
    from swarm.worker.prompts import build_worker_prompt
    st = _mk("st-x", desc="消费实体")
    prompt = build_worker_prompt(
        subtask=st,
        shared_contract={"dtos": [{"name": "AlarmRobot", "module": "ruoyi-alarm",
                                   "defined_in": _ROBOT}]})
    assert "defined_in" in prompt
    assert "臆造" in prompt or "权威" in prompt


def test_prompt_directive_absent_without_pin():
    """无 defined_in 的契约不注入该指令（不给 worker 无中生有的字段名）。"""
    from swarm.worker.prompts import build_worker_prompt
    st = _mk("st-x", desc="消费实体")
    prompt = build_worker_prompt(
        subtask=st,
        shared_contract={"dtos": [{"name": "AlarmRobot", "module": "ruoyi-alarm"}]})
    assert "权威文件落点" not in prompt


# ───────────────────────── 对抗复核回归锁 ─────────────────────────

def test_pin_tier2_only_leaves_trace(caplog):
    """hunter#3：契约符号仅装饰前缀弱等价（tier2）命中 → 不钉，但必须留痕（不许全静默）。"""
    plan = _plan([_mk("a", create=["mod/src/com/x/AlarmTaskService.java"])],
                 shared_contract={"interfaces": [{"name": "TaskService", "module": ""}]})
    with caplog.at_level(logging.INFO):
        assert pin_contract_symbol_paths(plan) == 0
    assert any("tier2" in r.message for r in caplog.records)


def test_wire_referenced_noise_stem_leaves_trace(caplog):
    """hunter#3：真实类型撞噪音表（如实体叫 Model）被排除且语料确有引用 → DEBUG 留痕。"""
    p = _mk("p", create=["mod/src/com/x/Model.java"])
    c = _mk("c", desc="mapper 返回 Model 列表")
    plan = _plan([p, c])
    # 指定 logger 名：全量套件里早前测试可能已把 swarm 层级 logger 抬到 INFO，
    # 只设 root 的 caplog 级别抓不到本模块 DEBUG（单跑绿/套件红的经典坑）
    with caplog.at_level(logging.DEBUG, logger="swarm.brain.symbol_provenance"):
        res = wire_created_type_references(plan)
    assert not res["wired"]
    assert any("噪音表" in r.message for r in caplog.records)


def test_wire_writable_only_pin_warns_unordered(caplog):
    """复核 R1（MEDIUM CONFIRMED）：契约通道钉到 writable-only 落点（上游脚手架已建），
    布线成功但 G2 不会为 writable 加依赖边 → 必须 WARNING（fail-open 可观测），不静默无序。"""
    prod = _mk("p", writable=[_ROBOT], desc="给 AlarmRobot 增加 robotId 字段")
    cons = _mk("c", desc="Controller 查询 AlarmRobot 列表")
    plan = _plan([prod, cons],
                 shared_contract={"dtos": [{"name": "AlarmRobot", "module": "ruoyi-alarm"}]})
    pin_contract_symbol_paths(plan)
    with caplog.at_level(logging.WARNING):
        res = wire_created_type_references(plan)
    assert ("c", _ROBOT) in res["wired"]
    assert any("writable" in r.message and "T4" in r.message for r in caplog.records)
    added, _ = wire_readable_provenance(plan)
    assert not added, "G2 对 writable-only producer 不加边（前提成立本测试才有意义）"


def test_elaborate_wire_exception_half_applied_trace(monkeypatch, caplog):
    """hunter#1 HIGH：pin 成功后 wire 抛异常 → 半应用状态必须留痕（不得谎报'跳过'），
    且已钉的 defined_in 照常回写 state（事实已提交，不装没发生）。"""
    from swarm.brain import planning_nodes as P
    from swarm.brain import symbol_provenance as SP
    st7 = _mk("st-7", create=[_SENDLOG])
    st14 = _mk("st-14", desc="聚合 AlarmSendLog 统计",
               create=["ruoyi-alarm/src/main/java/com/ruoyi/alarm/engine/service/X.java"])
    plan = _plan([st7, st14],
                 shared_contract={"dtos": [{"name": "AlarmSendLog", "module": "ruoyi-alarm"}]})
    def _boom(_plan):
        raise RuntimeError("injected")
    monkeypatch.setattr(SP, "wire_created_type_references", _boom)
    with caplog.at_level(logging.WARNING):
        out = asyncio.run(P.elaborate({"plan": plan, "task_id": "", "project_id": ""}))
    assert (out.get("shared_contract") or {})["dtos"][0]["defined_in"] == _SENDLOG
    assert any("半应用" in r.message for r in caplog.records)


def test_prompt_shadowed_pin_keys_restore_defined_in(caplog):
    """hunter#5：子任务 contract 整键覆盖 dtos（旧 checkpoint 过期副本形态）顶掉 shared 的
    defined_in → 按符号名条目级回填 + WARNING；且绝不就地变异 subtask.contract 本体。"""
    from swarm.worker.prompts import build_worker_prompt
    override = {"dtos": [{"name": "AlarmRobot", "module": "ruoyi-alarm"}]}
    st = _mk("st-x", desc="消费实体", contract=override)
    with caplog.at_level(logging.WARNING):
        prompt = build_worker_prompt(
            subtask=st,
            shared_contract={"dtos": [{"name": "AlarmRobot", "module": "ruoyi-alarm",
                                       "defined_in": _ROBOT}]})
    assert _ROBOT in prompt and "defined_in" in prompt
    assert any("回填" in r.message for r in caplog.records)
    assert "defined_in" not in override["dtos"][0], "prompt 构造器不得变异 state 对象"
