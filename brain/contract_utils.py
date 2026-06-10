"""共享契约 — Brain 统一定义、注入 Worker、L2 校验。"""

from __future__ import annotations

import json
from typing import Any

from swarm.types import TaskPlan


def enrich_plan_with_shared_contract(plan: TaskPlan) -> TaskPlan:
    """将 plan.shared_contract 合并进各子任务 contract（子任务字段优先）。"""
    shared = plan.shared_contract or {}
    if not shared:
        return plan
    for st in plan.subtasks:
        merged: dict[str, Any] = dict(shared)
        if st.contract:
            merged.update(st.contract)
        st.contract = merged
    return plan


def format_shared_contract_for_prompt(plan: TaskPlan | None) -> str:
    if not plan or not plan.shared_contract:
        return "（无 Brain 级共享契约）"
    try:
        return json.dumps(plan.shared_contract, indent=2, ensure_ascii=False)
    except (TypeError, ValueError):
        return str(plan.shared_contract)


def contract_symbols(shared_contract: dict[str, Any] | None) -> list[str]:
    """从共享契约提取需出现在变更中的符号/接口名。"""
    if not shared_contract:
        return []
    symbols: list[str] = []
    for key in ("interfaces", "types", "apis", "fields", "methods"):
        val = shared_contract.get(key)
        if isinstance(val, list):
            for item in val:
                if isinstance(item, str):
                    symbols.append(item)
                elif isinstance(item, dict):
                    symbols.append(str(item.get("name") or item.get("id") or ""))
        elif isinstance(val, dict):
            symbols.extend(str(k) for k in val.keys())
    for item in shared_contract.get("symbols", []) or []:
        if isinstance(item, str):
            symbols.append(item)
    return [s for s in symbols if s]
