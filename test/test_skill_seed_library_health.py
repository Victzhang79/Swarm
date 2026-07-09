"""内置技能库（skills_library）健康闸——机械强制导入技能的"规范"。

导入 ECC 技能后,种子库规模变大;本测试确保每条都合法、路由词表有效、无 ECC 工具/厂商/
项目泄漏字样、无重复 id、全部 native(显式路由)。任何新种子(内置或导入)都必须过本闸。
"""
from __future__ import annotations

import pytest
from swarm.config.settings import PROJECT_ROOT
from swarm.experience.library import load_skills
from swarm.experience.selector import select_skills

_LIB = PROJECT_ROOT / "skills_library"

# 允许的路由词表（与 types.TaskIntent / WorkerPhase 词表 + selector 归一语言集对齐）。
_OK_INTENTS = {"*", "create", "modify", "debug", "audit", "refactor"}
_OK_PHASES = {"*", "plan", "code", "produce"}
_OK_TARGETS = {"worker", "planner"}
_OK_STACKS = {"*", "python", "node", "java", "kotlin", "go", "rust", "cpp", "php", "ruby", "csharp"}

# 禁止出现在正文里的 ECC 工具/机制/厂商/激活散文字样（导入时必须已剥离）。
_FORBIDDEN = [
    "AgentShield", "TodoWrite", "~/.claude", ".claude/", "Claude Code", "claude-code",
    "When to Activate", "Supabase", "database-reviewer agent",
]

# 至少应包含的核心 id（原生种子 + 关键导入），漏了说明导入/解析出问题。
_MUST_HAVE = {
    "coding-standards-core", "security-review-checklist", "api-design", "tdd-workflow-guide",
    "python-patterns", "springboot-patterns", "database-migrations",
    "backend-patterns", "deployment-patterns", "docker-patterns", "postgres-patterns",
    "golang-patterns", "python-testing", "django-patterns", "java-coding-standards",
    "cpp-coding-standards", "laravel-patterns", "frontend-patterns",
    # 第二批导入
    "error-handling", "redis-patterns", "mysql-patterns", "kubernetes-patterns",
    "hexagonal-architecture", "git-workflow", "fastapi-patterns", "nestjs-patterns",
    "prisma-patterns", "react-patterns", "react-performance", "react-testing",
    "vue-patterns", "rust-patterns", "rust-testing", "kotlin-patterns",
    "kotlin-coroutines-flows",
}


def _docs():
    return load_skills(_LIB)


def test_library_size_and_must_have():
    docs = _docs()
    ids = {d.id for d in docs}
    assert len(docs) >= 20, f"种子库过小（{len(docs)}）——导入可能失败"
    missing = _MUST_HAVE - ids
    assert not missing, f"缺少核心种子技能：{sorted(missing)}"


def test_no_duplicate_ids():
    docs = _docs()
    ids = [d.id for d in docs]
    assert len(ids) == len(set(ids)), "存在重复 id"


def test_all_native_and_valid_vocab():
    for d in _docs():
        assert d.imported is False, f"{d.id}: 应为 native（显式声明路由）"
        assert set(d.applies_to_intents) <= _OK_INTENTS, f"{d.id}: 非法 intent"
        assert set(d.applies_to_phases) <= _OK_PHASES, f"{d.id}: 非法 phase"
        assert set(d.target) <= _OK_TARGETS, f"{d.id}: 非法 target {d.target}"
        assert set(d.applies_to_stacks) <= _OK_STACKS, f"{d.id}: 非法 stack"
        assert d.title, f"{d.id}: 缺 title"
        assert d.body.strip(), f"{d.id}: 空正文"


def test_bodies_bounded_and_no_ecc_leak():
    for d in _docs():
        # 导入技能已精炼；正文体量应有界（防原始 20KB 泄漏进来）
        assert len(d.body) <= 4000, f"{d.id}: 正文过长（{len(d.body)}）——应精炼"
        low = d.body.lower()
        for bad in _FORBIDDEN:
            assert bad.lower() not in low, f"{d.id}: 正文含禁止字样 {bad!r}（ECC/厂商未剥离）"


@pytest.mark.parametrize("stack,expect_id", [
    ("go", "golang-patterns"),
    ("python", "python-testing"),
    ("java", "java-coding-standards"),
    ("php", "laravel-patterns"),
    ("cpp", "cpp-coding-standards"),
])
def test_stack_specific_skills_selectable(stack, expect_id):
    """栈特化技能必须能被对应栈选中（否则 selector 归一漏了该栈 → 永不触达）。"""
    docs = _docs()
    picked = select_skills(
        docs, stack_langs={stack}, intent="create", phase="code",
        target="worker", budget_chars=10**9, max_k=50,
    )
    assert expect_id in {p.id for p in picked}, f"栈 {stack} 未选中 {expect_id}"


def test_stack_agnostic_present_for_any_stack():
    """栈无关技能对任意栈都在候选里。"""
    docs = _docs()
    picked = select_skills(
        docs, stack_langs={"go"}, intent="create", phase="code",
        target="worker", budget_chars=10**9, max_k=50,
    )
    ids = {p.id for p in picked}
    assert "backend-patterns" in ids and "coding-standards-core" in ids
