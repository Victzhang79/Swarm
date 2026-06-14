"""Brain 规划子图节点 — Q4 交互式渐进规划 Agent

逻辑独立的规划子图（非独立进程），集中在本模块便于维护与日志追踪。
节点：clarify(多轮澄清) / assess(澄清后定级) / tech_design(技术方案+接口先行) /
      review_design(人工评审) / elaborate(渐进明细+上下文预算+INVEST 自检)

设计依据：docs/Q4_Planning_Agent_Design.md (v3)。
所有节点遵循 (BrainState) -> dict 约定，返回 dict merge 回 state。
LLM 失败一律降级为"安全继续"（不阻断主流程），与现有节点风格一致。
"""
from __future__ import annotations

import json
import logging
import os

from langgraph.types import interrupt

from swarm.brain.state import BrainState
from swarm.config.settings import get_config
from swarm.types import Complexity

logger = logging.getLogger(__name__)

# ── 复用 nodes.py 的核心辅助（避免重造）──
from swarm.brain.nodes import (  # noqa: E402
    _brain_profile_prompt,
    _get_brain_llm,
    _parse_json_from_llm,
)

# ── 配置常量（带默认，可被 env 覆盖）──
MAX_CLARIFY_ROUNDS = 5          # Q1：自适应轮数封顶
MAX_QUESTIONS_PER_ROUND = 3     # 每轮最多问题数
MAX_DESIGN_REJECTS = 3          # E：评审打回收敛上限
DEFAULT_CONTEXT_BUDGET = 150_000  # Q7：子任务上下文预算（留余量 < 本地小模型 196k）
MAX_ELABORATE_RESPLIT = 3       # 超预算二次拆分上限


def _auto_mode(state: BrainState) -> bool:
    """API/CI 自动化模式：永不交互（澄清/评审走默认假设）。"""
    if state.get("auto_accept"):
        return True
    return os.environ.get("SWARM_AUTO_ACCEPT", "").lower() in ("1", "true", "yes")


def _context_budget() -> int:
    """子任务上下文预算（设计 v3 A.4，诚实分步）。

    预算 = min(实际干活模型真实 context_window × 0.75, 启发式兜底)。
    - 用能力库探测到的真实窗口设上限（消除写死 150k 与真实模型脱钩的债）。
    - 取所有候选 worker 模型里**最小**的窗口（保守：预算须让各难度子任务都装得下）。
    - 无能力数据 / 全是 default 兜底 → 退回写死兜底常量，不假装精确。
    - env 显式设 SWARM_SUBTASK_CONTEXT_BUDGET 时**强制覆盖**（运维逃生口）。

    诚实声明：est_context_tokens 当前仍是启发式粗估（难度基线+文件数×6k），
    本批只把"上限"接到真实窗口；预估精度升级（tokenizer 实算+执行回写校准）
    是后续债，见 docs/Multimodal_Ingestion_Design.md A.5 第二步。
    """
    # env 显式覆盖优先（运维逃生口，向后兼容）
    env_val = os.environ.get("SWARM_SUBTASK_CONTEXT_BUDGET", "").strip()
    if env_val:
        try:
            return int(env_val)
        except (ValueError, TypeError):
            pass

    fallback = DEFAULT_CONTEXT_BUDGET
    real_window = _min_worker_context_window()
    if real_window and real_window > 0:
        # 真实窗口 × 0.75 与兜底取 min（既用真值设上限，又保留保守兜底）
        return min(int(real_window * 0.75), fallback)
    return fallback


def _min_worker_context_window() -> int | None:
    """从能力库取所有候选 worker 模型里最小的真实 context_window。

    候选 = 路由三档 primary + fallback（实际可能干活的模型）。
    只采纳 source != default 的记录（真值/解析/人工）；全是 default 兜底则返回 None
    → 调用方退回写死兜底，不假装精确。
    """
    try:
        from swarm.config.settings import get_config
        from swarm.models import capability_store as cap

        cfg = get_config().model
        candidate_models = [
            cfg.routing_trivial, cfg.routing_trivial_fallback,
            cfg.routing_medium, cfg.routing_medium_fallback,
            cfg.routing_complex, cfg.routing_complex_fallback,
        ]
        candidate_models = [m for m in candidate_models if m]

        windows: list[int] = []
        for model_name in candidate_models:
            pc = cfg.provider_for_model(model_name)
            if pc is None:
                continue
            rec = cap.get_capability(pc.id, model_name)
            # 只信真值（探测/解析/人工），跳过启发式默认与缺失
            if (rec and rec.get("context_window") and rec.get("source") != cap.SOURCE_DEFAULT):
                windows.append(int(rec["context_window"]))
        return min(windows) if windows else None
    except Exception as exc:  # noqa: BLE001
        logger.debug("读能力库取最小窗口失败，回退写死兜底: %s", exc)
        return None


# ══════════════════════════════════════════════
# 节点 1：clarify — 多轮自适应澄清（≤5 轮，滚动摘要）
# ══════════════════════════════════════════════

CLARIFY_SYSTEM = """你是资深技术规划顾问。用户给出一个需求，但描述往往不完整\
（用户是产品视角：知道"要什么"，未必知道"怎么实现"）。你的任务是通过启发式提问\
把需求澄清到"足以做出可靠技术规划"的程度。

规则：
- 每轮最多提 3 个最高价值的问题，按对规划的影响力降序。
- 只问真正影响技术方案/架构/拆解的问题（技术栈倾向、规模、性能要求、关键约束、验收标准等），\
不问无关紧要的细节。
- 每个问题给出 default_if_skipped（用户跳过时的合理默认假设），让规划永不阻塞。
- 评估当前信息是否已足够规划：足够则 done=true、questions=[]。
- 参考已有澄清历史，不要重复问已答过的问题；基于已有答复追问更深的缺口。

严格输出 JSON：
{
  "done": true/false,            // 信息是否已足够做可靠规划
  "reason": "为何足够/还缺什么",
  "questions": [                 // done=false 时给出，≤3 条；done=true 时为 []
    {"q": "问题", "why": "为何影响规划", "default_if_skipped": "用户跳过的默认假设"}
  ]
}"""

CLARIFY_USER = """需求描述：
{task_description}

项目知识库上下文：
{knowledge}

已有澄清历史（轮次/问题/答复）：
{history}

当前是第 {round} 轮（最多 {max_rounds} 轮）。请评估信息是否足够，不够则给出本轮问题。"""


async def clarify(state: BrainState) -> dict:
    """多轮自适应澄清节点。

    - 微任务/自动化模式：直接跳过（clarify_done=True）。
    - 每轮：LLM 评估信息是否足够；不够则 interrupt 向人类提问，把答复并入历史 + 滚动摘要。
    - 达上限或 LLM 判定足够或用户跳过 → clarify_done=True，进入 assess。
    返回的 clarify_round 递增，由 graph 的 after_clarify 决定是否再循环。
    """
    if state.get("is_micro_task"):
        return {"clarify_done": True, "clarify_summary": "微任务，跳过澄清。"}
    if _auto_mode(state):
        return {"clarify_done": True, "clarify_summary": "自动化模式，跳过澄清，用默认假设。"}

    rnd = int(state.get("clarify_round", 0))
    history = list(state.get("clarify_history", []))

    if rnd >= MAX_CLARIFY_ROUNDS:
        logger.info("[CLARIFY] 达轮数上限 %d，结束澄清", MAX_CLARIFY_ROUNDS)
        return {"clarify_done": True}

    # ── LLM 评估本轮是否需要提问 ──
    # 注：本节点采用 LangGraph interrupt。为避免 resume 重跑时 LLM 二次判断丢弃用户答复，
    # LLM 调用结果（问题列表）必须在 interrupt 前确定。LangGraph 会缓存 interrupt 前的
    # task 输出，resume 时 interrupt() 直接返回 resume 值并继续 interrupt 之后的代码，
    # 故 history 记录（interrupt 之后）能正确拿到答复。
    knowledge_prompt = _format_knowledge(state)
    history_prompt = _format_clarify_history(history) or "（无）"
    # B 部分：有待确认的 AI 视觉理解 → 提示 LLM 优先确认（防幻觉，B.2）。
    vision_pending = state.get("ingest_vision_pending") or []
    if vision_pending:
        vlines = "\n".join(
            f"- 文件「{v.get('filename')}」AI 理解为：{v.get('understanding', '')[:200]}"
            for v in vision_pending
        )
        knowledge_prompt = (
            f"{knowledge_prompt}\n\n"
            f"【⚠️ 以下是 AI 对上传图片/扫描件的视觉理解，尚未经用户确认，"
            f"请优先生成问题让用户核对其准确性】：\n{vlines}"
        )
    try:
        llm = _get_brain_llm()
        resp = await llm.ainvoke([
            {"role": "system", "content": CLARIFY_SYSTEM},
            {"role": "user", "content": CLARIFY_USER.format(
                task_description=state.get("task_description", ""),
                knowledge=knowledge_prompt,
                history=history_prompt,
                round=rnd + 1,
                max_rounds=MAX_CLARIFY_ROUNDS,
            )},
        ])
        result = _parse_json_from_llm(resp.content)
    except Exception as exc:  # noqa: BLE001
        logger.warning("[CLARIFY] LLM 失败，跳过澄清安全继续: %s", exc)
        return {"clarify_done": True}

    if result.get("done") or not result.get("questions"):
        logger.info("[CLARIFY] LLM 判定信息已足够（轮次 %d）", rnd)
        return {"clarify_done": True}

    questions = (result.get("questions") or [])[:MAX_QUESTIONS_PER_ROUND]

    # ── interrupt 向人类提问（resume 后此处直接返回答复）──
    answer = interrupt({
        "type": "clarify",
        "task_id": state.get("task_id"),
        "task_description": state.get("task_description"),
        "round": rnd + 1,
        "max_rounds": MAX_CLARIFY_ROUNDS,
        "questions": questions,
        "message": f"规划前需要澄清（第 {rnd + 1}/{MAX_CLARIFY_ROUNDS} 轮，可逐条回答，也可整体跳过用默认假设）。",
    })

    # 用户整体跳过
    if isinstance(answer, dict) and answer.get("action") == "skip":
        logger.info("[CLARIFY] 用户跳过，用默认假设继续")
        return {
            "clarify_done": True,
            "clarify_summary": (state.get("clarify_summary") or "") + "\n[用户跳过剩余澄清，采用默认假设]",
        }

    answers = answer if isinstance(answer, dict) else {}
    history.append({"round": rnd + 1, "questions": questions, "answers": answers})
    new_summary = _roll_clarify_summary(state.get("clarify_summary", ""), rnd + 1, questions, answers)

    # 递增轮次；clarify_done=False → after_clarify 回到 clarify 评估下一轮
    return {
        "clarify_round": rnd + 1,
        "clarify_history": history,
        "clarify_summary": new_summary,
        "clarify_done": False,
    }


def _roll_clarify_summary(prev: str, rnd: int, questions: list, answers: dict) -> str:
    """滚动摘要（C）：把本轮问答压成简短条目追加，避免上下文无限堆积。"""
    lines = [prev] if prev else []
    lines.append(f"[第{rnd}轮]")
    for i, q in enumerate(questions):
        qa = q.get("q", "") if isinstance(q, dict) else str(q)
        ans = ""
        if isinstance(answers, dict):
            ans = answers.get(str(i)) or answers.get(i) or answers.get(qa) or ""
        if not ans and isinstance(q, dict):
            ans = f"(默认){q.get('default_if_skipped', '')}"
        lines.append(f"  Q:{qa} → A:{ans}")
    return "\n".join(lines)


def _format_clarify_history(history: list) -> str:
    if not history:
        return ""
    out = []
    for h in history:
        out.append(f"第{h.get('round')}轮:")
        for i, q in enumerate(h.get("questions", [])):
            qa = q.get("q", "") if isinstance(q, dict) else str(q)
            ans = (h.get("answers") or {}).get(str(i)) or (h.get("answers") or {}).get(qa) or "(未答)"
            out.append(f"  Q:{qa} A:{ans}")
    return "\n".join(out)


def _format_knowledge(state: BrainState) -> str:
    """格式化项目知识库上下文供规划用（复用现有 service）。"""
    try:
        from swarm.knowledge.service import format_brain_knowledge_prompt
        return format_brain_knowledge_prompt(
            state.get("knowledge_context", {}), state.get("task_description", "")
        ) or "（无项目知识库上下文）"
    except Exception:  # noqa: BLE001
        return "（无项目知识库上下文）"


# ══════════════════════════════════════════════
# 节点 2：assess — 澄清后定级（Q2 复杂度后置）
# ══════════════════════════════════════════════

ASSESS_SYSTEM = """你是资深技术架构师。基于【澄清后的完整需求信息】+ 项目知识库，\
评估这个任务的真实复杂度。注意：用户最初的描述可能不准确，要以澄清后的信息为准。

复杂度分级：
- simple：单点改动、无架构影响、单文件/少量改动（如改文案、调样式、加一个小函数）。
- medium：多文件协作、有一定逻辑、但不涉及架构决策或新技术选型。
- complex：跨模块、需技术方案/选型、多个相互依赖的子任务。
- ultra：新建项目级别、或大规模重构、或高风险架构变更。

严格输出 JSON：
{
  "complexity": "simple|medium|complex|ultra",
  "reason": "定级理由（基于澄清后信息）",
  "needs_tech_design": true/false   // complex/ultra 或新建项目通常 true
}"""

ASSESS_USER = """原始需求：
{task_description}

澄清摘要（多轮问答结论）：
{clarify_summary}

是否新建项目（greenfield）：{greenfield}

项目知识库上下文：
{knowledge}

请定级并判断是否需要技术方案。"""


async def assess(state: BrainState) -> dict:
    """澄清后基于完整信息 + 知识库定真复杂度（覆盖 analyze 初判）。"""
    if state.get("is_micro_task"):
        return {"assessed_complexity": Complexity.SIMPLE}

    greenfield = bool((state.get("session_metadata") or {}).get("greenfield"))
    try:
        llm = _get_brain_llm()
        resp = await llm.ainvoke([
            {"role": "system", "content": ASSESS_SYSTEM},
            {"role": "user", "content": ASSESS_USER.format(
                task_description=state.get("task_description", ""),
                clarify_summary=state.get("clarify_summary", "") or "（无澄清）",
                greenfield="是" if greenfield else "否",
                knowledge=_format_knowledge(state),
            )},
        ])
        result = _parse_json_from_llm(resp.content)
        comp_str = str(result.get("complexity", "medium")).lower()
        comp = {
            "simple": Complexity.SIMPLE, "medium": Complexity.MEDIUM,
            "complex": Complexity.COMPLEX, "ultra": Complexity.ULTRA,
        }.get(comp_str, Complexity.MEDIUM)
        # 新建项目至少 complex（需技术方案）
        if greenfield and comp in (Complexity.SIMPLE, Complexity.MEDIUM):
            comp = Complexity.COMPLEX
        logger.info("[ASSESS] 澄清后定级: %s (%s)", comp.value, result.get("reason", "")[:60])
        return {"assessed_complexity": comp, "complexity": comp}
    except Exception as exc:  # noqa: BLE001
        logger.warning("[ASSESS] LLM 失败，沿用 analyze 初判: %s", exc)
        return {"assessed_complexity": state.get("complexity", Complexity.MEDIUM)}


# ══════════════════════════════════════════════
# 节点 3：tech_design — 技术方案 + 接口先行（Q6/B）
# ══════════════════════════════════════════════

TECH_DESIGN_SYSTEM = """你是资深技术负责人。基于澄清后的完整需求 + 项目知识库，\
产出一份可评审的技术方案。

要求：
- 新建项目：给出技术栈选型（前端/后端/存储）及理由。
- 既有项目：沿用既有技术栈（从知识库判断），重点评估【变更对原功能的影响】和【可维护性】。
- 必含：架构概述、数据模型（用文字描述的 ER/结构，可 mermaid）、业务流程（文字描述的流程，可 mermaid）、\
关键风险、注意事项、验收标准、代码注释要求。
- 接口先行：定义本任务的共享契约（API schema / 关键数据结构），供后续并行子任务作稳定前置。

严格输出 JSON：
{
  "stack": {"frontend": "...", "backend": "...", "storage": "...", "rationale": "选型理由（新建项目）/沿用说明（既有项目）"},
  "architecture": "架构概述",
  "data_model_diagram": "数据模型（文字/mermaid）",
  "flow_diagram": "业务流程（文字/mermaid）",
  "risks": ["风险1", "风险2"],
  "notes": ["注意事项1", "注意事项2"],
  "acceptance": ["验收标准1", "验收标准2"],
  "change_impact": "变更对原功能的影响评估（既有项目；新建项目填'新建，无存量影响'）",
  "maintainability": "可维护性考量",
  "comment_requirements": "代码注释要求（保证易读）",
  "shared_contract": {"apis": [...], "data_structures": [...]}
}"""

TECH_DESIGN_USER = """需求：
{task_description}

澄清摘要：
{clarify_summary}

复杂度：{complexity}　是否新建项目：{greenfield}

项目知识库（既有技术栈/结构）：
{knowledge}

{review_feedback}请产出技术方案。"""


async def tech_design(state: BrainState) -> dict:
    """产出技术方案 + 共享契约草案。打回重做时带上评审反馈。"""
    greenfield = bool((state.get("session_metadata") or {}).get("greenfield"))
    prev_review = state.get("design_review") or {}
    review_feedback = ""
    if prev_review.get("decision") == "reject" and prev_review.get("feedback"):
        review_feedback = f"【上一版被打回，评审反馈】{prev_review.get('feedback')}\n请据此改进。\n\n"

    comp = state.get("assessed_complexity") or state.get("complexity", Complexity.MEDIUM)
    comp_str = comp.value if hasattr(comp, "value") else str(comp)
    try:
        llm = _get_brain_llm()
        resp = await llm.ainvoke([
            {"role": "system", "content": TECH_DESIGN_SYSTEM},
            {"role": "user", "content": TECH_DESIGN_USER.format(
                task_description=state.get("task_description", ""),
                clarify_summary=state.get("clarify_summary", "") or "（无澄清）",
                complexity=comp_str,
                greenfield="是" if greenfield else "否",
                knowledge=_format_knowledge(state),
                review_feedback=review_feedback,
            )},
        ])
        result = _parse_json_from_llm(resp.content)
        contract = result.pop("shared_contract", {}) if isinstance(result, dict) else {}
        logger.info("[TECH_DESIGN] 技术方案已产出 (stack=%s)", (result.get("stack") or {}).get("backend", "?"))
        return {"tech_design": result, "shared_contract_draft": contract or {}}
    except Exception as exc:  # noqa: BLE001
        logger.warning("[TECH_DESIGN] LLM 失败，产出空方案安全继续: %s", exc)
        return {"tech_design": {"architecture": "（自动生成失败，降级直接规划）", "risks": [], "notes": []}, "shared_contract_draft": {}}


# ══════════════════════════════════════════════
# 节点 4：review_design — 人工评审（Q5/E）
# ══════════════════════════════════════════════


async def review_design(state: BrainState) -> dict:
    """技术方案人工评审。
    - 自动化模式：自动通过。
    - 打回次数达上限：强制通过（防无限循环），标记需人工关注。
    - 否则 interrupt 等人类 approve/reject(带反馈)。
    """
    prev = state.get("design_review") or {}
    reject_count = int(prev.get("reject_count", 0))

    if _auto_mode(state):
        return {"design_review": {"decision": "approve", "feedback": "自动化模式自动通过", "reject_count": reject_count}}

    if reject_count >= MAX_DESIGN_REJECTS:
        logger.warning("[REVIEW_DESIGN] 打回达上限 %d，强制通过并标记需人工关注", MAX_DESIGN_REJECTS)
        return {"design_review": {"decision": "approve", "feedback": f"打回{reject_count}次达上限，强制继续", "reject_count": reject_count, "forced": True}}

    decision = interrupt({
        "type": "review_design",
        "task_id": state.get("task_id"),
        "tech_design": state.get("tech_design"),
        "shared_contract": state.get("shared_contract_draft"),
        "reject_count": reject_count,
        "message": "请评审技术方案：通过则进入任务拆解，打回请填写反馈（最多打回 3 次）。",
    })

    if isinstance(decision, dict) and decision.get("decision") == "reject":
        fb = decision.get("feedback", "")
        logger.info("[REVIEW_DESIGN] 方案被打回（第 %d 次）: %s", reject_count + 1, fb[:60])
        return {"design_review": {"decision": "reject", "feedback": fb, "reject_count": reject_count + 1}}

    logger.info("[REVIEW_DESIGN] 方案通过")
    return {"design_review": {"decision": "approve", "feedback": (decision or {}).get("feedback", "") if isinstance(decision, dict) else "", "reject_count": reject_count}}


# ══════════════════════════════════════════════
# 节点 5：elaborate — 渐进明细 + 上下文预算 + INVEST 自检（Q7/A）
# ══════════════════════════════════════════════


def _decouple_independent_subtasks(plan_obj) -> int:
    """剥离 LLM 误加的【假 depends_on】，提升并行度（I6，原地修改 plan_obj.subtasks）。

    背景：dispatch 用 depends_on DAG 决定并行（get_dispatch_batch：依赖全完成才就绪）。
    parallel_groups 的过度串行已被 get_dispatch_batch 绕过，但 depends_on 本身是硬约束——
    LLM 常给本可并行的独立子任务加无谓 depends_on（如"先建 utils 再写 service"，但 service
    根本不碰 utils 的文件、不引用其契约），导致无谓串行。

    判定一条 depends_on 是【假依赖】需【同时】满足（保守，宁可漏剥不可误剥）：
      1. 被依赖任务的写文件 ∩ 当前任务的(读∪写文件) = ∅（当前任务完全不碰它产出/改动的文件）
      2. 当前任务 contract 为空 或 被依赖任务 contract 为空（无跨任务接口契约耦合）
      3. 两者都不是 allow_any（allow_any 边界不可判定，保守保留依赖）
    真依赖（文件重叠 / 契约耦合 / allow_any）一律保留。merge 的冲突检测是最终兜底。

    Returns: 剥离的假依赖条数。
    """
    subtasks = getattr(plan_obj, "subtasks", None)
    if not subtasks:
        return 0
    by_id = {st.id: st for st in subtasks}

    def _write_set(st) -> set[str]:
        sc = getattr(st, "scope", None)
        if sc is None:
            return set()
        return set(getattr(sc, "writable", []) or []) | set(getattr(sc, "create_files", []) or []) | set(getattr(sc, "delete_files", []) or [])

    def _touch_set(st) -> set[str]:
        sc = getattr(st, "scope", None)
        if sc is None:
            return set()
        return _write_set(st) | set(getattr(sc, "readable", []) or [])

    def _allow_any(st) -> bool:
        sc = getattr(st, "scope", None)
        return bool(getattr(sc, "allow_any", False)) if sc else False

    removed = 0
    for st in subtasks:
        deps = list(getattr(st, "depends_on", []) or [])
        if not deps:
            continue
        kept: list[str] = []
        cur_touch = _touch_set(st)
        cur_contract = dict(getattr(st, "contract", {}) or {})
        for dep_id in deps:
            dep = by_id.get(dep_id)
            if dep is None:
                kept.append(dep_id)  # 悬空依赖 ID 保留（不臆断）
                continue
            # 条件3：任一 allow_any → 保留
            if _allow_any(st) or _allow_any(dep):
                kept.append(dep_id)
                continue
            # 条件1：文件重叠 → 真依赖，保留
            if _write_set(dep) & cur_touch:
                kept.append(dep_id)
                continue
            # 条件2：双方都有 contract → 可能契约耦合，保守保留
            if cur_contract and dict(getattr(dep, "contract", {}) or {}):
                kept.append(dep_id)
                continue
            # 三条件均不构成真依赖 → 判定为假依赖，剥离
            removed += 1
            logger.info("[ELABORATE] 剥离假依赖: %s ⊥ %s（零文件重叠+无契约耦合，可并行）", st.id, dep_id)
        if len(kept) != len(deps):
            st.depends_on = kept
    if removed:
        logger.info("[ELABORATE] 共剥离 %d 条假依赖，提升并行度", removed)
    return removed


async def elaborate(state: BrainState) -> dict:
    """渐进明细：对超上下文预算 / INVEST 不过的子任务做二次 LLM 拆分（打回循环），
    直到每个子任务都在预算内且可独立验证，或达拆分上限（标记 oversized 供人工介入）。

    Q7 上下文预算硬约束 + A INVEST 自检。拆分上限 MAX_ELABORATE_RESPLIT 防无限拆。
    """
    plan_obj = state.get("plan")
    if not plan_obj or not getattr(plan_obj, "subtasks", None):
        return {"plan_elaborated": True}

    budget = _context_budget()
    invest_fail = 0
    resplit_rounds = 0
    # 多轮：每轮找出"需再拆"的子任务，二次拆分替换，重新检查
    while resplit_rounds < MAX_ELABORATE_RESPLIT:
        need_resplit = [st for st in plan_obj.subtasks if _needs_resplit(st, budget)]
        if not need_resplit:
            break
        resplit_rounds += 1
        new_subtasks = list(plan_obj.subtasks)
        changed = False
        for st in need_resplit:
            children = await _resplit_subtask(st, state, budget)
            if children and len(children) > 1:
                idx = next((i for i, x in enumerate(new_subtasks) if x.id == st.id), None)
                if idx is not None:
                    new_subtasks[idx:idx + 1] = children
                    changed = True
                    logger.info("[ELABORATE] 子任务 %s 二次拆分为 %d 个", st.id, len(children))
        if not changed:
            break  # LLM 拆不动了，避免空转
        plan_obj = _rebuild_plan(plan_obj, new_subtasks)

    # ── I6：剥离 LLM 误加的假 depends_on，提升 dispatch 并行度 ──
    decoupled = _decouple_independent_subtasks(plan_obj)

    # 最终检查：仍超预算/缺验收的标记出来（人工介入信号）
    oversized: list[str] = []
    for st in plan_obj.subtasks:
        est = getattr(st, "est_context_tokens", 0) or 0
        if est > budget:
            oversized.append(st.id)
        if not getattr(st, "acceptance_criteria", None):
            invest_fail += 1

    if oversized:
        logger.warning("[ELABORATE] %d 个子任务拆分后仍超预算 %d（需人工重新切分需求）: %s",
                       len(oversized), budget, oversized)

    # ── 可观测：规划决策上报 LangSmith（tracing 关闭 no-op）──
    try:
        from swarm.tracing import push_planning_feedback
        comp = state.get("assessed_complexity") or state.get("complexity")
        review = state.get("design_review") or {}
        push_planning_feedback({
            "clarify_rounds": int(state.get("clarify_round", 0)),
            "clarify_max": MAX_CLARIFY_ROUNDS,
            "clarify_skipped": bool(state.get("clarify_done") and not state.get("clarify_history")),
            "assessed_complexity": comp.value if hasattr(comp, "value") else (comp or None),
            "design_review_decision": review.get("decision"),
            "design_reject_count": review.get("reject_count", 0),
            "subtask_count": len(plan_obj.subtasks),
            "milestone_count": len(state.get("plan_milestones") or []) or 1,
            "oversized_count": len(oversized),
            "invest_fail_count": invest_fail,
            "resplit_rounds": resplit_rounds,
        })
    except Exception as exc:  # noqa: BLE001
        logger.debug("[ELABORATE] planning feedback skipped: %s", exc)

    # ── 持久化(F)：规划产物写 store，任务详情可回看 ──
    _persist_planning_artifacts(state)

    out: dict = {
        "plan_elaborated": True,
        "oversized_subtask_ids": oversized,
        "invest_fail_count": invest_fail,
    }
    if resplit_rounds > 0 or decoupled > 0:
        # 拆分或剥离假依赖改变了 plan，回写
        out["plan"] = plan_obj
    return out


def _needs_resplit(st, budget: int) -> bool:
    """子任务是否需二次拆分：超上下文预算（INVEST 缺验收不强制拆，仅标记）。"""
    est = getattr(st, "est_context_tokens", 0) or 0
    return est > budget


async def _resplit_subtask(st, state: BrainState, budget: int) -> list:
    """调 LLM 把一个超预算子任务拆成 2-4 个更小的、各自在预算内的子任务。

    失败/拆不动时返回 [原子任务]（不阻断）。新子任务继承原 scope/依赖，id 加后缀。
    """
    from swarm.types import FileScope, SubTask, SubTaskDifficulty, SubTaskModality
    try:
        llm = _get_brain_llm()
        est = getattr(st, "est_context_tokens", 0) or 0
        resp = await llm.ainvoke([
            {"role": "system", "content": RESPLIT_SYSTEM.format(budget=budget)},
            {"role": "user", "content": RESPLIT_USER.format(
                desc=getattr(st, "description", ""),
                est=est,
                budget=budget,
                files=", ".join(getattr(getattr(st, "scope", None), "writable", []) or []) or "（无）",
            )},
        ])
        result = _parse_json_from_llm(resp.content)
        subs = result.get("subtasks") or []
        if len(subs) < 2:
            return [st]
        children = []
        base_scope = getattr(st, "scope", None) or FileScope(writable=[], readable=[])
        for i, s in enumerate(subs[:4]):
            children.append(SubTask(
                id=f"{st.id}-{i + 1}",
                description=s.get("description", "")[:500],
                difficulty=getattr(st, "difficulty", SubTaskDifficulty.MEDIUM),
                modality=getattr(st, "modality", SubTaskModality.TEXT),
                scope=base_scope,
                depends_on=list(getattr(st, "depends_on", []) or []) + (
                    [f"{st.id}-{i}"] if i > 0 else []  # 子任务间默认串行(保守，避免同 scope 并行写冲突)
                ),
                acceptance_criteria=s.get("acceptance_criteria", []) or [],
                est_context_tokens=int(s.get("est_context_tokens", budget // 2) or budget // 2),
            ))
        return children
    except Exception as exc:  # noqa: BLE001
        logger.warning("[ELABORATE] 子任务 %s 二次拆分失败，保留原样: %s", getattr(st, "id", "?"), exc)
        return [st]


def _rebuild_plan(plan_obj, new_subtasks):
    """用新子任务列表重建 TaskPlan，保留 shared_contract，parallel_groups 失效用空(依赖驱动调度)。"""
    from swarm.types import TaskPlan
    return TaskPlan(
        subtasks=new_subtasks,
        parallel_groups=[],  # 拆分后旧分组失效；依赖驱动调度不需要它
        shared_contract=getattr(plan_obj, "shared_contract", {}) or {},
    )


RESPLIT_SYSTEM = """你是任务拆解专家。一个子任务预估执行上下文超过预算({budget} tokens)，\
说明它太大，本地小模型做不完会上下文爆炸。把它拆成 2-4 个更小的、各自上下文在预算内、\
可独立验证的子任务。每个子任务必须单一职责、有明确验收标准。

严格输出 JSON：
{{
  "subtasks": [
    {{"description": "子任务描述", "acceptance_criteria": ["验收1"], "est_context_tokens": 数字}}
  ]
}}"""

RESPLIT_USER = """需二次拆分的子任务：
{desc}

预估上下文：{est} tokens（预算 {budget}）
涉及文件：{files}

请拆成 2-4 个各自在预算内的子任务。"""


def _persist_planning_artifacts(state: BrainState) -> None:
    """把澄清历史/技术方案/评审决策持久化到 store（best-effort，失败不阻断）。"""
    task_id = state.get("task_id")
    if not task_id:
        return
    artifacts = {
        "clarify_history": state.get("clarify_history") or [],
        "clarify_summary": state.get("clarify_summary") or "",
        "tech_design": state.get("tech_design") or {},
        "design_review": state.get("design_review") or {},
        "assessed_complexity": (
            state.get("assessed_complexity").value
            if hasattr(state.get("assessed_complexity"), "value")
            else state.get("assessed_complexity")
        ),
    }
    # 仅当有规划产物时才写（微任务/轻量路径无）
    if not (artifacts["clarify_history"] or artifacts["tech_design"]):
        return
    try:
        from swarm.project import store
        fn = getattr(store, "save_planning_artifacts", None)
        if callable(fn):
            fn(task_id, artifacts)
        else:
            logger.debug("[ELABORATE] store.save_planning_artifacts 未定义，跳过持久化")
    except Exception as exc:  # noqa: BLE001
        logger.debug("[ELABORATE] 持久化规划产物失败: %s", exc)
