"""学习落库 — PatternExtractor + L2/L5/L6 写入"""

from __future__ import annotations

import json
import logging
from typing import Any

from swarm.brain.state import BrainState
from swarm.memory.pattern_extractor import (
    build_l2_summary,
    build_mistake_payload,
    build_success_payload,
    should_write_success,
)
from swarm.memory.store import MemoryStore, MistakeEntry, SuccessEntry, TaskSummary

logger = logging.getLogger(__name__)


def _as_list(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, list):
        return [str(v) for v in value if v]
    return [str(value)]


async def persist_learn_success(state: BrainState, parsed: dict[str, Any]) -> dict[str, Any]:
    """成功 → L2 摘要；L6 仅 medium+ 复杂度写入。"""
    project_id = state.get("project_id") or ""
    task_id = state.get("task_id") or ""
    if not project_id:
        logger.warning("[LEARN_STORE] 无 project_id，跳过落库")
        return {"persisted": False, "reason": "no_project_id"}

    parsed = dict(parsed)
    parsed.setdefault("source", "learn_success")
    l2 = build_l2_summary(state, outcome="success", parsed=parsed)
    success_payload = build_success_payload(state, parsed)

    store = MemoryStore()
    await store.connect()
    try:
        success_id = None
        if should_write_success(state):
            metadata = {
                "key_decisions": _as_list(parsed.get("key_decisions")),
                "lessons_learned": _as_list(parsed.get("lessons_learned")),
                "tags": success_payload.get("tags", []),
                "code_snippet": success_payload.get("code_snippet", ""),
                "source": "learn_success",
            }
            success_id = await store.write_success(
                project_id,
                SuccessEntry(
                    pattern_name=success_payload["pattern_name"],
                    description=success_payload["description"],
                    approach=success_payload.get("approach"),
                    applicable_when=success_payload.get("applicable_when"),
                    task_id=task_id or None,
                    metadata=metadata,
                ),
            )
            logger.info("[LEARN_STORE] L6 成功模式 id=%s", success_id)
        else:
            logger.info("[LEARN_STORE] SIMPLE 任务跳过 L6，仅写 L2")

        await store.write_task_summary(
            project_id,
            TaskSummary(
                task_id=task_id or "unknown",
                summary=l2["summary"],
                outcome="success",
                lessons_learned=(
                    str(l2["lessons_learned"])[:500] if l2.get("lessons_learned") else None
                ),
                metadata={**(l2.get("metadata") or {}), "success_id": success_id},
            ),
        )
        return {
            "persisted": True,
            "success_id": success_id,
            "l2_written": True,
            "l6_skipped": success_id is None,
        }
    except Exception as exc:
        logger.exception("[LEARN_STORE] 成功模式落库失败: %s", exc)
        return {"persisted": False, "error": str(exc)}
    finally:
        await store.close()


async def persist_learn_failure(state: BrainState, parsed: dict[str, Any]) -> dict[str, Any]:
    """失败/拒绝 → L5 错题 + L2 摘要。"""
    project_id = state.get("project_id") or ""
    task_id = state.get("task_id") or ""
    if not project_id:
        logger.warning("[LEARN_STORE] 无 project_id，跳过落库")
        return {"persisted": False, "reason": "no_project_id"}

    parsed = dict(parsed)
    parsed.setdefault("source", "learn_failure")
    feedback = state.get("revision_feedback") or ""
    mistake = build_mistake_payload(state, parsed, feedback=feedback)
    l2 = build_l2_summary(state, outcome="failure", parsed=parsed)

    metadata = {
        "mistake_name": parsed.get("mistake_name"),
        "trigger_conditions": _as_list(parsed.get("trigger_conditions")),
        "prevention_measures": _as_list(parsed.get("prevention_measures")),
        "early_warning_signs": _as_list(parsed.get("early_warning_signs")),
        "tags": mistake.get("tags", []),
        "code_snippet": mistake.get("code_snippet", ""),
        "source": "learn_failure",
    }

    store = MemoryStore()
    await store.connect()
    try:
        mistake_id = await store.write_mistake(
            project_id,
            MistakeEntry(
                error_type=mistake["error_type"],
                description=mistake["description"],
                context=mistake.get("context"),
                fix_description=mistake.get("fix_description"),
                task_id=task_id or None,
                metadata=metadata,
            ),
        )
        await store.write_task_summary(
            project_id,
            TaskSummary(
                task_id=task_id or "unknown",
                summary=l2["summary"],
                outcome="failure",
                lessons_learned=mistake.get("fix_description"),
                metadata={**(l2.get("metadata") or {}), "mistake_id": mistake_id},
            ),
        )
        logger.info("[LEARN_STORE] L5 错题 id=%s project=%s", mistake_id, project_id)
        return {"persisted": True, "mistake_id": mistake_id, "l2_written": True}
    except Exception as exc:
        logger.exception("[LEARN_STORE] 错题落库失败: %s", exc)
        return {"persisted": False, "error": str(exc)}
    finally:
        await store.close()


def merge_persist_meta(learn_summary: str, persist_meta: dict[str, Any]) -> str:
    try:
        data = json.loads(learn_summary) if learn_summary.startswith("{") else {"raw": learn_summary}
    except json.JSONDecodeError:
        data = {"raw": learn_summary}
    data["persist"] = persist_meta
    return json.dumps(data, ensure_ascii=False)
