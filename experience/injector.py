"""P2 · 注入器：渲染注入文本块。

两种形态，都是 advisory（只往提示词【拼文本】，没有任何返回值能挡交付/挡 L6）：
  - render_skills_block：全文注入（planner 单发用——无工具调用能力，只能 push）。
  - render_experience_tool_catalog：轻量【工具目录】（worker 用——正文由离散 experience__<id>
    工具按需 pull，提示里只挂 标题+摘要 提示这些工具存在，压 prefill）。
措辞明确"参考·非强制，与固化闸无关"，防小模型把经验误当硬约束凌驾任务约束。
"""

from __future__ import annotations

from collections.abc import Sequence

from swarm.experience.models import SkillDoc, experience_tool_name

_HEADER = "──────────── 相关经验（参考·非强制，与固化闸无关）────────────"
_INTRO = (
    "以下是与【当前栈/意图/阶段】匹配的策展经验，择优采用；"
    "与任务约束或固化闸冲突时，一律以任务约束与固化闸为准。"
)
_FOOTER = "────────────────────────────────────────────────────────"

_TOOLS_INTRO = (
    "你有以下【经验工具】可按需调用，获取对应主题的完整最佳实践（advisory·参考·非强制，"
    "与固化闸无关）。不确定某方面怎么做更好时，优先调用相关工具再动手；不相关的不必调："
)


def render_skills_block(picked: Sequence[SkillDoc]) -> str:
    """选中的技能 → 全文注入块（planner）。空集 → 空串。"""
    if not picked:
        return ""
    parts: list[str] = [_HEADER, _INTRO, ""]
    for s in picked:
        parts.append(f"【{s.title}】")
        parts.append(s.capped_body())
        parts.append("")
    parts.append(_FOOTER)
    return "\n".join(parts).rstrip() + "\n"


def render_experience_tool_catalog(skills: Sequence[SkillDoc]) -> str:
    """选中的技能 → 轻量工具目录（worker）。列 工具名 + 标题 + 一行摘要，不含正文。空集 → 空串。"""
    if not skills:
        return ""
    parts: list[str] = [_HEADER, _TOOLS_INTRO, ""]
    for s in skills:
        summary = (s.summary or "").strip()
        line = f"- {experience_tool_name(s.id)}：【{s.title}】"
        if summary:
            line += f" {summary}"
        parts.append(line)
    parts.append(_FOOTER)
    return "\n".join(parts).rstrip() + "\n"
