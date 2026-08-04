"""P-M3（27 号文）：契约符号安置的「首字母小写排除」按栈门控——非 JVM 栈不再误杀。

治前：`hard` 过滤无条件 `not e["symbol"][0].islower()`——这是 Maven/Java 时代的
「方法名/字段名不是类文件」护栏，但对 Python(get_user_report)/JS(fetchReport)/
Go(非导出符号) 把【契约正主符号】挡在确定性安置门外 → 留 VALIDATE/烧 LLM 重试。

治法定案：排除只在【plan 主导源扩展名 ∈ JVM 类文件集】时保留。JVM 集派生自
STACK_SPEC（shares_classpath_namespace=True 栈的 source_exts 并集），绝不手写表。
消费契约审计：排除的下游=hard→todo→groups→安置子任务，门控只改成员资格，
不改任何下游形状（血规 10③ 无分档需求）。
"""
from __future__ import annotations

from swarm.brain.plan_finisher import (
    _JVM_CLASS_FILE_EXTS,
    _domicile_contract_symbols,
)
from swarm.types import FileScope, SubTask, SubTaskDifficulty, TaskPlan


def _st(sid, *, create=None, writable=None):
    return SubTask(id=sid, description=f"task {sid}",
                   difficulty=SubTaskDifficulty.MEDIUM,
                   scope=FileScope(create_files=create or [], writable=writable or [],
                                   readable=[]),
                   acceptance_criteria=[])


def _plan(subs):
    plan = TaskPlan(subtasks=subs, parallel_groups=[[s.id for s in subs]])
    plan.shared_contract = {}
    return plan


def _placed_files(plan):
    """全 plan 的 create_files（安置结果落点集合）。"""
    return [f for st in plan.subtasks for f in st.scope.create_files]


def test_jvm_exts_derived_from_stack_spec():
    """单一事实源锁：JVM 类文件集=STACK_SPEC 派生（shares_classpath_namespace 并集），
    绝不手抄——加 JVM 系新栈=加表行，本集自动跟随。"""
    from swarm.stacks import STACK_SPEC
    expect = {e.lstrip(".") for s in STACK_SPEC.values()
              if s.shares_classpath_namespace for e in s.source_exts}
    assert _JVM_CLASS_FILE_EXTS == frozenset(expect)
    assert "java" in _JVM_CLASS_FILE_EXTS and "py" not in _JVM_CLASS_FILE_EXTS


def test_java_plan_still_excludes_lowercase_symbol(tmp_path):
    """★回归锁（逐字节不变）★：java 主导 plan 的小写符号（方法名形状）仍被排除——
    同 contract 里的大写符号照常安置，证明函数活着、只有小写被挡（断言区分力：
    防"整个安置路径死掉"的假绿）。"""
    plan = _plan([_st("st-1", create=[
        "alarm/src/main/java/com/x/AlarmService.java",
        "alarm/src/main/java/com/x/AlarmController.java"])])
    contract = {"types": [
        {"name": "getUserReport", "module": "alarm"},
        {"name": "UserReportService", "module": "alarm"}]}
    created = _domicile_contract_symbols(plan, contract, str(tmp_path), "任务", None)
    placed = _placed_files(plan)
    assert any(f.endswith("UserReportService.java") for f in placed), \
        "大写契约符号必须照常安置（函数活着的证据）"
    assert not any("getUserReport" in f for f in placed), \
        "JVM 主导 plan 的小写符号排除必须逐字节保留（Java 方法名不是类文件）"
    assert not any("getUserReport" in s for k, v in created.items()
                   if not k.startswith("_") for s in v)


def test_python_plan_lowercase_symbol_domiciled(tmp_path):
    """★主治锁★：python 主导 plan 的 get_user_report 是契约正主 → 确定性安置成真
    create 文件（治前被小写排除挡死，只能留 VALIDATE 烧重试）。"""
    plan = _plan([_st("st-1", create=[
        "report_service/services/user_service.py",
        "report_service/services/report_query.py"])])
    contract = {"types": [{"name": "get_user_report", "module": "report_service"}]}
    created = _domicile_contract_symbols(plan, contract, str(tmp_path), "任务", None)
    placed = _placed_files(plan)
    assert any(f.endswith("get_user_report.py") for f in placed), \
        f"python 小写契约符号必须安置成真文件: {placed}"
    assert any("get_user_report" in v for v in created.values())


def test_go_plan_lowercase_symbol_domiciled(tmp_path):
    """go 主导 plan：非导出符号 fetchReport 同样安置（栈中立——门控看扩展名派生集，
    不是 if stack== 分支）。"""
    plan = _plan([_st("st-1", create=[
        "report/cmd/server/main.go",
        "report/internal/service/report.go"])])
    contract = {"types": [{"name": "fetchReport", "module": "report"}]}
    _domicile_contract_symbols(plan, contract, str(tmp_path), "任务", None)
    assert any(f.endswith("fetchReport.go") for f in _placed_files(plan))


def test_no_code_evidence_still_returns_empty(tmp_path):
    """早返保持：plan 零源码扩展名证据（纯 SQL/配置）→ 如实 {} 留 VALIDATE，
    门控不造新分支（空 exts 时 _dominant_ext='' 不在 JVM 集，但下方 exts 早返兜底，
    行为与治前逐字节一致）。"""
    plan = _plan([_st("st-1", create=["db/migration/001_init.sql"])])
    contract = {"types": [{"name": "get_user_report", "module": "report_service"}]}
    created = _domicile_contract_symbols(plan, contract, str(tmp_path), "任务", None)
    assert created == {}
    assert not any("get_user_report" in f for f in _placed_files(plan))


def test_mixed_stack_java_dominant_keeps_exclusion(tmp_path):
    """★保守边界锁（reviewer 残留 #3 补夹具）★：java 主导 + 零星 py 模块的混合栈
    plan，py 模块的小写符号仍被排除（=治前行为，保守方向，注释登记的边界）。"""
    plan = _plan([_st("st-1", create=[
        "alarm/src/main/java/com/x/A.java",
        "alarm/src/main/java/com/x/B.java",
        "alarm/src/main/java/com/x/C.java",
        "tools/scripts/helper.py"])])
    contract = {"types": [{"name": "get_user_report", "module": "tools"}]}
    created = _domicile_contract_symbols(plan, contract, str(tmp_path), "任务", None)
    assert not any("get_user_report" in f for f in _placed_files(plan)), \
        "java 主导 plan 按主导扩展名门控：py 小写符号仍排除（保守=治前形状）"
    assert not any("get_user_report" in s for k, v in created.items()
                   if not k.startswith("_") for s in v)


def test_tie_break_is_deterministic(tmp_path):
    """★hunter F2 锁★：java/py 扩展名平票时，most_common 按插入序会让结论随 LLM
    输出序抖动；治法=（-计数, 字典序）双键排序 → 两种文件顺序结论必须一致
    （java 先=排除保留，保守方向）。"""
    def _build(files):
        return _plan([_st("st-1", create=files)])
    files_a = ["m/src/main/java/A.java", "m/src/main/java/B.java",
               "svc/x.py", "svc/y.py"]
    files_b = ["svc/x.py", "svc/y.py",
               "m/src/main/java/A.java", "m/src/main/java/B.java"]  # 插入序对调
    results = []
    for files in (files_a, files_b):
        plan = _build(files)
        contract = {"types": [{"name": "get_user_report", "module": "svc"}]}
        _domicile_contract_symbols(plan, contract, str(tmp_path), "任务", None)
        results.append(any("get_user_report" in f for f in _placed_files(plan)))
    assert results == [False, False], \
        f"平票门控必须确定性（文件序无关），实测 {results}"
