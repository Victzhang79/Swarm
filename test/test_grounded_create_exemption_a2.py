"""A2（2026-07-09 深读登记册·阶段0）：grounded 虚假前提豁免计划新建文件 — 行为测试。

定案依据 DEEP_READ_REGISTER_2026-07-09_E2E.md §二 A2：
  - PRD 点名任何【待创建】文件（如新增某 Controller）→ _verify_named_files_exist
    确定性 exists=False → tech_design 追加 grounded=True 虚假前提 → after_tech_design
    强制 CLARIFY（覆盖 auto_accept）→ auto 模式 clarify_blocked_by_facts →
    DELIVER 拒绝 → 任务死，零重试。
  - 但 file_plan 里 action=create 的路径是【计划新建】的文件——磁盘不存在是工作本身，
    不是虚假前提。治本：grounded 判定按 basename 豁免计划新建者（与既有"路径校正"
    的 basename 匹配口径一致）。
  - 附带封堵：grounded 只能由确定性磁盘核验授予——LLM 自由文本若自带 grounded 字段
    必须剥除（否则绕过豁免直接 block）。

栈无关：测试用 .java 仅作扩展名样例（核验白名单含多栈扩展名），断言不依赖任何语言语义。
"""

from __future__ import annotations

from swarm.brain.planning_nodes import _label_grounded_fact_issues


def _check(file, exists=False, candidates=None):
    return {"file": file, "exists": exists, "confidence": "high",
            "sources": [], "candidates": list(candidates or [])}


def test_planned_create_file_not_grounded_false_premise():
    """PRD 点名的缺失文件在 file_plan 中 action=create → 不追加 grounded 虚假前提。"""
    issues = _label_grounded_fact_issues(
        [],
        [_check("NewThingController.java")],
        [{"path": "src/controller/NewThingController.java", "action": "create"}],
    )
    grounded = [i for i in issues if i.get("grounded")]
    assert grounded == [], (
        "计划新建的文件磁盘必然不存在——判 grounded 虚假前提会让 auto 模式 CLARIFY 阻断任务")


def test_missing_file_without_plan_stays_grounded():
    """缺失且无人计划创建 → 仍是真虚假前提（原行为回归保护）。"""
    issues = _label_grounded_fact_issues(
        [], [_check("Ghost.java")], [{"path": "src/Other.java", "action": "create"}])
    grounded = [i for i in issues if i.get("grounded")]
    assert len(grounded) == 1 and "Ghost.java" in grounded[0]["claim"]


def test_missing_file_planned_modify_stays_grounded():
    """file_plan 声称 modify 一个不存在的文件 → 真虚假前提，不豁免。"""
    issues = _label_grounded_fact_issues(
        [], [_check("Ghost.java")], [{"path": "src/Ghost.java", "action": "modify"}])
    grounded = [i for i in issues if i.get("grounded")]
    assert len(grounded) == 1


def test_llm_issue_referencing_planned_create_not_grounded():
    """LLM 自己的 verdict=false 提及计划新建文件名 → grounded 判 False（advisory 不阻断）。"""
    llm_issue = {"claim": "需求提到 NewThingController.java 但项目里没有",
                 "verdict": "false", "detail": ""}
    issues = _label_grounded_fact_issues(
        [llm_issue],
        [_check("NewThingController.java")],
        [{"path": "src/controller/NewThingController.java", "action": "create"}],
    )
    assert all(not i.get("grounded") for i in issues)


def test_llm_self_claimed_grounded_is_stripped():
    """grounded 只能由磁盘核验授予：LLM 输出自带 grounded=True 必须被剥除重判。"""
    llm_issue = {"claim": "项目用的框架不对", "verdict": "false",
                 "detail": "纯臆测", "grounded": True}
    issues = _label_grounded_fact_issues([llm_issue], [], [])
    assert all(not i.get("grounded") for i in issues), (
        "无磁盘佐证的 LLM 判定绝不能 block（否则绕过确定性坐实原则）")


def test_action_default_is_create_for_exemption():
    """file_plan 项缺 action 字段 → 与路径校正同口径默认 create → 豁免。"""
    issues = _label_grounded_fact_issues(
        [], [_check("NewThing.java")], [{"path": "m/NewThing.java"}])
    assert all(not i.get("grounded") for i in issues)
