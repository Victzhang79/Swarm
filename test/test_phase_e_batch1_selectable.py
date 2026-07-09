"""阶段E 批1（登记册 §七b）：工具可选中性+上下文卫生地基——G1/G2/G3/G4/G11/G12 行为锁。

G1 43 技能 0 description → 工具 desc=标题复读两遍，15 个工具语义同构小模型无从判断
   → 全量补 description（agent 起草）+ 准入闸 target=worker 缺 description 从 warning
   升 error + 工具 desc 以 description（触发条件）开头。
G2 worker 工具面常态满额（基础 12+经验 15=27 恒满，C10 处方红线 5 倍）→ 止血
   worker_max_tools 15→3（结构解 G8 push top-1 + pull ≤3）。
G3 截断按字母序且静默：排序键 (-priority, id) 使 Vue 项目丢 vue-patterns(48) 留
   mysql(48)+postgres(50) 双挂 → (-specificity, -priority, id)（specificity=栈特化
   非'*'）+ dropped debug 留痕。
G4 零遥测：工具闭包裸返回不留痕，加分/减分数据上不可证伪 → 闭包记结构化日志
   (skill_id, subtask_id)。
G11 imported 宽默认风险：ECC 原文 drop-in 即全局候选 → 缺 description 的 imported
   直接跳过（loud）；有 description 的 imported 默认 priority 降 40（低于 native 50）。
G12 工具/planner 双路径截断实现重复（tools._cap vs SkillDoc.capped_body）→ 单源
   cap_text。
"""

from __future__ import annotations

import logging

from swarm.experience.library import parse_skill_text
from swarm.experience.models import SkillDoc
from swarm.experience.selector import select_skills
from swarm.experience.validation import validate_skill_doc

# ─────────────── G2：worker_max_tools 止血 ───────────────


def test_g2_worker_max_tools_default_is_3():
    from swarm.config.settings import SkillsConfig
    cfg = SkillsConfig(_env_file=None)
    assert cfg.worker_max_tools == 3, (
        "基础 12+经验 15=27 工具恒满是复读死循环土壤（C10 处方红线=经验≤3 的 5 倍）；"
        "止血默认 3，结构解见 G8")


# ─────────────── G3：栈特化优先 + dropped 留痕 ───────────────


def _skill(sid, *, stacks=("*",), priority=50, body="x" * 100, target=("worker",),
           summary=""):
    return SkillDoc(id=sid, title=sid, body=body, priority=priority,
                    applies_to_stacks=tuple(stacks), target=tuple(target),
                    summary=summary)


def test_g3_stack_specific_beats_wildcard_at_cutoff():
    """Vue 项目实测场景：vue-patterns(48, vue) 必须排在 mysql(48,*)/postgres(50,*) 前。"""
    skills = [
        _skill("mysql-patterns", stacks=("*",), priority=48),
        _skill("postgres-patterns", stacks=("*",), priority=50),
        _skill("vue-patterns", stacks=("vue",), priority=48),
    ]
    picked = select_skills(
        skills, stack_langs={"vue", "javascript"}, intent="create", phase="code",
        target="worker", budget_chars=10**9, max_k=1)
    assert [s.id for s in picked] == ["vue-patterns"], (
        "旧排序 (-priority, id) 按字母序截断：Vue 项目丢 vue-patterns 留双 DB 通配"
        "（必吃一份错库建议）——栈特化(非'*')必须先于 priority")


def test_g3_within_same_specificity_priority_still_wins():
    skills = [
        _skill("a-low", stacks=("java",), priority=40),
        _skill("z-high", stacks=("java",), priority=60),
    ]
    picked = select_skills(
        skills, stack_langs={"java"}, intent="create", phase="code",
        target="worker", budget_chars=10**9, max_k=1)
    assert [s.id for s in picked] == ["z-high"]


def test_g3_dropped_candidates_logged(caplog):
    skills = [_skill(f"s-{i:02d}", priority=50 - i) for i in range(5)]
    with caplog.at_level(logging.DEBUG, logger="swarm.experience.selector"):
        picked = select_skills(
            skills, stack_langs=set(), intent="create", phase="code",
            target="worker", budget_chars=10**9, max_k=2)
    assert len(picked) == 2
    dropped_msgs = [r.message for r in caplog.records if "s-04" in r.message]
    assert dropped_msgs, (
        "截断静默=配了等于没配不可观测——dropped 技能 id 必须 debug 留痕")


# ─────────────── G1：准入闸升级 + 工具 desc 用 description ───────────────


def test_g1_worker_skill_missing_description_is_error():
    doc = _skill("no-desc-worker", target=("worker",), summary="")
    r = validate_skill_doc(doc, use_llm_judge=False)
    assert any("description" in e for e in r.errors), (
        "worker 技能缺 description=工具 desc 退化为标题复读（小模型唯一判断依据）——"
        "准入闸必须 error 拒绝，不是 warning 建议")
    assert not r.ok


def test_g1_planner_only_skill_missing_description_stays_warning():
    doc = _skill("no-desc-planner", target=("planner",), summary="")
    r = validate_skill_doc(doc, use_llm_judge=False)
    assert not any("description" in e for e in r.errors), (
        "planner 技能是 push 全文注入，description 不是选中依据——保持 warning 不误杀")


def test_g1_native_library_all_have_descriptions():
    """43 条内置技能全量补齐 description（G1 批量起草落盘后此测试转 GREEN）。"""
    from swarm.experience.library import load_skills
    docs = load_skills("skills_library")
    missing = [d.id for d in docs if not d.summary.strip()]
    assert not missing, f"内置技能缺 description：{missing}"


def test_g1_tool_description_leads_with_trigger_condition():
    from swarm.experience.tools import build_experience_tools
    s = _skill("redis-patterns", summary="当你在实现缓存/分布式锁时调用：返回 Redis 惯用法")
    tools = build_experience_tools([s], max_chars=4000)
    assert len(tools) == 1
    assert tools[0].description.startswith("当你在实现缓存/分布式锁时调用"), (
        "desc 必须以 description（触发条件）开头——旧格式『获取「标题」…{summary=标题}』"
        "=标题复读两遍，15 个工具语义同构")


# ─────────────── G4：遥测 ───────────────


def test_g4_tool_invocation_emits_structured_telemetry(caplog):
    from swarm.experience.tools import build_experience_tools
    s = _skill("jpa-patterns", summary="当你写 JPA 实体/仓库时调用：返回 N+1 与事务边界规则")
    tools = build_experience_tools([s], max_chars=4000, subtask_id="st-42")
    with caplog.at_level(logging.INFO, logger="swarm.experience.tools"):
        body = tools[0].func()
    assert body.startswith("x"), "正文照常返回（遥测绝不改变行为）"
    hits = [r for r in caplog.records
            if "jpa-patterns" in r.message and "st-42" in r.message]
    assert hits, (
        "零遥测=加分/减分在数据上不可证伪（F11/G4）——每次调用必须结构化留痕"
        "(skill_id, subtask_id)，任务终态可 join 通过率")


# ─────────────── G11：imported 收窄 ───────────────

_IMPORTED_WITH_DESC = """---
name: ecc-something
description: 当你在做 X 时调用
---
正文内容。
"""

_IMPORTED_NO_DESC = """---
name: ecc-bare
---
正文内容。
"""


def test_g11_imported_missing_description_skipped():
    doc = parse_skill_text(_IMPORTED_NO_DESC, source_path="ecc-bare/SKILL.md")
    assert doc is None, (
        "无 description 的第三方 drop-in=不可判别的全局候选（宽默认+desc 复读）——"
        "loader 必须 loud 跳过，不再零编辑放行")


def test_g11_imported_default_priority_below_native():
    doc = parse_skill_text(_IMPORTED_WITH_DESC, source_path="ecc-something/SKILL.md")
    assert doc is not None and doc.imported
    assert doc.priority < 50, (
        "imported 未声明路由（stacks/intents/phases 全'*'）——默认 priority 必须低于"
        " native 默认 50，否则任何 drop-in 大概率挤进 worker 工具面")


# ─────────────── G12：截断单源 ───────────────


def test_g12_single_source_truncation():
    from swarm.experience import models as m
    assert hasattr(m, "cap_text"), "截断算法单源 models.cap_text（tools/capped_body 共用）"
    body = "line1\n" + "y" * 2000
    s = SkillDoc(id="s", title="s", body=body, max_chars=100)
    assert s.capped_body() == m.cap_text(body, 100)
    from swarm.experience import tools as t
    assert t._cap(body, 100) == m.cap_text(body, 100), (
        "双实现漂移隐患：tools._cap 与 capped_body 必须同一实现")
