"""round67g create-vs-base【modify-only 安全子集】红灯先行：SysUser 型确定性归位。

死型（task=b3659ca9 FAILED@PLAN，2026-07-23）：LLM 想【改】base 既有实体（SysUser），却把落点写成
【幻觉异路径】——file_plan `action=modify ruoyi-system/domain/SysUser.java`（真身在 base
`ruoyi-common/.../entity/SysUser.java`），PLAN batch 把它落进某子任务 create_files → G1 ③f
`_created_class_shadows_base`（读子任务 create_files）判 create-vs-base shadow REJECT → 重试从恒定
file_plan 重拆原样重犯 → 层② 熔断，产不出合法 plan。

治本 = `deconflict_create_vs_base_modify_shadow`（子任务 scope 层，接 resolve_plan_conflicts）：
★唯一安全信号 = file_plan 该 simple-name 的 action=modify（且无 create）★——LLM 自认改既有类，凭此
归位 create_files 幻觉异路径 → base 真身（改 writable/modify）。SysMenu 型（file_plan action=create，
LLM 认作新类）无信号 → 不碰，交 G1 ③f REJECT（fail-closed，绝不复活 round67c 已删的裸 basename 挑边）。
"""
import swarm.brain.contract_utils as cu
from swarm.brain.contract_utils import deconflict_create_vs_base_modify_shadow
from swarm.brain.plan_validator import _created_class_shadows_base
from swarm.brain.planning_nodes import _contract_base_entity_hints
from swarm.types import FileScope, SubTask, TaskHarness, TaskPlan

_BASE_SYSUSER = "ruoyi-common/src/main/java/com/ruoyi/common/core/domain/entity/SysUser.java"
_SHADOW_SYSUSER = "ruoyi-system/src/main/java/com/ruoyi/system/domain/SysUser.java"
_BASE_SYSMENU = "ruoyi-common/src/main/java/com/ruoyi/common/core/domain/entity/SysMenu.java"
_SHADOW_SYSMENU = "ruoyi-system/src/main/java/com/ruoyi/system/domain/SysMenu.java"


def _st(sid, *, create=None, writable=None, depends=None, lang="java"):
    return SubTask(
        id=sid, description="d",
        scope=FileScope(writable=writable or [], create_files=create or [], readable=[]),
        harness=TaskHarness(language=lang), depends_on=depends or [],
    )


def _fp(path, action):
    return {"path": path, "action": action, "module": ""}


def _tree(monkeypatch, *paths):
    monkeypatch.setattr(cu, "_base_tree_listing", lambda *a, **k: list(paths))


# ── 主治：SysUser 型（file_plan modify）归位 ─────────────────────────────────

def test_sysuser_modify_shadow_relocated(monkeypatch):
    """file_plan action=modify + base 唯一同名异路径 → 子任务 create_files 幻觉路径归位到 base 真身
    (改 writable)，且 G1 ③f 不再报 SysUser。"""
    _tree(monkeypatch, _BASE_SYSUSER)
    plan = TaskPlan(subtasks=[_st("st-16-1", create=[_SHADOW_SYSUSER])])
    fp = [_fp(_SHADOW_SYSUSER, "modify")]

    assert "sysuser.java" in _created_class_shadows_base(plan, "/x", "HEAD"), "红灯前提不成立"
    n = deconflict_create_vs_base_modify_shadow(plan, fp, project_path="/x", base_ref="HEAD")
    assert n == 1
    sc = plan.subtasks[0].scope
    assert _SHADOW_SYSUSER not in (sc.create_files or []), "幻觉 create 未剥"
    assert _BASE_SYSUSER in (sc.writable or []), "未归位到 base 真身 writable"
    assert "sysuser.java" not in _created_class_shadows_base(plan, "/x", "HEAD"), "③f 仍报 SysUser"


def test_sysmenu_create_left_for_g1_3f(monkeypatch):
    """SysMenu 型：file_plan action=create（LLM 认作新类）→ 无 modify 信号 → 本 pass 不碰，
    仍交 G1 ③f REJECT（绝不复活裸 basename 挑边腐化）。"""
    _tree(monkeypatch, _BASE_SYSMENU)
    plan = TaskPlan(subtasks=[_st("st-16-1", create=[_SHADOW_SYSMENU])])
    fp = [_fp(_SHADOW_SYSMENU, "create")]
    n = deconflict_create_vs_base_modify_shadow(plan, fp, project_path="/x", base_ref="HEAD")
    assert n == 0, "create 型被静默归位（腐化风险）"
    assert _SHADOW_SYSMENU in (plan.subtasks[0].scope.create_files or [])
    assert "sysmenu.java" in _created_class_shadows_base(plan, "/x", "HEAD"), "SysMenu 该留 ③f"


def test_mixed_only_modify_relocated(monkeypatch):
    """SysUser(modify)+SysMenu(create) 同子任务共存 → 只归位 SysUser，SysMenu 原样留 ③f。"""
    _tree(monkeypatch, _BASE_SYSUSER, _BASE_SYSMENU)
    plan = TaskPlan(subtasks=[_st("st-16-1", create=[_SHADOW_SYSUSER, _SHADOW_SYSMENU])])
    fp = [_fp(_SHADOW_SYSUSER, "modify"), _fp(_SHADOW_SYSMENU, "create")]
    n = deconflict_create_vs_base_modify_shadow(plan, fp, project_path="/x", base_ref="HEAD")
    assert n == 1
    left = set(_created_class_shadows_base(plan, "/x", "HEAD"))
    assert left == {"sysmenu.java"}, f"应只剩 SysMenu，实得 {sorted(left)}"


# ── fail-closed 边界 ────────────────────────────────────────────────────────

def test_no_base_tree_greenfield_skip(monkeypatch):
    """无 base 树（greenfield/非 git）→ 整体跳过，绝不误伤纯新建。"""
    monkeypatch.setattr(cu, "_base_tree_listing", lambda *a, **k: None)
    plan = TaskPlan(subtasks=[_st("st-1", create=[_SHADOW_SYSUSER])])
    fp = [_fp(_SHADOW_SYSUSER, "modify")]
    assert deconflict_create_vs_base_modify_shadow(plan, fp, project_path="/x", base_ref="HEAD") == 0
    assert _SHADOW_SYSUSER in (plan.subtasks[0].scope.create_files or [])


def test_no_file_plan_skip(monkeypatch):
    """无 file_plan → 无 modify 信号源 → 跳过（不凭 base 同名裸挑边）。"""
    _tree(monkeypatch, _BASE_SYSUSER)
    plan = TaskPlan(subtasks=[_st("st-1", create=[_SHADOW_SYSUSER])])
    assert deconflict_create_vs_base_modify_shadow(plan, None, project_path="/x", base_ref="HEAD") == 0
    assert deconflict_create_vs_base_modify_shadow(plan, [], project_path="/x", base_ref="HEAD") == 0


def test_base_multiple_same_name_fail_closed(monkeypatch):
    """base 同名【非唯一】命中（≥2 处）→ 命名空间容忍/歧义，绝不挑边。"""
    other = "ruoyi-quartz/src/main/java/com/ruoyi/quartz/domain/SysUser.java"
    _tree(monkeypatch, _BASE_SYSUSER, other)
    plan = TaskPlan(subtasks=[_st("st-1", create=[_SHADOW_SYSUSER])])
    fp = [_fp(_SHADOW_SYSUSER, "modify")]
    assert deconflict_create_vs_base_modify_shadow(plan, fp, project_path="/x", base_ref="HEAD") == 0


def test_path_not_modify_anchored_skip(monkeypatch):
    """★对抗双复核 HIGH/PLAUSIBLE-1 整改（路径粒度）★：被归位的 create 落点【本身】不是 file_plan
    modify（file_plan 对【另一个不同类/路径】的同名 modify）→ 绝不误归并合法新类（复活 round67c 腐化）。
    场景：某子任务新建 moduleA 的 Constants（脚手架/符号安置注入，路径不在 file_plan），file_plan 只对
    base 真身 moduleB Constants 声明 modify（正常改动）→ moduleA 新类必须留 ③f，绝不静默蒸发。"""
    base_const = "ruoyi-common/src/main/java/com/ruoyi/common/utils/Constants.java"
    new_const = "ruoyi-alarm/src/main/java/com/ruoyi/alarm/util/Constants.java"   # 合法新类，非 file_plan
    _tree(monkeypatch, base_const)
    plan = TaskPlan(subtasks=[_st("st-5", create=[new_const])])
    fp = [_fp(base_const, "modify")]   # file_plan modify 指向 base 真身本身，非 new_const
    assert deconflict_create_vs_base_modify_shadow(plan, fp, project_path="/x", base_ref="HEAD") == 0, \
        "非路径锚定的同名 modify 误授权归并合法新类（round67c 腐化复活）"
    assert new_const in (plan.subtasks[0].scope.create_files or []), "合法新类被静默剥除"


def test_same_path_create_and_modify_ambiguous_fail_closed(monkeypatch):
    """同一路径被 file_plan 同时声明 create 与 modify（意图歧义）→ fail-closed 不动。"""
    _tree(monkeypatch, _BASE_SYSUSER)
    plan = TaskPlan(subtasks=[_st("st-1", create=[_SHADOW_SYSUSER])])
    fp = [_fp(_SHADOW_SYSUSER, "modify"), _fp(_SHADOW_SYSUSER, "create")]
    assert deconflict_create_vs_base_modify_shadow(plan, fp, project_path="/x", base_ref="HEAD") == 0


def test_exact_base_path_left_for_rule0_reverse(monkeypatch):
    """create_files 精确 ∈ base 树 → 归 R67-T8 规则0逆向降级 modify，本 pass 不重复处理。"""
    _tree(monkeypatch, _BASE_SYSUSER)
    plan = TaskPlan(subtasks=[_st("st-1", create=[_BASE_SYSUSER])])   # 落点=base 真身本身
    fp = [_fp(_BASE_SYSUSER, "modify")]
    assert deconflict_create_vs_base_modify_shadow(plan, fp, project_path="/x", base_ref="HEAD") == 0
    assert _BASE_SYSUSER in (plan.subtasks[0].scope.create_files or [])


def test_test_layout_path_exempt(monkeypatch):
    """test 布局路径豁免（每模块独立 test classpath，同 ③f/层③）。"""
    base_test = "ruoyi-common/src/test/java/com/ruoyi/common/FooTest.java"
    shadow_test = "ruoyi-system/src/test/java/com/ruoyi/system/FooTest.java"
    _tree(monkeypatch, base_test)
    plan = TaskPlan(subtasks=[_st("st-1", create=[shadow_test])])
    fp = [_fp(shadow_test, "modify")]
    assert deconflict_create_vs_base_modify_shadow(plan, fp, project_path="/x", base_ref="HEAD") == 0


def test_stack_neutral_non_jvm_untouched(monkeypatch):
    """非 JVM 类路径（Go/Py flat）→ classpath_fqn_key=None → 天然豁免，绝不误伤。"""
    base_go = "internal/user/user.go"
    shadow_go = "cmd/user/user.go"
    _tree(monkeypatch, base_go)
    plan = TaskPlan(subtasks=[_st("st-1", create=[shadow_go], lang="go")])
    fp = [_fp(shadow_go, "modify")]
    assert deconflict_create_vs_base_modify_shadow(plan, fp, project_path="/x", base_ref="HEAD") == 0
    assert shadow_go in (plan.subtasks[0].scope.create_files or [])


def test_wired_into_resolve_plan_conflicts(monkeypatch):
    """接线核验：resolve_plan_conflicts 传 file_plan → cvb pass 生效（唯一事实源）。"""
    _tree(monkeypatch, _BASE_SYSUSER)
    plan = TaskPlan(subtasks=[_st("st-16-1", create=[_SHADOW_SYSUSER])])
    fp = [_fp(_SHADOW_SYSUSER, "modify")]
    counts = cu.resolve_plan_conflicts(plan, project_path="/x", base_ref="HEAD", file_plan=fp)
    assert counts.get("cvb_modify_shadow_relocated", 0) == 1
    assert _BASE_SYSUSER in (plan.subtasks[0].scope.writable or [])


# ── 信号3（SysMenu 型·契约 defined_in 权威·治法A，用户拍板确定性执行层）─────────

def _plan_with_contract(subtasks, contract):
    p = TaskPlan(subtasks=subtasks)
    p.shared_contract = contract
    return p


def test_signal3_contract_defined_in_base_relocates(monkeypatch):
    """SysMenu 死型（file_plan create）：契约【显式声明】defined_in=base 真身 → 信号3 归位、clear ③f。"""
    _tree(monkeypatch, _BASE_SYSMENU)
    plan = _plan_with_contract(
        [_st("st-16-1", create=[_SHADOW_SYSMENU])],
        {"dtos": [{"name": "SysMenu", "defined_in": _BASE_SYSMENU}]})
    fp = [_fp(_SHADOW_SYSMENU, "create")]
    assert "sysmenu.java" in _created_class_shadows_base(plan, "/x", "HEAD"), "红灯前提"
    n = deconflict_create_vs_base_modify_shadow(plan, fp, project_path="/x", base_ref="HEAD")
    assert n == 1
    assert _SHADOW_SYSMENU not in (plan.subtasks[0].scope.create_files or [])
    assert _BASE_SYSMENU in (plan.subtasks[0].scope.writable or [])
    assert "sysmenu.java" not in _created_class_shadows_base(plan, "/x", "HEAD")


def test_signal3_contract_defined_in_new_path_no_relocate(monkeypatch):
    """★不复活 round67c★：契约声明 defined_in=【新落点】(非 base 实存)→ 合法新类 → 不归位、留 ③f。
    这正是 SecurityConfig 型合法新类的安全路径：contract 认作新类→defined_in 指新路径→信号3 不触发。"""
    _tree(monkeypatch, _BASE_SYSMENU)
    plan = _plan_with_contract(
        [_st("st-16-1", create=[_SHADOW_SYSMENU])],
        {"dtos": [{"name": "SysMenu", "defined_in": _SHADOW_SYSMENU}]})  # 契约指新落点(shadow 自身)
    fp = [_fp(_SHADOW_SYSMENU, "create")]
    n = deconflict_create_vs_base_modify_shadow(plan, fp, project_path="/x", base_ref="HEAD")
    assert n == 0, "契约未把 defined_in 声明在 base 真身却被归位（腐化风险）"
    assert _SHADOW_SYSMENU in (plan.subtasks[0].scope.create_files or [])


def test_signal3_no_contract_declaration_left_for_g1_3f(monkeypatch):
    """契约【未声明】该类（现实 SysMenu 状况）→ 无信号3 → 留 ③f（诚实 FAILED@PLAN）。"""
    _tree(monkeypatch, _BASE_SYSMENU)
    plan = _plan_with_contract([_st("st-16-1", create=[_SHADOW_SYSMENU])], {})
    fp = [_fp(_SHADOW_SYSMENU, "create")]
    assert deconflict_create_vs_base_modify_shadow(plan, fp, project_path="/x", base_ref="HEAD") == 0
    assert "sysmenu.java" in _created_class_shadows_base(plan, "/x", "HEAD")


def test_signal3_generic_name_legit_new_safe(monkeypatch):
    """★round67c 血泪红线★：合法模块级新类 SecurityConfig@alarm，base 有单个同名 SecurityConfig@
    framework，契约声明 defined_in=alarm 新落点 → 信号3 不触发（无 file_plan modify、契约非 base 真身）
    → 合法新类存活留 ③f，绝不误并进 base（治法A 用契约显式权威而非结构猜测，不复活 signal2 被否决的腐化）。"""
    base_sc = "ruoyi-framework/src/main/java/com/ruoyi/framework/config/SecurityConfig.java"
    new_sc = "ruoyi-alarm/src/main/java/com/ruoyi/alarm/config/SecurityConfig.java"
    _tree(monkeypatch, base_sc)
    plan = _plan_with_contract(
        [_st("st-3", create=[new_sc])],
        {"dtos": [{"name": "SecurityConfig", "defined_in": new_sc}]})   # 契约认作新类→新落点
    fp = [_fp(new_sc, "create")]
    assert deconflict_create_vs_base_modify_shadow(plan, fp, project_path="/x", base_ref="HEAD") == 0
    assert new_sc in (plan.subtasks[0].scope.create_files or [])


# ── contract-side 既有实体提示（治法A·T4）──────────────────────────────────

def test_base_entity_hints_flags_collision(monkeypatch):
    """既有实体提示：本模块 file_plan create 撞 base 唯一同名 → 输出真身路径 + defined_in 引导。"""
    import swarm.brain.planning_nodes as pn
    monkeypatch.setattr(pn.cu if hasattr(pn, "cu") else cu, "_base_tree_listing",
                        lambda *a, **k: [_BASE_SYSMENU], raising=False)
    monkeypatch.setattr(cu, "_base_tree_listing", lambda *a, **k: [_BASE_SYSMENU])
    fp = [{"path": _SHADOW_SYSMENU, "action": "create", "module": "ruoyi-system"}]
    h = _contract_base_entity_hints(fp, "ruoyi-system", "/x", "HEAD")
    assert "SysMenu.java" in h and _BASE_SYSMENU in h and "defined_in" in h


def test_base_entity_hints_empty_greenfield(monkeypatch):
    """无 base 树（greenfield/非 git）→ 空串（prompt 无此段，零副作用）。"""
    monkeypatch.setattr(cu, "_base_tree_listing", lambda *a, **k: None)
    fp = [{"path": _SHADOW_SYSMENU, "action": "create", "module": "ruoyi-system"}]
    assert _contract_base_entity_hints(fp, "ruoyi-system", "/x", "HEAD") == ""


def test_base_entity_hints_no_collision_empty(monkeypatch):
    """本模块 create 无 base 同名（纯新类）→ 空串（不误导 contract）。"""
    monkeypatch.setattr(cu, "_base_tree_listing", lambda *a, **k: [_BASE_SYSMENU])
    fp = [{"path": "ruoyi-alarm/src/main/java/com/ruoyi/alarm/AlarmTask.java",
           "action": "create", "module": "ruoyi-alarm"}]
    assert _contract_base_entity_hints(fp, "ruoyi-alarm", "/x", "HEAD") == ""


def test_signal3_ambiguous_contract_fail_closed(monkeypatch):
    """★对抗复核 hunter PLAUSIBLE-HIGH 整改★：契约给同 simple-name 【两个不同 defined_in】(一条 base
    真身 + 一条新落点=同名异 owner 歧义)→ 集合非 {base真身} → fail-closed 不归位，绝不把职责不同的合法
    同名新类误并进 base（对齐层③ ambiguous_base 守卫）。"""
    _tree(monkeypatch, _BASE_SYSMENU)
    new_menu = "ruoyi-alarm/src/main/java/com/ruoyi/alarm/domain/SysMenu.java"
    plan = _plan_with_contract(
        [_st("st-9", create=[new_menu])],                 # 建的是【新落点】的合法新类
        {"dtos": [{"name": "SysMenu", "defined_in": _BASE_SYSMENU},   # 一条指 base 真身
                  {"name": "SysMenu", "defined_in": new_menu}]})       # 一条指新落点(歧义)
    fp = [_fp(new_menu, "create")]
    n = deconflict_create_vs_base_modify_shadow(plan, fp, project_path="/x", base_ref="HEAD")
    assert n == 0, "契约同名异 owner 歧义却归位（并列副本漏移植 ambiguous 守卫，腐化风险）"
    assert new_menu in (plan.subtasks[0].scope.create_files or [])


# ── round67h：归位必须与 file_plan 同串归一（否则 R40-1 判孤儿→attach 复活→成环）──────
# 死型（task=a259e59b FAILED@PLAN，2026-07-23）：T2 只改子任务 create_files/writable 归位到 base，
# 却【没同步 file_plan 里那条 shadow 路径条目】→ VALIDATE R40-1 file_plan 归属闸看 file_plan 仍指
# shadow 路径【无 owner】→ 打回 PLAN → finish 孤儿挂靠把 shadow 重挂回新子任务 create（复活）→ CVB
# 再归位 → 无限环耗尽 MAX retry。根因=违背仓内既定不变量（plan_finisher:777/contract_utils:2324：
# "rename create_files 必须与 file_plan 同串归一"）——T2 是家族里唯一没遵守的 relocation pass。
from swarm.brain.plan_validator import validate_file_plan_ownership  # noqa: E402


def _fp_paths(fp):
    """归一 file_plan 各条目路径（dict/str 两形态）为集合，供断言。"""
    out = set()
    for e in fp:
        p = e.get("path") if isinstance(e, dict) else e
        out.add(cu._norm_scope_path(str(p)))
    return out


def test_r67h_sysuser_relocation_syncs_file_plan_dict(monkeypatch):
    """★主治★：SysUser 归位后 file_plan 里 shadow 条目(dict)的 path 也 relocate 到 base 真身、
    action→modify——否则 R40-1 判 shadow 孤儿成环。"""
    _tree(monkeypatch, _BASE_SYSUSER)
    plan = TaskPlan(subtasks=[_st("st-16-1", create=[_SHADOW_SYSUSER])])
    fp = [_fp(_SHADOW_SYSUSER, "modify")]
    n = deconflict_create_vs_base_modify_shadow(plan, fp, project_path="/x", base_ref="HEAD")
    assert n == 1
    paths = _fp_paths(fp)
    assert cu._norm_scope_path(_SHADOW_SYSUSER) not in paths, "file_plan 仍留 shadow 路径（R40-1 判孤儿成环）"
    assert cu._norm_scope_path(_BASE_SYSUSER) in paths, "file_plan 未联动到 base 真身"
    moved = [e for e in fp if cu._norm_scope_path(str(e["path"])) == cu._norm_scope_path(_BASE_SYSUSER)]
    assert moved and moved[0].get("action") == "modify", "归位后 file_plan 条目 action 应为 modify"


def test_r67h_r40_ownership_passes_after_relocation(monkeypatch):
    """★端到端复现死型★：归位后 validate_file_plan_ownership(R40-1) 必须【过】——旧码（不同步
    file_plan）会在此判 shadow 孤儿打回 → 成环。"""
    _tree(monkeypatch, _BASE_SYSUSER)
    other = "ruoyi-alarm/src/main/java/com/ruoyi/alarm/AlarmTask.java"
    plan = TaskPlan(subtasks=[
        _st("st-16-1", create=[_SHADOW_SYSUSER]),
        _st("st-2", create=[other]),
    ])
    fp = [_fp(_SHADOW_SYSUSER, "modify"), _fp(other, "create")]
    # 归位前：shadow 有 owner(st-16-1 create) → R40-1 过（红灯前提=归位后才炸）
    assert validate_file_plan_ownership(plan, fp).valid, "红灯前提不成立"
    deconflict_create_vs_base_modify_shadow(plan, fp, project_path="/x", base_ref="HEAD")
    res = validate_file_plan_ownership(plan, fp)
    assert res.valid, f"归位后 R40-1 仍判孤儿（成环死型）: {res.issues}"


def test_r67h_file_plan_sync_str_form(monkeypatch):
    """★"只改 dict 漏 str" 防线（contract_utils:2324 hunter HIGH 同型）★：file_plan bare-str 条目
    也要 relocate，否则 str 形态 shadow 漏改仍判孤儿。"""
    _tree(monkeypatch, _BASE_SYSUSER)
    plan = TaskPlan(subtasks=[_st("st-16-1", create=[_SHADOW_SYSUSER])])
    # str 形态无 action → 无 file_plan modify 信号，但契约权威(信号3)可触发归位
    plan.shared_contract = {"dtos": [{"name": "SysUser", "defined_in": _BASE_SYSUSER}]}
    fp = [_SHADOW_SYSUSER]  # bare-str
    n = deconflict_create_vs_base_modify_shadow(plan, fp, project_path="/x", base_ref="HEAD")
    assert n == 1
    assert fp == [_BASE_SYSUSER], f"str 形态 file_plan 未 relocate: {fp}"


def test_r67h_file_plan_dedup_base_already_present(monkeypatch):
    """base 真身已在 file_plan（合法 modify）→ 归位时删 shadow 条目防重，绝不产生重复 base 条目。"""
    _tree(monkeypatch, _BASE_SYSUSER)
    plan = TaskPlan(subtasks=[_st("st-16-1", create=[_SHADOW_SYSUSER])])
    fp = [_fp(_SHADOW_SYSUSER, "modify"), _fp(_BASE_SYSUSER, "modify")]
    deconflict_create_vs_base_modify_shadow(plan, fp, project_path="/x", base_ref="HEAD")
    base_entries = [e for e in fp if cu._norm_scope_path(str(e["path"])) == cu._norm_scope_path(_BASE_SYSUSER)]
    assert len(base_entries) == 1, f"base 条目重复（未防重）: {len(base_entries)}"
    assert cu._norm_scope_path(_SHADOW_SYSUSER) not in _fp_paths(fp), "shadow 条目未删"


def test_r67h_file_plan_mutated_in_place(monkeypatch):
    """★持久化前提★：归位就地 mutate 传入的同一 list 对象（=state['tech_design_file_plan']）→
    改动持久化进 state，同一 VALIDATE 内 R40-1 生效、重试不复现 shadow。"""
    _tree(monkeypatch, _BASE_SYSUSER)
    plan = TaskPlan(subtasks=[_st("st-16-1", create=[_SHADOW_SYSUSER])])
    fp = [_fp(_SHADOW_SYSUSER, "modify")]
    fp_id = id(fp)
    deconflict_create_vs_base_modify_shadow(plan, fp, project_path="/x", base_ref="HEAD")
    assert id(fp) == fp_id, "file_plan 被 rebind 非就地 mutate（改动丢失不持久化）"
    assert cu._norm_scope_path(_BASE_SYSUSER) in _fp_paths(fp)


def test_r67h_no_relocation_leaves_file_plan_untouched(monkeypatch):
    """fail-closed 不归位时（SysMenu create 无信号）→ file_plan 一字不动（无副作用）。"""
    _tree(monkeypatch, _BASE_SYSMENU)
    plan = TaskPlan(subtasks=[_st("st-16-1", create=[_SHADOW_SYSMENU])])
    fp = [_fp(_SHADOW_SYSMENU, "create")]
    before = [dict(e) for e in fp]
    n = deconflict_create_vs_base_modify_shadow(plan, fp, project_path="/x", base_ref="HEAD")
    assert n == 0
    assert fp == before, "未归位却动了 file_plan"


def test_r67h_relocation_via_resolve_syncs_file_plan(monkeypatch):
    """接线核验：经 resolve_plan_conflicts 归位后 file_plan 同步到 base（唯一事实源全链路）。"""
    _tree(monkeypatch, _BASE_SYSUSER)
    plan = TaskPlan(subtasks=[_st("st-16-1", create=[_SHADOW_SYSUSER])])
    fp = [_fp(_SHADOW_SYSUSER, "modify")]
    cu.resolve_plan_conflicts(plan, project_path="/x", base_ref="HEAD", file_plan=fp)
    assert cu._norm_scope_path(_BASE_SYSUSER) in _fp_paths(fp)
    assert cu._norm_scope_path(_SHADOW_SYSUSER) not in _fp_paths(fp)


def test_r67h_dedup_drop_merges_responsibility(monkeypatch):
    """★对抗复核 hunter MEDIUM 整改★：base 真身已在 file_plan、shadow 归位撞它 → 删 shadow 防重时
    把 shadow 独有 responsibility 并入保留条目（绝不静默丢需求文本）。"""
    _tree(monkeypatch, _BASE_SYSUSER)
    plan = TaskPlan(subtasks=[_st("st-16-1", create=[_SHADOW_SYSUSER])])
    fp = [{"path": _BASE_SYSUSER, "action": "modify", "module": "", "responsibility": "改动A"},
          {"path": _SHADOW_SYSUSER, "action": "modify", "module": "", "responsibility": "改动B-2FA字段"}]
    deconflict_create_vs_base_modify_shadow(plan, fp, project_path="/x", base_ref="HEAD")
    base_entries = [e for e in fp if cu._norm_scope_path(str(e["path"])) == cu._norm_scope_path(_BASE_SYSUSER)]
    assert len(base_entries) == 1, "base 条目应唯一（防重）"
    resp = base_entries[0].get("responsibility", "")
    assert "改动A" in resp and "改动B-2FA字段" in resp, f"shadow responsibility 被静默丢弃: {resp!r}"


_SHADOW_SYSUSER2 = "ruoyi-quartz/src/main/java/com/ruoyi/quartz/domain/SysUser.java"


def test_r67h_relocated_entry_drops_stale_module(monkeypatch):
    """★对抗复核 reviewer round2 HIGH 整改★：归位 dict 条目丢 stale module——shadow 的 module 是
    幻觉错模块，path 改到 base 真身（物理属别模块）后保留旧 module 会令 _file_plan_module_paths 按错
    模块分桶→G1 coherence 误判 module/path 错配（换 R40-1→G1 又 churn）。"""
    _tree(monkeypatch, _BASE_SYSUSER)
    plan = TaskPlan(subtasks=[_st("st-16-1", create=[_SHADOW_SYSUSER])])
    fp = [{"path": _SHADOW_SYSUSER, "action": "modify", "module": "ruoyi-system", "responsibility": "2FA"}]
    deconflict_create_vs_base_modify_shadow(plan, fp, project_path="/x", base_ref="HEAD")
    base_e = [e for e in fp if cu._norm_scope_path(str(e["path"])) == cu._norm_scope_path(_BASE_SYSUSER)][0]
    assert base_e.get("module", "") != "ruoyi-system", "归位条目保留了 shadow 的 stale module（G1 误判 churn）"
    buckets = cu._file_plan_module_paths(fp)
    assert cu._norm_scope_path(_BASE_SYSUSER) not in buckets.get("ruoyi-system", []), \
        "base 路径被错分桶进 shadow 模块 ruoyi-system"


def test_r67h_two_shadows_same_base_preserve_both_responsibilities(monkeypatch):
    """★对抗复核 hunter round2 MEDIUM-A 整改★：两 shadow 归位到同一【新建】base（base 本不在
    file_plan）→ 两条 responsibility 都保留（pass2 顺序无关合并，绝不丢第二条）。"""
    _tree(monkeypatch, _BASE_SYSUSER)
    plan = TaskPlan(subtasks=[_st("st-1", create=[_SHADOW_SYSUSER]),
                              _st("st-2", create=[_SHADOW_SYSUSER2])])
    fp = [{"path": _SHADOW_SYSUSER, "action": "modify", "module": "ruoyi-system", "responsibility": "改动A加头像"},
          {"path": _SHADOW_SYSUSER2, "action": "modify", "module": "ruoyi-quartz", "responsibility": "改动B加手机"}]
    deconflict_create_vs_base_modify_shadow(plan, fp, project_path="/x", base_ref="HEAD")
    base_es = [e for e in fp
               if cu._norm_scope_path(str(e["path"] if isinstance(e, dict) else e)) == cu._norm_scope_path(_BASE_SYSUSER)]
    assert len(base_es) == 1, f"base 条目应唯一（防重），实得 {len(base_es)}"
    resp = base_es[0].get("responsibility", "")
    assert "改动A加头像" in resp and "改动B加手机" in resp, f"第二条 shadow responsibility 丢失: {resp!r}"


def test_r67h_bare_str_base_upgraded_to_preserve_responsibility(monkeypatch):
    """★对抗复核 hunter round2 MEDIUM-B 整改★：base 真身在 file_plan 是 bare-str 形态 + shadow dict 有
    responsibility → 删 shadow 时把 bare-str base 升级为 dict 承接 responsibility（绝不丢需求）。"""
    _tree(monkeypatch, _BASE_SYSUSER)
    plan = TaskPlan(subtasks=[_st("st-16-1", create=[_SHADOW_SYSUSER])])
    fp = [_BASE_SYSUSER,  # bare-str 预存 base
          {"path": _SHADOW_SYSUSER, "action": "modify", "module": "ruoyi-system", "responsibility": "改动加2FA"}]
    deconflict_create_vs_base_modify_shadow(plan, fp, project_path="/x", base_ref="HEAD")
    base_es = [e for e in fp
               if cu._norm_scope_path(str(e["path"] if isinstance(e, dict) else e)) == cu._norm_scope_path(_BASE_SYSUSER)]
    assert len(base_es) == 1
    e0 = base_es[0]
    assert isinstance(e0, dict) and "改动加2FA" in e0.get("responsibility", ""), \
        f"bare-str base 未升级承接 responsibility（静默丢需求）: {e0!r}"


def test_r67h_substring_responsibility_not_dropped(monkeypatch):
    """★hunter round3 MEDIUM 整改★：第二条 shadow 的 responsibility 恰是已合并文本的【子串】
    （"2FA" ⊂ "加字段2FA用于登录校验"）→ 精确分段去重不误吞（旧子串判定会静默丢独立需求）。"""
    _tree(monkeypatch, _BASE_SYSUSER)
    plan = TaskPlan(subtasks=[_st("st-1", create=[_SHADOW_SYSUSER]),
                              _st("st-2", create=[_SHADOW_SYSUSER2])])
    fp = [{"path": _SHADOW_SYSUSER, "action": "modify", "module": "ruoyi-system",
           "responsibility": "加字段2FA用于登录校验"},
          {"path": _SHADOW_SYSUSER2, "action": "modify", "module": "ruoyi-quartz", "responsibility": "2FA"}]
    deconflict_create_vs_base_modify_shadow(plan, fp, project_path="/x", base_ref="HEAD")
    base = [e for e in fp
            if cu._norm_scope_path(str(e["path"] if isinstance(e, dict) else e)) == cu._norm_scope_path(_BASE_SYSUSER)][0]
    segs = base.get("responsibility", "").split(" / ")
    assert "加字段2FA用于登录校验" in segs and "2FA" in segs, \
        f"子串 responsibility 被静默吞（旧子串去重复发）: {base.get('responsibility')!r}"


# ── round67h：节点级返回契约（对抗双复核 CRITICAL/HIGH：就地 mutate 必须随【返回键】回写 state）──
# reviewer+hunter 独立同判：elaborate()/revision() 就地 mutate file_plan 但从不把 tech_design_file_plan
# 放进返回 dict → LangGraph checkpoint 恢复语义下变异丢失 → VALIDATE R40-1 重读旧 shadow → 成环复现。
# 既有测试全是函数级直调（绕过节点返回契约）=测试盲区。以下断言【节点返回 dict】本身。

async def test_r67h_elaborate_returns_synced_file_plan(monkeypatch):
    """★复核 CRITICAL 整改★：elaborate() 触发 CVB 归位后返回 dict 必须含 tech_design_file_plan
    （shadow→base）——否则就地变异 checkpoint 恢复丢失、R40-1 成环。断言【返回契约】非仅局部 list。"""
    import swarm.brain.planning_nodes as pn
    monkeypatch.setattr(cu, "_base_tree_listing", lambda *a, **k: [_BASE_SYSUSER])
    monkeypatch.setattr(pn, "_persist_planning_artifacts", lambda *a, **k: None)
    plan = TaskPlan(subtasks=[_st("st-16-1", create=[_SHADOW_SYSUSER])])
    state = {"plan": plan, "tech_design_file_plan": [_fp(_SHADOW_SYSUSER, "modify")],
             "project_id": "", "base_commit": "HEAD"}
    out = await pn.elaborate(state)
    assert "tech_design_file_plan" in out, "elaborate 未回写 tech_design_file_plan（就地变异丢失→成环复现）"
    paths = _fp_paths(out["tech_design_file_plan"])
    assert cu._norm_scope_path(_BASE_SYSUSER) in paths
    assert cu._norm_scope_path(_SHADOW_SYSUSER) not in paths


async def test_r67h_elaborate_no_relocation_no_file_plan_key(monkeypatch):
    """未触发 CVB 归位 → elaborate 不返回 tech_design_file_plan（不无谓覆盖恒定通道，避免副作用）。"""
    import swarm.brain.planning_nodes as pn
    _new = "ruoyi-alarm/src/main/java/com/ruoyi/alarm/AlarmTask.java"
    monkeypatch.setattr(cu, "_base_tree_listing", lambda *a, **k: [_BASE_SYSMENU])  # 无 SysUser/AlarmTask 同名
    monkeypatch.setattr(pn, "_persist_planning_artifacts", lambda *a, **k: None)
    plan = TaskPlan(subtasks=[_st("st-1", create=[_new])])
    state = {"plan": plan, "tech_design_file_plan": [_fp(_new, "create")],
             "project_id": "", "base_commit": "HEAD"}
    out = await pn.elaborate(state)
    assert "tech_design_file_plan" not in out, "无归位却回写 tech_design_file_plan（无谓覆盖恒定通道）"


async def test_r67h_revision_returns_synced_file_plan(monkeypatch):
    """★复核 HIGH 整改★：revision() 触发 CVB 归位后返回 dict 同样含 tech_design_file_plan
    （人工 REVISE 路径同 elaborate 缺陷，同款回写）。LLM 调用 monkeypatch 抛错走默认子任务兜底。"""
    import swarm.brain.nodes as nodes_mod

    def _boom(*a, **k):
        raise RuntimeError("no-llm-in-test")
    monkeypatch.setattr(nodes_mod, "_get_brain_llm", _boom, raising=False)
    monkeypatch.setattr(nodes_mod, "_get_project_path", lambda *a, **k: "/x", raising=False)
    monkeypatch.setattr(cu, "_base_tree_listing", lambda *a, **k: [_BASE_SYSUSER])
    plan = TaskPlan(subtasks=[_st("st-16-1", create=[_SHADOW_SYSUSER])])
    state = {"plan": plan, "revision_feedback": "fix", "merged_diff": "", "task_description": "t",
             "tech_design_file_plan": [_fp(_SHADOW_SYSUSER, "modify")],
             "project_id": "", "base_commit": "HEAD"}
    out = await nodes_mod.revision(state)
    assert "tech_design_file_plan" in out, "revision 未回写 tech_design_file_plan（就地变异丢失）"
    assert cu._norm_scope_path(_BASE_SYSUSER) in _fp_paths(out["tech_design_file_plan"])
