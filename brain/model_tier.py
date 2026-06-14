"""模型能力分级（I1）——按模型能力调整 Brain 编排的约束强度。

启发：Anthropic managed-agents —— "harness 编码的是模型现在不能做什么，模型变强后
这些假设变成死重"（如 Sonnet 4.5 的 context anxiety 兜底在 Opus 4.5 上成了死重）。

Swarm 的多重校验/澄清/二次拆分上限，前提是"模型不够强需硬约束兜底"。强模型（Claude-4 /
GLM-5 级）一次到位率高，过度澄清/打回/拆分反而是延迟死重。本模块让这些上限随模型能力分级：
  - strong：收紧（少澄清、少打回、少拆分）
  - standard：现状不变（保证零行为变化，默认兜底）
  - weak：放宽（多兜底）

tier 解析优先级：SWARM_MODEL_TIER 手动覆盖 > 模型名自动推断 > standard 兜底。
默认（推断不出 + 无 env）= standard = 与改动前完全一致，零风险。
"""
from __future__ import annotations

import logging
import os
import re
from enum import Enum

logger = logging.getLogger(__name__)


class ModelCapabilityTier(str, Enum):
    STRONG = "strong"
    STANDARD = "standard"
    WEAK = "weak"


# 约束上限映射：standard 一栏 == 现有硬编码值（MAX_CLARIFY_ROUNDS=5 /
# MAX_DESIGN_REJECTS=3 / MAX_ELABORATE_RESPLIT=3），保证默认零行为变化。
_TIER_CONSTRAINTS: dict[ModelCapabilityTier, dict[str, int]] = {
    ModelCapabilityTier.STRONG: {
        "clarify_rounds": 3,
        "design_rejects": 2,
        "elaborate_resplit": 2,
    },
    ModelCapabilityTier.STANDARD: {
        "clarify_rounds": 5,
        "design_rejects": 3,
        "elaborate_resplit": 3,
    },
    ModelCapabilityTier.WEAK: {
        "clarify_rounds": 6,
        "design_rejects": 3,
        "elaborate_resplit": 4,
    },
}

# 模型名 → strong 的推断规则（前沿大模型，一次到位率高）。
# 保守：只把明确的前沿模型判 strong，其余一律 standard（不冒进）。
_STRONG_PATTERNS = [
    r"claude.*(?:4|opus-4|sonnet-4|4\.\d)",   # claude-opus-4 / sonnet-4 / claude-4.x
    r"gpt-?5",                                  # gpt-5
    r"glm-?5",                                  # glm-5 / GLM-5.1
    r"gemini.*(?:2\.5|3)",                      # gemini-2.5 / gemini-3
    r"deepseek.*(?:v3|r1)",                     # deepseek-v3 / r1
    r"o3|o4",                                    # openai o3/o4 推理模型
    r"grok-?[34]",                              # grok-3/4
    r"kimi-?k2",                                 # kimi k2
    r"minimax-?m2",                              # minimax m2
]
# 明确的小/弱模型 → weak（参数量小、易跑偏，多兜底）。
_WEAK_PATTERNS = [
    r"(?:^|[-/])(?:1\.5|3|7|8)b\b",            # 1.5B/3B/7B/8B 小模型
    r"qwen.*(?:1\.5|3|7)b",
    r"gemma.*[27]b",
    r"phi-?[23]",
    r"llama.*(?:1|3\.2-[13])b",
]


def infer_tier_from_model(model_name: str | None) -> ModelCapabilityTier:
    """从模型名推断能力 tier。推断不出 → STANDARD（保守，现状不变）。"""
    if not model_name:
        return ModelCapabilityTier.STANDARD
    name = model_name.lower()
    for pat in _STRONG_PATTERNS:
        if re.search(pat, name):
            return ModelCapabilityTier.STRONG
    for pat in _WEAK_PATTERNS:
        if re.search(pat, name):
            return ModelCapabilityTier.WEAK
    return ModelCapabilityTier.STANDARD


def resolve_tier(model_name: str | None = None) -> ModelCapabilityTier:
    """解析当前生效 tier：SWARM_MODEL_TIER 手动覆盖 > 模型名推断 > STANDARD。"""
    override = (os.environ.get("SWARM_MODEL_TIER", "") or "").strip().lower()
    if override in (t.value for t in ModelCapabilityTier):
        return ModelCapabilityTier(override)
    if override:
        logger.warning("[MODEL_TIER] 无效的 SWARM_MODEL_TIER=%r，忽略（用推断/默认）", override)
    return infer_tier_from_model(model_name)


def tier_constraints(model_name: str | None = None) -> dict[str, int]:
    """返回当前 tier 的约束上限 dict（clarify_rounds/design_rejects/elaborate_resplit）。

    全局开关 SWARM_MODEL_TIER_ENABLED：默认 false（=永远 standard，行为与改动前一致，零风险）。
    显式置 true 才让 tier 分级生效。这是"默认关 + 显式启用 + A/B"的安全闸门。
    """
    enabled = (os.environ.get("SWARM_MODEL_TIER_ENABLED", "false") or "false").lower() in ("true", "1", "yes")
    if not enabled:
        return dict(_TIER_CONSTRAINTS[ModelCapabilityTier.STANDARD])
    tier = resolve_tier(model_name)
    if tier != ModelCapabilityTier.STANDARD:
        logger.info("[MODEL_TIER] 生效 tier=%s（约束已按能力调整）", tier.value)
    return dict(_TIER_CONSTRAINTS[tier])
