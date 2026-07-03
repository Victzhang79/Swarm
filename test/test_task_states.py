#!/usr/bin/env python3
"""任务状态单一事实源（SSOT）单测。

修前：`brain/runner._ACTIVE_DB_STATUSES` 与 `project/store._TERMINAL_STATUSES` 两处各自定义、
会漂移，且活跃集缺 CLARIFYING/DESIGN_REVIEW（→ P0-D cancel 死区）。
修后：三子集收敛到 swarm/task_states.py，runner/store 引用之，并集含四个中断挂起态。

纯逻辑，不依赖 DB/Redis。
"""

from __future__ import annotations

from swarm.task_states import (
    ACTIVE_DB_STATUSES,
    ACTIVE_EXECUTION_STATES,
    INTERRUPT_SUSPENDED_STATES,
    TERMINAL_STATES,
)


def test_three_subsets_pairwise_disjoint():
    assert ACTIVE_EXECUTION_STATES & INTERRUPT_SUSPENDED_STATES == frozenset()
    assert ACTIVE_EXECUTION_STATES & TERMINAL_STATES == frozenset()
    assert INTERRUPT_SUSPENDED_STATES & TERMINAL_STATES == frozenset()


def test_active_db_statuses_is_union_of_active_and_interrupt():
    assert ACTIVE_DB_STATUSES == ACTIVE_EXECUTION_STATES | INTERRUPT_SUSPENDED_STATES


def test_interrupt_states_include_all_four_human_gates():
    # P0-D：CLARIFYING/DESIGN_REVIEW 必须在中断挂起集内，否则 cancel/delete 落 409 死区。
    assert {"CONFIRMING", "DELIVERING", "CLARIFYING", "DESIGN_REVIEW"} <= INTERRUPT_SUSPENDED_STATES
    assert INTERRUPT_SUSPENDED_STATES <= ACTIVE_DB_STATUSES


def test_submitted_is_active_but_not_terminal_or_interrupt():
    # SUBMITTED 是"进行中/可取消"，但恢复语义特殊（重入队而非 fail-closed）。
    assert "SUBMITTED" in ACTIVE_EXECUTION_STATES
    assert "SUBMITTED" in ACTIVE_DB_STATUSES
    assert "SUBMITTED" not in TERMINAL_STATES


def test_pooled_is_not_in_any_active_or_terminal_set():
    # POOLED（需求池待执行）不是孤儿候选，也不是终态——不应被 reconcile 触碰。
    assert "POOLED" not in ACTIVE_DB_STATUSES
    assert "POOLED" not in TERMINAL_STATES


def test_terminal_states_exact():
    assert TERMINAL_STATES == frozenset({"DONE", "FAILED", "CANCELLED", "PARTIAL"})


def test_runner_alias_equals_ssot():
    from swarm.brain.runner import _ACTIVE_DB_STATUSES

    assert set(_ACTIVE_DB_STATUSES) == set(ACTIVE_DB_STATUSES)


def test_store_terminal_alias_equals_ssot():
    from swarm.project.store import _TERMINAL_STATUSES

    assert set(_TERMINAL_STATUSES) == set(TERMINAL_STATES)
