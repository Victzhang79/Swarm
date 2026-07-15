"""经验拔插层 P2/P3/P4 · injector + service + 接线测试。

覆盖：render 格式 / 空集→空串、service fail-open（禁用/坏目录/选择器抛错 → ""）、
SWARM_SKILLS_ENABLED 旁路、worker/planner 注入非空、build_worker_prompt 无 KeyError
且技能块进入最终 prompt、栈变则技能变。
"""
from __future__ import annotations

import contextlib

import swarm.experience.service as svc
from swarm.experience.injector import (
    render_experience_tool_catalog,
    render_skills_block,
)
from swarm.experience.models import SkillDoc
from swarm.experience.service import (
    build_worker_experience_tools,
    planner_skills_block,
    select_worker_skills,
    worker_skills_block,
)
from swarm.experience.tools import build_experience_tools
from swarm.types import (
    FileScope,
    SubTask,
    SubTaskDifficulty,
    SubTaskModality,
    TaskIntent,
)

_JAVA_STACK = {"backend": "Spring Boot (java)", "build": "maven",
               "frontend": "服务端模板（Thymeleaf）"}
_PY_STACK = {"backend": "python", "build": "pip"}


@contextlib.contextmanager
def _skills_cfg(**overrides):
    """临时改 get_config().skills 字段并清缓存，退出恢复。"""
    from swarm.config.settings import get_config

    cfg = get_config().skills
    saved = {k: getattr(cfg, k) for k in overrides}
    for k, v in overrides.items():
        setattr(cfg, k, v)
    svc.invalidate_cache()
    try:
        yield
    finally:
        for k, v in saved.items():
            setattr(cfg, k, v)
        svc.invalidate_cache()


def _sub(intent=TaskIntent.CREATE):
    return SubTask(
        id="st-1", description="do a thing", intent=intent,
        difficulty=SubTaskDifficulty.MEDIUM, modality=SubTaskModality.TEXT,
        scope=FileScope(writable=["a.py"], readable=[], create_files=[]),
        acceptance_criteria=["works"],
    )


# ── injector ──
def test_render_format_and_empty():
    assert render_skills_block([]) == ""
    doc = SkillDoc(id="a", title="标题A", body="- 一条", target=("worker",))
    out = render_skills_block([doc])
    assert "参考·非强制" in out
    assert "【标题A】" in out and "- 一条" in out
    assert out.endswith("\n")


# ── discrete experience tools (pull 模型) ──
def test_build_experience_tools_names_and_invoke():
    docs = [
        SkillDoc(id="coding-standards-core", title="通用编码规范", body="- 铁律正文",
                 target=("worker",), summary="栈无关规范"),
        SkillDoc(id="springboot-patterns", title="Spring Boot", body="- java 正文",
                 target=("worker",)),
    ]
    tools = build_experience_tools(docs, max_chars=4000)
    names = {t.name for t in tools}
    assert names == {"experience__coding-standards-core", "experience__springboot-patterns"}
    t = next(t for t in tools if t.name == "experience__coding-standards-core")
    assert t.invoke({}) == "- 铁律正文"          # 无参调用返回正文
    assert "栈无关规范" in t.description          # 描述带摘要供模型判断是否调用


def test_build_experience_tools_body_capped():
    doc = SkillDoc(id="big", title="B", body="x" * 5000, target=("worker",))
    tools = build_experience_tools([doc], max_chars=100)
    out = tools[0].invoke({})
    assert "经验预算裁剪" in out and len(out) < 5000


def test_select_worker_skills_bounded_by_max_tools():
    # E9-7 语义演进：旧入口收编为 push/pull 兼容壳——上界 = pull(max_tools) + push(≤1)
    with _skills_cfg(worker_max_tools=2):
        picked = select_worker_skills(_sub(), _JAVA_STACK)
    assert len(picked) <= 3


def test_build_worker_experience_tools_from_seed():
    # R40-3：pull 默认关（两轮实证 experience__ 调用恒 0）→ 默认零工具；开关回退
    assert build_worker_experience_tools(_sub(TaskIntent.CREATE), _JAVA_STACK) == []
    with _skills_cfg(worker_pull_enabled=True):
        tools = build_worker_experience_tools(_sub(TaskIntent.CREATE), _JAVA_STACK)
        names = {t.name for t in tools}
        assert any(n.startswith("experience__") for n in names)
        assert "experience__python-patterns" not in names  # java 栈不挂 python


def test_worker_experience_tools_wired_into_agent(monkeypatch):
    """create_worker_agent 应把经验工具挂进 agent 的 tools。"""
    import swarm.worker.agent as agent_mod

    captured = {}

    def _spy(*a, **k):
        captured["tools"] = k.get("tools", [])
        return object()  # 不真正建图（避免连模型）

    class _FakeRouter:
        def get_worker_llm(self, **k):
            return object()

    monkeypatch.setattr(agent_mod, "create_react_agent", _spy)
    monkeypatch.setattr(agent_mod, "ModelRouter", _FakeRouter)
    # R40-3 默认：经验走 push 全文进 prompt，不再占工具槽
    agent_mod.create_worker_agent(_sub(TaskIntent.CREATE), project_stack=_JAVA_STACK)
    tool_names = {getattr(t, "name", "") for t in captured["tools"]}
    assert "patch_file" in tool_names                       # 基础工具仍在
    assert not any(n.startswith("experience__") for n in tool_names), (
        "pull 默认关：经验不再占 worker 工具槽")
    with _skills_cfg(worker_pull_enabled=True):
        agent_mod.create_worker_agent(_sub(TaskIntent.CREATE), project_stack=_JAVA_STACK)
        tool_names = {getattr(t, "name", "") for t in captured["tools"]}
        assert any(n.startswith("experience__") for n in tool_names)  # 回退阀恢复挂载


# ── catalog nudge ──
def test_render_experience_tool_catalog():
    docs = [SkillDoc(id="api-design", title="API 设计", body="x", target=("worker",),
                     summary="REST 要点")]
    out = render_experience_tool_catalog(docs)
    assert "experience__api-design" in out
    assert "API 设计" in out and "REST 要点" in out
    assert render_experience_tool_catalog([]) == ""


# ── service happy path ──
def test_worker_block_push_fulltext_no_catalog_by_default():
    # R40-3：默认 push top-K 全文（不再是目录+按需 pull）；pull 开时目录恢复
    out = worker_skills_block(_sub(TaskIntent.CREATE), _JAVA_STACK)
    assert out and "相关经验" in out
    assert "experience__" not in out, "pull 关时无工具目录（目录与工具一一对应）"
    with _skills_cfg(worker_pull_enabled=True):
        out2 = worker_skills_block(_sub(TaskIntent.CREATE), _JAVA_STACK)
        assert "experience__" in out2, "回退阀开=目录恢复"


def test_worker_block_stack_specific_swap():
    with _skills_cfg(worker_pull_enabled=True):
        java = worker_skills_block(_sub(), _JAVA_STACK)
        py = worker_skills_block(_sub(), _PY_STACK)
    # java 栈应含 springboot 段而非 python-patterns；python 栈相反
    assert ("Spring Boot" in java) and ("Spring Boot" not in py)
    assert ("Python 惯用" in py) and ("Python 惯用" not in java)


def test_planner_block_targets_planner_skills():
    out = planner_skills_block(_JAVA_STACK)
    # planner 面 + plan 阶段：api-design / database-migrations 应命中
    assert out
    assert ("API 设计" in out) or ("数据库迁移" in out)


# ── G10（审计⑤）：架构分解类技能绝不进大脑 planner 面 ──
def test_g10_hexagonal_never_reaches_planner_even_with_huge_budget():
    """定时炸弹拆除：即便把 planner 预算撑到远超 hexagonal 正文，它也进不了大脑面。

    revert-check：若 hexagonal 仍 target=[...,'planner'] 且无 _PLANNER_DENY_TAGS，
    足够大的预算会让它注入 → 断言必红。现在 target=worker + 结构性 deny 双保险。
    """
    with _skills_cfg(planner_budget_chars=100_000):
        out = planner_skills_block(_JAVA_STACK)
    assert out, "预算撑大后 planner 块仍应有通用技能"
    for poison in ("六边形", "端口与适配器", "Adapters", "Domain：实体"):
        assert poison not in out, f"架构分层内容 {poison!r} 泄漏进大脑 planner 面"


def test_g10_select_skills_exclude_tags_filters_structurally():
    """结构性 deny：tags 命中 exclude_tags 的技能整条剔除，与预算/排序无关。"""
    from swarm.experience.selector import select_skills

    arch = SkillDoc(id="arch-x", title="Arch", body="layer split guidance",
                    target=("planner",), applies_to_phases=("plan",),
                    priority=99, tags=("architecture", "ddd"))
    plain = SkillDoc(id="plain-y", title="Plain", body="api convention",
                     target=("planner",), applies_to_phases=("plan",),
                     priority=10, tags=("api",))
    picked = select_skills(
        [arch, plain], stack_langs=set(), intent="*", phase="plan",
        target="planner", budget_chars=100_000, max_k=10,
        exclude_tags={"architecture"},
    )
    ids = {s.id for s in picked}
    assert "arch-x" not in ids, "带 architecture tag 的技能必须被 deny"
    assert "plain-y" in ids, "无毒技能不受影响"


def test_g10_planner_deny_set_covers_hexagonal_tags():
    """守卫：hexagonal 的 tags 与 _PLANNER_DENY_TAGS 必有交集（防其 tag 改名后漏网）。"""
    from swarm.experience.service import _PLANNER_DENY_TAGS

    hexa_tags = {"architecture", "ddd", "ports-adapters"}
    assert hexa_tags & _PLANNER_DENY_TAGS, "hexagonal 的架构 tag 必须在 planner deny 集内"


# ── fail-open / bypass ──
def test_disabled_bypass_returns_empty():
    with _skills_cfg(enabled=False):
        assert worker_skills_block(_sub(), _JAVA_STACK) == ""
        assert planner_skills_block(_JAVA_STACK) == ""


def test_bad_dir_returns_empty():
    with _skills_cfg(dir="/no/such/dir/zzz"):
        assert worker_skills_block(_sub(), _JAVA_STACK) == ""


def test_selector_exception_fails_open(monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("selector down")

    monkeypatch.setattr(svc, "select_skills", boom)
    svc.invalidate_cache()
    assert worker_skills_block(_sub(), _JAVA_STACK) == ""


def test_none_stack_still_returns_stack_agnostic():
    # 无 project_stack：通配技能不 push（E9-3）且 pull 默认关 → 默认空块=诚实
    # （pull 通道两轮实证死渠道，删掉不算丢功能）；回退阀开时目录恢复且有界。
    assert worker_skills_block(_sub(), None) == "", "默认无栈=无 push 无 pull=空块"
    with _skills_cfg(worker_pull_enabled=True):
        out = worker_skills_block(_sub(), None)
        assert out and "experience__" in out
        assert out.count("- experience__") <= 3, "G2：worker 工具目录收敛 ≤3"


# ── 接线：build_worker_prompt ──
def test_build_worker_prompt_includes_skills_block():
    from swarm.worker.prompts import build_worker_prompt

    prompt = build_worker_prompt(_sub(TaskIntent.CREATE), project_stack=_JAVA_STACK)
    assert "相关经验（参考·非强制" in prompt   # 技能块已拼入
    assert "{skills_block}" not in prompt        # 占位符已被填充，无 KeyError/残留


def test_build_worker_prompt_ok_when_skills_disabled():
    from swarm.worker.prompts import build_worker_prompt

    with _skills_cfg(enabled=False):
        prompt = build_worker_prompt(_sub(), project_stack=_JAVA_STACK)
    assert "{skills_block}" not in prompt         # 空块也不残留占位符
    assert "相关经验（参考·非强制" not in prompt   # 禁用则不注入
