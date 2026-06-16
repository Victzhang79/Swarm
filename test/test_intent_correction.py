"""task dbfc265f 回归：LLM 误判 AUDIT 但 scope 有写文件 → 校正为 MODIFY/CREATE。

根因：功能需求"操作日志导出 Excel"被 LLM 误判 intent=AUDIT（语义联想"日志/权限校验"），
走 security_audit 不产 diff → findings=0 判失败 → retry 死循环。
修复：AUDIT 是只读安全分析；子任务有 writable/create 文件即非 audit，以确定性信号纠正。
"""
from swarm.brain.contract_utils import correct_misclassified_intent
from swarm.types import FileScope, SubTask, TaskIntent, TaskPlan


def test_audit_with_writable_corrected_to_modify():
    st = SubTask(id="st-1", description="导出日志功能", intent=TaskIntent.AUDIT,
                 scope=FileScope(writable=["Ctrl.java"]))
    plan = TaskPlan(subtasks=[st])
    assert correct_misclassified_intent(plan)
    assert plan.subtasks[0].intent == TaskIntent.MODIFY


def test_audit_with_create_corrected_to_create():
    st = SubTask(id="st-1", description="新增导出类", intent=TaskIntent.AUDIT,
                 scope=FileScope(create_files=["NewExporter.java"]))
    plan = TaskPlan(subtasks=[st])
    assert correct_misclassified_intent(plan)
    assert plan.subtasks[0].intent == TaskIntent.CREATE


def test_real_audit_no_write_kept():
    """真正的 audit（无写文件）保持 AUDIT，不误伤。"""
    st = SubTask(id="st-1", description="安全审计", intent=TaskIntent.AUDIT,
                 scope=FileScope(readable=["a.java", "b.java"]))
    plan = TaskPlan(subtasks=[st])
    assert correct_misclassified_intent(plan) is False
    assert plan.subtasks[0].intent == TaskIntent.AUDIT


def test_non_audit_untouched():
    """非 AUDIT 意图不受影响。"""
    st = SubTask(id="st-1", description="改代码", intent=TaskIntent.MODIFY,
                 scope=FileScope(writable=["a.java"]))
    plan = TaskPlan(subtasks=[st])
    assert correct_misclassified_intent(plan) is False
    assert plan.subtasks[0].intent == TaskIntent.MODIFY
