"""B8（2026-07-09 深读登记册·阶段0）：失败分类中文盲区 + summary 遮蔽 error — 行为测试。

定案依据 DEEP_READ_REGISTER_2026-07-09_E2E.md §三 B8：
  - _TRANSIENT_MARKERS 全英文：本系统自身错误文案大量中文（"调用超时/连接中断/限流"），
    匹配不到 → classify_failure 返回 None → 误入 capability 阶梯烧配额换模型
    （transient 本应同模型退避，基建抖动换模型纯浪费）。
  - failure.py _failure_class_of 兜底用 `summary or error`：summary 是叙述性文本
    （常无特征词），非空即遮蔽 error 字段里的原始异常文本 → 真 transient 被判 None。
    治本：error（原始异常，最可靠）优先，判不出再退 summary。

注：marker 是对本系统自身错误文案的分类，与"目标项目多栈通用"铁律无关。
"""

from __future__ import annotations

import asyncio
from unittest.mock import patch

import swarm.brain.nodes as nodes
from swarm.models.errors import CAPABILITY, TRANSIENT, classify_failure
from swarm.types import FileScope, SubTask, SubTaskDifficulty, TaskPlan, WorkerOutput


# ─────────────────── classify_failure 中文特征 ───────────────────

def test_cjk_timeout_is_transient():
    assert classify_failure("调用超时（300s 墙钟）") == TRANSIENT


def test_cjk_connection_errors_are_transient():
    assert classify_failure("连接中断，请稍后重试") == TRANSIENT
    assert classify_failure("连接被重置") == TRANSIENT
    assert classify_failure("服务暂时不可用") == TRANSIENT


def test_cjk_rate_limit_is_transient():
    assert classify_failure("触发限流：请求过多") == TRANSIENT


def test_english_markers_regression():
    assert classify_failure("Connection error while streaming") == TRANSIENT
    assert classify_failure("i cannot complete this") == CAPABILITY


def test_cjk_context_length_still_capability():
    """既有中文 capability 特征（上下文长度）优先级不被 transient 补齐破坏。"""
    assert classify_failure("上下文长度超限，同时请求超时") == CAPABILITY


# ─────────────────── summary 遮蔽 error 字段 ───────────────────

def _st(sid):
    return SubTask(id=sid, description="d", difficulty=SubTaskDifficulty.MEDIUM,
                   scope=FileScope(writable=["a"]))


class _FakeResp:
    def __init__(self, content):
        self.content = content


def _fake_llm_returning(strategy):
    class _L:
        async def ainvoke(self, _msgs):
            return _FakeResp('{"strategy":"%s","reasoning":"r"}' % strategy)
    return lambda: _L()


def test_narrative_summary_does_not_mask_transient_error_field():
    """summary=中文叙述（无特征词）+ l1_details.error=真 transient 异常文本 →
    必须走 transient 退避快路（不换模型、不烧 capability 配额），而非被 summary 遮蔽。"""
    wo = WorkerOutput(
        subtask_id="st-1", diff="", summary="子任务执行失败",
        l1_passed=False,
        l1_details={"error": "Connection error: peer closed connection"},
    )
    state = {
        "plan": TaskPlan(subtasks=[_st("st-1")], parallel_groups=[["st-1"]]),
        "failed_subtask_ids": ["st-1"],
        "subtask_results": {"st-1": wo},
        "subtask_retry_counts": {},
        "dispatch_remaining": [],
        "degraded_reasons": [],
    }
    with patch.object(nodes, "_get_brain_llm", _fake_llm_returning("retry")):
        out = asyncio.run(nodes.handle_failure(state))
    assert out.get("subtask_transient_counts", {}).get("st-1") == 1, (
        "error 字段的真 transient 特征被叙述性 summary 遮蔽——误入 capability 阶梯烧配额")
    assert out.get("use_alternate_model") is False
