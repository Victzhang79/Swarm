"""学习落库 — PatternExtractor + L2/L5/L6 写入"""

from __future__ import annotations

import asyncio
import hashlib
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

# P1-DEBT-03：错题重现/成功模式复用时强化【已有】记录(occurrence_count++/reuse_count++)
# 而非每次插新行。否则两计数器恒为初值，decay 的"常遇错题重振权重 / 高复用模式衰减更慢"
# 完全失效。阈值取偏高余弦相似度，确保只对【确属同一条】记录强化，避免误并不同记录。
# 注：query_* 在 embedding 不可用(零向量)时返回 []，此时自动跳过强化、退回插新行(优雅降级)。
_REINFORCE_SIMILARITY = 0.92

# B7 治本：learn 落库的"检查幂等 → 写入(含 reinforce 读改写)"临界区序列化锁。
# TOCTOU 根因：_already_persisted 检查在事务外，并发同键 learn 都通过检查后各写一条 →
# 重复 L2 + 双计 reuse/occurrence。目标拓扑=单 brain 进程(单 asyncio)，故一把进程级 async 锁
# 把临界区串行化即【原子】闭合：同键 learn 第二个进锁时必见首个已写的键 → 幂等命中跳过；
# reinforce 的 query→insert 也在锁内 → 两个相似 learn 不再各插一行(计数虚高)。learn 频次极低
# (每任务终态一次)，全局串行开销可忽略。cross-process 硬化(唯一约束迁移)因需处理存量重复行、
# 且当前非多进程拓扑，留作显式后续项(见交接)。
_persist_lock = asyncio.Lock()


def _idempotency_key(task_id: str, outcome: str, content: str) -> str:
    """learn 写入的确定性幂等键：同一(task, outcome, 摘要)重放得同键 → 用于去重防双计数。"""
    raw = f"{task_id}|{outcome}|{content}".encode("utf-8", "ignore")
    return hashlib.sha256(raw).hexdigest()[:32]


async def _already_persisted(store: MemoryStore, project_id: str, idem_key: str) -> bool:
    """L2 摘要里是否已落过该幂等键(防 learn 重放：成功后重试会二次写 L2 + 二次强化计数)。

    best-effort：检查失败时返回 False(放行)，绝不因可观测性阻塞主落库。
    注：检查与写入非原子(TOCTOU)，挡的是【顺序重放/重试】这一现实场景；
    并发同任务 learn 极罕见(一个任务一次 learn)，真要强一致需加唯一约束(留作迁移)。
    """
    try:
        return await store.summary_has_idempotency_key(project_id, idem_key)
    except Exception as exc:  # noqa: BLE001
        logger.warning("[LEARN_STORE] 幂等检查跳过(非致命): %s", exc)
        return False


def _as_list(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, list):
        return [str(v) for v in value if v]
    return [str(value)]


async def _maybe_reinforce_mistake(
    store: MemoryStore, project_id: str, error_type: str, description: str
) -> int | None:
    """错题重现检测：找到高相似已有错题则 occurrence_count++ 并复用其 id；否则 None(插新)。

    best-effort：任何异常都不影响主落库（退回插新行）。
    """
    try:
        hits = await store.query_mistakes(
            project_id, description, top_k=1, error_type=error_type
        )
        if hits and float(hits[0].get("similarity") or 0.0) >= _REINFORCE_SIMILARITY:
            mid = hits[0]["id"]
            await store.increment_mistake_occurrence(mid)
            return mid
    except Exception as exc:  # noqa: BLE001
        logger.warning("[LEARN_STORE] 错题强化检查跳过(非致命): %s", exc)
    return None


async def _maybe_reinforce_success(
    store: MemoryStore, project_id: str, description: str
) -> int | None:
    """成功模式复用检测：找到高相似已有模式则 reuse_count++ 并复用其 id；否则 None(插新)。"""
    try:
        hits = await store.query_successes(project_id, description, top_k=1)
        if hits and float(hits[0].get("similarity") or 0.0) >= _REINFORCE_SIMILARITY:
            sid = hits[0]["id"]
            await store.increment_success_reuse(sid)
            return sid
    except Exception as exc:  # noqa: BLE001
        logger.warning("[LEARN_STORE] 成功模式强化检查跳过(非致命): %s", exc)
    return None


async def persist_learn_success(state: BrainState, parsed: dict[str, Any]) -> dict[str, Any]:
    """成功 → L2 摘要；L6 仅 medium+ 复杂度写入。"""
    project_id = state.get("project_id") or ""
    task_id = state.get("task_id") or ""
    if not project_id:
        logger.warning("[LEARN_STORE] 无 project_id，跳过落库")
        return {"persisted": False, "reason": "no_project_id"}

    parsed = dict(parsed)
    parsed.setdefault("source", "learn_success")
    # A-P1-05 / #3 round22：部分交付(放弃了子任务)= 终态 PARTIAL，L2 摘要如实记 outcome=partial，
    # 不得标 success；should_write_success 已同源拦下 L6 成功模式的写入。
    # 单一事实源 is_partial_delivery = abandoned ∪ give_up（原只看 abandoned 漏 give_up →
    # give_up-only PARTIAL 被误记 outcome=success）。
    from swarm.brain.gates import is_partial_delivery
    _outcome = "partial" if is_partial_delivery(state) else "success"
    l2 = build_l2_summary(state, outcome=_outcome, parsed=parsed)
    success_payload = build_success_payload(state, parsed)

    idem_key = _idempotency_key(task_id, _outcome, l2["summary"])

    # B7 + 复核 CR-3：先 acquire 再【在 try 内】建连接——cancel 若发生在 acquire 等待期，try 未
    # 进入、连接未建，无泄漏；acquire 成功后无 await 直接进 try，finally 保证 release + close。
    # 临界区(检查幂等 → 写入含 reinforce)在进程级锁内串行，原子闭合 TOCTOU/竞态。
    await _persist_lock.acquire()
    # D60：store 先绑定 None——若 MemoryStore() 构造即抛，finally 里 close 引用未绑定名会抛
    # NameError/UnboundLocalError【顶替原始异常】（except 已如实返回的 error dict 也被吞掉）。
    store: MemoryStore | None = None
    try:
        store = MemoryStore()
        await store.connect()
        # WS4 幂等：learn 重放(成功后重试)若已落过同键 → 跳过，避免二次写 L2 + 二次 reuse_count++。
        if await _already_persisted(store, project_id, idem_key):
            logger.info("[LEARN_STORE] 幂等命中(重放)，跳过成功落库 key=%s", idem_key)
            return {"persisted": False, "reason": "duplicate", "idempotent": True}
        # A-P1-26：L6 成功模式(写新 / reuse_count++) 与 L2 任务摘要 包进单事务。
        # 否则 step-1(写 success / 强化复用计数) 成功而 step-2(写 task_summary) 失败时，
        # 会留下孤儿成功记录 + 已自增的 reuse_count（双重计数），下次仍可被强化，污染权重。
        success_id = None
        async with store.transaction():
            if should_write_success(state):
                metadata = {
                    "key_decisions": _as_list(parsed.get("key_decisions")),
                    "lessons_learned": _as_list(parsed.get("lessons_learned")),
                    "tags": success_payload.get("tags", []),
                    "code_snippet": success_payload.get("code_snippet", ""),
                    "source": "learn_success",
                }
                reinforced_id = await _maybe_reinforce_success(
                    store, project_id, success_payload["description"]
                )
                if reinforced_id is not None:
                    success_id = reinforced_id
                    logger.info("[LEARN_STORE] L6 成功模式复用，强化已有 id=%s(reuse_count++)", success_id)
                else:
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
                    outcome=_outcome,
                    lessons_learned=(
                        str(l2["lessons_learned"])[:500] if l2.get("lessons_learned") else None
                    ),
                    metadata={**(l2.get("metadata") or {}), "success_id": success_id,
                              "idempotency_key": idem_key},
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
        _persist_lock.release()
        if store is not None:
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

    idem_key = _idempotency_key(task_id, "failure", l2["summary"])

    # B7 + 复核 CR-3：先 acquire 再【在 try 内】建连接——cancel 若发生在 acquire 等待期，try 未
    # 进入、连接未建，无泄漏；acquire 成功后无 await 直接进 try，finally 保证 release + close。
    # 临界区(检查幂等 → 写入含 reinforce)在进程级锁内串行，原子闭合 TOCTOU/竞态。
    await _persist_lock.acquire()
    store: MemoryStore | None = None  # D60：防构造抛异常时 finally close NameError 顶替原异常
    try:
        store = MemoryStore()
        await store.connect()
        # WS4 幂等：learn 重放若已落过同键 → 跳过，避免二次写 L5 + 二次 occurrence_count++。
        if await _already_persisted(store, project_id, idem_key):
            logger.info("[LEARN_STORE] 幂等命中(重放)，跳过错题落库 key=%s", idem_key)
            return {"persisted": False, "reason": "duplicate", "idempotent": True}
        # A-P1-26：L5 错题(写新 / occurrence_count++) 与 L2 摘要 包进单事务，
        # 避免 step-2 失败留下孤儿错题 + 双重计数的 occurrence_count。
        async with store.transaction():
            reinforced_id = await _maybe_reinforce_mistake(
                store, project_id, mistake["error_type"], mistake["description"]
            )
            if reinforced_id is not None:
                mistake_id = reinforced_id
                logger.info("[LEARN_STORE] L5 错题重现，强化已有 id=%s(occurrence_count++)", mistake_id)
            else:
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
                    metadata={**(l2.get("metadata") or {}), "mistake_id": mistake_id,
                              "idempotency_key": idem_key},
                ),
            )
        logger.info("[LEARN_STORE] L5 错题 id=%s project=%s", mistake_id, project_id)
        return {"persisted": True, "mistake_id": mistake_id, "l2_written": True}
    except Exception as exc:
        logger.exception("[LEARN_STORE] 错题落库失败: %s", exc)
        return {"persisted": False, "error": str(exc)}
    finally:
        _persist_lock.release()
        if store is not None:
            await store.close()


def merge_persist_meta(learn_summary: str, persist_meta: dict[str, Any]) -> str:
    try:
        data = json.loads(learn_summary) if learn_summary.startswith("{") else {"raw": learn_summary}
    except json.JSONDecodeError:
        data = {"raw": learn_summary}
    data["persist"] = persist_meta
    return json.dumps(data, ensure_ascii=False)
