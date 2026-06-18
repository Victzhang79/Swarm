"""brain/nodes/dispatch.py — dispatch/monitor 节点 + 安全审计（B1 批3 抽出）。

被测试 patch 的 _dispatch_to_worker 留在 __init__.py；本模块内对它的调用用
`nodes._dispatch_to_worker(...)` 模块限定，使 patch("swarm.brain.nodes._dispatch_to_worker") 命中。
"""

from __future__ import annotations

import asyncio
import logging

from swarm.audit import audit
from swarm.brain import nodes
from swarm.brain.context_log import touch_context
from swarm.brain.nodes.shared import _diff_has_changes, _worker_profile_prompt
from swarm.brain.state import BrainState
from swarm.config.settings import get_config
from swarm.memory.sliding_window import PRIORITY_WORKER
from swarm.types import Confidence, SubTask, WorkerOutput

logger = logging.getLogger(__name__)

# H9 修复：fire-and-forget 后台任务强引用集合，防 asyncio 弱引用下被 GC。
_BG_TASKS: set = set()


def _inject_predecessor_context(to_dispatch, plan_obj, subtask_results: dict) -> None:
    """跨子任务上下文传递：把前序已完成依赖子任务的产出注入后序子任务的 context_snippets。

    B3 依赖序拆分（接口→实现→装配）场景：后序子任务（如 ServiceImpl）需要看到前序
    （Service 接口）实际定义了什么方法签名，才能正确实现。这里把前序产出的 diff 里【新增的
    方法/类签名行】抽出来，append 到后序子任务的 context_snippets，随 worker prompt 下发。
    无依赖 / 前序未完成 → no-op。幂等：重复注入同一前序会去重（按标记）。
    """
    import re

    for st in to_dispatch:
        deps = [d for d in (getattr(st, "depends_on", []) or []) if d in subtask_results]
        if not deps:
            continue
        marker = "\n\n🔗 前序子任务已产出（实现时对齐这些已定义的接口/签名）：\n"
        if marker.strip()[:10] in (getattr(st, "context_snippets", "") or ""):
            continue  # 已注入过
        pred_blocks: list[str] = []
        for dep_id in deps:
            out = subtask_results.get(dep_id)
            diff = getattr(out, "diff", "") or ""
            if not diff.strip():
                continue
            added = [ln[1:].strip() for ln in diff.split("\n")
                     if ln.startswith("+") and not ln.startswith("+++")]
            # ① 类/方法/接口签名
            sigs = []
            for s in added:
                if re.match(r"^(public|private|protected|class|interface|enum|def |func |function |export )", s) \
                   or re.search(r"\b[A-Za-z_]\w*\s*\([^)]*\)\s*[{;:]?\s*$", s):
                    sigs.append(s[:140])
            # ② API 端点契约（第二批-4：前端最需要——后端暴露了哪些 HTTP 端点）。
            # 抽 @GetMapping/@PostMapping/@PutMapping/@DeleteMapping/@RequestMapping(含路径) +
            # @RequestMapping 类级前缀；前端据此对齐 URL，减少前后端契约偏离。
            endpoints = []
            for s in added:
                m = re.search(r'@(Get|Post|Put|Delete|Patch|Request)Mapping\s*\(\s*(?:value\s*=\s*)?["\']([^"\']+)["\']', s)
                if m:
                    verb = m.group(1).upper()
                    endpoints.append(f"{verb if verb != 'REQUEST' else 'ANY'} {m.group(2)}")
                elif re.search(r"@(Get|Post|Put|Delete|Patch|Request)Mapping", s):
                    endpoints.append(s[:100])
            block_parts = []
            if endpoints:
                block_parts.append("API 端点（前端请对齐这些后端实际暴露的 URL/方法）:\n" + "\n".join(f"  {e}" for e in endpoints[:30]))
            if sigs:
                block_parts.append("方法/类签名:\n" + "\n".join(sigs[:30]))
            if block_parts:
                pred_blocks.append(f"### 来自 {dep_id} 的产出契约:\n" + "\n".join(block_parts))
        if pred_blocks:
            st.context_snippets = (getattr(st, "context_snippets", "") or "") + marker + "\n\n".join(pred_blocks)
            logger.info("[DISPATCH] 跨子任务上下文：已为 %s 注入 %d 个前序产出签名", st.id, len(pred_blocks))


def _feedback_to_knowledge(project_id: str, subtask, worker_output) -> None:
    """事实库回灌：子任务产出的变更文件 → knowledge updater 增量索引（best-effort，不阻塞）。

    补"worker 产出不回灌"的断裂——worker 在沙箱建文件 pull-back 到本地但不 git push，
    knowledge 收不到事件 → 事实库滞后 → 下个任务核验"文件在不在"误判。这里 DONE 后直接喂。
    只索引本子任务变更的少数文件（updater 本就支持文件级增量），轻量。异步 fire-and-forget。
    """
    if not project_id:
        return
    try:
        import re

        from swarm.knowledge.updater import ChangeType, FileChange, UpdateEvent

        diff = worker_output.diff or ""
        changes: list = []
        for m in re.finditer(r"^\+\+\+ b/(\S+)", diff, re.MULTILINE):
            fpath = m.group(1)
            seg_start = m.start()
            prev = diff.rfind("--- ", 0, seg_start)
            is_new = prev >= 0 and "/dev/null" in diff[prev:seg_start]
            changes.append(FileChange(
                file_path=fpath,
                change_type=ChangeType.ADDED if is_new else ChangeType.MODIFIED,
            ))
        if not changes:
            return
        event = UpdateEvent(
            project_id=project_id,
            task_id=getattr(subtask, "id", None),
            changes=changes,
            metadata={"source": "worker_feedback"},
        )

        async def _run():
            try:
                from swarm.knowledge.hooks import enqueue_kb_update
                await enqueue_kb_update(event)
            except Exception as exc:  # noqa: BLE001
                logger.debug("[DISPATCH] 知识库回灌入队失败(非致命): %s", exc)

        # fire-and-forget：入队异步消费，不阻塞主流程
        import asyncio
        try:
            loop = asyncio.get_running_loop()
            # H9 修复：持任务强引用 + 完成时移除，防 asyncio 弱引用下被 GC 静默丢失回灌
            _t = loop.create_task(_run())
            _BG_TASKS.add(_t)
            _t.add_done_callback(_BG_TASKS.discard)
        except RuntimeError:
            pass
        logger.info("[DISPATCH] 事实库回灌：%d 个变更文件入队增量索引（子任务 %s）",
                    len(changes), getattr(subtask, "id", "?"))
    except Exception as exc:  # noqa: BLE001
        logger.debug("[DISPATCH] 知识库回灌跳过(非致命): %s", exc)


async def dispatch(state: BrainState) -> dict:
    """DISPATCH 节点 — 将就绪的子任务派发给 Worker

    输入: plan, dispatch_remaining, subtask_results, knowledge_context
    输出: subtask_results, dispatch_remaining
    """
    plan_obj = state.get("plan")
    if plan_obj is None:
        logger.error("[DISPATCH] 没有执行计划")
        return {"dispatch_remaining": []}

    subtask_results: dict = state.get("subtask_results", {})
    dispatch_remaining: list = state.get("dispatch_remaining", [])
    knowledge_context = state.get("knowledge_context", {})

    # 如果是首次进入 dispatch，初始化 dispatch_remaining
    if not dispatch_remaining and not subtask_results:
        dispatch_remaining = [t.id for t in plan_obj.subtasks]

    # audit #19：重入防护——dispatch_remaining 为空但仍有"既未完成、也不在 remaining"
    # 的子任务时（理论上不该出现，但 handle_failure/rebase 等异常路径可能造成），
    # 把这些遗漏子任务补回 remaining，避免直接跳过派发导致任务卡死/漏做。
    _completed = set(subtask_results.keys())
    if not dispatch_remaining:
        _orphaned = [t.id for t in plan_obj.subtasks if t.id not in _completed]
        if _orphaned:
            logger.warning(
                "[DISPATCH] 检测到 %d 个未完成但不在 remaining 的子任务，补回派发队列: %s",
                len(_orphaned), _orphaned,
            )
            dispatch_remaining = _orphaned

    completed_ids = set(subtask_results.keys())
    config = get_config()
    max_concurrent = config.worker.max_concurrent

    to_dispatch = plan_obj.get_dispatch_batch(
        completed_ids, dispatch_remaining, max_concurrent
    )

    # ── 跨子任务上下文传递(B3 配套)：把【前序已完成依赖子任务】的产出注入后序子任务，
    # 让后序看到前序定义的真实接口签名/新建符号，避免接口对不上（依赖序拆分场景）。
    _inject_predecessor_context(to_dispatch, plan_obj, subtask_results)

    logger.info(
        f"[DISPATCH] 派发 {len(to_dispatch)} 个子任务（并行批次） "
        f"(已完成={len(completed_ids)}, 剩余={len(dispatch_remaining)})"
    )

    if not to_dispatch:
        return {
            "subtask_results": subtask_results,
            "dispatch_remaining": dispatch_remaining,
        }

    project_id = state.get("project_id", "")
    task_id = state.get("task_id", "")

    # 注：原先这里调用 SandboxPool(...).warmup(project_id) 做"预热"，但那是
    # 失效死代码——每次都 new 一个临时 SandboxPool，warmup 把沙箱塞进它的 _pool
    # 后实例即被 GC，远端沙箱却永不回收 → 每次 dispatch 必产生 1 个孤儿沙箱。
    # 而真正的 worker 走 executor 的 create 路径，从不 acquire 这个池。
    # 预热既无收益又泄漏，直接移除。如需预热，应由长生命周期的单例池统一管理。

    use_alternate = bool(state.get("use_alternate_model", False))
    shared_contract = state.get("shared_contract") or (
        plan_obj.shared_contract if plan_obj else {}
    )
    # 主力并行轮转：把本批子任务按序轮转分配到 worker_parallel_pool 里的本地主力，
    # 让两个能力相当的本地主力(Qwen3.6-40B-Claude/MiniMax)同时干、分散负载、产出更快。
    # 轮转不覆盖 alternate(失败重试)；池空则不轮转(按 difficulty 路由)。
    _pool = list(getattr(config.worker, "worker_parallel_pool", []) or [])
    _force_strong = state.get("subtask_force_strong") or {}  # FINDING-12

    async def _run_one(subtask: SubTask, idx: int = 0) -> tuple[SubTask, WorkerOutput | Exception]:
        # FINDING-12：拒答/步数耗尽子任务强制走【最强模型】(routing_complex=40B 256k)，不走 alternate
        # 也不走轮转池——小模型 agent 循环不收敛，最强模型最能在步数内完成。
        _fs = bool(_force_strong.get(subtask.id))
        _ua = use_alternate and not _fs
        _override = _pool[idx % len(_pool)] if (_pool and not _ua) else None
        if _fs:
            _override = config.model.routing_complex
        try:
            output = await nodes._dispatch_to_worker(
                subtask,
                knowledge_context,
                project_id=project_id,
                task_id=task_id,
                use_alternate=_ua,
                user_profile_prompt=_worker_profile_prompt(state),
                shared_contract=shared_contract,
                model_override=_override,
            )
            return subtask, output
        except Exception as e:
            return subtask, e

    outcomes = await asyncio.gather(*[_run_one(st, i) for i, st in enumerate(to_dispatch)])

    def _worker_batch_context() -> dict:
        lines: list[str] = []
        for st, oc in outcomes:
            if isinstance(oc, WorkerOutput):
                summary = (oc.summary or "")[:120]
                l1 = "通过" if oc.l1_passed else "未通过"
                lines.append(f"{st.id}: {summary} (L1={l1}, diff={len(oc.diff or '')} chars)")
            elif isinstance(oc, Exception):
                lines.append(f"{st.id}: 执行异常 — {str(oc)[:100]}")
        if not lines:
            return {}
        return touch_context(
            state,
            "worker_batch",
            "\n".join(lines),
            priority=PRIORITY_WORKER,
        )

    worker_ctx = _worker_batch_context()

    # 收集整批结果 —— 不再遇到首个失败就 return，避免丢弃同批已完成的兄弟结果
    failed_ids = list(state.get("failed_subtask_ids", []))
    for subtask, outcome in outcomes:
        if isinstance(outcome, Exception):
            logger.error(f"[DISPATCH] 子任务 {subtask.id} 执行失败: {outcome}")
            subtask_results[subtask.id] = WorkerOutput(
                subtask_id=subtask.id,
                diff="",
                summary=f"执行失败: {outcome}",
                confidence=Confidence.LOW,
                l1_passed=False,
                l1_details={"error": str(outcome)},
            )
            if subtask.id not in failed_ids:
                failed_ids.append(subtask.id)
            if subtask.id in dispatch_remaining:
                dispatch_remaining.remove(subtask.id)
            continue

        worker_output = outcome
        subtask_results[subtask.id] = worker_output
        if subtask.id in dispatch_remaining:
            dispatch_remaining.remove(subtask.id)
        logger.info(
            f"[DISPATCH] 子任务 {subtask.id} 完成 "
            f"(L1={'通过' if worker_output.l1_passed else '未通过'}, "
            f"diff={len(worker_output.diff or '')} chars)"
        )
        if not _diff_has_changes(worker_output.diff or "") or not worker_output.l1_passed:
            if subtask.id not in failed_ids:
                failed_ids.append(subtask.id)
        else:
            # 事实库回灌（补滞后断裂）：子任务 L1 通过 + 有改动 → 把变更文件喂 knowledge updater
            # 增量索引，让后续子任务/任务的事实核验能看到最新产出（worker 不 git push，否则知识库永远不知）。
            _feedback_to_knowledge(state.get("project_id", ""), subtask, worker_output)

    result: dict = {
        "subtask_results": subtask_results,
        "dispatch_remaining": dispatch_remaining,
        **worker_ctx,
    }
    # H3 修复：永远回填 failed_subtask_ids（空也回填）。state 无 reducer(last-write-wins)，
    # 若仅非空时返回，上一轮失败列表会残留 → gates 误拒真正成功的运行。
    result["failed_subtask_ids"] = failed_ids
    return result


def monitor(state: BrainState) -> dict:
    """MONITOR 节点 — 监控执行进度，检查是否还有下游/有无失败

    输入: dispatch_remaining, subtask_results, failed_subtask_ids
    输出: 无状态变更，仅作为路由判断节点
    """
    dispatch_remaining = state.get("dispatch_remaining", [])
    subtask_results: dict = state.get("subtask_results", {})
    failed_ids = state.get("failed_subtask_ids", [])

    logger.info(
        f"[MONITOR] 剩余={len(dispatch_remaining)}, "
        f"已完成={len(subtask_results)}, 失败={len(failed_ids)}"
    )

    # 此节点不做状态变更，仅用于条件路由
    return {}
