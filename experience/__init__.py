"""经验拔插层（Pluggable Experience Layer）。

北极星"靠经验+闸门而非换模型"的【经验】一侧：把人工策展的成功经验（惯用法/模式/
清单/规范）按【栈×意图×阶段】按需注入 planner/worker 提示词。

架构第一原则（见 docs/PLUGGABLE_EXPERIENCE_LAYER_HANDOFF.md §1）——
**固化 vs 拔插**：
  - 固化（确定性闸 T1-T4/L1/L2/覆盖闸）裁决"能不能"交付，模型无权跳过，fail-closed。
  - 拔插（本层）提供"怎么做更好"，模型按相关性择优，**advisory，永不阻断交付**，fail-open。

本层三组件：
  - library.py：loader，扫描技能库 *.md → list[SkillDoc]（坏文件跳过 + warning）。
  - selector.py：混合选择器，栈×意图×阶段标签预筛 → 优先级+预算截断 → 可选 LLM rerank。
  - injector.py：render_skills_block，把选中的技能拼成注入文本块（参考·非强制）。

铁律：栈特化只进技能正文 + applies_to_stacks 标签，selector/injector 代码绝不写死
语言/框架/示例项目词；任一异常 fail-open 到空串，绝不因经验层拖垮主流程。
"""

from __future__ import annotations

from swarm.experience.injector import (
    render_experience_tool_catalog,
    render_skills_block,
)
from swarm.experience.library import load_skills, load_skills_from
from swarm.experience.models import SkillDoc, experience_tool_name
from swarm.experience.selector import select_skills, stack_langs_from_project_stack
from swarm.experience.service import (
    build_worker_experience_tools,
    invalidate_cache,
    planner_skills_block,
    select_worker_skills,
    worker_skills_block,
)

__all__ = [
    "SkillDoc",
    "experience_tool_name",
    "load_skills",
    "load_skills_from",
    "select_skills",
    "stack_langs_from_project_stack",
    "render_skills_block",
    "render_experience_tool_catalog",
    "select_worker_skills",
    "build_worker_experience_tools",
    "worker_skills_block",
    "planner_skills_block",
    "invalidate_cache",
]
