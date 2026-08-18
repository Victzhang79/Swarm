"""Brain 摄取节点 — 多模态需求摄取（设计 v3 B批3，前置于 analyze）。

把任务创建时上传的文件（文档/图片/扫描件）解析理解，产出需求草稿并入 task_description，
供下游 analyze/clarify 使用。

设计约束：
  - 前置于 analyze（B.1）：graph 入口 ingest → analyze。
  - 无上传文件 → no-op 直通（对现有纯文字任务零影响）。
  - 幂等：ingest_done=True 时跳过（避免 replan 循环重复摄取）。
  - 文档走确定性解析（ingest.py），图片/扫描件走多模态理解（vision_ingest.py）。
  - 防幻觉（B.2）：AI 视觉理解标 confirmed=False，记入 ingest_vision_pending 待 clarify 确认。
  - 预算（B.4 消费 A.5）：草稿按目标模型 context_window 控制注入量。
  - 失败降级：摄取异常不阻断主流程（记 ingest_errors，继续用原 task_description）。
"""

from __future__ import annotations

import logging

from swarm.brain.state import BrainState

logger = logging.getLogger(__name__)


async def ingest(state: BrainState) -> dict:
    """摄取节点：解析上传文件 → 需求草稿 → 并入 task_description。

    无文件或已摄取过 → 直通。任何异常都降级（不阻断），保证主流程健壮。
    """
    # 幂等：已摄取过则跳过（replan 循环回到入口时不重复摄取）
    if state.get("ingest_done"):
        return {}

    uploaded = state.get("uploaded_files") or []
    # #5(b) LFI 说明：untrusted 边界是 create_task（唯一接收客户端 uploaded_files 的 API 入口，
    # 已在那里 fail-closed 复核 uploads 归属；retry 复用原已校验路径）。ingest_node 是内部节点，
    # 设计上可摄取本地路径（单元/程序化调用），此处【不再】硬过滤，避免误伤合法本地文件摄取——
    # 信任边界应设在不可信输入的入口，而非内部节点。
    if not uploaded:
        # 无上传文件 → no-op 直通（纯文字任务零影响）
        return {"ingest_done": True}

    logger.info("[INGEST] 开始摄取 %d 个上传文件", len(uploaded))

    try:
        return await _run_ingest(state, uploaded)
    except Exception as exc:  # noqa: BLE001
        logger.exception("[INGEST] 摄取失败，降级为原始描述继续")
        return {
            "ingest_done": True,
            "ingest_errors": [f"摄取层异常: {exc}"],
            # ★32 号文 A5-H1★ 整层异常＝**全部**上传文件都没进草稿，是最重的摄取失败形态。
            # 必须进 degraded_reasons（见 _ingest_degraded 的完整论证）。
            "degraded_reasons": [
                f"ingest_partial_failure:layer_error:{len(uploaded)}/{len(uploaded)}"],
        }


async def _run_ingest(state: BrainState, uploaded: list[str]) -> dict:
    import asyncio

    from swarm.brain import ingest as doc_ingest
    from swarm.brain import vision_ingest

    base_text = state.get("task_description", "") or ""
    budget = _ingest_budget()

    # 1) 确定性文档解析（同步，纯 CPU/IO → to_thread 避免阻塞事件循环）
    result = await asyncio.to_thread(
        doc_ingest.ingest_files,
        list(uploaded),
        base_text=base_text,
        context_budget_tokens=budget,
    )

    draft_parts = [result.draft_text] if result.draft_text else []
    errors = list(result.errors)
    vision_pending: list[dict] = []

    # 2) 图片/扫描件多模态理解（B批2）
    auto_confirm = bool(state.get("auto_confirm_vision"))
    needs_vision_docs = [d for d in result.documents if d.needs_vision and not d.error]
    for doc in needs_vision_docs:
        # 找到原始路径（result.documents 只存 filename，从 uploaded 匹配）
        path = _match_path(uploaded, doc.filename)
        if not path:
            errors.append(f"{doc.filename}: 找不到原始文件路径")
            continue
        # B6：走异步版——LLM 调用 await ainvoke 可被 cancel_task 中断（不再 to_thread 里跑到结束占 GPU）。
        vresult = await vision_ingest.understand_file_async(path, doc.kind)
        if vresult.ok:
            # 防幻觉：标 confirmed（auto_confirm 时直接确认，否则待 clarify）
            confirmed = auto_confirm
            vresult.confirmed = confirmed
            draft_parts.append(vision_ingest.annotate_for_draft(vresult))
            if not confirmed:
                vision_pending.append({
                    "filename": vresult.filename,
                    "understanding": vresult.understanding,
                    "model_used": vresult.model_used,
                    "confirmed": False,
                })
        else:
            errors.append(f"{vresult.filename}: {vresult.error}")

    draft = "\n\n".join(p for p in draft_parts if p)

    # 3) 把草稿并入 task_description（下游 analyze/clarify 用增强后的描述）
    out: dict = {
        "ingest_draft": draft,
        "ingest_vision_pending": vision_pending,
        "ingest_errors": errors,
        "ingest_done": True,
    }
    if draft.strip():
        out["task_description"] = draft
    # ★32 号文 A5-H1★ 摄取失败必须进 degraded_reasons（此前只有 ingest_errors，生产侧零消费者）
    _dg = _ingest_degraded(errors, uploaded)
    if _dg:
        out["degraded_reasons"] = _dg
    logger.info(
        "[INGEST] 完成：草稿 %d 字，待确认视觉 %d 项，错误 %d 项",
        len(draft), len(vision_pending), len(errors),
    )
    return out


def _ingest_degraded(errors: list[str], uploaded: list[str]) -> list[str]:
    """★32 号文 A5-H1（唯一 HIGH）：摄取失败 → degraded_reasons（纯函数、可测）★

    **病根**：`ingest_errors` 生产侧【零消费者】（全仓引用只有声明 + docstring + 两处写入，
    唯一"消费者"是 `test_ingest_e2e.py` 里 `assert out.get("ingest_errors")` 的自证式断言），
    且本模块此前**零处**写 `degraded_reasons`。于是这条假 DONE 通道全程无声：

      用户传 PRD（PDF + 截图）→ 若干文件校验失败（超大/不可读）/ 无解析器（.xlsx/.pptx）
      / 解析异常（损坏 PDF）/ 视觉理解失败（图片 PRD 的主力失败面）→ errors 收集但无人消费
      → `task_description` 被【部分】草稿覆盖 → extract_requirements 只看得见解析成功那部分
      → plan 覆盖缩水后的需求 → deliver 逐条对账【对着缩水需求集全绿】→ auto_accept 放行。

    这是"降级路径至少一个机读键 + 一次 WARNING，且该键必须有人消费"的教科书违例：
    WARNING 有（ingest.py 解析失败处）、机读键有（ingest_errors）、**消费者没有**。
    与 norms 层死 12 天同型（`return []` 与"真没有"不可分，那一层能死很久没人知道）。

    ★分档（血规：后果不同必须分档）★ 只收【真失败】。`ingest_vision_pending` 是"待人工确认"
    （非失败，已有真消费者 `planning_nodes.py`）——绝不与失败混成一条，否则正常的
    图片 PRD 流程会被误判成需求蒸发。

    返回 `[]` = 真的没有失败（此时**不写键**，不制造空态粘滞）。
    """
    _errs = [str(e) for e in (errors or []) if str(e).strip()]
    if not _errs:
        return []
    return [f"ingest_partial_failure:{len(_errs)}/{len(uploaded or [])}"]


def _ingest_budget() -> int:
    """摄取草稿的 token 注入预算（消费 A 能力库）。

    复用 planning_nodes 的 _context_budget（读真实模型 context_window×0.75 或兜底），
    但摄取草稿只占整体预算的一部分（留给系统 prompt/知识库），取其一半作上限。
    """
    try:
        from swarm.brain.planning_nodes import _context_budget

        return max(_context_budget() // 2, 8000)
    except Exception:  # noqa: BLE001
        return 8000


def _match_path(uploaded: list[str], filename: str) -> str | None:
    """从上传路径列表里按文件名匹配回原始路径。"""
    from pathlib import Path

    for p in uploaded:
        if Path(p).name == filename:
            return p
    return None
