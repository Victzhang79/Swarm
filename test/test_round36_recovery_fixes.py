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
