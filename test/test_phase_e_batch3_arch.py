"""阶段E 批3（登记册 §七b）：G8 push top-1 + pull ≤3 混合架构 / G10 ULTRA 注入卫生。

G8 离散 pull 对小模型严格劣于混合：15 个不可判别工具把选择负担压给最弱环节；目录措辞
   "不确定就先调用"主动邀请仪式性调用（每次白烧 1 迭代+ToolMessage 挤占历史）。
   拍板方案=push top-1 栈特化技能全文进 prompt（零迭代成本拿到最相关经验）+ pull ≤3
   按需深度工具；目录措辞改按需触发。
G10 ULTRA 分批每批重复注入 planner 块（round37 实测 12 批≈10KB 纯重复）→ 只注首批；
   planner 预算填缝使超预算技能永进不去（死配置）→ 清理。
"""

from __future__ import annotations

from swarm.experience.models import SkillDoc
from swarm.experience.service import select_worker_push_pull


def _skill(sid, *, stacks=("*",), priority=50, body=None, summary="s"):
    return SkillDoc(id=sid, title=sid, body=body or (f"BODY-{sid} " * 20),
                    priority=priority, applies_to_stacks=tuple(stacks),
                    target=("worker",), summary=summary)


# ─────────────── G8：push/pull 分离 ───────────────


def test_g8_stack_specialized_top1_pushed_rest_pulled(monkeypatch):
    import swarm.experience.service as svc
    skills = [
        _skill("vue-patterns", stacks=("node",), priority=48),
        _skill("frontend-patterns", stacks=("node",), priority=50),
        _skill("error-handling", priority=60),
        _skill("api-design", priority=55),
        _skill("backend-patterns", priority=48),
    ]
    monkeypatch.setattr(svc, "_merged_skills", lambda dirs: skills)

    class _Sub:
        id = "st-1"
        intent = "create"

    push, pull = select_worker_push_pull(
        _Sub(), {"frontend": "Vue3", "backend": "Node (javascript)", "build": "npm"})
    assert push is not None and "*" not in push.applies_to_stacks, (
        "push top-1 必须是栈特化技能（全文零迭代成本进 prompt，最相关经验不靠小模型"
        "自己想起来去调工具）")
    assert push.id not in {s.id for s in pull}, "已 push 全文的技能不再占 pull 工具位"
    assert len(pull) <= 3, "pull ≤3（G2/G8 拍板）"


def test_g8_wildcard_only_candidates_push_nothing(monkeypatch):
    import swarm.experience.service as svc
    skills = [_skill("error-handling", priority=60), _skill("api-design", priority=55)]
    monkeypatch.setattr(svc, "_merged_skills", lambda dirs: skills)

    class _Sub:
        id = "st-1"
        intent = "create"

    push, pull = select_worker_push_pull(_Sub(), None)
    assert push is None, (
        "无栈特化候选时不 push（通配技能是泛化建议，不值得无条件占 prefill）")
    assert 0 < len(pull) <= 3


def test_g8_worker_block_contains_push_fulltext(monkeypatch):
    import swarm.experience.service as svc
    skills = [_skill("vue-patterns", stacks=("node",), priority=48,
                     body="UNIQUE-VUE-RULE-LINE follow defineModel"),
              _skill("error-handling", priority=60)]
    monkeypatch.setattr(svc, "_merged_skills", lambda dirs: skills)

    class _Sub:
        id = "st-1"
        intent = "create"

    blk = svc.worker_skills_block(_Sub(), {"frontend": "Vue3", "backend": "",
                                           "build": "npm"})
    assert "UNIQUE-VUE-RULE-LINE" in blk, "push 技能【全文】进 prompt（非仅目录行）"
    assert "experience__error-handling" in blk, "pull 工具目录仍在（按需深度）"
    assert "experience__vue-patterns" not in blk, "push 技能不再出现在工具目录"


def test_g8_tools_built_only_for_pull(monkeypatch):
    import swarm.experience.service as svc
    skills = [_skill("vue-patterns", stacks=("node",), priority=48),
              _skill("error-handling", priority=60)]
    monkeypatch.setattr(svc, "_merged_skills", lambda dirs: skills)

    class _Sub:
        id = "st-1"
        intent = "create"

    tools = svc.build_worker_experience_tools(
        _Sub(), {"frontend": "Vue3", "backend": "", "build": "npm"})
    names = {t.name for t in tools}
    assert "experience__vue-patterns" not in names, "push 技能不挂工具（全文已在 prompt）"
    assert "experience__error-handling" in names


def test_g8_catalog_wording_is_trigger_based():
    from swarm.experience.injector import _TOOLS_INTRO
    assert "优先调用相关工具再动手" not in _TOOLS_INTRO, (
        "『不确定就先调用』主动邀请仪式性调用——每次白烧 1 迭代+ToolMessage 挤占历史")
    assert "触发条件" in _TOOLS_INTRO


# ─────────────── G10：ULTRA 注入卫生 ───────────────


def test_g10_batched_planner_block_first_batch_only():
    from swarm.brain.plan_batch import skills_block_for_batch
    blk = "──经验块──"
    assert skills_block_for_batch(blk, 0) != ""
    assert skills_block_for_batch(blk, 1) == "" and skills_block_for_batch(blk, 11) == "", (
        "ULTRA 分批每批重复注入 planner 块=round37 实测 12 批≈10KB 纯重复 prefill")


def test_g10_no_structurally_dead_planner_skills():
    from swarm.config.settings import SkillsConfig
    from swarm.experience.library import load_skills
    budget = SkillsConfig(_env_file=None).planner_budget_chars
    dead = [d.id for d in load_skills("skills_library")
            if d.enabled and "planner" in d.target
            and min(len(d.body), d.max_chars if d.max_chars > 0 else len(d.body)) > budget]
    assert not dead, (
        f"planner 技能声明体积 > 预算 {budget} = 永进不去的死配置：{dead}")
