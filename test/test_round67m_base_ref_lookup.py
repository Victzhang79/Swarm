"""R67M-T2 B5（23号文，round67m CVB 死因治本）行为测试：安置前 base 树查表。

round67m 实证（task f156ab29 四轮打回，轮1/轮4 同形复发）：契约符号 ISysJobService
是 base 既有实体（ruoyi-quartz/…/ISysJobService.java 早已存在），LLM 把 defined_in
染成新包幻影路径 → C1 判无主 → R48b-1 造 st-contract-quartz-task 影子 create
→ G1 ③f _created_class_shadows_base 硬打回，PLAN-only 重试够不到契约层永不愈。
治=_domicile_contract_symbols 安置前确定性查表：
  Case A：defined_in 已指向盘上实存文件（stem==符号）→ 信任显式声明，跳过安置；
  Case B：defined_in 空/幻影 + purpose/description 显式复用语义 + base 树唯一 stem
          命中 + 命中在该模块候选物理根内 → defined_in 归位 base 真身，跳过安置。
fail-closed 边界：无意图语义 / 多命中歧义 / 命中跨模块 → 原样安置（③f 兜底）。
"""
from __future__ import annotations

import pytest
from swarm.types import FileScope, SubTask, SubTaskDifficulty, TaskPlan

from swarm.brain.plan_finisher import _domicile_contract_symbols

BASE_SVC = "ruoyi-quartz/src/main/java/com/ruoyi/quartz/service/ISysJobService.java"


def _st(sid, *, create=None, desc=None):
    return SubTask(id=sid, description=desc or f"task {sid}",
                   difficulty=SubTaskDifficulty.MEDIUM,
                   scope=FileScope(create_files=create or [], writable=[], readable=[]),
                   acceptance_criteria=[])


def _plan(subs):
    plan = TaskPlan(subtasks=subs, parallel_groups=[[s.id for s in subs]])
    return plan


@pytest.fixture
def proj(tmp_path):
    """棕地 base 树：ISysJobService 存量真身 + 一个 plan 既有的新文件落点。"""
    f = tmp_path / BASE_SVC
    f.parent.mkdir(parents=True)
    f.write_text("package com.ruoyi.quartz.service;\npublic interface ISysJobService {}\n")
    return str(tmp_path)


_FP = [{"module": "quartz-task",
        "path": "ruoyi-quartz/src/main/java/com/ruoyi/quartz/task/SysJobTask.java"}]


def _contract(**item_overrides):
    item = {"name": "ISysJobService", "module": "quartz-task",
            "purpose": "定时任务调度服务接口",
            "signature": "ISysJobService.selectJobList(SysJob job)"}
    item.update(item_overrides)
    return {"interfaces": [item]}


def _run(plan, contract, proj, file_plan=_FP):
    return _domicile_contract_symbols(plan, contract, proj, "任务描述",
                                      file_plan=file_plan)


def _new_st_plan():
    return _plan([_st("st-1",
                      create=["ruoyi-quartz/src/main/java/com/ruoyi/quartz/task/SysJobTask.java"],
                      desc="实现调度任务")])


# ─── Case A：defined_in 已实存（LLM 显式声明存量引用）───


def test_case_a_defined_in_exists_skips_shadow_placement(proj):
    """★round67m CVB 正面证据★：defined_in=base 真身实存 → 不造 st-contract 影子。"""
    contract = _contract(defined_in=BASE_SVC)
    plan = _new_st_plan()
    dom = _run(plan, contract, proj)
    assert dom.get("_base_referenced"), "存量引用必须入转换账"
    assert any("ISysJobService" in s for s in dom["_base_referenced"])
    assert not any(sid.startswith("st-contract") for sid in plan_subtask_ids(plan)), \
        "defined_in 实存的存量符号绝不再造影子安置子任务（round67m ③f 打回本体）"
    # Case A 不改写 defined_in（本来就是对的）
    assert contract["interfaces"][0]["defined_in"] == BASE_SVC


def plan_subtask_ids(plan):
    return [st.id for st in plan.subtasks]


# ─── Case B：幻影 defined_in + 显式复用语义 + 唯一命中 → 归位 ───


def test_case_b_phantom_defined_in_relocated_to_base(proj):
    """幻影 defined_in + “复用既有”语义 + base 唯一命中 → defined_in 归位真身、跳过安置。"""
    contract = _contract(defined_in="ruoyi-quartz/src/main/java/com/ruoyi/quartz/task/ISysJobService.java",
                         purpose="复用既有调度服务接口，不新增")
    plan = _new_st_plan()
    dom = _run(plan, contract, proj)
    assert any("ISysJobService" in s and "归位" in s
               for s in dom.get("_base_referenced", []))
    assert contract["interfaces"][0]["defined_in"] == BASE_SVC, \
        "幻影 defined_in 必须归位到 base 真身（round67g 治法A 形态）"
    assert not any(sid.startswith("st-contract") for sid in plan_subtask_ids(plan))


def test_case_b_empty_defined_in_with_reuse_prose(proj):
    """defined_in 空缺 + “既有”语义同样归位（空与幻影同案）。"""
    contract = _contract(purpose="承接既有 ISysJobService 的调用方")
    plan = _new_st_plan()
    dom = _run(plan, contract, proj)
    assert contract["interfaces"][0]["defined_in"] == BASE_SVC
    assert dom.get("_base_referenced")


# ─── fail-closed 边界：缺一即不动，原样安置留 ③f ───


def test_no_reuse_prose_stays_placed_fail_closed(proj):
    """幻影 defined_in 但【无】复用语义 → 不动（round67c 血泪：绝不靠结构命中挑边）。"""
    contract = _contract(defined_in="ruoyi-quartz/src/main/java/com/ruoyi/quartz/task/ISysJobService.java",
                         purpose="定时任务调度服务接口")
    plan = _new_st_plan()
    dom = _run(plan, contract, proj)
    assert not dom.get("_base_referenced")
    assert any(sid.startswith("st-contract") for sid in plan_subtask_ids(plan)), \
        "无意图证据必须原样安置（影子照造，交 ③f fail-closed 硬打回）"
    assert contract["interfaces"][0]["defined_in"].endswith("task/ISysJobService.java"), \
        "未转换时 defined_in 绝不被改写"


def test_ambiguous_base_hits_stay_placed(proj, tmp_path):
    """base 树两处同名 stem=歧义 → 不动（多命中绝不挑边）。"""
    dup = tmp_path / "ruoyi-quartz/src/main/java/com/ruoyi/other/ISysJobService.java"
    dup.parent.mkdir(parents=True)
    dup.write_text("package com.ruoyi.other;\npublic interface ISysJobService {}\n")
    contract = _contract(purpose="复用既有调度服务接口")
    plan = _new_st_plan()
    dom = _run(plan, contract, proj)
    assert not dom.get("_base_referenced")
    assert any(sid.startswith("st-contract") for sid in plan_subtask_ids(plan))


def test_hit_outside_module_root_stays_placed(proj):
    """base 命中不在该模块候选物理根（file_plan 首段 ∪ phys 根）内 → 不算归属证据。"""
    contract = _contract(purpose="复用既有调度服务接口")
    plan = _new_st_plan()
    fp = [{"module": "quartz-task",
           "path": "ruoyi-admin/src/main/java/com/ruoyi/web/controller/SysJobController.java"}]
    dom = _run(plan, contract, proj, file_plan=fp)
    assert not dom.get("_base_referenced")
    assert any(sid.startswith("st-contract") for sid in plan_subtask_ids(plan))


def test_no_project_path_behavior_unchanged():
    """无 project_path（greenfield/无基线）→ B5 整体不激活，老路径零回归。"""
    contract = _contract(defined_in=BASE_SVC, purpose="复用既有调度服务接口")
    plan = _new_st_plan()
    dom = _run(plan, contract, None)
    assert not dom.get("_base_referenced")
    assert any(sid.startswith("st-contract") for sid in plan_subtask_ids(plan))


def test_meta_key_survives_when_all_symbols_converted(proj):
    """全部符号被转换（零安置）时转换账也绝不丢（元键独立于 created 空账）。"""
    contract = _contract(defined_in=BASE_SVC)
    plan = _new_st_plan()
    dom = _run(plan, contract, proj)
    assert dom == {"_base_referenced": dom["_base_referenced"]}
    assert dom["_base_referenced"]


# ─── 复核 R1 整改（reviewer HIGH-1/hunter LOW-4/5/MEDIUM-3）：Case A 证据卫生 + 否定意图 + punt 观测 ───


def test_case_a_xml_defined_in_not_trusted(proj, tmp_path, caplog):
    """★reviewer HIGH-1 本体★：defined_in 指向实存 .xml（MyBatis mapper 同名 stem）→
    非代码证据不信任，原样安置（旧实现会跳过安置+C1 豁免=迟发编译失败假过）。"""
    xml = tmp_path / "ruoyi-quartz/src/main/resources/mapper/ISysJobService.xml"
    xml.parent.mkdir(parents=True)
    xml.write_text("<mapper/>")
    contract = _contract(defined_in="ruoyi-quartz/src/main/resources/mapper/ISysJobService.xml")
    plan = _new_st_plan()
    dom = _run(plan, contract, proj)
    assert not dom.get("_base_referenced")
    assert any(sid.startswith("st-contract") for sid in plan_subtask_ids(plan)), \
        ".xml 实存不得触发 Case A（否则自愈路径变迟发编译失败）"


def test_case_a_test_tree_defined_in_not_trusted(proj, tmp_path):
    """defined_in 指向 src/test/… 同名 .java → 测试源集非主代码真身，不信任。"""
    t = tmp_path / "ruoyi-quartz/src/test/java/com/ruoyi/quartz/service/ISysJobService.java"
    t.parent.mkdir(parents=True)
    t.write_text("package com.ruoyi.quartz.service;\npublic interface ISysJobService {}\n")
    contract = _contract(
        defined_in="ruoyi-quartz/src/test/java/com/ruoyi/quartz/service/ISysJobService.java")
    plan = _new_st_plan()
    dom = _run(plan, contract, proj)
    # 测试树文件不进证据面；base 主树真身（service/）仍在 → Case B 语义缺省无 → 原样安置
    assert not dom.get("_base_referenced")
    assert any(sid.startswith("st-contract") for sid in plan_subtask_ids(plan))


def test_case_b_test_tree_hit_not_counted(proj, tmp_path):
    """base 主树无真身、仅测试树有同名 → 测试树不计命中（零命中=真新符号原样安置）。"""
    import os
    os.remove(os.path.join(proj, BASE_SVC))  # 拿掉主树真身
    t = tmp_path / "ruoyi-quartz/src/test/java/com/ruoyi/quartz/service/ISysJobService.java"
    t.parent.mkdir(parents=True)
    t.write_text("package com.ruoyi.quartz.service;\npublic interface ISysJobService {}\n")
    contract = _contract(purpose="复用既有调度服务接口")
    plan = _new_st_plan()
    dom = _run(plan, contract, proj)
    assert not dom.get("_base_referenced"), "测试树同名文件绝不能当存量真身归位"
    assert any(sid.startswith("st-contract") for sid in plan_subtask_ids(plan))


def test_case_a_dotdot_escape_not_trusted(proj):
    """defined_in='../x.java' 逃逸项目根 → 不信任（hunter LOW-5）。"""
    contract = _contract(defined_in="../outside/ISysJobService.java",
                         purpose="复用既有调度服务接口")
    plan = _new_st_plan()
    # Case A 被逃逸挡下后落 Case B：主树唯一命中+语义+候选根 → 归位真身（非逃逸路径）
    dom = _run(plan, contract, proj)
    di = contract["interfaces"][0]["defined_in"]
    assert not di.startswith(".."), "归位结果绝不能是逃逸路径"
    assert di == BASE_SVC


def test_negated_reuse_prose_not_converted(proj, caplog):
    """“不复用既有实现，另起一套”=否定语境 → 不转换（reviewer LOW-1/hunter MEDIUM-3②）。"""
    contract = _contract(purpose="不复用既有实现，另起一套调度接口")
    plan = _new_st_plan()
    with caplog.at_level("INFO", logger="swarm.brain.plan_finisher"):
        dom = _run(plan, contract, proj)
    assert not dom.get("_base_referenced")
    assert any(sid.startswith("st-contract") for sid in plan_subtask_ids(plan))
    assert any("punt" in r.getMessage() and "无显式复用语义" in r.getMessage()
               for r in caplog.records), "punt 方向必须有聚合 INFO 观测（hunter MEDIUM-3①）"


def test_punt_info_log_on_ambiguous_hit(proj, tmp_path, caplog):
    """多命中 punt 也有 INFO 观测（不转换方向不再零直接观测）。"""
    dup = tmp_path / "ruoyi-quartz/src/main/java/com/ruoyi/other/ISysJobService.java"
    dup.parent.mkdir(parents=True)
    dup.write_text("package com.ruoyi.other;\npublic interface ISysJobService {}\n")
    contract = _contract(purpose="复用既有调度服务接口")
    plan = _new_st_plan()
    with caplog.at_level("INFO", logger="swarm.brain.plan_finisher"):
        dom = _run(plan, contract, proj)
    assert not dom.get("_base_referenced")
    assert any("punt" in r.getMessage() and "多命中" in r.getMessage()
               for r in caplog.records)
