"""R67M2-T2（24号文，round67m2 FAILED@PLAN 证据层+幽灵+模型面治本）行为测试。

四项机制：
  B1 file_plan 合流闸（_merge_designed_file_plan）：后补 create 同 simple-name 异 FQN
     拒收——round67m2 实证原始组+后补组双落点合流 414 条 → _tech_design_authority
     自判 ambiguous → td 后备权威全灭、两 deconflict pass 双失据。
  C1 幽灵布局族（plan_finisher._domicile_contract_symbols）：
     Case C=幽灵布局 defined_in（JVM 扩展名+classpath_fqn_key None+幻影）+ base 唯一
       命中+候选根内 → 免散文归位 base 真身（round67m2 SysJob 形态；IGenTableColumnService
       controller/→service/ 错包族同治）；
     安置落点布局闸=安置路径不过类路径布局 → 不建安置（mvn 不编译验收假过+③b/③f
       结构性失明的幽灵面封死），留契约无主交 C1 owner 闸打回。
  B2 契约策展扩面（planning_nodes CONTRACT_MODULE prompt）：defined_in 必填引导。
  A3 凭据类错误可观测（models.errors.is_auth_shaped_error + router 回调升 ERROR）——
     round67m2 k3 403 PermissionDenied 静默 2h20m 零 WARNING。
"""
from __future__ import annotations

import logging

import pytest
from swarm.types import FileScope, SubTask, SubTaskDifficulty, TaskPlan

from swarm.brain.plan_finisher import _domicile_contract_symbols

BASE_DOMAIN = "ruoyi-quartz/src/main/java/com/ruoyi/quartz/domain/SysJob.java"


def _st(sid, *, create=None, desc=None):
    return SubTask(id=sid, description=desc or f"task {sid}",
                   difficulty=SubTaskDifficulty.MEDIUM,
                   scope=FileScope(create_files=create or [], writable=[], readable=[]),
                   acceptance_criteria=[])


def _plan(subs):
    return TaskPlan(subtasks=subs, parallel_groups=[[s.id for s in subs]])


def _ids(plan):
    return [s.id for s in plan.subtasks]


@pytest.fixture
def proj(tmp_path):
    f = tmp_path / BASE_DOMAIN
    f.parent.mkdir(parents=True)
    f.write_text("package com.ruoyi.quartz.domain;\npublic class SysJob {}\n")
    return str(tmp_path)


_FP = [{"module": "quartz-task",
        "path": "ruoyi-quartz/src/main/java/com/ruoyi/quartz/task/SysJobTask.java"}]


def _run(plan, contract, proj, file_plan=_FP):
    return _domicile_contract_symbols(plan, contract, proj, "任务描述",
                                      file_plan=file_plan)


# ─── C1 Case C：幽灵布局 defined_in 对账归位（免复用散文） ───


def test_case_c_ghost_layout_defined_in_relocates_without_prose(proj):
    """★round67m2 SysJob 形态★：defined_in=ruoyi-quartz/SysJob.java（无 src 布局段=
    classpath_fqn_key None=自证幻觉）+ base 唯一命中+候选根内+【无复用散文】——
    旧 Case B 必 punt（散文缺证据），Case C 凭幻觉自证直接归位 base 真身。"""
    contract = {"interfaces": [{"name": "SysJob", "module": "quartz-task",
                                "purpose": "定时任务实体",  # 无"复用/既有"语义
                                "defined_in": "ruoyi-quartz/SysJob.java"}]}
    plan = _plan([_st("st-1", create=[_FP[0]["path"]])])
    dom = _run(plan, contract, proj)
    assert contract["interfaces"][0]["defined_in"] == BASE_DOMAIN, \
        "幽灵布局 defined_in 必须对账归位 base 真身（免散文——幻觉声明自损信用）"
    assert any("对账归位" in s for s in dom.get("_base_referenced") or [])
    assert not any(sid.startswith("st-contract") for sid in _ids(plan)), \
        "归位后绝不再造影子安置（round67m2 幽灵+③f 迟发雷双面封死）"


def test_case_c_cross_module_hit_stays_punt(proj):
    """round67c 护栏：唯一命中在【模块候选根外】（跨模块同名）→ 绝不凭名对账，
    原样安置留 ③f fail-closed（Case C 绝不放大挑边面）。"""
    contract = {"interfaces": [{"name": "SysJob", "module": "other-mod",
                                "purpose": "定时任务实体",
                                "defined_in": "other-mod/SysJob.java"}]}
    # other-mod 的 file_plan 落点在别的物理根 → base 命中 ruoyi-quartz ∉ 候选根
    fp = [{"module": "other-mod",
           "path": "ruoyi-other/src/main/java/com/ruoyi/other/X.java"}]
    plan = _plan([_st("st-1", create=[fp[0]["path"]])])
    dom = _run(plan, contract, proj, file_plan=fp)
    assert contract["interfaces"][0]["defined_in"] == "other-mod/SysJob.java", \
        "跨模块命中绝不改写 defined_in（round67c：通用名误合并=静默腐化）"
    assert not any("对账归位" in s for s in dom.get("_base_referenced") or [])


def test_case_c_non_jvm_ghost_not_touched(proj):
    """栈中立边界：非 JVM 扩展名的 defined_in（.xml 等）不触发 Case C（布局语义
    只适用 JVM 类路径，异栈/非代码文件零行为变化）。"""
    contract = {"interfaces": [{"name": "SysJob", "module": "quartz-task",
                                "purpose": "定时任务实体",
                                "defined_in": "ruoyi-quartz/SysJob.xml"}]}
    plan = _plan([_st("st-1", create=[_FP[0]["path"]])])
    _run(plan, contract, proj)
    assert contract["interfaces"][0]["defined_in"] == "ruoyi-quartz/SysJob.xml"


def test_case_c_module_name_disk_dir_as_candidate_root(proj):
    """★round67m2 SysJob 实证缺口★：模块名=base 树盘上实存顶层目录（ruoyi-quartz/），
    但 file_plan 该模块零条目、phys 未映射 → 候选根取证空集会 punt（旧码即此漏）。
    磁盘实证的模块名目录必须算候选根（非名字臆造——目录实存是最强物理根证据）。"""
    contract = {"interfaces": [{"name": "SysJob", "module": "ruoyi-quartz",
                                "purpose": "定时任务实体",
                                "defined_in": "ruoyi-quartz/SysJob.java"}]}
    # file_plan 完全没有 ruoyi-quartz 模块条目（真实 cassette 形态）
    fp = [{"module": "other", "path": "ruoyi-other/src/main/java/com/ruoyi/other/X.java"}]
    plan = _plan([_st("st-1", create=[fp[0]["path"]])])
    dom = _run(plan, contract, proj, file_plan=fp)
    assert contract["interfaces"][0]["defined_in"] == BASE_DOMAIN, \
        "模块名=盘上实存顶层目录即候选根（round67m2 SysJob 错过 Case C 的实证缺口）"
    assert not any(sid.startswith("st-contract") for sid in _ids(plan))


# ─── C1 安置落点布局闸：幽灵落点不建安置 ───


def test_placement_layout_gate_blocks_ghost_path(proj, caplog):
    """★round67m2 SysJob 幽灵第二面★：模块权威落点目录本身非类路径布局（file_plan
    幽灵证据 ruoyi-quartz/SysJob.java）→ 安置路径不过 classpath_fqn_key → 不建安置
    （mvn 不编译验收假过+③b/③f 失明面封死），符号留契约无主交 C1 owner 闸。"""
    fp = [{"module": "ruoyi-quartz", "path": "ruoyi-quartz/SysJob.java"}]  # 幽灵落点证据
    contract = {"interfaces": [{"name": "NewSchedulerApi", "module": "ruoyi-quartz",
                                "purpose": "全新调度 API（base 无同名）"}]}
    plan = _plan([_st("st-1", create=["ruoyi-quartz/SysJob.java"])])
    with caplog.at_level(logging.WARNING):
        _run(plan, contract, proj, file_plan=fp)
    assert not any(sid.startswith("st-contract") for sid in _ids(plan)), \
        "幽灵落点绝不建安置子任务（假过+失明面本体）"
    assert any("布局闸" in r.message for r in caplog.records), "闸住必须 WARNING 可观测"


def test_placement_layout_gate_passes_classpath_path(proj):
    """对照面：落点过类路径布局（file_plan 正常 src 证据）→ 安置照建（闸绝不误杀正常面）。"""
    fp = [{"module": "ruoyi-quartz",
           "path": "ruoyi-quartz/src/main/java/com/ruoyi/quartz/api/NewSchedulerApi.java"}]
    contract = {"interfaces": [{"name": "NewSchedulerApi", "module": "ruoyi-quartz",
                                "purpose": "全新调度 API（base 无同名）"}]}
    # st-1 创建的是别的文件——NewSchedulerApi 保持无主（否则符号有 owner 不进安置面）
    plan = _plan([_st("st-1", create=[
        "ruoyi-quartz/src/main/java/com/ruoyi/quartz/api/OtherApi.java"])])
    _run(plan, contract, proj, file_plan=fp)
    assert any(sid.startswith("st-contract") for sid in _ids(plan)), \
        "正常类路径落点的无主符号必须照常安置（布局闸只闸幽灵）"


def test_placement_gate_root_src_single_module_passes(proj):
    """★复核 HIGH-1★：根级 src 单模块 JVM 工程（src/main/java/... 直接在仓库根，模块根
    为空串）classpath_fqn_key 恒 None 但完全可编译——布局闸绝不可误杀（旧判据会整类
    punt=单模块工程确定性死循环）。"""
    fp = [{"module": "app", "path": "src/main/java/com/x/OtherApi.java"}]
    contract = {"interfaces": [{"name": "NewApi", "module": "app", "purpose": "全新 API"}]}
    plan = _plan([_st("st-1", create=["src/main/java/com/x/OtherApi.java"])])
    _run(plan, contract, proj, file_plan=fp)
    assert any(sid.startswith("st-contract") for sid in _ids(plan)), \
        "根级 src 单模块工程是系统显式支持形态，布局闸绝不可 punt（HIGH-1 误杀面）"


def test_case_c_root_src_defined_in_not_relocated(proj):
    """★复核 HIGH-1 对称面★：根级 src 布局的 defined_in（盘上不存在但布局可编译）不是
    幽灵——可能是合法计划新文件，Case C 绝不对账归位（round67c 护栏）。"""
    contract = {"interfaces": [{"name": "SysJob", "module": "quartz-task",
                                "purpose": "定时任务实体",
                                "defined_in": "src/main/java/com/x/SysJob.java"}]}
    plan = _plan([_st("st-1", create=[_FP[0]["path"]])])
    dom = _run(plan, contract, proj)
    assert contract["interfaces"][0]["defined_in"] == "src/main/java/com/x/SysJob.java", \
        "可编译布局的 defined_in 绝不进 Case C（只有自证幻觉的幽灵布局才对账）"
    assert not any("对账归位" in s for s in dom.get("_base_referenced") or [])


def test_case_c_negated_reuse_intent_stays_punt(proj):
    """★复核 LOW-1★：LLM 显式否定复用（"不复用既有 SysJob"）→ 尊重声明不越权归位，
    punt 留安置/布局闸/③f 权威链（fail-closed），绝不拿幻觉声明压过显式否定。"""
    contract = {"interfaces": [{"name": "SysJob", "module": "quartz-task",
                                "purpose": "不复用既有 SysJob，按新需求重新实现",
                                "defined_in": "ruoyi-quartz/SysJob.java"}]}
    plan = _plan([_st("st-1", create=[_FP[0]["path"]])])
    dom = _run(plan, contract, proj)
    assert contract["interfaces"][0]["defined_in"] == "ruoyi-quartz/SysJob.java"
    assert not any("对账归位" in s for s in dom.get("_base_referenced") or [])


def test_layout_punted_meta_key_exported(proj):
    """★复核 HIGH-2/MEDIUM-3★：布局闸 punt 账必须以元键带出（state 账交 C1 硬打回），
    绝不只活在日志（日志级别调高即蒸发=同 C1 hunter⑤ 已治过的盲区）。"""
    fp = [{"module": "ruoyi-quartz", "path": "ruoyi-quartz/SysJob.java"}]  # 幽灵落点证据
    contract = {"interfaces": [{"name": "NewSchedulerApi", "module": "ruoyi-quartz",
                                "purpose": "全新调度 API（base 无同名）"}]}
    plan = _plan([_st("st-1", create=["ruoyi-quartz/SysJob.java"])])
    dom = _run(plan, contract, proj, file_plan=fp)
    assert dom.get("_layout_punted"), "punt 账必须带出（C1 硬打回的数据源）"
    assert any(s.startswith("NewSchedulerApi→") for s in dom["_layout_punted"])


def test_g4_ghost_layout_warning_marker(proj, caplog):
    """★复核 MEDIUM-4★：G4 零证据兜底落点本身非可编译布局（tpl_dir 退化 src）→ 豁免
    照建（防死循环）但 G4 WARNING 必须带幽灵判语（幻影文件面唯一观测，与 punt 可区分）。"""
    contract = {"interfaces": [{"name": "GhostApi", "module": "ghost-mod",
                                "purpose": "全新 API（零物理证据模块）"}]}
    # 根级 X.java：exts 有 java 但零目录证据 → tpl_dir 退化为 "src" → G4 落点 mod/src/seg 幽灵
    plan = _plan([_st("st-1", create=["X.java"])])
    with caplog.at_level(logging.WARNING):
        _run(plan, contract, proj, file_plan=[])
    g4 = [r.message for r in caplog.records if "G4" in r.message]
    assert g4 and any("幻影文件面" in m for m in g4), \
        "G4 落点幽灵时 WARNING 必须如实标注（豁免即失明的补偿观测）"


# ─── C1 布局闸 punt → C1 owner 闸硬打回（不占 0.4 宽容） ───


def test_c1_punted_symbols_reject_below_ratio():
    """★复核 HIGH-2（reviewer+hunter 双逮）★：布局闸 punt 的符号仍无主时【不占 0.4 无主
    宽容】直接硬打回——宽容=胖契约下符号静默蒸发（不建安置+无 owner+占比内仅 warn=
    本批要防的假过，爆点后移到 L2/交付）。"""
    from swarm.brain.plan_validator import validate_contract_ownership
    contract = {"interfaces": [{"name": f"IApi{i}", "module": "m"} for i in range(10)]}
    # 9/10 有 owner，IApi9 无主 → 占比 10% 远低于 0.4
    subs = [_st(f"st-{i}", create=[f"m/src/main/java/com/x/IApi{i}.java"]) for i in range(9)]
    plan = _plan(subs)
    r2 = validate_contract_ownership(plan, contract)
    assert r2.valid, "对照面：无 punt 账时 10% 无主仅 warn 放行（0.4 宽容既有语义）"
    r = validate_contract_ownership(
        plan, contract, layout_punted=["IApi9→m/IApi9.java"])
    assert not r.valid, "布局闸 punt 的符号绝不进 0.4 宽容——确定性判死的必须硬打回"
    assert any("布局闸" in str(i) or "可编译源码布局" in str(i) for i in r.issues)


def test_c1_punted_referenced_dto_also_rejects():
    """★复核 R2 MEDIUM-R2-1★：被接口签名引用的 dto（T6② 幻影 DTO 族，domicile 当硬
    符号安置，round63 cannot-find-symbol×8）被布局闸 punt 时，C1 绝不按 kind=dtos
    降软仅 warn——安置侧视其为必安置，打回侧必须对称（否则迟败面留缝）。"""
    from swarm.brain.plan_validator import validate_contract_ownership
    contract = {"interfaces": [{"name": f"IApi{i}", "module": "m",
                                "signature": f"Resp{i} list(AlarmDTO q)"}
                               for i in range(10)],
                "dtos": [{"name": "AlarmDTO", "module": "m", "purpose": "查询入参"}]}
    subs = [_st(f"st-{i}", create=[f"m/src/main/java/com/x/IApi{i}.java"]) for i in range(9)]
    plan = _plan(subs)
    r2 = validate_contract_ownership(plan, contract)
    assert r2.valid, "对照面：无 punt 账时软性 dto 无主仅 warn（R39-3 既有语义）"
    r = validate_contract_ownership(
        plan, contract, layout_punted=["AlarmDTO→m/AlarmDTO.java"])
    assert not r.valid, "punt 的引用 dto 绝不降软放行（与安置侧硬符号对待对称）"


# ─── B1 file_plan 合流闸 ───


from swarm.brain.nodes import _merge_designed_file_plan  # noqa: E402


def test_merge_gate_rejects_same_name_divergent_fqn(caplog):
    """round67m2 自污染本体：后补 create 与既有 create 同 simple-name 异 FQN → 拒收
    （首写者=tech_design 原始权威优先），td 权威不再自判 ambiguous 全灭。"""
    existing = [{"module": "m1", "action": "create",
                 "path": "m1/src/main/java/com/a/AlarmService.java"}]
    new = [{"module": "m2", "path": "m2/src/main/java/com/b/AlarmService.java"}]
    with caplog.at_level(logging.WARNING):
        merged, added, dropped = _merge_designed_file_plan(existing, new)
    assert added == 0 and dropped == 1, "同 simple-name 异 FQN 后补 create 必须拒收"
    assert len(merged) == 1, "首写者权威保留、第二落点绝不进 file_plan"
    assert any("合流闸" in r.message for r in caplog.records)


def test_merge_gate_allows_non_jvm_and_same_fqn():
    """边界：非 JVM 路径（无 FQN）不参与判撞；同 simple-name 同 FQN（幂等补排）放行。"""
    existing = [{"module": "m1", "action": "create",
                 "path": "m1/src/main/java/com/a/AlarmService.java"}]
    merged, added, _ = _merge_designed_file_plan(
        existing, [{"module": "m1", "path": "m1/src/main/resources/AlarmService.xml"}])
    assert added == 1, "非 JVM 类路径路径绝不参与同名判撞（栈中立）"
    # 既有条目 action=modify（非 create）不建权威 → 后补 create 放行
    merged2, added2, _ = _merge_designed_file_plan(
        [{"module": "m1", "action": "modify",
          "path": "m1/src/main/java/com/a/AlarmService.java"}],
        [{"module": "m2", "path": "m2/src/main/java/com/b/AlarmService.java"}])
    assert added2 == 1, "既有非 create 条目不构成 create 权威（判据与 _tech_design_authority 同源）"


def test_merge_gate_default_action_and_case_variant_rejected():
    """★复核 HIGH-1/LOW-9（reviewer+hunter 双逮）★：判据与 _tech_design_authority 逐字
    同源——action 键缺省=create（LLM JSON 常漏字段=生产真实形态，缺省条目不进权威集
    =round67m2 死因原样复发）；stem 键 lower 归一（AlarmService/alarmservice 变体对
    逃逸判撞=ambiguous 崩塌穿闸）。"""
    # 既有条目无 action 键（缺省即 create）+ 后补大小写变体异 FQN → 必须拒收
    existing = [{"module": "m1", "path": "m1/src/main/java/com/a/AlarmService.java"}]
    new = [{"module": "m2", "path": "m2/src/main/java/com/b/alarmservice.java"}]
    merged, added, dropped = _merge_designed_file_plan(existing, new)
    assert added == 0 and dropped == 1, \
        "action 缺省条目必须登记权威、大小写变体必须判撞（与 td_authority 同源）"
    assert len(merged) == 1


# ─── B2 契约策展扩面（prompt 引导面） ───


def test_contract_module_prompt_requires_defined_in():
    """B2：契约 prompt 必须要求 defined_in 落点策展（实现细节类跨批共享强制声明）——
    策展盲区（entity/mapper/impl/controller 零 defined_in）是 round67m2 契约权威全空的源头。"""
    from swarm.brain.planning_nodes import CONTRACT_MODULE_SYSTEM, CONTRACT_MODULE_USER
    assert "defined_in" in CONTRACT_MODULE_SYSTEM
    assert "defined_in" in CONTRACT_MODULE_USER
    assert "实现细节类" in CONTRACT_MODULE_SYSTEM, \
        "实现细节类（Impl/entity/mapper/controller/SPI）跨批共享必须被引导声明 defined_in"


# ─── A3 凭据类错误可观测 ───


def test_auth_shaped_error_markers():
    """A3 判形：401/403/PermissionDenied 全命中；瞬时超时/限流绝不误判凭据类。
    ★复核 MEDIUM-2/MEDIUM-5（双逮）★：数字码词边界+只取首行——供应商业务错误码
    （errcode=40302）、端口/trace id（2401s/4030）、次行 403 绝不误升 ERROR 把 ops
    指向"查凭据"的错误方向。"""
    from swarm.models.errors import is_auth_shaped_error
    assert is_auth_shaped_error(RuntimeError("403 PermissionDenied: 无权限"))
    assert is_auth_shaped_error(RuntimeError("Error code: 401 - invalid api key"))
    assert not is_auth_shaped_error(RuntimeError("connection timeout"))
    assert not is_auth_shaped_error(RuntimeError("rate limit exceeded"))
    assert not is_auth_shaped_error(RuntimeError("error_code=14030: system busy"))
    assert not is_auth_shaped_error(RuntimeError("upstream port 4030 connect timeout"))
    assert not is_auth_shaped_error(RuntimeError("retry after 2401s"))
    assert not is_auth_shaped_error(RuntimeError("request failed\n403 forbidden")), \
        "只取首行——次行 403 不构成凭据判形（traceback/明细行噪声）"


def test_router_callback_auth_error_escalates(caplog):
    """round67m2 k3 静默面正面证据：凭据类失败升 ERROR（人工可修事故），
    瞬时失败维持 WARNING——每次失败一条=持续面可观测。"""
    from swarm.models.router import ModelInvocationLogger
    cb = ModelInvocationLogger("brain", "k3-for-coding", "k3")
    with caplog.at_level(logging.WARNING):
        cb.on_llm_error(RuntimeError("403 PermissionDenied"))
        cb.on_llm_error(RuntimeError("request timed out"))
    auth = [r for r in caplog.records if "凭据" in r.message]
    assert auth and all(r.levelno >= logging.ERROR for r in auth), \
        "凭据类错误必须 ERROR 级（round67m2 403 静默 2h20m 零 WARNING 的反面）"
    assert any(r.levelno == logging.WARNING and "timed out" in r.message
               for r in caplog.records), "瞬时错误维持 WARNING 分级（不抬噪声）"
