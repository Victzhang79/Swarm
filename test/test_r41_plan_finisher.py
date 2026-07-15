#!/usr/bin/env python3
"""R41（round41 治本批）—— PLAN 确定性收尾器：外科通道互斥病根的组合修复。

取证（task 3740e421，2026-07-12 FAILED@PLAN，2h22min 0 执行）：
1. 直接死因：最后一轮重试的校验失败同时携带【覆盖缺口 + file_plan 孤儿】两类
   issue，P1 覆盖外科抢跑（first-match-wins：task_plan is None 才轮到下一通道），
   R40-1 缺件外科全程零触发——一个 `sql/alarm_notice_read.sql` 无 owner 带病重验，
   重试耗尽 → CONFIRM auto_accept fail-fast 拒绝 → FAILED。
2. 次生：R39-4 脚手架注入只接线在符号外科内部；符号外科修不了硬符号如实回退时，
   注入随被丢弃的候选一起蒸发（02:18:46.049 注入 11 模块 → 全量重拆冲掉），
   规则5 预警 11 模块贯穿三轮原样复现。
治本：finish_plan_deterministic 在 PLAN 后处理区统一跑（任何产出路径），
  ①脚手架注入 ②孤儿挂靠（fail-open：挂不上的留 VALIDATE 权威打回）。
"""
from __future__ import annotations

import importlib.util
from pathlib import Path

_bs = Path(__file__).resolve().parent / "swarm_bootstrap.py"
_spec = importlib.util.spec_from_file_location("swarm_bootstrap", _bs)
_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_mod)

from swarm.brain.plan_finisher import finish_plan_deterministic  # noqa: E402
from swarm.brain.plan_validator import validate_file_plan_ownership  # noqa: E402
from swarm.brain.symbol_surgery import (  # noqa: E402
    attach_orphan_file_plan_entries,
    maybe_file_plan_repair,
)
from swarm.types import (  # noqa: E402
    FileScope,
    SubTask,
    SubTaskDifficulty,
    TaskPlan,
)


def _st(sid, desc="", writable=None, create=None):
    return SubTask(id=sid, description=desc or f"task {sid}",
                   difficulty=SubTaskDifficulty.MEDIUM,
                   scope=FileScope(writable=writable or [], create_files=create or []))


# ── ① 共享内核：孤儿挂靠 ──

def test_orphan_attach_round41_death_scenario():
    """round41 真死因复现：sql/ 模块孤儿文件必须挂到已有 sql 文件的子任务。"""
    plan = TaskPlan(subtasks=[
        _st("st-1", create=["alarm-task/src/main/java/com/x/A.java"]),
        _st("st-sql", create=["sql/alarm_task.sql", "sql/alarm_channel.sql"]),
    ], parallel_groups=[["st-1", "st-sql"]])
    fp = ["alarm-task/src/main/java/com/x/A.java", "sql/alarm_task.sql",
          "sql/alarm_channel.sql", "sql/alarm_notice_read.sql"]
    attached, left = attach_orphan_file_plan_entries(plan, fp)
    assert attached == 1 and not left
    assert "sql/alarm_notice_read.sql" in plan.subtasks[1].scope.create_files
    assert validate_file_plan_ownership(plan, fp).valid, "挂靠后闸必过"


def test_orphan_attach_no_candidate_fail_open():
    """无同模块候选：不猜挂，如实回 left（收尾器语义=留 VALIDATE 打回）。"""
    plan = TaskPlan(subtasks=[
        _st("st-1", create=["mod-a/A.java"]),
        _st("st-2", create=["mod-a/B.java"]),
    ], parallel_groups=[["st-1", "st-2"]])
    attached, left = attach_orphan_file_plan_entries(
        plan, ["mod-a/A.java", "mod-b/C.java"])
    assert attached == 0 and left == ["mod-b/C.java"]


def test_orphan_attach_prefers_deepest_prefix():
    plan = TaskPlan(subtasks=[
        _st("st-shallow", create=["mod-a/pom-notes.md"]),
        _st("st-deep", create=["mod-a/src/main/java/com/x/A.java"]),
    ], parallel_groups=[["st-shallow", "st-deep"]])
    attached, left = attach_orphan_file_plan_entries(
        plan, ["mod-a/src/main/java/com/x/B.java"])
    assert attached == 1 and not left
    assert ("mod-a/src/main/java/com/x/B.java"
            in plan.subtasks[1].scope.create_files), "共享前缀最深者优先"


def test_orphan_attach_idempotent():
    plan = TaskPlan(subtasks=[
        _st("st-1", create=["mod-a/A.java"]),
        _st("st-2", create=["mod-a/sub/B.java"]),
    ], parallel_groups=[["st-1", "st-2"]])
    fp = ["mod-a/A.java", "mod-a/sub/B.java", "mod-a/sub/C.java"]
    a1, _ = attach_orphan_file_plan_entries(plan, fp)
    a2, _ = attach_orphan_file_plan_entries(plan, fp)
    assert a1 == 1 and a2 == 0, "二次调用零变更（幂等）"
    assert plan.subtasks[1].scope.create_files.count("mod-a/sub/C.java") == 1


# ── ② 外科通道 strict 语义回归（重构后不回归 round40 行为）──

def test_fileplan_repair_strict_still_bails_on_unattachable():
    prior = TaskPlan(subtasks=[
        _st("st-1", create=["mod-a/A.java"]),
        _st("st-2", create=["mod-a/B.java"]),
    ], parallel_groups=[["st-1", "st-2"]])
    state = {
        "plan": prior,
        "plan_validation_feedback": "file_plan 文件无 owner 子任务: mod-b/C.java",
        "plan_validation_issues": ["file_plan 文件无 owner 子任务: mod-b/C.java"],
        "tech_design_file_plan": ["mod-a/A.java", "mod-a/B.java", "mod-b/C.java"],
    }
    assert maybe_file_plan_repair(state) is None, "挂不上必须整体回退全量重拆（不半修）"


def test_fileplan_repair_strict_repairs_when_attachable():
    prior = TaskPlan(subtasks=[
        _st("st-1", create=["mod-a/A.java"]),
        _st("st-2", create=["mod-a/sub/B.java"]),
    ], parallel_groups=[["st-1", "st-2"]])
    state = {
        "plan": prior,
        "plan_validation_feedback": "file_plan 文件无 owner 子任务: mod-a/sub/C.java",
        "plan_validation_issues": ["file_plan 文件无 owner 子任务: mod-a/sub/C.java"],
        "tech_design_file_plan": ["mod-a/A.java", "mod-a/sub/B.java", "mod-a/sub/C.java"],
    }
    repaired = maybe_file_plan_repair(state)
    assert repaired is not None
    assert "mod-a/sub/C.java" in repaired.subtasks[1].scope.create_files
    assert prior.subtasks[1].scope.create_files == ["mod-a/sub/B.java"], \
        "deepcopy：绝不半改原 plan"


# ── ③ 收尾器：组合修复（P1 抢跑后的 plan 也能被修）──

def test_finisher_attaches_orphans_regardless_of_plan_source():
    """互斥病根治本：收尾器不看 plan 从哪来，孤儿一律确定性挂靠。"""
    plan = TaskPlan(subtasks=[
        _st("st-1", create=["alarm-task/src/A.java"]),
        _st("st-sql", create=["sql/a.sql"]),
    ], parallel_groups=[["st-1", "st-sql"]])
    fp = ["alarm-task/src/A.java", "sql/a.sql", "sql/orphan.sql"]
    out = finish_plan_deterministic(plan, fp)
    assert out["orphans_attached"] == 1 and not out["orphans_left"]
    assert validate_file_plan_ownership(plan, fp).valid


def test_finisher_injects_scaffolds_for_unclaimed_deps():
    """R41-2：脚手架注入不再依赖符号外科存活——收尾器直接注入。"""
    plan = TaskPlan(subtasks=[
        _st("st-1", create=["mod-a/src/A.java", "mod-a/pom.xml"]),
        _st("st-c", create=["mod-c/src/C.java", "mod-c/pom.xml"]),
        _st("st-2", create=["mod-b/src/B.java"]),
    ], parallel_groups=[["st-1", "st-c", "st-2"]])
    plan.shared_contract = {"dependencies": [
        {"module": "mod-b", "artifacts": ["org.x:mod-a"]},
    ]}
    out = finish_plan_deterministic(
        plan, ["mod-a/src/A.java", "mod-c/src/C.java", "mod-b/src/B.java"])
    assert out["scaffolds"] == ["mod-b"]
    sids = {st.id for st in plan.subtasks}
    assert "st-scaffold-mod-b" in sids
    scaffold = next(st for st in plan.subtasks if st.id == "st-scaffold-mod-b")
    # F5：无 project_path 时保守 MODIFY（writable）——绝不 CREATE 盖基线 pom
    assert "mod-b/pom.xml" in (list(scaffold.scope.writable)
                               + list(scaffold.scope.create_files))
    # bootstrap 已执行的证据：推断 harness 至少带命令白名单（语言推断依赖真实任务
    # 描述，最小测试描述推不出 build_command 属 _infer_harness 正常保守行为）
    assert scaffold.harness is not None and scaffold.harness.extra_whitelist, \
        "收尾器自行 bootstrap harness（错过主循环）"
    assert scaffold.est_context_tokens > 0
    st2 = next(st for st in plan.subtasks if st.id == "st-2")
    assert "st-scaffold-mod-b" in st2.depends_on, "同模块写码子任务依赖脚手架"


def test_finisher_single_subtask_plan_skips_orphan_attach():
    """SIMPLE 面自证：单子任务计划收尾器不越权挂靠（与闸同口径跳过）。"""
    plan = TaskPlan(subtasks=[_st("st-1", create=["mod-a/A.java"])],
                    parallel_groups=[["st-1"]])
    out = finish_plan_deterministic(plan, ["mod-a/A.java", "mod-a/B.java"])
    assert out["orphans_attached"] == 0 and not out["orphans_left"]
    assert plan.subtasks[0].scope.create_files == ["mod-a/A.java"]


def test_finisher_fail_open_on_none_plan():
    out = finish_plan_deterministic(None, ["a/b.java"])
    assert out == {"scaffolds": [], "orphans_attached": 0, "orphans_left": []}


# ── ④ 对抗复核整改回归（F1/F2/F3/F5）──

def test_f3_attach_injects_intent_into_description_and_ac():
    """F3：挂靠必须带意图——worker prompt 提及该文件 + 验收标准兜底。"""
    plan = TaskPlan(subtasks=[
        _st("st-1", create=["mod-a/A.java"]),
        _st("st-2", create=["sql/a.sql"]),
    ], parallel_groups=[["st-1", "st-2"]])
    attach_orphan_file_plan_entries(plan, ["sql/orphan.sql"])
    st2 = plan.subtasks[1]
    assert "sql/orphan.sql" in (st2.description or "")
    assert any("sql/orphan.sql" in c for c in (st2.acceptance_criteria or []))


def test_f1_attach_recorded_and_covers_merge_survives_scope_drift():
    """F1：挂靠记录进 plan.finisher_attached；#6 覆盖单调化跨轮键漂移仍能配对并回 covers。"""
    from swarm.brain.nodes import _merge_prior_covers_by_scope

    # 挂靠轮（prior）：st-2 被收尾器挂了 orphan.sql 并记录
    prior = TaskPlan(subtasks=[
        _st("st-1", create=["mod-a/A.java"]),
        _st("st-2", create=["sql/a.sql"]),
    ], parallel_groups=[["st-1", "st-2"]])
    attach_orphan_file_plan_entries(prior, ["sql/orphan.sql"])
    assert prior.finisher_attached == {"st-2": ["sql/orphan.sql"]}
    prior.subtasks[1].covers = ["req-1"]

    # 全量重拆轮（new）：LLM 原始 scope 不带 orphan.sql（键漂移场景）
    new = TaskPlan(subtasks=[
        _st("st-x", create=["mod-a/A.java"]),
        _st("st-y", create=["sql/a.sql"]),
    ], parallel_groups=[["st-x", "st-y"]])
    injected = _merge_prior_covers_by_scope(new, prior, {"req-1"})
    assert injected.get("st-y") == {"req-1"}, \
        "剔除挂靠记录后 scope 身份还原，covers 必须并回（不再静默丢失）"
    assert "req-1" in (new.subtasks[1].covers or [])


def test_f1_surgical_deepcopy_side_still_matches():
    """F1 对称性：外科 deepcopy 路径两侧都带挂靠文件+记录 → 剔除后键仍相等。"""
    from swarm.brain.nodes import _merge_prior_covers_by_scope
    prior = TaskPlan(subtasks=[
        _st("st-1", create=["mod-a/A.java"]),
        _st("st-2", create=["sql/a.sql"]),
    ], parallel_groups=[["st-1", "st-2"]])
    attach_orphan_file_plan_entries(prior, ["sql/orphan.sql"])
    prior.subtasks[1].covers = ["req-1"]
    copied = prior.model_copy(deep=True)  # 外科通道语义
    for st in copied.subtasks:
        st.covers = []
    injected = _merge_prior_covers_by_scope(copied, prior, {"req-1"})
    assert injected.get("st-2") == {"req-1"}


def test_f2_ownership_denominator_excludes_unrequested_tests():
    """F2：任务未要求测试时，测试路径不进归属分母（防挂靠→剥离→打回确定性弹跳）。"""
    plan = TaskPlan(subtasks=[
        _st("st-1", create=["mod-a/A.java"]),
        _st("st-2", create=["mod-a/B.java"]),
    ], parallel_groups=[["st-1", "st-2"]])
    fp = ["mod-a/A.java", "mod-a/B.java",
          "mod-a/src/test/java/ATest.java"]
    assert not validate_file_plan_ownership(plan, fp).valid, "不排除时按旧口径打回"
    assert validate_file_plan_ownership(plan, fp, exclude_test_paths=True).valid


def test_f2_finisher_skips_test_orphans():
    """F2：收尾器在 strip 之后运行，绝不把刚被剥掉的测试文件挂回去。"""
    plan = TaskPlan(subtasks=[
        _st("st-1", create=["mod-a/A.java"]),
        _st("st-2", create=["mod-a/B.java"]),
    ], parallel_groups=[["st-1", "st-2"]])
    fp = ["mod-a/A.java", "mod-a/B.java", "mod-a/src/test/java/ATest.java"]
    out = finish_plan_deterministic(plan, fp, task_description="加一个接口")
    assert out["orphans_attached"] == 0 and not out["orphans_left"]
    all_files = [f for st in plan.subtasks for f in st.scope.create_files]
    assert "mod-a/src/test/java/ATest.java" not in all_files


def test_f5_inject_unknown_project_path_defaults_modify():
    """F5：project_path 未知时脚手架保守走 MODIFY（writable），绝不 CREATE 盖基线 pom。"""
    from swarm.brain.contract_utils import inject_build_scaffold_subtasks
    plan = TaskPlan(subtasks=[
        _st("st-1", create=["mod-a/src/A.java", "mod-a/pom.xml"]),
        _st("st-c", create=["mod-c/src/C.java", "mod-c/pom.xml"]),
        _st("st-2", create=["mod-b/src/B.java"]),
    ], parallel_groups=[["st-1", "st-c", "st-2"]])
    plan.shared_contract = {"dependencies": [
        {"module": "mod-b", "artifacts": ["org.x:mod-a"]},
    ]}
    injected = inject_build_scaffold_subtasks(plan, None)
    assert injected and injected[0]["pom_exists"] is True
    scaffold = next(st for st in plan.subtasks if st.id == "st-scaffold-mod-b")
    assert scaffold.scope.writable == ["mod-b/pom.xml"]
    assert not scaffold.scope.create_files


def test_f5_owner_check_normalizes_dot_slash():
    """F5：'./mod/pom.xml' 写法的 owner 必须被识别（防重复注入→T3 降级空壳）。"""
    from swarm.brain.contract_utils import unclaimed_contract_deps
    plan = TaskPlan(subtasks=[
        _st("st-1", create=["./mod-b/pom.xml", "mod-b/src/B.java"]),
        _st("st-2", create=["mod-a/pom.xml", "mod-a/src/A.java"]),
    ], parallel_groups=[["st-1", "st-2"]])
    plan.shared_contract = {"dependencies": [
        {"module": "mod-b", "artifacts": ["org.x:mod-a"]},
    ]}
    assert unclaimed_contract_deps(plan) == [], "归一后 ./mod-b/pom.xml 是合法 owner"


# ── ③ R48-1：无候选孤儿 → 确定性新建承接子任务 ──
def test_r48_orphan_no_candidate_synthesizes_subtask():
    """round48 死因回归：ruoyi-common 孤儿文件无任何同模块子任务 → 三轮原样打回
    CONFIRM 拒绝。治=收尾器新建承接子任务，VALIDATE 归属过闸。"""
    plan = TaskPlan(
        task_id="t-r48", subtasks=[
            _st("st-1", create=["alarm-core/src/A.java"]),
            _st("st-2", create=["alarm-core/src/B.java"])],
        parallel_groups=[["st-1", "st-2"]])
    fp = [{"path": "ruoyi-common/src/main/java/com/ruoyi/common/utils/sign/SysPasswordService.java",
           "purpose": "密码策略校验服务"}]
    out = finish_plan_deterministic(plan, fp, None, "实现密码策略")
    assert out["orphans_left"] == [], "无候选孤儿必须被承接，不再留给 VALIDATE 打回"
    assert out.get("orphan_subtasks"), "机读账必须记录新建承接"
    sid = next(iter(out["orphan_subtasks"]))
    st = next(s for s in plan.subtasks if s.id == sid)
    assert st.scope.create_files == [fp[0]["path"]]
    assert "密码策略校验服务" in st.description, "file_plan purpose 必须进 worker 明示意图"
    assert [sid] in plan.parallel_groups, "parallel_groups 完整性守约"
    # 归属校验过闸（VALIDATE 权威口径）
    res = validate_file_plan_ownership(plan, fp)
    assert res.valid and not res.issues


def test_r48_orphan_synthesis_idempotent():
    plan = TaskPlan(
        task_id="t-r48b", subtasks=[_st("st-1", create=["m/x.java"])],
        parallel_groups=[["st-1"]])
    # 单子任务计划跳过孤儿处理（既有语义）——用双子任务触发
    plan.subtasks.append(_st("st-2", create=["m/y.java"]))
    plan.parallel_groups[0].append("st-2")
    fp = [{"path": "other/z.java"}]
    out1 = finish_plan_deterministic(plan, fp, None, "t")
    n1 = len(plan.subtasks)
    out2 = finish_plan_deterministic(plan, fp, None, "t")
    assert len(plan.subtasks) == n1, "二跑不得重复建子任务"
    assert out1.get("orphan_subtasks")
    assert not out2.get("orphan_subtasks") and out2["orphans_left"] == []


def test_r48_existing_baseline_file_goes_writable(tmp_path):
    """基线已存在的孤儿文件 → writable（改）而非 create_files（防 clobber 语义错位）。"""
    (tmp_path / "modx").mkdir()
    (tmp_path / "modx" / "Existing.java").write_text("class E {}", "utf-8")
    plan = TaskPlan(
        task_id="t-r48c", subtasks=[
            _st("st-1", create=["m/a.java"]), _st("st-2", create=["m/b.java"])],
        parallel_groups=[["st-1", "st-2"]])
    fp = [{"path": "modx/Existing.java"}]
    out = finish_plan_deterministic(plan, fp, str(tmp_path), "t")
    sid = next(iter(out["orphan_subtasks"]))
    st = next(s for s in plan.subtasks if s.id == sid)
    assert st.scope.writable == ["modx/Existing.java"]
    assert st.scope.create_files == []


def test_r48_depends_on_same_module_scaffold():
    """同模块已有脚手架 → 新建承接子任务依赖之（先有 pom 再写码）。"""
    plan = TaskPlan(
        task_id="t-r48d", subtasks=[
            _st("st-scaffold-modz", create=["modz/pom.xml"]),
            _st("st-1", create=["m/a.java"])],
        parallel_groups=[["st-scaffold-modz", "st-1"]])
    fp = [{"path": "modz/src/Svc.java"}]
    out = finish_plan_deterministic(plan, fp, None, "t")
    sid = next(iter(out["orphan_subtasks"]))
    st = next(s for s in plan.subtasks if s.id == sid)
    assert "st-scaffold-modz" in st.depends_on


def test_r48_f1_adopt_into_existing_fileplan_subtask():
    """复核 F1：sid 撞既有 st-fileplan-* → 收养追加，绝不丢弃后到孤儿。"""
    plan = TaskPlan(
        task_id="t-r48e", subtasks=[
            _st("st-fileplan-modq", create=["modq/pom.xml"]),  # 只有构建清单=非候选
            _st("st-1", create=["m/a.java"]), _st("st-2", create=["m/b.java"])],
        parallel_groups=[["st-fileplan-modq", "st-1", "st-2"]])
    fp = [{"path": "modq/src/NewSvc.java", "purpose": "新服务"}]
    out = finish_plan_deterministic(plan, fp, None, "t")
    host = next(s for s in plan.subtasks if s.id == "st-fileplan-modq")
    assert "modq/src/NewSvc.java" in host.scope.create_files, "后到孤儿必须被收养"
    assert out["orphans_left"] == []
    assert "新服务" in host.description


def test_r48_f2_large_group_presharded():
    """复核 F2：15 个同模块孤儿 → 预分片（每组 ≤6），绝不造超限子任务。"""
    plan = TaskPlan(
        task_id="t-r48f", subtasks=[
            _st("st-1", create=["m/a.java"]), _st("st-2", create=["m/b.java"])],
        parallel_groups=[["st-1", "st-2"]])
    fp = [{"path": f"bigmod/src/F{i}.java"} for i in range(15)]
    out = finish_plan_deterministic(plan, fp, None, "t")
    assert out["orphans_left"] == []
    sts = [s for s in plan.subtasks if s.id.startswith("st-fileplan-bigmod")]
    assert len(sts) == 3, "15 文件 → 3 片（6+6+3）"
    for s in sts:
        assert len(s.scope.create_files) + len(s.scope.writable) <= 6


# ── ④ R48b-1：契约符号安置 ──
_SC = {"interfaces": [
    {"name": "RobotQueryService", "module": "alarm-robot"},
    {"name": "TemplateRenderService", "module": "alarm-template"},
    {"name": "IChannelSender", "module": "alarm-channel"}],
    "dtos": [{"name": "RobotDTO", "module": "alarm-robot"}]}


def test_r48b_unowned_hard_symbols_domiciled():
    """round48b 死因回归：P1 短路符号外科后，收尾器必须安置无主硬符号过 C1。"""
    plan = TaskPlan(
        task_id="t-48b", subtasks=[
            _st("st-1", create=["ruoyi-alarm/src/main/java/com/ruoyi/alarm/A.java"]),
            _st("st-2", create=["ruoyi-alarm/src/main/java/com/ruoyi/alarm/B.java"])],
        parallel_groups=[["st-1", "st-2"]])
    out = finish_plan_deterministic(plan, [], None, "t", shared_contract=_SC)
    dom = out.get("symbols_domiciled")
    assert dom, "无主硬符号必须被安置"
    from swarm.brain.plan_validator import unowned_contract_symbols
    hard = ["RobotQueryService", "TemplateRenderService", "IChannelSender"]
    assert unowned_contract_symbols(plan, hard) == [], "安置后 C1 硬符号全员有主"
    # 软符号（dtos kind）不安置——随宿主落地
    all_files = [f for s in plan.subtasks for f in s.scope.create_files]
    assert not any("RobotDTO" in f for f in all_files)
    # 扩展名取 plan 众数（java）
    assert any(f.endswith("RobotQueryService.java") for f in all_files)


def test_r48b_scaffold_dependency_and_idempotent():
    plan = TaskPlan(
        task_id="t-48c", subtasks=[
            _st("st-scaffold-alarm-robot", create=["alarm-robot/pom.xml"]),
            _st("st-1", create=["m/a.java"]), _st("st-2", create=["m/b.java"])],
        parallel_groups=[["st-scaffold-alarm-robot", "st-1", "st-2"]])
    sc = {"interfaces": [{"name": "RobotQueryService", "module": "alarm-robot"}]}
    out1 = finish_plan_deterministic(plan, [], None, "t", shared_contract=sc)
    assert out1.get("symbols_domiciled")
    st = next(s for s in plan.subtasks if s.id == "st-contract-alarm-robot")
    assert "st-scaffold-alarm-robot" in st.depends_on
    n1 = len(plan.subtasks)
    out2 = finish_plan_deterministic(plan, [], None, "t", shared_contract=sc)
    assert len(plan.subtasks) == n1 and not out2.get("symbols_domiciled")


def test_r48b_module_missing_left_to_validate():
    """module 归属缺失的符号如实留给 VALIDATE，绝不猜模块。"""
    plan = TaskPlan(
        task_id="t-48d", subtasks=[
            _st("st-1", create=["m/a.java"]), _st("st-2", create=["m/b.java"])],
        parallel_groups=[["st-1", "st-2"]])
    sc = {"interfaces": [{"name": "OrphanNoModuleService", "module": ""}]}
    out = finish_plan_deterministic(plan, [], None, "t", shared_contract=sc)
    assert not out.get("symbols_domiciled")


def test_r48b_adopt_into_existing_contract_subtask():
    plan = TaskPlan(
        task_id="t-48e", subtasks=[
            _st("st-contract-alarm-robot",
                create=["alarm-robot/src/x/RobotQueryService.java"]),
            _st("st-1", create=["m/a.java"]), _st("st-2", create=["m/b.java"])],
        parallel_groups=[["st-contract-alarm-robot", "st-1", "st-2"]])
    sc = {"interfaces": [
        {"name": "RobotQueryService", "module": "alarm-robot"},
        {"name": "RobotAuditService", "module": "alarm-robot"}]}
    out = finish_plan_deterministic(plan, [], None, "t", shared_contract=sc)
    host = next(s for s in plan.subtasks if s.id == "st-contract-alarm-robot")
    assert any("RobotAuditService" in f for f in host.scope.create_files), "后到符号收养"
    assert out["symbols_domiciled"]["st-contract-alarm-robot"] == ["RobotAuditService"]


def test_r48b_f1_dirty_symbol_names_rejected():
    """复核 F1：脏符号名（URL/泛型/穿越）绝不进路径。"""
    plan = TaskPlan(
        task_id="t-48f", subtasks=[
            _st("st-1", create=["m/src/a.java"]), _st("st-2", create=["m/src/b.java"])],
        parallel_groups=[["st-1", "st-2"]])
    sc = {"interfaces": [
        {"name": "GET /system/robot/Export", "module": "alarm-api"},
        {"name": "IChannelSender<T>", "module": "alarm-channel"},
        {"name": "../EvilService", "module": "alarm-x"},
        {"name": "GoodService", "module": "alarm-y"}]}
    out = finish_plan_deterministic(plan, [], None, "t", shared_contract=sc)
    all_files = [f for s in plan.subtasks for f in s.scope.create_files]
    assert not any(" " in f or "<" in f or ".." in f for f in all_files)
    assert any(f.endswith("GoodService.java") for f in all_files)


def test_r48b_f2_single_module_layout_keeps_src_root():
    """复核 F2：单模块工程（src/ 顶层）——新模块路径必须保留完整源根段。"""
    plan = TaskPlan(
        task_id="t-48g", subtasks=[
            _st("st-1", create=["src/main/java/com/x/A.java"]),
            _st("st-2", create=["src/main/java/com/x/B.java"])],
        parallel_groups=[["st-1", "st-2"]])
    sc = {"interfaces": [{"name": "RobotQueryService", "module": "alarm-robot"}]}
    out = finish_plan_deterministic(plan, [], None, "t", shared_contract=sc)
    assert out.get("symbols_domiciled")
    f = next(f for s in plan.subtasks for f in s.scope.create_files
             if f.endswith("RobotQueryService.java"))
    assert "/src/main/java/" in f, f"必须含完整源根: {f}"


def test_r48b_f3_no_source_ext_evidence_fails_open():
    """复核 F3：纯配置 plan（无源码扩展名）→ 不猜语言，安置跳过。"""
    plan = TaskPlan(
        task_id="t-48h", subtasks=[
            _st("st-1", create=["conf/app.yml"]), _st("st-2", create=["db/init.sql"])],
        parallel_groups=[["st-1", "st-2"]])
    sc = {"interfaces": [{"name": "RobotQueryService", "module": "alarm-robot"}]}
    out = finish_plan_deterministic(plan, [], None, "t", shared_contract=sc)
    assert not out.get("symbols_domiciled")


def test_r48b_f4_adopt_overflow_shards():
    """复核 F4：host 满员后溢出分片，符号一个不丢。"""
    plan = TaskPlan(
        task_id="t-48i", subtasks=[
            _st("st-contract-alarm-robot",
                create=[f"alarm-robot/src/S{i}.java" for i in range(6)]),
            _st("st-1", create=["m/src/a.java"]), _st("st-2", create=["m/src/b.java"])],
        parallel_groups=[["st-contract-alarm-robot", "st-1", "st-2"]])
    sc = {"interfaces": [
        {"name": f"NewSvc{i}", "module": "alarm-robot"} for i in range(8)]}
    out = finish_plan_deterministic(plan, [], None, "t", shared_contract=sc)
    dom = out["symbols_domiciled"]
    placed = [s for v in dom.values() for s in v]
    assert sorted(placed) == sorted(f"NewSvc{i}" for i in range(8)), "8 符号全安置"
    host = next(s for s in plan.subtasks if s.id == "st-contract-alarm-robot")
    assert len(host.scope.create_files) <= 6, "host 不超员"
    assert any(s.id.startswith("st-contract-alarm-robot-") for s in plan.subtasks)


def test_r48b_f5_new_module_gets_scaffold(tmp_path):
    """复核 F5：物理不存在的新模块 → 补注脚手架并依赖之（防 reactor missing-child）。"""
    (tmp_path / "pom.xml").write_text(
        '<?xml version="1.0"?><project>'
        "<groupId>com.x</groupId><artifactId>root</artifactId>"
        "<version>1.0</version><packaging>pom</packaging></project>", "utf-8")
    plan = TaskPlan(
        task_id="t-48j", subtasks=[
            _st("st-1", create=["m/src/a.java"]), _st("st-2", create=["m/src/b.java"])],
        parallel_groups=[["st-1", "st-2"]])
    sc = {"interfaces": [{"name": "RobotQueryService", "module": "alarm-robot"}]}
    finish_plan_deterministic(plan, [], str(tmp_path), "t", shared_contract=sc)
    sc_st = next((s for s in plan.subtasks if s.id == "st-scaffold-alarm-robot"), None)
    assert sc_st is not None, "新模块必须补注脚手架"
    assert "权威 pom 模板" in sc_st.description
    ct = next(s for s in plan.subtasks if s.id == "st-contract-alarm-robot")
    assert "st-scaffold-alarm-robot" in ct.depends_on


# ── ④' Task1（round62 治本）：契约符号安置落点走 file_plan 权威物理路径 ──

def test_r62_task1_domicile_uses_file_plan_physical_path():
    """round62 幻影落点治本：逻辑模块名 ≠ 物理目录（契约 alarm-sdk 实住
    ruoyi-alarm/alarm-interface/）时，符号必须落到 file_plan 权威物理目录，
    绝不拼幻影 `alarm-sdk/…`、绝不把 .java 落进 resources/mapper。"""
    plan = TaskPlan(
        task_id="t-62task1", subtasks=[
            _st("st-1", create=[
                "ruoyi-alarm/alarm-interface/src/main/java/com/ruoyi/alarm/"
                "sdk/AlarmSdkConfig.java"]),
            _st("st-2", create=[
                "ruoyi-alarm/alarm-api/src/main/java/com/ruoyi/alarm/api/"
                "AlarmApiDTO.java"])],
        parallel_groups=[["st-1", "st-2"]])
    file_plan = [
        {"module": "alarm-sdk", "path":
            "ruoyi-alarm/alarm-interface/src/main/java/com/ruoyi/alarm/sdk/"
            "AlarmSdkConfig.java"},
        {"module": "alarm-sdk", "path":
            "ruoyi-alarm/alarm-interface/src/main/java/com/ruoyi/alarm/sdk/"
            "http/HttpUtil.java"},
        {"module": "alarm-api", "path":
            "ruoyi-alarm/alarm-api/src/main/java/com/ruoyi/alarm/api/AlarmApiDTO.java"},
    ]
    sc = {"interfaces": [{"name": "IAlarmHttpClient", "module": "alarm-sdk"}]}
    out = finish_plan_deterministic(plan, file_plan, None, "t", shared_contract=sc)
    assert out.get("symbols_domiciled"), "无主硬符号必须被安置"
    f = next(f for s in plan.subtasks for f in s.scope.create_files
             if f.endswith("IAlarmHttpClient.java"))
    # 逻辑名 alarm-sdk → 物理目录 ruoyi-alarm/alarm-interface/（file_plan 权威）
    assert f.startswith("ruoyi-alarm/alarm-interface/src/main/java/"), f
    assert "/resources/mapper/" not in f, f
    assert not f.startswith("alarm-sdk/"), f


def test_r62_task1_main_test_split_places_in_main_not_shallow_src():
    """对抗复核 HIGH 回归：模块源文件同时含 main 与 test 分支时，落点必须是 main 下的
    真源目录，绝不塌成不可编译的浅目录 `.../src/`（旧公共前缀会塌）。"""
    plan = TaskPlan(
        task_id="t-62task1m", subtasks=[
            _st("st-1", create=[
                "ruoyi-alarm/alarm-interface/src/main/java/com/ruoyi/alarm/"
                "sdk/AlarmSdkConfig.java"]),
            _st("st-2", create=["ruoyi-alarm/alarm-api/src/main/java/com/x/A.java"])],
        parallel_groups=[["st-1", "st-2"]])
    file_plan = [
        {"module": "alarm-sdk", "path":
            "ruoyi-alarm/alarm-interface/src/main/java/com/ruoyi/alarm/sdk/"
            "AlarmSdkConfig.java"},
        {"module": "alarm-sdk", "path":
            "ruoyi-alarm/alarm-interface/src/test/java/com/ruoyi/alarm/sdk/"
            "AlarmSdkConfigTest.java"},
    ]
    sc = {"interfaces": [{"name": "IAlarmHttpClient", "module": "alarm-sdk"}]}
    finish_plan_deterministic(plan, file_plan, None, "t", shared_contract=sc)
    f = next(f for s in plan.subtasks for f in s.scope.create_files
             if f.endswith("IAlarmHttpClient.java"))
    assert "/src/main/java/" in f, f          # 真 main 源根，非浅 .../src/
    assert "/src/test/" not in f, f           # 主代码符号不落测试目录
    assert f.rsplit("/", 1)[0].endswith("com/ruoyi/alarm/sdk"), f


def test_r62_task1_cross_module_feature_placed_in_dominant_module():
    """round62 治本：跨【多个物理模块】的功能分组（module≠单一 build 单元）→ 落到
    主模块（源文件众数所在真目录），绝不臆造 `alarm-strategy/...` 幻影、也绝不丢符号
    （丢=C1 占比不足则仅告警不拦→符号既不落地又不被拦）。结构性归一交 Task4。"""
    plan = TaskPlan(
        task_id="t-62task1b", subtasks=[
            _st("st-1", create=[
                "ruoyi-alarm/alarm-schedule/src/main/java/com/ruoyi/alarm/"
                "schedule/service/A.java"]),
            _st("st-2", create=[
                "ruoyi-admin/src/main/java/com/ruoyi/web/controller/alarm/B.java"])],
        parallel_groups=[["st-1", "st-2"]])
    # alarm-schedule 3 文件（主）vs ruoyi-admin 1 文件 → 众数落 alarm-schedule
    file_plan = [
        {"module": "alarm-strategy", "path":
            "ruoyi-alarm/alarm-schedule/src/main/java/com/ruoyi/alarm/schedule/"
            f"service/S{i}.java"} for i in range(3)] + [
        {"module": "alarm-strategy", "path":
            "ruoyi-admin/src/main/java/com/ruoyi/web/controller/alarm/Ctrl.java"}]
    sc = {"interfaces": [{"name": "DutyScheduleService", "module": "alarm-strategy"}]}
    out = finish_plan_deterministic(plan, file_plan, None, "t", shared_contract=sc)
    assert out.get("symbols_domiciled"), "跨模块功能分组也要安置，绝不丢符号"
    f = next(f for s in plan.subtasks for f in s.scope.create_files
             if f.endswith("DutyScheduleService.java"))
    assert f.startswith("ruoyi-alarm/alarm-schedule/src/main/java/"), f  # 主模块
    assert not f.startswith("alarm-strategy/"), f                        # 非幻影逻辑名
    assert "/resources/mapper/" not in f, f


def test_r62_task1_partial_file_plan_places_via_phys_not_dropped():
    """对抗复核 MEDIUM 回归：file_plan 非空但【不含】某模块时，该模块仍应经 physical
    证据（含 flat 裸根）落到真目录，绝不因"有 file_plan 就走新路"而丢覆盖。"""
    plan = TaskPlan(
        task_id="t-62task1p", subtasks=[
            _st("st-1", create=[
                "alarm-sdk/src/main/java/com/ruoyi/alarm/sdk/AlarmSdkConfig.java"]),
            _st("st-2", create=["alarm-sdk/src/main/java/com/ruoyi/alarm/sdk/X.java"])],
        parallel_groups=[["st-1", "st-2"]])
    # file_plan 只覆盖别的模块，故意不含 alarm-sdk
    file_plan = [{"module": "alarm-api", "path":
                  "alarm-api/src/main/java/com/ruoyi/alarm/api/Foo.java"}]
    sc = {"interfaces": [{"name": "IAlarmHttpClient", "module": "alarm-sdk"}]}
    out = finish_plan_deterministic(plan, file_plan, None, "t", shared_contract=sc)
    assert out.get("symbols_domiciled"), "部分 file_plan 不得丢覆盖"
    f = next(f for s in plan.subtasks for f in s.scope.create_files
             if f.endswith("IAlarmHttpClient.java"))
    assert f.startswith("alarm-sdk/src/main/java/"), f   # flat 裸根经 physical 证据落真目录


def test_r62_task1_polyglot_module_uses_own_extension_not_plan_mode():
    """对抗复核 HIGH 回归：Java 主导计划里的 TS 模块，符号必须落到该模块【自身】的
    .ts 真目录、且文件名用 .ts（per-module 扩展名），绝不因 plan 全局 ext=java 饿死它
    而拼幻影（旧实现 plan 全局 ext 过滤会把异栈模块打回名字臆造路径）。"""
    plan = TaskPlan(
        task_id="t-62task1poly", subtasks=[
            _st("st-1", create=[
                "backend/alarm-core/src/main/java/com/x/A.java"]),
            _st("st-2", create=[
                "backend/alarm-core/src/main/java/com/x/B.java"]),
            _st("st-3", create=[
                "frontend/alarm-web/src/ts/AlarmWebClient.ts"])],
        parallel_groups=[["st-1", "st-2", "st-3"]])
    file_plan = [
        {"module": "alarm-core", "path": "backend/alarm-core/src/main/java/com/x/A.java"},
        {"module": "alarm-core", "path": "backend/alarm-core/src/main/java/com/x/B.java"},
        {"module": "alarm-web-sdk", "path": "frontend/alarm-web/src/ts/AlarmWebClient.ts"},
        {"module": "alarm-web-sdk", "path": "frontend/alarm-web/src/ts/http/Req.ts"},
    ]
    sc = {"interfaces": [{"name": "IAlarmWebConfig", "module": "alarm-web-sdk"}]}
    out = finish_plan_deterministic(plan, file_plan, None, "t", shared_contract=sc)
    assert out.get("symbols_domiciled"), "异栈模块也要安置，绝不饿死"
    f = next(f for s in plan.subtasks for f in s.scope.create_files
             if "IAlarmWebConfig" in f)
    assert f.startswith("frontend/alarm-web/src/ts/"), f   # 落自身 TS 真目录
    assert f.endswith("IAlarmWebConfig.ts"), f             # per-module 扩展名 .ts
    assert not f.startswith("alarm-web-sdk/"), f           # 非幻影逻辑名
    assert "/java/" not in f, f                            # 绝不落 java 目录


def test_r62_task2_legacy_tpl_dir_excludes_resource_dirs():
    """Task2：file_plan 缺席的老路径下，mod_dirs/tpl_dir 只统计【源码】目录——MyBatis
    `.xml`(src/main/resources/mapper) 即便数量占优也绝不把 tpl_dir 拽进资源目录，令
    .java 落到 classpath 不可见的 resources/mapper（不编译）。"""
    plan = TaskPlan(
        task_id="t-62task2", subtasks=[
            # 资源 .xml（多）vs 源码 .java（少）——旧实现 tpl_dir 会取众数 mapper 目录
            _st("st-1", create=[
                "biz/src/main/resources/mapper/AMapper.xml",
                "biz/src/main/resources/mapper/BMapper.xml",
                "biz/src/main/resources/mapper/CMapper.xml"]),
            _st("st-2", create=["biz/src/main/java/com/x/Svc.java"])],
        parallel_groups=[["st-1", "st-2"]])
    sc = {"interfaces": [{"name": "NewFeatureService", "module": "newmod"}]}
    out = finish_plan_deterministic(plan, [], None, "t", shared_contract=sc)
    assert out.get("symbols_domiciled"), "无主硬符号必须被安置"
    f = next(f for s in plan.subtasks for f in s.scope.create_files
             if f.endswith("NewFeatureService.java"))
    assert "/resources/mapper/" not in f, f      # 绝不落资源目录
    assert "/resources/" not in f, f
    assert "/src/main/java/" in f, f             # 落真源根


def test_r62_task1_empty_file_plan_keeps_legacy_heuristic():
    """回归护栏：file_plan 为空（老流程）时，安置退回旧启发式、行为不变。"""
    plan = TaskPlan(
        task_id="t-62task1c", subtasks=[
            _st("st-1", create=["ruoyi-alarm/src/main/java/com/ruoyi/alarm/A.java"]),
            _st("st-2", create=["ruoyi-alarm/src/main/java/com/ruoyi/alarm/B.java"])],
        parallel_groups=[["st-1", "st-2"]])
    sc = {"interfaces": [{"name": "RobotQueryService", "module": "alarm-robot"}]}
    out = finish_plan_deterministic(plan, [], None, "t", shared_contract=sc)
    assert out.get("symbols_domiciled"), "空 file_plan 走旧启发式，行为不变"
    assert any(f.endswith("RobotQueryService.java")
               for s in plan.subtasks for f in s.scope.create_files)


# ── ⑤ Task3（round62 治本）：R57-6 收权后剪除空写 scope 死子任务 ──

def _st_scope(sid, scope, depends_on=None):
    return SubTask(id=sid, description=f"task {sid}",
                   difficulty=SubTaskDifficulty.MEDIUM, scope=scope,
                   depends_on=depends_on or [])


def test_r62_task3_prune_empty_scope_no_dependents():
    """R57-6 收权留下的空写 scope 死子任务（无人依赖）→ 确定性剪除，不派 worker 空转。"""
    from swarm.brain.contract_utils import prune_empty_scope_subtasks
    plan = TaskPlan(
        task_id="t-62t3", subtasks=[
            _st_scope("st-1", FileScope(create_files=["a/A.java"])),
            _st_scope("st-dead", FileScope()),          # 空写 scope、非 allow_any = 死
            _st_scope("st-2", FileScope(writable=["b/B.java"])),
        ],
        parallel_groups=[["st-1", "st-dead", "st-2"]])
    pruned = prune_empty_scope_subtasks(plan)
    assert pruned == ["st-dead"]
    assert {s.id for s in plan.subtasks} == {"st-1", "st-2"}
    # parallel_groups 同步清理，无空组、无残留 id
    assert all("st-dead" not in g for g in plan.parallel_groups)


def test_r62_task3_keep_dead_task_with_dependents():
    """空写 scope 死任务【被别人依赖】→ 保留 + 告警，绝不静默重映射把工作丢了。"""
    from swarm.brain.contract_utils import prune_empty_scope_subtasks
    plan = TaskPlan(
        task_id="t-62t3b", subtasks=[
            _st_scope("st-dead", FileScope()),
            _st_scope("st-child", FileScope(create_files=["c/C.java"]),
                      depends_on=["st-dead"]),
        ],
        parallel_groups=[["st-dead", "st-child"]])
    pruned = prune_empty_scope_subtasks(plan)
    assert pruned == []                                  # 被依赖 → 不剪
    assert {s.id for s in plan.subtasks} == {"st-dead", "st-child"}


def test_r62_task3_allow_any_empty_scope_not_pruned():
    """allow_any 的空 scope 不是死任务（worker 有全域写权）→ 绝不剪。"""
    from swarm.brain.contract_utils import prune_empty_scope_subtasks
    plan = TaskPlan(
        task_id="t-62t3c", subtasks=[
            _st_scope("st-free", FileScope(allow_any=True)),
            _st_scope("st-1", FileScope(create_files=["a/A.java"])),
        ],
        parallel_groups=[["st-free", "st-1"]])
    assert prune_empty_scope_subtasks(plan) == []
    assert {s.id for s in plan.subtasks} == {"st-free", "st-1"}


def test_r62_task3_delete_only_scope_not_pruned():
    """仅 delete_files 也是有效写目标（删除是真工作）→ 绝不剪。"""
    from swarm.brain.contract_utils import prune_empty_scope_subtasks
    plan = TaskPlan(
        task_id="t-62t3d", subtasks=[
            _st_scope("st-del", FileScope(delete_files=["old/Legacy.java"])),
            _st_scope("st-1", FileScope(create_files=["a/A.java"])),
        ],
        parallel_groups=[["st-del", "st-1"]])
    assert prune_empty_scope_subtasks(plan) == []
    assert {s.id for s in plan.subtasks} == {"st-del", "st-1"}


def test_r62_task3_prune_cleans_stale_depends_on_ref():
    """剪掉死任务时，其它子任务里指向它的 depends_on 引用也一并清（无悬空边）。"""
    from swarm.brain.contract_utils import prune_empty_scope_subtasks
    # st-dead 无人依赖被剪；st-2 曾错误地 depends_on 一个【也被剪】的死任务是不可能的
    # （被依赖就不剪）——此处验证：另一个死任务 st-dead2 无依赖被剪后，其自身 depends_on
    # 被清、且不给别人留悬空。
    plan = TaskPlan(
        task_id="t-62t3e", subtasks=[
            _st_scope("st-1", FileScope(create_files=["a/A.java"])),
            _st_scope("st-dead", FileScope(), depends_on=["st-1"]),
        ],
        parallel_groups=[["st-1", "st-dead"]])
    pruned = prune_empty_scope_subtasks(plan)
    assert pruned == ["st-dead"]
    assert {s.id for s in plan.subtasks} == {"st-1"}
    assert all("st-dead" not in (s.depends_on or []) for s in plan.subtasks)


def test_r62_task3_audit_intent_not_pruned():
    """对抗复核 Finding1（CRITICAL）：intent=AUDIT 不产 diff、走审计专路，空写 scope 是
    预期形态——绝不当死任务剪，否则静默删真安全审计工作。"""
    from swarm.brain.contract_utils import prune_empty_scope_subtasks
    from swarm.types import TaskIntent
    audit = SubTask(id="st-audit", description="安全审计", intent=TaskIntent.AUDIT,
                    difficulty=SubTaskDifficulty.MEDIUM, scope=FileScope())
    plan = TaskPlan(
        task_id="t-62t3audit",
        subtasks=[audit, _st_scope("st-1", FileScope(create_files=["a/A.java"]))],
        parallel_groups=[["st-audit", "st-1"]])
    assert prune_empty_scope_subtasks(plan) == []       # AUDIT 不剪
    assert {s.id for s in plan.subtasks} == {"st-audit", "st-1"}


def test_r62_task3_chained_dead_tasks_pruned_to_fixpoint():
    """对抗复核 Finding3：链尾死任务剪掉后，上游死任务变得无人依赖 → 不动点再剪，
    绝不留链上的死任务继续空转（单趟会漏）。"""
    from swarm.brain.contract_utils import prune_empty_scope_subtasks
    # st-A 死（无依赖）；st-B 死且 depends_on st-A；二者都无外部依赖者
    plan = TaskPlan(
        task_id="t-62t3chain", subtasks=[
            _st_scope("st-real", FileScope(create_files=["a/A.java"])),
            _st_scope("st-A", FileScope()),
            _st_scope("st-B", FileScope(), depends_on=["st-A"])],
        parallel_groups=[["st-real", "st-A", "st-B"]])
    pruned = prune_empty_scope_subtasks(plan)
    assert set(pruned) == {"st-A", "st-B"}              # 两个都剪（不动点）
    assert {s.id for s in plan.subtasks} == {"st-real"}


def test_r62_task3_never_prune_to_empty_plan():
    """对抗复核 + 回归（degraded 兜底）：全部子任务都是空写 scope（LLM 双超时兜底的
    单个占位 st-1）→ 绝不剪成空计划，保留交下游 plan_generation_failed fail-fast。"""
    from swarm.brain.contract_utils import prune_empty_scope_subtasks
    plan = TaskPlan(
        task_id="t-62t3empty",
        subtasks=[_st_scope("st-1", FileScope(writable=[], readable=[]))],
        parallel_groups=[["st-1"]])
    assert prune_empty_scope_subtasks(plan) == []
    assert [s.id for s in plan.subtasks] == ["st-1"]    # 计划恒 ≥1 子任务


# ── ⑦ Task5（round62 治本）：readable 幻影包路径归一到 producer 真实落点 ──

def test_r62_task5_phantom_readable_aligned_to_unique_producer():
    """consumer readable 引幻影子路径 `.../sdk/model/X.java`，唯一 producer 建在
    `.../sdk/X.java` → 归一到真实落点（provenance 一致，import 编得过）。"""
    from swarm.brain.contract_utils import align_readable_to_producer
    plan = TaskPlan(
        task_id="t-62t5", subtasks=[
            _st_scope("st-producer", FileScope(
                create_files=["m/src/main/java/com/x/sdk/AlarmRequest.java"])),
            _st_scope("st-consumer", FileScope(
                create_files=["m/src/main/java/com/x/svc/Use.java"],
                readable=["m/src/main/java/com/x/sdk/model/AlarmRequest.java"],
                upstream_artifacts=[
                    "m/src/main/java/com/x/sdk/model/AlarmRequest.java"]))],
        parallel_groups=[["st-producer", "st-consumer"]])
    out = align_readable_to_producer(plan)
    assert out["aligned"] == 2   # readable + upstream_artifacts 各 1
    consumer = next(s for s in plan.subtasks if s.id == "st-consumer")
    assert consumer.scope.readable == ["m/src/main/java/com/x/sdk/AlarmRequest.java"]
    assert consumer.scope.upstream_artifacts == [
        "m/src/main/java/com/x/sdk/AlarmRequest.java"]


def test_r62_task5_ambiguous_basename_not_touched():
    """歧义 basename（多 producer，如每模块 pom.xml/同名类）绝不归一——避免误改。"""
    from swarm.brain.contract_utils import align_readable_to_producer
    plan = TaskPlan(
        task_id="t-62t5b", subtasks=[
            _st_scope("st-a", FileScope(create_files=["a/Foo.java", "a/pom.xml"])),
            _st_scope("st-b", FileScope(create_files=["b/Foo.java", "b/pom.xml"])),
            _st_scope("st-c", FileScope(
                create_files=["c/Use.java"],
                readable=["wrong/Foo.java", "ruoyi-common/pom.xml"]))],
        parallel_groups=[["st-a", "st-b", "st-c"]])
    out = align_readable_to_producer(plan)
    assert out["aligned"] == 0   # Foo.java 多 producer、pom.xml 非 code → 不动
    stc = next(s for s in plan.subtasks if s.id == "st-c")
    assert stc.scope.readable == ["wrong/Foo.java", "ruoyi-common/pom.xml"]


def test_r62_task5_no_producer_readable_untouched():
    """无 producer 的 readable（baseline 只读文件）不是幻影 → 不动。"""
    from swarm.brain.contract_utils import align_readable_to_producer
    plan = TaskPlan(
        task_id="t-62t5c", subtasks=[
            _st_scope("st-1", FileScope(
                create_files=["m/A.java"],
                readable=["baseline/only/Existing.java"]))],
        parallel_groups=[["st-1"]])
    assert align_readable_to_producer(plan)["aligned"] == 0
    assert next(s for s in plan.subtasks
                if s.id == "st-1").scope.readable == ["baseline/only/Existing.java"]


def test_r62_task5_real_baseline_same_basename_not_redirected(tmp_path):
    """★对抗复核 CRITICAL #1★：consumer 读的是【磁盘真实 baseline 文件】，只是恰好与
    某唯一 producer 同 basename → 绝不重定向（同名纯巧合，不是幻影）。"""
    from swarm.brain.contract_utils import align_readable_to_producer
    # 真实 baseline 文件
    real = tmp_path / "ruoyi-common/src/main/java/com/ruoyi/common/core/domain"
    real.mkdir(parents=True)
    (real / "Result.java").write_text("class Result{}", "utf-8")
    plan = TaskPlan(
        task_id="t-62t5d", subtasks=[
            _st_scope("st-prod", FileScope(
                create_files=["modA/newfeature/Result.java"])),   # 唯一 Result.java
            _st_scope("st-cons", FileScope(
                create_files=["modB/Use.java"],
                readable=[
                    "ruoyi-common/src/main/java/com/ruoyi/common/core/domain/"
                    "Result.java"]))],                            # 真 baseline，同名巧合
        parallel_groups=[["st-prod", "st-cons"]])
    out = align_readable_to_producer(plan, str(tmp_path))
    assert out["aligned"] == 0, "磁盘真实 baseline 文件绝不被同名 producer 重定向"
    cons = next(s for s in plan.subtasks if s.id == "st-cons")
    assert cons.scope.readable[0].endswith(
        "ruoyi/common/core/domain/Result.java")


def test_r62_task5_real_planned_path_not_redirected():
    """真实计划落点（别的子任务在建/改的文件）不是幻影 → 同名也不重定向。"""
    from swarm.brain.contract_utils import align_readable_to_producer
    plan = TaskPlan(
        task_id="t-62t5e", subtasks=[
            _st_scope("st-prod", FileScope(create_files=["modA/deep/Result.java"])),
            _st_scope("st-other", FileScope(create_files=["modB/other/Result.java"])),
            _st_scope("st-cons", FileScope(
                create_files=["modC/Use.java"],
                readable=["modB/other/Result.java"]))],   # 真实计划落点（st-other 建）
        parallel_groups=[["st-prod", "st-other", "st-cons"]])
    # Result.java 有两个 producer（modA, modB）→ 非唯一 → 本就不动；此处额外证真实落点保护
    out = align_readable_to_producer(plan)
    assert out["aligned"] == 0
    cons = next(s for s in plan.subtasks if s.id == "st-cons")
    assert cons.scope.readable == ["modB/other/Result.java"]
