"""Brain 规划链 LLM 响应的 Pydantic 校验边界（Wave 1 / TD2606-B1）。

根因：规划链历史上把 LLM 输出当裸 dict + `.get()` 直读，下一个没见过的形状要么抛深层
AttributeError/TypeError、要么 `.get()` 静默返回 None 让错形数据流向下游。本模块给【载荷
关键】的 LLM 响应建类型化边界——

设计原则（与 fail-closed 一致）：
  - 载荷关键字段【严格类型】：非法形状 → ValidationError → 调用方【显式降级/重试】，
    绝不静默错形。
  - 装饰性字段（reasoning/key_risks 等）【容忍】：用 before-validator 把异常形状归一为默认，
    不因非关键字段拒掉整条本可用的响应（避免把校验边界变成新的脆弱点）。

配套助手 `parse_and_validate` 见 brain/nodes/shared.py。
"""
from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field, field_validator

from swarm.types import Complexity


class ComplexityAssessmentResponse(BaseModel):
    """ANALYZE / ASSESS 复杂度评估响应。complexity 为载荷关键，必须是合法枚举。"""
    model_config = {"extra": "ignore"}

    complexity: Complexity
    reasoning: str = ""
    key_risks: list[str] = Field(default_factory=list)
    suggested_subtask_count: int | None = None

    @field_validator("complexity", mode="before")
    @classmethod
    def _norm_complexity(cls, v):
        # 大小写/空白归一；非字符串(list/dict 等)原样下传 → 由枚举校验拒绝(显式失败)。
        return v.strip().lower() if isinstance(v, str) else v

    @field_validator("key_risks", mode="before")
    @classmethod
    def _coerce_risks(cls, v):
        # 装饰性字段容忍：字符串→单元素列表，其它非列表→空列表（不因此拒整条响应）。
        if v is None:
            return []
        if isinstance(v, str):
            return [v]
        return v if isinstance(v, list) else []


class StackAdjudicateResponse(BaseModel):
    """DETECT_STACK 大模型裁决响应。frontend 为载荷关键(调用方据其决定是否采纳裁决)。"""
    model_config = {"extra": "ignore"}

    frontend: str
    frontend_kind: str = ""
    backend: str = ""
    build: str = ""
    confidence: float = 0.5
    reason: str = ""

    @field_validator("confidence", mode="before")
    @classmethod
    def _coerce_conf(cls, v):
        try:
            return float(v)
        except (TypeError, ValueError):
            return 0.5


class FailureStrategyResponse(BaseModel):
    """HANDLE_FAILURE 策略响应。strategy 必须是已知策略，未知→ValidationError→调用方确定性回退 retry。"""
    model_config = {"extra": "ignore"}

    strategy: Literal["retry", "retry_alternate", "replan", "escalate"]
    reasoning: str = ""

    @field_validator("strategy", mode="before")
    @classmethod
    def _norm_strategy(cls, v):
        return v.strip().lower() if isinstance(v, str) else v


class FilePlanItem(BaseModel):
    """TECH_DESIGN file_plan 单项。path 为载荷关键(worker 据其定位/创建文件)，缺 path 无意义。"""
    model_config = {"extra": "allow"}  # 保留 description/responsibility/module 等额外字段

    path: str

    @field_validator("path")
    @classmethod
    def _nonempty(cls, v):
        if not v or not str(v).strip():
            raise ValueError("file_plan 项缺少有效 path")
        return v


def validate_file_plan(items: object, *, module: str = "") -> list[dict]:
    """校验并清洗 file_plan：丢弃无有效 path 的 malformed 项（不静默，由调用方记数告警）。

    返回保留下来的 dict 项列表（保留原始额外字段）。非列表输入 → 空列表。
    """
    if not isinstance(items, list):
        return []
    kept: list[dict] = []
    for it in items:
        if not isinstance(it, dict):
            continue
        try:
            FilePlanItem.model_validate(it)
        except Exception:  # noqa: BLE001 — 校验失败=该项无效，丢弃
            continue
        if module and not it.get("module"):
            it["module"] = module
        kept.append(it)
    return kept
