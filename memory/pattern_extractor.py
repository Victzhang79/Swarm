"""PatternExtractor — L5/L6/L2 写入门槛与结构化抽取（对齐记忆架构设计）。"""

from __future__ import annotations

import re
from typing import Any

from swarm.brain.state import BrainState
from swarm.project.diff_apply import files_from_unified_diff
from swarm.types import Complexity, TaskPlan

# 低于此复杂度的成功任务不写入 L6（仍写 L2 摘要）
SUCCESS_WRITE_MIN_COMPLEXITY = {Complexity.MEDIUM, Complexity.COMPLEX, Complexity.ULTRA}


def should_write_success(state: BrainState) -> bool:
    complexity = state.get("complexity")
    if isinstance(complexity, str):
        try:
            complexity = Complexity(complexity)
        except ValueError:
            return True
    return complexity in SUCCESS_WRITE_MIN_COMPLEXITY


def extract_key_lines(diff: str, *, max_lines: int = 20) -> str:
    """从 unified diff 提取关键新增行（不存全文）。"""
    if not diff:
        return ""
    lines: list[str] = []
    for raw in diff.splitlines():
        if raw.startswith("+") and not raw.startswith("+++"):
            lines.append(raw[1:].rstrip())
        if len(lines) >= max_lines:
            break
    return "\n".join(lines)


def extract_modules(state: BrainState) -> list[str]:
    """从 plan / diff 推断涉及模块路径。"""
    modules: set[str] = set()
    plan_obj = state.get("plan")
    if isinstance(plan_obj, TaskPlan):
        for t in plan_obj.subtasks:
            for fp in (t.scope.writable or []) + (t.scope.readable or []):
                parts = fp.replace("\\", "/").split("/")
                if len(parts) > 1:
                    modules.add("/".join(parts[:2]))
                elif fp:
                    modules.add(fp)
    diff = state.get("merged_diff") or ""
    for fp in files_from_unified_diff(diff)[:15]:
        parts = fp.replace("\\", "/").split("/")
        if len(parts) > 1:
            modules.add("/".join(parts[:2]))
    return sorted(modules)[:10]


def classify_error(state: BrainState, feedback: str = "") -> str:
    text = " ".join(
        [
            feedback,
            state.get("revision_feedback") or "",
            state.get("verification_failure") or "",
        ]
    ).lower()
    if state.get("l2_passed") is False or "l2" in text:
        return "integration_failure"
    if state.get("l3_passed") is False:
        return "staging_failure"
    if state.get("failed_subtask_ids"):
        return "test_failure"
    if any(k in text for k in ("compile", "编译", "syntax")):
        return "compile_error"
    if any(k in text for k in ("style", "规范", "lint")):
        return "style_violation"
    if any(k in text for k in ("架构", "contract", "契约")):
        return "architecture_violation"
    return "logic_error"


def extract_tags(state: BrainState, parsed: dict[str, Any] | None = None) -> list[str]:
    tags: list[str] = []
    parsed = parsed or {}
    tags.extend(str(t) for t in (parsed.get("tags") or parsed.get("rule_tags") or []) if t)
    for mod in extract_modules(state)[:5]:
        tags.append(f"module:{mod}")
    complexity = state.get("complexity")
    if complexity:
        tags.append(f"complexity:{complexity.value if hasattr(complexity, 'value') else complexity}")
    return list(dict.fromkeys(tags))[:12]


def build_one_line_digest(
    state: BrainState,
    *,
    outcome: str,
    parsed: dict[str, Any] | None = None,
) -> str:
    """L2 一句话摘要 — 不存原始需求全文。"""
    parsed = parsed or {}
    modules = extract_modules(state)
    mod_part = f"，涉及 {len(modules)} 个模块" if modules else ""
    file_count = len(files_from_unified_diff(state.get("merged_diff") or ""))
    file_part = f"，{file_count} 个文件" if file_count else ""

    summary = (
        parsed.get("pattern_description")
        or parsed.get("mistake_description")
        or parsed.get("summary")
        or parsed.get("pattern_name")
        or parsed.get("mistake_name")
    )
    if summary and len(str(summary)) <= 160:
        base = str(summary).strip()
    else:
        desc = (state.get("task_description") or "任务")[:80].strip()
        base = desc if desc else "未命名任务"

    outcome_label = {"success": "成功", "failure": "失败", "rejected": "被拒绝"}.get(outcome, outcome)
    return f"{base}{file_part}{mod_part} — {outcome_label}"[:240]


def build_mistake_payload(
    state: BrainState,
    parsed: dict[str, Any],
    *,
    feedback: str = "",
) -> dict[str, Any]:
    diff = state.get("merged_diff") or ""
    for out in (state.get("subtask_results") or {}).values():
        if hasattr(out, "diff") and out.diff:
            diff = diff or out.diff
            break
        if isinstance(out, dict) and out.get("diff"):
            diff = diff or out["diff"]
            break

    snippet = extract_key_lines(diff, max_lines=20)
    error_type = classify_error(state, feedback)
    description = (
        parsed.get("mistake_description")
        or parsed.get("description")
        or build_one_line_digest(state, outcome="failure", parsed=parsed)
    )
    correction = (
        feedback
        or state.get("revision_feedback")
        or parsed.get("root_cause")
        or ""
    )
    return {
        "error_type": error_type,
        "description": str(description)[:2000],
        "context": snippet[:2000] or None,
        "fix_description": str(correction)[:2000] if correction else None,
        "tags": extract_tags(state, parsed),
        "code_snippet": snippet,
    }


def build_success_payload(state: BrainState, parsed: dict[str, Any]) -> dict[str, Any]:
    diff = state.get("merged_diff") or ""
    snippet = extract_key_lines(diff, max_lines=30)
    pattern_name = (
        parsed.get("pattern_name")
        or parsed.get("pattern_description", "")[:80]
        or f"成功模式-{state.get('complexity', 'medium')}"
    )
    description = (
        parsed.get("pattern_description")
        or parsed.get("description")
        or build_one_line_digest(state, outcome="success", parsed=parsed)
    )
    scenario_tags = extract_tags(state, parsed)
    return {
        "pattern_name": str(pattern_name)[:200],
        "description": str(description)[:2000],
        "approach": snippet[:2000] or parsed.get("subtask_decomposition_strategy"),
        "applicable_when": "; ".join(scenario_tags[:8]) or None,
        "tags": scenario_tags,
        "code_snippet": snippet,
    }


def build_l2_summary(state: BrainState, *, outcome: str, parsed: dict[str, Any] | None = None) -> dict[str, Any]:
    """L2 Task Digest 条目 — 仅摘要 + 模块 + 状态。"""
    parsed = parsed or {}
    lessons_raw = parsed.get("lessons_learned")
    if isinstance(lessons_raw, list):
        lessons_learned = lessons_raw[0] if lessons_raw else None
    else:
        lessons_learned = lessons_raw or parsed.get("root_cause") or parsed.get("prevention_measures")

    return {
        "summary": build_one_line_digest(state, outcome=outcome, parsed=parsed),
        "outcome": outcome,
        "lessons_learned": lessons_learned,
        "metadata": {
            "modules": extract_modules(state),
            "tags": extract_tags(state, parsed),
            "source": parsed.get("source") or outcome,
        },
    }
