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
