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
    if max_chars > 0 and len(body) > max_chars:
        cut = body[:max_chars]
        nl = cut.rfind("\n")
        if nl > max_chars * 0.6:
            cut = cut[:nl]
        return cut.rstrip() + "\n…（经验预算裁剪）"
    return body


def build_experience_tools(
    skills: Sequence[SkillDoc], *, max_chars: int = 4000
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
        summary = (s.summary or s.title).strip()
        desc = f"获取「{s.title}」的完整最佳实践经验（advisory 参考·非强制）。{summary}"[:1024]

        def _make(_body: str):
            def _fn() -> str:
                return _body
            return _fn

        try:
            tools.append(
                StructuredTool.from_function(func=_make(body), name=name, description=desc)
            )
        except Exception as e:  # noqa: BLE001 — 单个工具构建失败不拖垮其余
            logger.warning("[skills] 构建经验工具 %s 失败，跳过：%s", name, e)
    return tools
