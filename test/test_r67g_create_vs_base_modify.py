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
