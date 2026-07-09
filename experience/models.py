"""经验拔插层数据模型：SkillDoc（一个技能 = 一份 drop-in 的 Markdown + frontmatter）。"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

# 注入面默认目标：导入的第三方技能（ECC / Claude Code SKILL.md）通常不声明 target，
# 缺省进 worker（主注入面）。native 技能可显式声明 ["worker","planner"]。
DEFAULT_TARGET: tuple[str, ...] = ("worker",)

# worker 经验工具命名：experience__<sanitized_id>。OpenAI/工具名约束 ^[a-zA-Z0-9_-]{1,64}$，
# 故把非法字符折成 _。放 models.py（无 langchain 依赖）供 tools.py 与 injector.py 共用、命名一致。
_TOOL_NAME_SANITIZE = re.compile(r"[^a-zA-Z0-9_-]+")


def experience_tool_name(skill_id: str) -> str:
    safe = _TOOL_NAME_SANITIZE.sub("_", skill_id).strip("_") or "skill"
    return f"experience__{safe}"[:64]


# G11（阶段E）：imported（第三方 drop-in，未声明路由）默认 priority 低于 native 默认 50——
# 宽默认（stacks/intents/phases 全 '*'）+ 高优先级会让任何 drop-in 挤占策展技能的工具位。
IMPORTED_DEFAULT_PRIORITY = 40


def cap_text(text: str, max_chars: int) -> str:
    """按 max_chars 截断（尽量整行、留痕后缀）。G12：截断算法单源——注入路径
    （SkillDoc.capped_body）与工具返回路径（tools._cap）共用，防双实现漂移。"""
    if max_chars > 0 and len(text) > max_chars:
        cut = text[:max_chars]
        nl = cut.rfind("\n")
        if nl > max_chars * 0.6:  # 尽量整行截断，不切半行
            cut = cut[:nl]
        return cut.rstrip() + "\n…（经验预算裁剪）"
    return text


@dataclass(frozen=True)
class SkillDoc:
    """单个策展经验技能。

    支持两种 frontmatter 来源（见 library.parse_skill_text）：
      - **native（本层原生）**：显式声明 applies_to_stacks/intents/phases/target/
        priority/max_chars，精确路由。
      - **imported（第三方，如 ECC / Claude Code 的 `<name>/SKILL.md`）**：只有
        name/description，路由字段全缺 → 全部落到宽默认（stacks/intents/phases='*'，
        target=DEFAULT_TARGET）。零编辑即可被本层消费——"用户可导入"的关键。

    frozen=True：解析后不可变，可安全跨请求缓存/共享。
    """

    id: str
    title: str
    body: str
    target: tuple[str, ...] = DEFAULT_TARGET
    applies_to_stacks: tuple[str, ...] = ("*",)
    applies_to_intents: tuple[str, ...] = ("*",)
    applies_to_phases: tuple[str, ...] = ("*",)
    priority: int = 50
    max_chars: int = 1200
    summary: str = ""  # 一行摘要（来自 description）；供 LLM rerank / 人检索，不进注入正文
    tags: tuple[str, ...] = field(default_factory=tuple)
    source_path: str = ""
    imported: bool = False  # True=从第三方 SKILL.md 导入（未声明路由标签，宽匹配）
    # G5/G6（阶段E）：文件级拔插开关。False=解析保留（前端/审计可见）但选择器排除
    # （绝不进注入面/工具面）。用于下架死件/矛盾件/niche 通配而不删文件。
    enabled: bool = True

    def capped_body(self) -> str:
        """按本技能 max_chars 截断正文（防单条挤爆注入预算）。

        导入的第三方技能常远超预算（ECC 单篇达 16KB），截断到 max_chars 只取开头；
        精炼版应由作者（见 handoff 附录 A2）产出，本层不臆改内容。
        """
        return cap_text(self.body, self.max_chars)  # G12：截断单源
