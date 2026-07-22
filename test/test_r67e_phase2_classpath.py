"""round67e Phase 2（类治）：契约类名 file-path 分叉【确定性对齐】。

死型（tier2_only 类名分叉）：契约 interfaces/types/dtos 条目 name=X（ScheduleStrategyService），
owner 子任务 create_files + file_plan 同漂到装饰变体 V（AlarmScheduleStrategyService.java，
basename_symbol_match tier2）→ 契约落单 → 消费方按契约 import X、只建了 V → L2 cannot find symbol X。
pin 现状：tier2 故意不钉 → tier2_only 甩执行期符号接地兜不住。

治本（v1 greenfield-only，fail-closed 重）：finish_plan_deterministic 早位新增对齐 pass，磁盘判方向
（git-pin base：非棕地才动），把 owner create_files + file_plan + desc/AC/verify 三面 + 契约 defined_in
对齐到契约名 X。names 转 tier0 后 elaborate 的 pin/wire 原样接管连消费方。

红灯先行：修复前 detect_contract_classname_divergences / reconcile_contract_symbol_paths 不存在 →
ImportError 红。设计明细 deep_read_findings/18。
"""
from __future__ import annotations

import os
import tempfile

import swarm.brain.contract_utils as _cu
from swarm.brain.contract_utils import reconcile_contract_symbol_paths
from swarm.brain.plan_finisher import finish_plan_deterministic
from swarm.brain.symbol_provenance import detect_contract_classname_divergences
from swarm.types import FileScope, SubTask, TaskHarness, TaskPlan

# 真实空目录 + 假 .git：过 reconcile 的 isdir + git 闸（gate 1b：非 git fail-closed）。方向控制
# （T4/T5 棕地/greenfield）全用 monkeypatch _exists_in_repo，与本目录内容无关。
_PROJ = tempfile.mkdtemp(prefix="r67e_p2_")
os.makedirs(os.path.join(_PROJ, ".git"), exist_ok=True)
# 非 git 目录：gate 1b fail-closed 用
_PROJ_NOGIT = tempfile.mkdtemp(prefix="r67e_p2_nogit_")

_DIR = "ruoyi-alarm/src/main/java/com/ruoyi/alarm/schedule/service/"
_X = "ScheduleStrategyService"          # 契约权威名
_V = "AlarmScheduleStrategyService"     # owner 漂移装饰变体（tier2）
_VPATH = _DIR + _V + ".java"
_TPATH = _DIR + _X + ".java"


def _owner(*, create=None, desc="", ac=None, vc=None, sid="st-owner", writable=None):
    st = SubTask(
        id=sid, description=desc,
        scope=FileScope(create_files=list(create or [_VPATH]),
                        writable=list(writable or [])),
        harness=TaskHarness(language="java", verify_commands=list(vc or [])))
    if ac is not None:
        st.acceptance_criteria = list(ac)
    return st


def _contract(name=_X, defined_in="", module="ruoyi-alarm"):
    # defined_in 为空模拟"未钉"（tier2_only：pin 因装饰前缀没钉上）
    return {"interfaces": [{"name": name, "module": module, "defined_in": defined_in,
                            "signature": f"List<X> query{name}(X q)"}]}


def _plan(owner, contract):
    return TaskPlan(subtasks=[owner], shared_contract=contract)


def _all_greenfield(*a, **k):
    """monkeypatch _exists_in_repo：任何路径都不在 base（纯 greenfield）。"""
    return False


# ── T14 detect 结构化：tier2_only 类名分叉被识别 ───────────────────────────────
def test_t14_detect_returns_structured_divergence():
    owner = _owner(desc=f"实现 {_V} 接口")
    plan = _plan(owner, _contract())
    divs = detect_contract_classname_divergences(plan)
    assert len(divs) == 1, divs
    d = divs[0]
    assert d["symbol"] == _X
    assert d["owner"] is owner
    assert d["v_path"].endswith(_V + ".java")
    assert d["v_stem"] == _V


# ── T1 greenfield 基本对齐：create_files V→X ──────────────────────────────────
def test_t1_greenfield_create_files_aligned(monkeypatch):
    monkeypatch.setattr(_cu, "_exists_in_repo", _all_greenfield)
    owner = _owner(desc=f"实现 {_V} 接口")
    plan = _plan(owner, _contract())
    fp = [{"path": _VPATH, "module": "ruoyi-alarm"}]
    summary = reconcile_contract_symbol_paths(plan, fp, project_path=_PROJ)
    assert "st-owner" in summary, summary
    assert _TPATH in owner.scope.create_files
    assert _VPATH not in owner.scope.create_files


# ── T2 ★三面文本同步 desc+AC+verify★ ──────────────────────────────────────────
def test_t2_three_faces_text_synced(monkeypatch):
    monkeypatch.setattr(_cu, "_exists_in_repo", _all_greenfield)
    owner = _owner(desc=f"实现 {_V} 接口的增删改查",
                   ac=[f"验收：{_V} 返回非空"],
                   vc=[f"grep -q 'class {_V}' Impl.java"])
    plan = _plan(owner, _contract())
    reconcile_contract_symbol_paths(plan, [{"path": _VPATH}], project_path=_PROJ)
    assert _V not in owner.description
    assert _X in owner.description
    assert not any(_V in a for a in owner.acceptance_criteria), "AC 未同步=半修复"
    assert not any(_V in v for v in owner.harness.verify_commands), "verify 未同步=半修复"


# ── T3 file_plan 归一（否则孤儿/改名复活）─────────────────────────────────────
def test_t3_file_plan_path_renamed(monkeypatch):
    monkeypatch.setattr(_cu, "_exists_in_repo", _all_greenfield)
    owner = _owner(desc=f"实现 {_V}")
    plan = _plan(owner, _contract())
    fp = [{"path": _VPATH, "module": "ruoyi-alarm"}]
    reconcile_contract_symbol_paths(plan, fp, project_path=_PROJ)
    paths = [e["path"] for e in fp]
    assert _TPATH in paths, "file_plan 未归一 → R40-1 判孤儿+改名复活"
    assert _VPATH not in paths


# ── T4 base 有 X.java（owner 应 MODIFY 非 create）→ fail-closed ────────────────
def test_t4_base_has_contract_name_fail_closed(monkeypatch):
    # 目标 T=X.java 已在 base → 对齐会令 owner "create" 既有 base 文件 → 高 blast，punt
    monkeypatch.setattr(_cu, "_exists_in_repo",
                        lambda pp, rel, cache, base_ref=None: rel == _TPATH)
    owner = _owner(desc=f"实现 {_V}")
    plan = _plan(owner, _contract())
    assert reconcile_contract_symbol_paths(plan, [{"path": _VPATH}], project_path=_PROJ) == {}
    assert _VPATH in owner.scope.create_files, "棕地 X 存在应 fail-closed 不动"


# ── T5 base 有 V.java（棕地既有类，改契约+消费方=高 blast）→ fail-closed ────────
def test_t5_base_has_owner_name_fail_closed(monkeypatch):
    monkeypatch.setattr(_cu, "_exists_in_repo",
                        lambda pp, rel, cache, base_ref=None: rel == _VPATH)
    owner = _owner(desc=f"实现 {_V}")
    plan = _plan(owner, _contract())
    assert reconcile_contract_symbol_paths(plan, [{"path": _VPATH}], project_path=_PROJ) == {}
    assert _VPATH in owner.scope.create_files


# ── T6 歧义：两 owner 都 create V.java → fail-closed（多 owner）─────────────────
def test_t6_ambiguous_multi_owner_fail_closed(monkeypatch):
    monkeypatch.setattr(_cu, "_exists_in_repo", _all_greenfield)
    o1 = _owner(desc=f"实现 {_V}", sid="st-1")
    o2 = _owner(desc=f"也建 {_V}", sid="st-2")
    plan = TaskPlan(subtasks=[o1, o2], shared_contract=_contract())
    # 两个子任务 create 同一 V → detect 阶段多 owner 即歧义
    assert reconcile_contract_symbol_paths(plan, [{"path": _VPATH}], project_path=_PROJ) == {}


# ── T7 目标 T 撞兄弟别包 create → fail-closed（G1 basename 防线）────────────────
def test_t7_target_collides_sibling_create_fail_closed(monkeypatch):
    monkeypatch.setattr(_cu, "_exists_in_repo", _all_greenfield)
    owner = _owner(desc=f"实现 {_V}", sid="st-owner")
    other_dir = "ruoyi-alarm/src/main/java/com/ruoyi/alarm/other/"
    sibling = _owner(create=[other_dir + _X + ".java"], desc="别包已有同名", sid="st-sib")
    plan = TaskPlan(subtasks=[owner, sibling], shared_contract=_contract())
    assert reconcile_contract_symbol_paths(plan, [{"path": _VPATH}], project_path=_PROJ) == {}
    assert _VPATH in owner.scope.create_files, "撞兄弟别包同名应 fail-closed"


# ── T8 幂等 ───────────────────────────────────────────────────────────────────
def test_t8_idempotent(monkeypatch):
    monkeypatch.setattr(_cu, "_exists_in_repo", _all_greenfield)
    owner = _owner(desc=f"实现 {_V}")
    plan = _plan(owner, _contract())
    fp = [{"path": _VPATH}]
    reconcile_contract_symbol_paths(plan, fp, project_path=_PROJ)
    assert reconcile_contract_symbol_paths(plan, fp, project_path=_PROJ) == {}, "非幂等"


# ── T9 词边界：VPaged 不误替 ──────────────────────────────────────────────────
def test_t9_word_boundary_no_substring_clobber(monkeypatch):
    monkeypatch.setattr(_cu, "_exists_in_repo", _all_greenfield)
    owner = _owner(desc=f"实现 {_V} 与 {_V}Paged 两个类")
    plan = _plan(owner, _contract())
    reconcile_contract_symbol_paths(plan, [{"path": _VPATH}], project_path=_PROJ)
    assert f"{_V}Paged" in owner.description, "词边界失守，子串被误替"


# ── T10 已一致（owner 建 X.java=tier0）→ no-op ────────────────────────────────
def test_t10_already_consistent_noop(monkeypatch):
    monkeypatch.setattr(_cu, "_exists_in_repo", _all_greenfield)
    owner = _owner(create=[_TPATH], desc=f"实现 {_X}")
    plan = _plan(owner, _contract(defined_in=_TPATH))
    assert detect_contract_classname_divergences(plan) == []
    assert reconcile_contract_symbol_paths(plan, [{"path": _TPATH}], project_path=_PROJ) == {}


# ── T11 无 project_path → fail-closed（无法判非棕地，绝不改文件）─────────────────
def test_t11_no_project_path_fail_closed():
    owner = _owner(desc=f"实现 {_V}")
    plan = _plan(owner, _contract())
    assert reconcile_contract_symbol_paths(plan, [{"path": _VPATH}], project_path=None) == {}
    assert _VPATH in owner.scope.create_files


# ── T12 畸形 owner 隔离（per-owner try/except 不拖垮兄弟）──────────────────────
def test_t12_malformed_owner_isolated(monkeypatch):
    monkeypatch.setattr(_cu, "_exists_in_repo", _all_greenfield)
    _DIR2 = "ruoyi-alarm/src/main/java/com/ruoyi/alarm/notify/service/"
    _V2, _X2 = "AlarmNotifyChannelService", "NotifyChannelService"
    good = _owner(desc=f"实现 {_V}", sid="st-good")
    bad = _owner(create=[_DIR2 + _V2 + ".java"], desc=f"实现 {_V2}", sid="st-bad")
    bad.harness.__dict__["verify_commands"] = [123]   # 触发 _sub(123) TypeError
    plan = TaskPlan(subtasks=[good, bad], shared_contract={"interfaces": [
        {"name": _X, "module": "ruoyi-alarm", "defined_in": "", "signature": "x"},
        {"name": _X2, "module": "ruoyi-alarm", "defined_in": "", "signature": "y"},
    ]})
    fp = [{"path": _VPATH}, {"path": _DIR2 + _V2 + ".java"}]
    summary = reconcile_contract_symbol_paths(plan, fp, project_path=_PROJ)
    assert "st-good" in summary
    assert _TPATH in good.scope.create_files
    # 畸形 owner 未半变异（create 仍原 V2；因整体在提交前算好三面，异常回滚）
    assert (_DIR2 + _V2 + ".java") in bad.scope.create_files


# ── T13 接线 finish_plan_deterministic + 失败写 degraded 键 ────────────────────
def test_t13_wired_in_finish(monkeypatch):
    monkeypatch.setattr(_cu, "_exists_in_repo", _all_greenfield)
    owner = _owner(desc=f"实现 {_V}")
    plan = _plan(owner, _contract())
    fp = [{"path": _VPATH, "module": "ruoyi-alarm"}]
    out = finish_plan_deterministic(plan, fp, project_path=_PROJ,
                                    shared_contract=plan.shared_contract)
    assert _TPATH in owner.scope.create_files, "finish 未跑 Phase2 对齐"
    assert out.get("contract_symbol_paths_reconciled"), "接线摘要缺失"


def test_t13b_finish_failure_sets_degraded(monkeypatch):
    def _boom(*a, **k):
        raise RuntimeError("boom")
    monkeypatch.setattr(_cu, "reconcile_contract_symbol_paths", _boom)
    owner = _owner(desc=f"实现 {_V}")
    plan = _plan(owner, _contract())
    out = finish_plan_deterministic(plan, [{"path": _VPATH}], project_path=_PROJ,
                                    shared_contract=plan.shared_contract)
    assert out.get("contract_symbol_paths_reconcile_failed") is True


# ── T15 契约 defined_in 钉到目标 T ────────────────────────────────────────────
def test_t15_contract_defined_in_pinned(monkeypatch):
    monkeypatch.setattr(_cu, "_exists_in_repo", _all_greenfield)
    owner = _owner(desc=f"实现 {_V}")
    plan = _plan(owner, _contract())
    reconcile_contract_symbol_paths(plan, [{"path": _VPATH}], project_path=_PROJ)
    assert plan.shared_contract["interfaces"][0]["defined_in"] == _TPATH


# ── T16 全 scope 路径重写（readable/upstream 的 V 也归一，无悬空）───────────────
def test_t16_all_scope_paths_rewritten(monkeypatch):
    monkeypatch.setattr(_cu, "_exists_in_repo", _all_greenfield)
    owner = _owner(desc=f"实现 {_V}")
    consumer = SubTask(
        id="st-cons", description=f"消费 {_X}",
        scope=FileScope(create_files=["ruoyi-alarm/.../Consumer.java"],
                        readable=[_VPATH], upstream_artifacts=[_VPATH]),
        harness=TaskHarness(language="java"))
    plan = TaskPlan(subtasks=[owner, consumer], shared_contract=_contract())
    reconcile_contract_symbol_paths(plan, [{"path": _VPATH}], project_path=_PROJ)
    assert _VPATH not in consumer.scope.readable, "消费方 readable 残留悬空 V 路径"
    assert _TPATH in consumer.scope.readable
    assert _TPATH in consumer.scope.upstream_artifacts


# ══ 对抗双复核整改红灯先行 ═══════════════════════════════════════════════════

# ── [reviewer HIGH] CJK 紧贴标识符：\b 零匹配半修复；ASCII lookaround 修 ──────────
def test_h1_cjk_abutting_text_aligned(monkeypatch):
    monkeypatch.setattr(_cu, "_exists_in_repo", _all_greenfield)
    # 中文无分隔惯例：标识符紧贴汉字，\b 不成边界 → 旧代码三面文本零替换=半修复
    owner = _owner(desc=f"实现{_V}接口的增删改查",          # 无空格
                   ac=[f"验收{_V}返回非空"],
                   vc=[f"grep '{_V}' Impl.java"])
    plan = _plan(owner, _contract())
    reconcile_contract_symbol_paths(plan, [{"path": _VPATH}], project_path=_PROJ)
    assert _V not in owner.description, "CJK 紧贴：desc 未替换=半修复（\\b 隐患）"
    assert _X in owner.description
    assert not any(_V in a for a in owner.acceptance_criteria), "CJK 紧贴：AC 半修复"
    assert not any(_V in v for v in owner.harness.verify_commands), "CJK 紧贴：verify 半修复"


# ── [hunter HIGH] file_plan bare-str 条目也要归一（否则 R40-1 孤儿复活）───────────
def test_h2_bare_string_file_plan_renamed(monkeypatch):
    monkeypatch.setattr(_cu, "_exists_in_repo", _all_greenfield)
    owner = _owner(desc=f"实现 {_V}")
    plan = _plan(owner, _contract())
    fp = [_VPATH]                                    # bare str（混合形态，pipeline 视为正常）
    reconcile_contract_symbol_paths(plan, fp, project_path=_PROJ)
    assert _TPATH in fp, "bare-str file_plan 未归一 → R40-1 孤儿复活"
    assert _VPATH not in fp


# ── [both MEDIUM] 多 div 同调用内都愈，撞名集就地更新不互扰 ─────────────────────
def test_m_two_divs_both_heal(monkeypatch):
    monkeypatch.setattr(_cu, "_exists_in_repo", _all_greenfield)
    _DIR2 = "ruoyi-alarm/src/main/java/com/ruoyi/alarm/notify/service/"
    _V2, _X2 = "AlarmNotifyChannelService", "NotifyChannelService"
    o1 = _owner(desc=f"实现 {_V}", sid="st-1")
    o2 = _owner(create=[_DIR2 + _V2 + ".java"], desc=f"实现 {_V2}", sid="st-2")
    plan = TaskPlan(subtasks=[o1, o2], shared_contract={"interfaces": [
        {"name": _X, "module": "ruoyi-alarm", "defined_in": "", "signature": "x"},
        {"name": _X2, "module": "ruoyi-alarm", "defined_in": "", "signature": "y"},
    ]})
    fp = [{"path": _VPATH}, {"path": _DIR2 + _V2 + ".java"}]
    summary = reconcile_contract_symbol_paths(plan, fp, project_path=_PROJ)
    assert "st-1" in summary and "st-2" in summary, summary
    assert _TPATH in o1.scope.create_files
    assert (_DIR2 + _X2 + ".java") in o2.scope.create_files


# ── [hunter LOW] 非 git 目录：无 base 权威 → fail-closed（高 blast 改文件不可信）───
def test_l1_non_git_project_fail_closed(monkeypatch):
    monkeypatch.setattr(_cu, "_exists_in_repo", _all_greenfield)
    owner = _owner(desc=f"实现 {_V}")
    plan = _plan(owner, _contract())
    assert reconcile_contract_symbol_paths(
        plan, [{"path": _VPATH}], project_path=_PROJ_NOGIT) == {}
    assert _VPATH in owner.scope.create_files, "非 git 应 fail-closed 不改文件"


# ── [reviewer LOW] delete_files 也随路径重写 ──────────────────────────────────
def test_l2_delete_files_renamed(monkeypatch):
    monkeypatch.setattr(_cu, "_exists_in_repo", _all_greenfield)
    owner = _owner(desc=f"实现 {_V}")
    sibling = SubTask(id="st-sib", description="清理旧文件",
                      scope=FileScope(create_files=["ruoyi-alarm/.../Keep.java"],
                                      delete_files=[_VPATH]),
                      harness=TaskHarness(language="java"))
    plan = TaskPlan(subtasks=[owner, sibling], shared_contract=_contract())
    reconcile_contract_symbol_paths(plan, [{"path": _VPATH}], project_path=_PROJ)
    assert _VPATH not in sibling.scope.delete_files
    assert _TPATH in sibling.scope.delete_files


# ── [hunter CRITICAL] 未愈分叉（punt）经 finish 重跑 detect 上报 degraded，非静默 ──
def test_c_unhealed_surfaced_via_finish(monkeypatch):
    # 棕地 V 存在 → reconcile 闸 2 punt（未愈）→ finish 重跑 detect 仍见分叉 → out 上报
    monkeypatch.setattr(_cu, "_exists_in_repo",
                        lambda pp, rel, cache, base_ref=None: rel == _VPATH)
    owner = _owner(desc=f"实现 {_V}")
    plan = _plan(owner, _contract())
    out = finish_plan_deterministic(plan, [{"path": _VPATH}], project_path=_PROJ,
                                    shared_contract=plan.shared_contract)
    assert not out.get("contract_symbol_paths_reconciled"), "棕地本应 punt"
    assert out.get("contract_symbol_paths_unhealed") == [_X], \
        "未愈分叉未上报 degraded=静默死 L2（hunter CRITICAL）"


# ── [round-2 hunter HIGH Finding A] 一文件 tier2 命中多契约名 → 歧义丢弃，无幻影 pin ──
def test_a_shared_source_file_multi_name_fail_closed(monkeypatch):
    monkeypatch.setattr(_cu, "_exists_in_repo", _all_greenfield)
    # AlarmScheduleStrategyService.java tier2 同时命中 ScheduleStrategyService 与 StrategyService
    owner = _owner(desc=f"实现 {_V}")
    plan = TaskPlan(subtasks=[owner], shared_contract={"interfaces": [
        {"name": _X, "module": "ruoyi-alarm", "defined_in": "", "signature": "x"},
        {"name": "StrategyService", "module": "ruoyi-alarm", "defined_in": "", "signature": "y"},
    ]})
    assert detect_contract_classname_divergences(plan) == [], "共享 v_path 歧义未丢弃"
    assert reconcile_contract_symbol_paths(plan, [{"path": _VPATH}], project_path=_PROJ) == {}
    assert _VPATH in owner.scope.create_files
    # 无幻影 pin：两契约条目 defined_in 均未被钉
    assert plan.shared_contract["interfaces"][0]["defined_in"] == ""
    assert plan.shared_contract["interfaces"][1]["defined_in"] == ""


def test_c2_healed_no_unhealed_signal(monkeypatch):
    # greenfield 全愈 → 无 unhealed 噪音（防误报）
    monkeypatch.setattr(_cu, "_exists_in_repo", _all_greenfield)
    owner = _owner(desc=f"实现 {_V}")
    plan = _plan(owner, _contract())
    out = finish_plan_deterministic(plan, [{"path": _VPATH}], project_path=_PROJ,
                                    shared_contract=plan.shared_contract)
    assert out.get("contract_symbol_paths_reconciled")
    assert not out.get("contract_symbol_paths_unhealed"), "全愈却报 unhealed=误报"
