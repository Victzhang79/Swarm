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
    with _skills_cfg(worker_max_tools=2):
        picked = select_worker_skills(_sub(), _JAVA_STACK)
    assert len(picked) <= 2


def test_build_worker_experience_tools_from_seed():
    tools = build_worker_experience_tools(_sub(TaskIntent.CREATE), _JAVA_STACK)
    names = {t.name for t in tools}
    assert any(n.startswith("experience__") for n in names)
    # java 栈应挂上 springboot 经验工具、不挂 python
    assert "experience__springboot-patterns" in names
    assert "experience__python-patterns" not in names


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
    agent_mod.create_worker_agent(_sub(TaskIntent.CREATE), project_stack=_JAVA_STACK)
    tool_names = {getattr(t, "name", "") for t in captured["tools"]}
    assert "patch_file" in tool_names                       # 基础工具仍在
    assert any(n.startswith("experience__") for n in tool_names)  # 经验工具已挂


# ── catalog nudge ──
def test_render_experience_tool_catalog():
    docs = [SkillDoc(id="api-design", title="API 设计", body="x", target=("worker",),
                     summary="REST 要点")]
    out = render_experience_tool_catalog(docs)
    assert "experience__api-design" in out
    assert "API 设计" in out and "REST 要点" in out
    assert render_experience_tool_catalog([]) == ""


# ── service happy path ──
def test_worker_block_is_tool_catalog_not_bodies():
    out = worker_skills_block(_sub(TaskIntent.CREATE), _JAVA_STACK)
    assert out and "相关经验" in out
    # 目录列工具名，不含技能正文（正文按需 pull）
    assert "experience__" in out
    assert "默认拒绝 / fail-closed" not in out   # coding-standards-core 正文不应出现在目录里


def test_worker_block_stack_specific_swap():
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
    # 无 project_stack：仍应命中栈无关技能。
    # G2/G7 语义演进（阶段E）：工具面收敛 max_k=3 且 coding-standards-core 让位
    # priority 45——不再断言特定技能必入前 3，只断言目录非空且有界。
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
