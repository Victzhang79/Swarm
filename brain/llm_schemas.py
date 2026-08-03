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
        # 装饰性字段【强容忍】：绝不因它的形状拒掉整条响应（否则 analyze 会把本判 ultra 的任务
        # 静默降级 MEDIUM——key_risks 是 list[dict] 时 list[str] 校验会失败）。
        # 字符串→单元素；列表→逐元素转字符串（兼容 list[dict]/混合）；其它→空。
        if v is None:
            return []
        if isinstance(v, str):
            return [v]
        if isinstance(v, list):
            return [x if isinstance(x, str) else str(x) for x in v]
        return []


# ★单一事实源★ `frontend_kind` 的可消费取值——schema 校验、裁决 prompt 契约、
# 清标合取三处同源（MEDIUM-4：判据必须锁在被消费的那个字段上）。
FRONTEND_KINDS = ("server-template", "spa", "separated", "none")

# SPA-like 子集：有【独立前端工程】（需要 npm 类构建/lint 闸门、ts 语言推断）的形态。
# 消费契约与 FRONTEND_KINDS 不同（后者是全枚举）：worker/l1_pipeline._stack_repair_langs
# 按它把 ts 纳入 repair 语言集。新增 kind 时须显式拍板是否归此子集——不自动获得是刻意的
# （kind 的语义后果逐个判断，fail-closed：漏接=少发闸门，可观测；错接=给无前端工程发
# npm 闸门，冤杀）。P-C3 复核 R2-H5：此前该子集是 l1_pipeline 里的手写字面量。
SPA_LIKE_KINDS = ("spa", "separated")


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

    @field_validator("frontend_kind")
    @classmethod
    def _kind_must_be_consumable(cls, v):
        """★P-C3 复核 MEDIUM-4★ `frontend_kind` 是**被消费的**字段（`format_stack_for_prompt`
        按它分档发前端约定），自由文本形态（`server_template` 下划线/`服务端模板` 中文）
        会让消费者一条约定都不发——与病灶原状态逐字相同，而标已被清掉 ⇒ 连"认不得"
        兜底提示都没了。非枚举值 ⇒ ValidationError ⇒ 调用方沿用确定性结果（整裁决作废，
        与"形状非法→抛→沿用"的 TD2606-B1 路径同构）。空串（漏字段）放行——由调用方的
        清标合取分档（不采纳 kind、不清标、WARNING）。"""
        if v and v not in FRONTEND_KINDS:
            raise ValueError(
                f"frontend_kind={v!r} 不在消费枚举 {FRONTEND_KINDS}——自由文本形态"
                "会让下游一条前端约定都不发（MEDIUM-4），整条裁决作废沿用确定性结果")
        return v


class FailureStrategyResponse(BaseModel):
    """HANDLE_FAILURE 策略响应。strategy 必须是已知策略，未知→ValidationError→调用方确定性回退 retry。"""
    model_config = {"extra": "ignore"}

    strategy: Literal["retry", "retry_alternate", "replan", "escalate"]
    reasoning: str = ""
    # B3-3（round38c）：结构化载荷。prompt 一直在要 adjusted_subtasks 但 extra:"ignore"
    # 丢弃、全仓零消费——LLM 点名"文件 X 从未被任何子任务创建"当散文扔掉（TwoFactorBindVO
    # 拖 3-5h 的机制之一）。missing_files=LLM 判定计划里无人创建的文件相对路径，供
    # replan 守卫走外科 scope 修正（补 create_files）而非降级 retry 白跑。
    missing_files: list[str] = Field(default_factory=list)
    adjusted_subtasks: list[str] = Field(default_factory=list)

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
