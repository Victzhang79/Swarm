"""round36 治本批（执行/恢复层）——取证见 E2E_ROUND35_REGISTER.md round36 段 + 双复核整改。

P0：worker 编码时引用【全场无生产者的内部类型/包】(round36 实证 st-12-1 引 TwoFactorSetupVO
    →全计划无 owner→连坐炸 62/64)。治=区分"真死上游"(连坐放弃对) vs "自造无生产者引用"→后者
    从(源根+blocked 包+编译错误里的类名)推出待建类型文件，加进 create_files 让消费者本模块补建
    +重派(按 targeted_recovery_counts 熔断)。★复核整改：改 create_files(非 allow_any，后者非空声明
    scope 下拉不回)；混批真死上游同 return 照常连坐放弃(HIGH#1)；推不出待建文件则回落放弃不空烧★。
#9：L2/执行 replan 复用覆盖重试已耗尽的 plan_retry_count → 只剩 1 轮即耗尽。治=replan 重置预算。
"""
import asyncio
from unittest.mock import patch

from swarm.brain.nodes import handle_failure
from swarm.types import FileScope, SubTask, TaskPlan, WorkerOutput

_JAVA_BO = "[ERROR] cannot find symbol\n  symbol:   class TwoFactorSetupVO\n  location: var user\n"


def _st(sid, depends=None):
    return SubTask(id=sid, description="d", scope=FileScope(writable=[f"{sid}.java"]),
                   depends_on=depends or [])


def _st_java(sid, java_file, depends=None):
    return SubTask(id=sid, description="d", scope=FileScope(writable=[java_file]),
                   depends_on=depends or [])


def _wo_blocked(sid, pkgs, build_output=""):
    return WorkerOutput(
        subtask_id=sid, diff="", summary="", l1_passed=False,
        l1_details={"pipeline_blocked": "internal_pkg_not_built",
                    "blocked_on_packages": pkgs, "not_run_kind": "blocked",
                    "failure_class": "transient", "build_output": build_output},
        confidence="low")


def _run(state):
    return asyncio.run(handle_failure(state))


# ── P0：无生产者内部类型(worker 自造引用) → create_files 自愈，不连坐放弃 ──

def test_p0_no_producer_reference_selfheals_via_create_files():
    """自造引用无生产者→把派生的待建类型文件纳入 create_files(非 allow_any)+重派，不连坐放弃。"""
    jf = "ruoyi-system/src/main/java/com/ruoyi/system/service/impl/SysUser2FAServiceImpl.java"
    plan = TaskPlan(subtasks=[_st_java("st-12", jf), _st("st-99", depends=["st-12"])])
    state = {
        "failed_subtask_ids": ["st-12"],
        "subtask_results": {
            "st-12": _wo_blocked("st-12", ["com.ruoyi.system.domain.vo"], _JAVA_BO)},
        "dispatch_remaining": ["st-99"],
        "plan": plan,
    }
    with patch("swarm.brain.nodes.failure._blocked_pkg_unrecoverable", return_value=True):
        r = _run(state)
    assert r["failure_strategy"] == "retry_alternate", r.get("failure_strategy")
    assert not r.get("abandoned_subtask_ids"), "无生产者自造引用不该连坐放弃闭包"
    st12 = next(s for s in r["plan"].subtasks if s.id == "st-12")
    assert any("TwoFactorSetupVO.java" in f and "com/ruoyi/system/domain/vo" in f
               for f in st12.scope.create_files), st12.scope.create_files
    assert st12.scope.allow_any is False, "改用 create_files(可拉回)，不设 allow_any(非空 scope 拉不回)"
    assert st12.retry_guidance and "新建" in st12.retry_guidance
    assert r["targeted_recovery_counts"]["st-12"] == 1
    assert "st-12" in r["dispatch_remaining"]


def test_p0_selfheal_underivable_falls_back_to_abandon():
    """推不出待建文件(无源根标记/无类名)→ 不自愈、回落连坐放弃，绝不空烧自愈预算。"""
    plan = TaskPlan(subtasks=[_st("st-12"), _st("st-99", depends=["st-12"])])  # writable 无 src 标记
    state = {
        "failed_subtask_ids": ["st-12"],
        "subtask_results": {
            "st-12": _wo_blocked("st-12", ["com.x.vo"], "cannot find symbol class Foo")},
        "dispatch_remaining": [],
        "plan": plan,
    }
    with patch("swarm.brain.nodes.failure._blocked_pkg_unrecoverable", return_value=True):
        r = _run(state)
    assert r["failure_strategy"] == "abandon", r.get("failure_strategy")
    assert "st-12" in set(r["abandoned_subtask_ids"])


def test_p0_selfheal_budget_exhausted_falls_back_to_abandon():
    """自愈预算耗尽(targeted_recovery_counts 达上限)→ 回落连坐放弃(原行为)，不无限自愈。"""
    jf = "mod/src/main/java/com/x/service/Foo.java"
    plan = TaskPlan(subtasks=[_st_java("st-12", jf), _st("st-99", depends=["st-12"])])
    state = {
        "failed_subtask_ids": ["st-12"],
        "subtask_results": {"st-12": _wo_blocked("st-12", ["com.x.domain.vo"], _JAVA_BO)},
        "targeted_recovery_counts": {"st-12": 9},  # 远超 max_retries
        "dispatch_remaining": ["st-99"],
        "plan": plan,
    }
    with patch("swarm.brain.nodes.failure._blocked_pkg_unrecoverable", return_value=True):
        r = _run(state)
    assert r["failure_strategy"] == "abandon", r.get("failure_strategy")
    assert "st-12" in set(r["abandoned_subtask_ids"])


def test_p0_dead_upstream_still_abandons_not_selfheal():
    """真死上游(依赖已放弃的上游)→ 仍连坐放弃，绝不误走自愈。"""
    plan = TaskPlan(subtasks=[_st("st-5"), _st("st-12", depends=["st-5"])])
    state = {
        "failed_subtask_ids": ["st-12"],
        "subtask_results": {"st-12": _wo_blocked("st-12", ["com.x.domain.vo"], _JAVA_BO)},
        "abandoned_subtask_ids": ["st-5"],  # 上游已放弃 → dep_hit=True
        "dispatch_remaining": [],
        "plan": plan,
    }
    with patch("swarm.brain.nodes.failure._blocked_pkg_unrecoverable", return_value=True):
        r = _run(state)
    assert r["failure_strategy"] == "abandon", r.get("failure_strategy")
    st12 = next(s for s in plan.subtasks if s.id == "st-12")
    assert st12.scope.allow_any is False and not st12.scope.create_files, "真死上游不该自愈授权"


def test_p0_mixed_batch_abandons_dead_and_selfheals_reference():
    """复核 HIGH#1（混批）：同批含真死上游 + 自造引用 → 死上游【照常连坐放弃】，自造引用自愈重派，
    只重派已愈项。绝不因存在自愈项就把真死上游拖着不放弃。"""
    jf = "mod/src/main/java/com/x/service/Foo.java"
    plan = TaskPlan(subtasks=[
        _st("st-up"),                       # 已放弃上游
        _st("st-A", depends=["st-up"]),     # 依赖已放弃上游 → dep_hit → 放弃
        _st_java("st-B", jf),               # 自造引用 → 自愈
    ])
    state = {
        "failed_subtask_ids": ["st-A", "st-B"],
        "subtask_results": {
            "st-A": _wo_blocked("st-A", ["com.x.vo"]),
            "st-B": _wo_blocked("st-B", ["com.x.domain.vo"], _JAVA_BO)},
        "abandoned_subtask_ids": ["st-up"],
        "dispatch_remaining": [],
        "plan": plan,
    }
    with patch("swarm.brain.nodes.failure._blocked_pkg_unrecoverable", return_value=True):
        r = _run(state)
    assert r["failure_strategy"] == "retry_alternate", r.get("failure_strategy")
    assert "st-A" in set(r.get("abandoned_subtask_ids") or []), "真死上游同批仍连坐放弃(HIGH#1)"
    assert "st-B" in r["dispatch_remaining"], "自造引用项自愈重派"
    assert "st-A" not in r["dispatch_remaining"], "已放弃项不重派"
    st_b = next(s for s in r["plan"].subtasks if s.id == "st-B")
    assert any("TwoFactorSetupVO.java" in f for f in st_b.scope.create_files)


# ── #9：L2/执行 replan 重置 plan 校验重试预算 ──

def test_fix9_l2_replan_resets_plan_retry_count():
    """L2 触发的 replan 给全新 plan 校验重试预算(清零)，别继承覆盖重试已耗尽的额度。"""
    plan = TaskPlan(subtasks=[_st("st-1")])
    state = {
        "verification_failure": "l2",
        "replan_count": 0,
        "plan_retry_count": 2,  # 覆盖重试已耗到 2
        "failed_subtask_ids": [],
        "subtask_results": {"st-1": _wo_blocked("st-1", [])},
        "plan": plan,
    }
    r = _run(state)
    assert r["failure_strategy"] == "replan", r.get("failure_strategy")
    assert r["plan_retry_count"] == 0, "L2-replan 必须重置 plan 校验重试预算(否则几乎必死)"


# ── #8：replan 按 scope 文件身份保留已完成产出(id 被重编号也不白重做) ──

def test_fix8_replan_preserves_completed_by_scope_when_id_churns():
    """真同一工作(描述+scope 一致，只 id 被 merge 重编号)→认领旧产物，免白重做。"""
    from swarm.brain.nodes import _surgical_replan_reset
    old_plan = TaskPlan(subtasks=[
        SubTask(id="st-24", description="模板", scope=FileScope(create_files=["a/A.java"])),
        SubTask(id="st-43", description="SDK", scope=FileScope(create_files=["b/B.java"]))])
    new_plan = TaskPlan(subtasks=[  # replan 重编号 id，描述+scope 不变
        SubTask(id="st-1", description="模板", scope=FileScope(create_files=["a/A.java"])),
        SubTask(id="st-2", description="SDK", scope=FileScope(create_files=["b/B.java"]))])
    old_results = {
        "st-24": WorkerOutput(subtask_id="st-24", diff="d", summary="", l1_passed=True,
                              confidence="high"),
        "st-43": WorkerOutput(subtask_id="st-43", diff="d", summary="", l1_passed=True,
                              confidence="high")}
    out = _surgical_replan_reset(old_results, old_plan, new_plan)
    preserved = out["subtask_results"]
    assert "st-1" in preserved and "st-2" in preserved, list(preserved.keys())
    assert preserved["st-1"].subtask_id == "st-24", "旧产物认领到新 id"
    assert preserved["st-2"].subtask_id == "st-43"


def test_fix8_description_change_same_scope_not_preserved():
    """复核 HIGH：REVISE/replan 改同文件子任务语义(描述变=意图变)→绝不认领旧产物静默跳过改动。"""
    from swarm.brain.nodes import _surgical_replan_reset
    old_plan = TaskPlan(subtasks=[
        SubTask(id="st-5", description="实现校验 A", scope=FileScope(writable=["UserService.java"]))])
    new_plan = TaskPlan(subtasks=[  # 同文件，但描述=意图变了
        SubTask(id="st-1", description="实现校验 B", scope=FileScope(writable=["UserService.java"]))])
    old_results = {"st-5": WorkerOutput(subtask_id="st-5", diff="d", summary="",
                                        l1_passed=True, confidence="high")}
    out = _surgical_replan_reset(old_results, old_plan, new_plan)
    assert not out["subtask_results"], "描述变(意图变)绝不用旧产物跳过——须真跑新工作"


def test_fix8_ambiguous_scope_not_preserved():
    """scope-key 不唯一(两子任务同 scope，如共享聚合文件)→不认领(防碰撞误保)。"""
    from swarm.brain.nodes import _surgical_replan_reset
    old_plan = TaskPlan(subtasks=[
        SubTask(id="st-1", description="x", scope=FileScope(writable=["pom.xml"])),
        SubTask(id="st-2", description="y", scope=FileScope(writable=["pom.xml"]))])
    new_plan = TaskPlan(subtasks=[
        SubTask(id="st-9", description="x2", scope=FileScope(writable=["pom.xml"]))])
    old_results = {"st-1": WorkerOutput(subtask_id="st-1", diff="d", summary="",
                                        l1_passed=True, confidence="high")}
    out = _surgical_replan_reset(old_results, old_plan, new_plan)
    assert not out["subtask_results"], "scope 不唯一不得认领"


# ── #6：覆盖单调化——按 scope 身份并回上一轮合法 covers，防重拆丢覆盖(打地鼠→收敛) ──

def _sub(sid, files, covers):
    return SubTask(id=sid, description="d", scope=FileScope(create_files=files), covers=covers)


def test_fix6_covers_merged_monotonic_by_scope():
    """本轮重拆丢了 req-1(上一轮同 scope 子任务覆盖过)→按 scope 身份并回，覆盖只增不减。"""
    from swarm.brain.nodes import _merge_prior_covers_by_scope
    old_plan = TaskPlan(subtasks=[_sub("st-24", ["a/A.java"], ["req-1", "req-2"])])
    new_plan = TaskPlan(subtasks=[_sub("st-1", ["a/A.java"], ["req-2"])])  # 丢了 req-1
    n = _merge_prior_covers_by_scope(new_plan, old_plan, {"req-1", "req-2", "req-3"})
    assert n == 1
    assert set(new_plan.subtasks[0].covers) == {"req-1", "req-2"}, new_plan.subtasks[0].covers


def test_fix6_hallucinated_covers_not_merged():
    """只并 valid_req_ids 内的 covers——臆造/悬空 covers(req-BOGUS)绝不重引入。"""
    from swarm.brain.nodes import _merge_prior_covers_by_scope
    old_plan = TaskPlan(subtasks=[_sub("st-24", ["a/A.java"], ["req-1", "req-BOGUS"])])
    new_plan = TaskPlan(subtasks=[_sub("st-1", ["a/A.java"], [])])
    n = _merge_prior_covers_by_scope(new_plan, old_plan, {"req-1", "req-2"})
    assert n == 1 and set(new_plan.subtasks[0].covers) == {"req-1"}, "臆造不并回"


def test_fix6_ambiguous_scope_covers_not_merged():
    """scope 不唯一(共享聚合文件)→不并(防碰撞误并)。"""
    from swarm.brain.nodes import _merge_prior_covers_by_scope
    old_plan = TaskPlan(subtasks=[
        SubTask(id="st-A", description="d", scope=FileScope(writable=["pom.xml"]), covers=["req-1"]),
        SubTask(id="st-B", description="d", scope=FileScope(writable=["pom.xml"]), covers=["req-2"])])
    new_plan = TaskPlan(subtasks=[
        SubTask(id="st-9", description="d", scope=FileScope(writable=["pom.xml"]), covers=[])])
    n = _merge_prior_covers_by_scope(new_plan, old_plan, {"req-1", "req-2"})
    assert n == 0, "scope 不唯一不并"
