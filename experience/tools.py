"""把选中的经验技能构建成【离散的 worker 工具】（pull 模型）。

用户拍板（2026-07-09）：不预注入全文、也不用单一"混合工具"，而是像 ECC 一样把每条相关
经验做成一个独立工具 experience__<id>，让小模型【自己选】调哪个（或不调）。选择器已按
栈×意图×阶段 收窄+封顶候选，故工具数有界；每个工具无参、调用即返回该技能完整正文。

advisory：这些工具只返回参考文本，**没有任何返回值能挡交付/挡固化闸**。构建失败 fail-open
（跳过该工具 + warning），绝不拖垮 worker agent 创建。
"""

from __future__ import annotations

import logging
from collections.abc import Sequence

from langchain_core.tools import BaseTool, StructuredTool

from swarm.experience.models import SkillDoc, experience_tool_name

logger = logging.getLogger(__name__)


def _cap(body: str, max_chars: int) -> str:
    # G12（阶段E）：截断算法单源 models.cap_text（与 SkillDoc.capped_body 共用，防双实现漂移）
    from swarm.experience.models import cap_text
    return cap_text(body, max_chars)


def build_experience_tools(
    skills: Sequence[SkillDoc], *, max_chars: int = 4000, subtask_id: str = ""
) -> list[BaseTool]:
    """每条技能 → 一个无参工具（调用返回该技能正文）。名字碰撞/构建异常 → 跳过 + warning。"""
    tools: list[BaseTool] = []
    seen: set[str] = set()
    for s in skills:
        name = experience_tool_name(s.id)
        if name in seen:  # sanitize 后同名 → 保留先出现者（复核 SF#4：碰撞=某技能永不触达）
            logger.warning(
                "[skills] 经验工具名 %s 碰撞，跳过 id=%s（该技能不会挂载）", name, s.id)
            continue
        seen.add(name)
        body = _cap(s.body, max_chars)
        summary = (s.summary or "").strip()
        # G1（阶段E）：desc 以 description（触发条件）开头——旧格式"获取「标题」…{摘要
        # 回退标题}"=标题复读两遍，15 个工具语义同构，小模型无从判别。缺 description 的
        # 技能（准入闸已升 error，仅剩历史存量）回退旧格式。
        if summary:
            desc = f"{summary}（advisory 参考·非强制）"[:1024]
        else:
            desc = f"获取「{s.title}」的完整最佳实践经验（advisory 参考·非强制）。"[:1024]

        def _make(_body: str, _sid: str):
            def _fn() -> str:
                # G4（阶段E）：结构化遥测——零留痕时"加分/减分"在数据上不可证伪。
                # 任务终态可按 (skill_id, subtask_id) join 出调用→子任务通过率。
                logger.info("[skills-telemetry] experience_tool_called skill_id=%s "
                            "subtask_id=%s", _sid_skill, _sid or "-")
                return _body
            _sid_skill = s.id
            return _fn

        try:
            tools.append(
                StructuredTool.from_function(func=_make(body, subtask_id),
                                             name=name, description=desc)
            )
        except Exception as e:  # noqa: BLE001 — 单个工具构建失败不拖垮其余
            logger.warning("[skills] 构建经验工具 %s 失败，跳过：%s", name, e)
    return tools
