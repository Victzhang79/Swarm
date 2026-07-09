"""经验拔插层 P1 · selector 测试（experience/selector.py）。

覆盖：栈×意图×阶段 标签预筛（含 '*' 双侧通配）、target 过滤、优先级降序稳定序、
预算截断填缝、max_k 硬上限、rerank 关时确定性 / 开时受约束 / 抛错回退、
stack_langs 归一（java/node/python/go/rust、None/空、javascript 不误判 java、词边界）。
"""
from __future__ import annotations

from swarm.experience.models import SkillDoc
from swarm.experience.selector import (
    select_skills,
    stack_langs_from_project_stack,
)


def _skill(sid, *, stacks=("*",), intents=("*",), phases=("*",), target=("worker",),
           priority=50, body="x", max_chars=1200):
    return SkillDoc(
        id=sid, title=sid, body=body, target=tuple(target),
        applies_to_stacks=tuple(stacks), applies_to_intents=tuple(intents),
        applies_to_phases=tuple(phases), priority=priority, max_chars=max_chars,
    )


def _sel(skills, **kw):
    base = dict(stack_langs={"python"}, intent="create", phase="code",
                target="worker", budget_chars=10000, max_k=10)
    base.update(kw)
    return [s.id for s in select_skills(skills, **base)]


# ── 标签预筛 ──
def test_stack_wildcard_and_specific_match():
    s = [_skill("star", stacks=("*",)), _skill("py", stacks=("python",)),
         _skill("java", stacks=("java",))]
    assert set(_sel(s, stack_langs={"python"})) == {"star", "py"}
    assert set(_sel(s, stack_langs={"java"})) == {"star", "java"}
    # 空 stack_langs → 只命中 '*'
    assert _sel(s, stack_langs=set()) == ["star"]


def test_intent_phase_match_with_wildcards():
    s = [_skill("a", intents=("create",), phases=("code",)),
         _skill("b", intents=("modify",), phases=("code",)),
         _skill("c", intents=("*",), phases=("plan",))]
    assert _sel(s, intent="create", phase="code") == ["a"]
    # 查询 intent='*' → 不按意图过滤（匹配任意技能意图）；phase 仍限定
    assert set(_sel(s, intent="*", phase="code")) == {"a", "b"}
    # 空 intent → 只命中技能侧 '*'
    assert _sel(s, intent="", phase="plan") == ["c"]


def test_target_filter():
    s = [_skill("w", target=("worker",)), _skill("p", target=("planner",)),
         _skill("both", target=("worker", "planner"))]
    assert set(_sel(s, target="worker")) == {"w", "both"}
    assert set(_sel(s, target="planner")) == {"p", "both"}


# ── 排序 / 预算 / max_k ──
def test_priority_desc_then_stable_id():
    s = [_skill("z", priority=10), _skill("a", priority=90), _skill("m", priority=90)]
    # 90 组按 id 升序稳定，然后 10
    assert _sel(s) == ["a", "m", "z"]


def test_budget_cap_allows_small_to_fill_after_big():
    big = _skill("big", priority=90, body="x" * 100, max_chars=100)
    small = _skill("small", priority=80, body="y" * 10, max_chars=10)
    # 预算 60：big(100) 放不下 → continue；small(10) 填进来
    assert _sel([big, small], budget_chars=60) == ["small"]


def test_max_k_hard_cap():
    s = [_skill(f"s{i}", priority=100 - i) for i in range(10)]
    assert len(_sel(s, max_k=3)) == 3


def test_max_k_zero_selects_none():
    """复核发现：max_k<=0（SWARM_SKILLS_MAX_K=0 全抑制）曾因先 append 后判而多带一条。"""
    s = [_skill("a"), _skill("b")]
    assert _sel(s, max_k=0) == []


def test_max_chars_bounds_cost_not_full_body():
    # body 很长但 max_chars 小 → 计价按 max_chars，能进预算
    s = [_skill("a", body="x" * 5000, max_chars=100)]
    assert _sel(s, budget_chars=200) == ["a"]


# ── rerank ──
def test_rerank_none_is_deterministic():
    s = [_skill(f"s{i}", priority=50) for i in range(6)]
    r1 = _sel(s, max_k=3)
    r2 = _sel(s, max_k=3)
    assert r1 == r2 and len(r1) == 3


def test_rerank_used_when_more_candidates_than_k():
    s = [_skill(f"s{i}", priority=50) for i in range(6)]

    def rr(cands, k, budget):
        # 反选后两个
        return list(cands)[-2:]

    picked = select_skills(s, stack_langs={"python"}, intent="create", phase="code",
                           target="worker", budget_chars=10000, max_k=2, rerank_fn=rr)
    assert [p.id for p in picked] == ["s4", "s5"]


def test_rerank_exception_falls_back():
    s = [_skill(f"s{i}", priority=100 - i) for i in range(6)]

    def boom(cands, k, budget):
        raise RuntimeError("rerank down")

    picked = select_skills(s, stack_langs={"python"}, intent="create", phase="code",
                           target="worker", budget_chars=10000, max_k=2, rerank_fn=boom)
    assert [p.id for p in picked] == ["s0", "s1"]  # 确定性回退


def test_rerank_not_called_when_candidates_within_k():
    s = [_skill("a"), _skill("b")]
    called = {"n": 0}

    def rr(cands, k, budget):
        called["n"] += 1
        return list(cands)

    select_skills(s, stack_langs={"python"}, intent="create", phase="code",
                  target="worker", budget_chars=10000, max_k=5, rerank_fn=rr)
    assert called["n"] == 0  # 候选<=max_k 不触发 rerank


# ── stack_langs 归一 ──
def test_stack_langs_java():
    assert "java" in stack_langs_from_project_stack(
        {"backend": "Spring Boot (java)", "build": "maven"})


def test_stack_langs_node_not_java():
    langs = stack_langs_from_project_stack(
        {"backend": "javascript/typescript", "build": "npm", "frontend": "Vue"})
    assert "node" in langs and "java" not in langs  # javascript 不误判成 java


def test_stack_langs_python_go_rust():
    assert "python" in stack_langs_from_project_stack({"backend": "python", "build": "pip"})
    assert "go" in stack_langs_from_project_stack({"backend": "go", "build": "go"})
    assert "rust" in stack_langs_from_project_stack({"backend": "rust", "build": "cargo"})


def test_stack_langs_php_cpp():
    assert "php" in stack_langs_from_project_stack({"backend": "Laravel", "build": "composer"})
    assert "cpp" in stack_langs_from_project_stack({"backend": "C++", "build": "cmake"})


def test_stack_langs_go_word_boundary():
    # "django"/"mongo" 含 'go' 子串但非独立词 → 不应误判 go
    langs = stack_langs_from_project_stack({"backend": "Django", "build": "pip"})
    assert "go" not in langs and "python" in langs


def test_stack_langs_none_and_empty():
    assert stack_langs_from_project_stack(None) == set()
    assert stack_langs_from_project_stack({}) == set()
    assert stack_langs_from_project_stack({"backend": "", "build": ""}) == set()
