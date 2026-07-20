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
import re

from langgraph.types import interrupt

from swarm.brain.state import BrainState
from swarm.config.settings import get_config
from swarm.types import Complexity

logger = logging.getLogger(__name__)

# ── 复用 nodes.py 的核心辅助（避免重造）──
from swarm.brain.nodes import (  # noqa: E402
    _brain_profile_prompt,
    _get_brain_llm,
    _get_project_path,
    _parse_json_from_llm,
)
from swarm.brain.nodes.shared import parse_and_validate  # noqa: E402
from swarm.brain.llm_schemas import (  # noqa: E402
    ComplexityAssessmentResponse,
    StackAdjudicateResponse,
    validate_file_plan,
)

# ── 配置常量（带默认，可被 env 覆盖）──
MAX_CLARIFY_ROUNDS = 5          # Q1：自适应轮数封顶
MAX_QUESTIONS_PER_ROUND = 3     # 每轮最多问题数
MAX_DESIGN_REJECTS = 3          # E：评审打回收敛上限
DEFAULT_CONTEXT_BUDGET = 150_000  # Q7：子任务上下文预算（留余量 < 本地小模型 196k）
MAX_ELABORATE_RESPLIT = 3       # 超预算二次拆分上限
# 子任务【跨实体】打包目标文件数。多实体子任务按此把小实体打包成批；但【单个实体的全栈永不
# 被拆穿】(契约自洽优先)，单实体即便超此数也整批原子，靠 A=900s 预算兜底(实测 9 文件≈560s)。
# RUN13(预算)+RUN14(契约漂移)双教训：拆分边界只能落在实体之间，不能落在一个实体的层之间。
MAX_FILES_PER_SUBTASK = 4
# round10 #3 治本：单实体全栈【文件数上界】。RUN14"不拆穿实体"对【正常 6 文件实体】(domain/
# mapper/xml/IService/Impl/Controller)成立；但【含多 DTO/helper 的大实体】(实测 st-8-1 10/
# st-12-1 11/st-16-1 14 文件)整批必撞 900s 超时(LLM 反复诊断该拆,旧逻辑守 RUN14 不拆→换模型空转)。
# 超此上界则【按层】拆(数据层 boilerplate 快 → 逻辑层 service/impl/controller 复杂,串行)：共享
# 契约(CONTRACT_MODULE)已锚定接口/DTO 签名,按层拆+契约注入不致漂移。正常实体(≤此数)仍整批守 RUN14。
MAX_SINGLE_ENTITY_FILES = 6
# D4(a) 治本 round18 st-16：【同层平行独立实现】家族(策略/插件模式：N 个兄弟实现共享一个抽象、
# 彼此无编译依赖，如 6 个渠道通知 impl)达此数即【一实现一子任务】。旧逻辑只按 Controller 锚点/单实体
# 分层拆，纯 Service 兄弟(无 Controller)落进单实体分支→不拆穿→被迫串烧多套异构外部集成→迭代耗尽 +
# 对某库 API 幻觉即拖垮整批。阈值≥3 避免 2 个也拆(2 个负担可控)。共享接口/抽象成前置上游批。
MIN_PARALLEL_IMPL_SIBLINGS = 3
# 约定的"实现/插件目录名"(平行独立实现常聚居于此)——纯路径信号，跨语言/跨栈通用不绑 Java。
_IMPL_DIR_NAMES = frozenset({
    "impl", "impls", "handler", "handlers", "channel", "channels",
    "strategy", "strategies", "provider", "providers", "processor", "processors",
    "adapter", "adapters", "filter", "filters", "listener", "listeners",
    "sender", "senders", "notifier", "notifiers", "executor", "executors",
    "plugin", "plugins", "connector", "connectors",
})
# 分层秩(数据模型→持久层→业务层→Web 层)：仅用于【批内文件排序】(描述里数据层在前读着自然)，
# 不再作为拆分边界——拆分边界是实体词干(_entity_stem)。
_LAYER_ORDER = {
    "domain": 0, "entity": 0, "vo": 1, "dto": 2,
    "mapper": 3, "mapperxml": 4, "service": 5, "serviceimpl": 6, "controller": 7,
}


def _tier_limits() -> dict:
    """I1：按 Brain 主模型能力 tier 返回约束上限。

    默认（SWARM_MODEL_TIER_ENABLED 未开）= standard = 上面的硬编码常量，行为零变化。
    显式启用后，强模型收紧上限（少澄清/打回/拆分=降延迟），弱模型放宽（多兜底）。
    """
    try:
        from swarm.brain.model_tier import tier_constraints
        from swarm.config.settings import get_config
        model_name = get_config().model.brain_primary
        return tier_constraints(model_name)
    except Exception:  # noqa: BLE001
        # 任何异常都回退到 standard 默认（绝不因 tier 解析失败影响主流程）
        return {"clarify_rounds": MAX_CLARIFY_ROUNDS, "design_rejects": MAX_DESIGN_REJECTS,
                "elaborate_resplit": MAX_ELABORATE_RESPLIT}


def _auto_mode(state: BrainState) -> bool:
    """API/CI 自动化模式：永不交互（澄清/评审走默认假设）。"""
    if state.get("auto_accept"):
        return True
    return os.environ.get("SWARM_AUTO_ACCEPT", "").lower() in ("1", "true", "yes")


def _stage2_module_timeout() -> float:
    """R64-T5：tech_design 阶段2 单模块超时（秒）——必须与 R56-1 思考失控无损切备预算
    保持【确定性排序】：节点超时 > 思考预算 + 余量。

    round64 实锤（cassette seq6）：ruoyi-framework 模块思考失控（500s 内 28841 个
    reasoning chunk、零正文），旧写死 500s 的 asyncio.wait_for 在 R56-1 预算（默认 600s）
    之前抢跑掐流 → 白烧 500s + 盲目同模型重试碰运气；而 600s 时 R56-1 本可【无损】切
    备用大脑（下游零 chunk，保留完整推理质量）。预算关闭（0）时保持 500s 原值；预算低于
    500s 时 500s 地板不变（此时 R56-1 先触发，排序天然成立）。"""
    try:
        budget = float(getattr(get_config(), "brain_reasoning_phase_budget_s", 0.0) or 0.0)
    except Exception as exc:  # noqa: BLE001 — 猎手批2 F3：config 失败回退地板，绝不让
        # 超时调参把整个 tech_design 炸成"LLM 异常"误导归因
        logger.warning("[TECH_DESIGN] _stage2_module_timeout 读配置失败，回退 500s 地板: %s", exc)
        budget = 0.0
    return max(500.0, budget + 120.0) if budget > 0 else 500.0


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
        wcfg = get_config().worker
        # 候选 = 首派池 ∪ 路由三档 primary+fallback——worker 子任务首派走池，失败按难度
        # fallback 链切备，故【实际可能落到】的模型是这全集，最小真实窗口须涵盖全部可达目标。
        # （2026-07-15 复核治本：池收成单 Qwopus 后，旧写法"池非空就只算池"会把 MiniMax 196K
        # 等 fallback 排除出最小窗口计算——安全下界虚高。改为并集，与本函数 docstring 一致。）
        # 安全性：budget×0.75×0.7≈103K < 最小 worker 窗口(MiniMax 196K)，即使降级切备，worker 的
        # pre_model_hook 裁剪后输入也装得下——所有 worker 模型通吃，无需额外重拆。
        candidate_models = (
            list(getattr(wcfg, "worker_parallel_pool", []) or [])
            + [cfg.routing_trivial, *cfg.routing_trivial_fallback,
               cfg.routing_medium, *cfg.routing_medium_fallback,
               cfg.routing_complex, *cfg.routing_complex_fallback]
        )
        candidate_models = [m for m in dict.fromkeys(candidate_models) if m]  # 去重保序

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

    # ── 虚假前提优先处理（需求转化层 tech_design 检出，覆盖 auto_accept）──
    # 用户决策："涉及事实需澄清，就不能 auto_accept"。虚假前提是【事实错误】非信息不足，
    # auto 模式也【绝不能用默认假设硬跑】——基于虚假前提产出的都是垃圾、纯烧算力。
    false_premises = [
        fi for fi in (state.get("tech_design_fact_issues") or [])
        if isinstance(fi, dict) and fi.get("verdict") == "false"
    ]
    # A-P1-02：轮数上限必须在虚假前提分支【之前】检查，否则该分支永远先命中，
    # 用户答复后 tech_design_fact_issues 未被清空 → 每轮重新推导出同一虚假前提 → 同问题死问到
    # recursion_limit。达上限后停止再问（保留 blocked 标记交人工），不再无限 interrupt。
    _fact_rnd = int(state.get("clarify_round", 0))
    _fact_max = _tier_limits()["clarify_rounds"]
    if false_premises and _fact_rnd >= _fact_max:
        logger.warning(
            "[CLARIFY] 虚假前提澄清达轮数上限 %d，停止再问，交人工/降级继续", _fact_max,
        )
        return {
            "clarify_done": True,
            "clarify_blocked_by_facts": True,
            "clarify_summary": (
                "需求存在虚假前提且澄清已达轮数上限，停止追问交人工处理。"
            ),
            # 消费掉，避免下游再次将其视作未决事实问题。
            "tech_design_fact_issues": [],
        }
    if false_premises:
        _msgs = []
        for fp in false_premises:
            sug = f"（{fp.get('suggestion')}）" if fp.get("suggestion") else ""
            _msgs.append(f"- {fp.get('claim', '?')}：{fp.get('detail', '事实核验未通过')}{sug}")
        summary = "需求存在虚假前提，无法基于不存在的事实执行：\n" + "\n".join(_msgs)
        if _auto_mode(state):
            # auto 模式：不假设、不硬跑，标记需人工澄清后终止本轮（覆盖 auto_accept）
            logger.warning("[CLARIFY] 检出虚假前提，auto 模式下仍终止待人工澄清（覆盖 auto_accept）")
            return {
                "clarify_done": True,
                "clarify_blocked_by_facts": True,
                "clarify_summary": summary,
            }
        # 交互模式：向用户提问"你是指 X 吗"
        from langgraph.types import interrupt as _interrupt
        ask = summary + "\n\n请确认或修正需求（如指明正确的文件/模块）。"
        try:
            answer = _interrupt({"type": "clarify_fact_issue", "question": ask})
            history = list(state.get("clarify_history", []))
            history.append({"q": ask, "a": str(answer)})
            # A-P1-02：用户已就该虚假前提作答 → 消费掉 tech_design_fact_issues，
            # 否则它会被反复重新识别为未决问题，每轮重问同一事实。后续若需基于答复
            # 重新核验，由 tech_design 重新生成新的 fact_issues（而非沿用旧的）。
            return {
                "clarify_history": history,
                "clarify_round": int(state.get("clarify_round", 0)) + 1,
                "tech_design_fact_issues": [],
            }
        except Exception as _exc:  # noqa: BLE001
            # I1-#15（round38c 主题I·外部深审）：interrupt() 靠抛 GraphInterrupt 让
            # runtime 暂停图等人工 resume——宽捕获把它吞掉=首次调用即落本分支，交互
            # 模式的虚假前提人工澄清闸【从未真正暂停过】（全仓其余 4 处 interrupt 均
            # 裸调用，唯此处被包）。GraphInterrupt 族必须重抛。
            _cls = type(_exc).__name__
            if "Interrupt" in _cls or "interrupt" in _cls:
                raise
            return {
                "clarify_done": True,
                "clarify_blocked_by_facts": True,
                "clarify_summary": summary,
                "tech_design_fact_issues": [],
            }

    if _auto_mode(state):
        return {"clarify_done": True, "clarify_summary": "自动化模式，跳过澄清，用默认假设。"}

    rnd = int(state.get("clarify_round", 0))
    history = list(state.get("clarify_history", []))

    _max_clarify = _tier_limits()["clarify_rounds"]
    if rnd >= _max_clarify:
        logger.info("[CLARIFY] 达轮数上限 %d，结束澄清", _max_clarify)
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
                max_rounds=_max_clarify,  # #13：用真实 tier 上限，非固定常量
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
        "max_rounds": _max_clarify,  # #13：用真实 tier 上限
        "questions": questions,
        "message": f"规划前需要澄清（第 {rnd + 1}/{_max_clarify} 轮，可逐条回答，也可整体跳过用默认假设）。",
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


_STACK_MISMATCH_KW = (
    "vue", "react", "angular", "svelte", "spa", "单页", "前端框架", "前端技术栈",
    "thymeleaf", "jsp", "freemarker", "velocity", "服务端模板", "element", "vite",
    "ruoyi-ui", ".vue", "前端为", "前端用", "前端是", "代码生成器", "生成器",
)


def _is_stack_mismatch_issue(fi: dict) -> bool:
    """判定一条 fact_issue 是否属于【技术栈/框架 doc-mismatch】（而非真·缺文件虚假前提）。

    治本 8537fa5e item-2：project_stack 已权威定栈、设计已按实际栈落地后，"PRD 说 Vue 但项目
    是 Thymeleaf" 这类只是【需适配的框架差异】，不该当虚假前提阻断。命中前端框架/技术栈关键词
    且不含"文件不存在/缺失"这类真缺失信号即判为栈差异。纯函数、保守（拿不准不剔除）。
    """
    text = f"{fi.get('claim', '')} {fi.get('detail', '')}".lower()
    if not text.strip():
        return False
    # 命中前端框架/技术栈关键词 = doc 与磁盘事实的【栈差异】→ project_stack 已权威定栈，
    # 一律【适配落地、不阻断】，哪怕文本含"不存在/没有"。治本（用户原则"不以文档为准"）：
    #   "PRD 假设的 Vue 在本项目【不存在】" 里的"不存在"是【栈差异本身的描述】，不是缺交付文件——
    #   旧实现把"不存在/缺失"无差别当真·缺文件优先保留，恰把这类框架差异误判成虚假前提阻断
    #   （实测 RuoYi retry：PRD 提到 Vue、磁盘是 Thymeleaf → fail-fast，明明已正确适配 Thymeleaf）。
    if any(k in text for k in _STACK_MISMATCH_KW):
        return True
    # 不含栈关键词时，交回上层按【真·缺文件/缺符号】虚假前提处理（保留阻断，不在此剔除）。
    return False


def _resolve_project_path(state: BrainState) -> str | None:
    """从 state 解析项目真实磁盘路径（事实核验/file_plan 的 ground truth 源）。"""
    pid = state.get("project_id") or ""
    if not pid:
        return None
    try:
        from swarm.project import store as _store
        proj = _store.get_project(pid)
        return proj.get("path") if proj else None
    except Exception:  # noqa: BLE001
        return None


# D57：tech_design 事实采集/点名文件核验的短 TTL memo——tech_design 主调 + review×3 +
# replan 重入会在短窗内对同一项目树反复整树 os.walk（每调两遍）。规划期项目树静态
# （dispatch 前无 worker 写盘），短 TTL 内复用安全；TTL 过期自动重扫，绝不永久缓存。
# facts（LLM 参考事实，advisory）用较长 TTL；verify（可触发澄清的存在性判定，correctness-
# relevant）用短 TTL，把 stale 误判窗压到复审突发之内。
_FACTS_MEMO_TTL_S = 120.0
_VERIFY_MEMO_TTL_S = 30.0
_FACTS_MEMO: dict[tuple, tuple[float, str]] = {}
_VERIFY_MEMO: dict[tuple, tuple[float, list]] = {}


def _gather_project_facts(project_path: str | None, max_dirs: int = 60) -> str:
    """采集项目真实结构事实供 tech_design 核验【事实依据】（ground truth=磁盘，不靠可能滞后的索引）。

    产出：① 顶层目录树（前若干层，识别分层规范如 RuoYi 的 controller/service/mapper/domain）；
    ② 各典型层下的样例文件名（让 LLM 学到命名/路径规律，设计新文件路径时照此推导）。
    这样 tech_design 能核验"需求点名的文件是否真实存在"+ 据真实结构设计新文件路径。
    D57：结果按 (project_path, max_dirs) 做 120s TTL memo（review×3/replan 短窗重入不重扫）。
    """
    if not project_path:
        return "（无项目路径，无法核验文件事实——方案中的文件存在性需 worker 沙箱实地确认）"
    import os
    import time as _time
    from collections import Counter
    _memo_key = (str(project_path), int(max_dirs))
    _hit = _FACTS_MEMO.get(_memo_key)
    if _hit is not None and (_time.monotonic() - _hit[0]) < _FACTS_MEMO_TTL_S:
        return _hit[1]
    try:
        lines: list[str] = []
        # 识别典型分层目录的样例文件（帮 LLM 学命名规律，语言/框架无关）
        sample_patterns = ("controller", "service", "mapper", "domain", "entity",
                            "model", "dao", "repository", "api", "handler", "router",
                            "views", "components", "pages", "templates")
        seen_samples: dict[str, list[str]] = {}
        dir_count = 0
        # ── 通用磁盘事实采集（框架无关，治本 task 8537fa5e）──
        # 不写死任何具体框架/项目：只客观采集"扩展名分布 + 构建清单 + 前端形态信号"，
        # 交给 tech_design 大模型据这些事实判定【真实技术栈】，而不是靠训练先验/需求文档假设。
        ext_counts: Counter = Counter()
        manifests: list[str] = []          # 构建/依赖清单文件（语言无关）
        frontend_proj_dirs: list[str] = []  # 含 package.json 的子目录 = 独立前端工程
        # 各类前端形态文件计数（不裁决，只摆事实）
        tmpl_exts = (".html", ".htm", ".ftl", ".jsp", ".erb", ".ejs", ".twig",
                     ".vm", ".mustache", ".hbs", ".cshtml", ".gohtml")  # 服务端模板族
        spa_exts = (".vue", ".jsx", ".tsx", ".svelte")                   # SPA 组件族
        tmpl_count = 0
        spa_count = 0
        manifest_names = {
            "pom.xml", "build.gradle", "build.gradle.kts", "settings.gradle", "build.sbt",
            "package.json", "requirements.txt", "pyproject.toml", "setup.py", "Pipfile",
            "go.mod", "Cargo.toml", "composer.json", "Gemfile", "build.xml",
            "CMakeLists.txt", "Makefile", "mix.exs", "pubspec.yaml",
        }
        for root, dirs, files in os.walk(project_path):
            # 跳过噪音目录
            dirs[:] = [d for d in dirs if d not in (
                ".git", "node_modules", "target", "dist", "build", ".venv",
                "__pycache__", ".idea", ".codegraph", "vendor")]
            rel = os.path.relpath(root, project_path)
            if rel == ".":
                rel = ""
            depth = rel.count(os.sep) if rel else 0
            dir_count += 1
            if dir_count > max_dirs * 40:  # ~2400 目录上限，约束超大仓遍历成本
                break
            low = rel.lower()
            # 扩展名/前端形态/清单 统计【全树】(深处的 src/main/java/**.java 也要计入，
            # 否则 Java 项目的 .java 因深度被漏掉，直方图失真误导 LLM)。计数廉价。
            for f in files:
                _, ext = os.path.splitext(f)
                if ext:
                    ext_counts[ext.lower()] += 1
                if ext.lower() in tmpl_exts:
                    tmpl_count += 1
                elif ext.lower() in spa_exts:
                    spa_count += 1
                if f in manifest_names or f.endswith((".csproj", ".sln")):
                    mp = os.path.join(rel, f) if rel else f
                    if len(manifests) < 10:
                        manifests.append(mp)
                    # 子目录(非根)里的 package.json → 独立前端工程信号
                    if f == "package.json" and rel:
                        frontend_proj_dirs.append(rel)
            # 样例文件只在浅层(≤4)采集即可（学命名/路径规律，无需深挖）
            if depth <= 4:
                for pat in sample_patterns:
                    if pat in low and len(seen_samples.get(pat, [])) < 3:
                        for f in files[:3]:
                            seen_samples.setdefault(pat, []).append(os.path.join(rel, f))

        # ── 输出客观事实，交大模型判定真实栈（不在代码里裁决具体框架）──
        lines.append(
            "【项目磁盘事实（ground truth）——据此判定项目【真实技术栈】，"
            "其优先级高于需求文档里的任何框架/技术假设】："
        )
        if manifests:
            lines.append("- 构建/依赖清单文件：" + "；".join(manifests))
        if ext_counts:
            top = "  ".join(f"{e}×{n}" for e, n in ext_counts.most_common(14))
            lines.append("- 源码文件类型分布：" + top)
        lines.append(
            f"- 前端形态信号：服务端模板文件(.html/.ftl/.jsp/.erb 等)共 {tmpl_count}；"
            f"SPA 组件文件(.vue/.jsx/.tsx/.svelte)共 {spa_count}；"
            + ("独立前端工程目录(含 package.json)：" + "、".join(sorted(set(frontend_proj_dirs))[:5])
               if frontend_proj_dirs else "未见独立前端工程(无子目录含 package.json)")
        )
        if seen_samples:
            lines.append("- 分层/目录样例文件（学其命名与路径规律，新文件路径照此推导）：")
            for pat, fs in list(seen_samples.items())[:10]:
                for f in fs[:2]:
                    lines.append(f"    {f}")
        lines.append(
            "请据以上磁盘事实判定项目【实际技术栈（前端/后端/存储/构建工具/分层约定）】，"
            "并严格按该实际栈与既有约定推导 file_plan 的路径与文件形态；需求文档若假定了与磁盘"
            "不同的框架/技术，一律以磁盘事实为准（适配落地，不算虚假前提）。"
        )
        _out = "\n".join(lines)
        _FACTS_MEMO[_memo_key] = (_time.monotonic(), _out)  # D57：成功才缓存，异常不缓存
        return _out
    except Exception as exc:  # noqa: BLE001
        return f"（项目结构扫描失败：{exc}；文件存在性需 worker 沙箱实地确认）"


def _label_grounded_fact_issues(fact_issues: list | None, file_checks: list,
                                file_plan) -> list:
    """给 verdict=false 的 fact_issues 标 grounded（确定性坐实才 True）——纯函数，供单测。

    grounded=True  ← 磁盘坐实"需求点名文件缺失"且【无人计划创建它】（真虚假前提，
                     after_tech_design 据此 block）
    grounded=False ← 纯 LLM 判定无磁盘佐证（框架/栈差异、语义臆测）→ advisory 不阻断

    A2（2026-07-09 登记册）：file_plan 中 action=create（含缺省，与路径校正同口径）的路径
    按 basename 豁免——计划新建的文件磁盘必然不存在，那是工作本身不是虚假前提；否则 PRD
    点名任何待创建文件都会 grounded→auto 模式 CLARIFY 阻断→DELIVER 拒绝→任务死，零重试。
    附带封堵：grounded 只能由本函数授予，LLM 输出自带的 grounded 字段一律剥除重判。
    """
    import os as _os
    fact_issues = [fi for fi in (fact_issues or [])]
    for fi in fact_issues:
        if isinstance(fi, dict):
            fi.pop("grounded", None)  # LLM 自由文本无权坐实，防绕过豁免直接 block
    _planned_create_paths = {
        str(fp.get("path", "")).lower().lstrip("./")
        for fp in (file_plan if isinstance(file_plan, list) else [])
        if isinstance(fp, dict) and fp.get("path")
        and str(fp.get("action") or "create").lower() == "create"
    }
    _planned_create_bases = {p.rsplit("/", 1)[-1] for p in _planned_create_paths}

    def _is_planned_create(fname: str) -> bool:
        # 复核 H3（2026-07-09）：路径形态的点名（含 /）按【后缀】匹配——跨目录同名
        # （com/b/X vs 计划新建 com/a/X）不得误豁免（round37 同名接口爆炸是本仓已证实
        # 模式）。裸文件名保持 basename 口径（PRD 常只写类文件名）。
        f = fname.lower().lstrip("./")
        if "/" in f:
            return any(p == f or p.endswith("/" + f) for p in _planned_create_paths)
        return f in _planned_create_bases

    det_false = [
        fc for fc in file_checks
        if not fc.get("exists") and not _is_planned_create(str(fc.get("file", "")))
    ]
    det_missing = {str(fc.get("file", "")).strip() for fc in det_false if fc.get("file")}
    det_missing_bases = {f.rsplit("/", 1)[-1] for f in det_missing if f}
    # 磁盘坐实的缺失文件【始终】作为 grounded 虚假前提在场（不再依赖 LLM 是否标了）
    _seen_text = " ".join(
        f"{fi.get('claim', '')}{fi.get('detail', '')}" for fi in fact_issues if isinstance(fi, dict)
    )
    for fc in det_false:
        f = str(fc.get("file", "")).strip()
        if f and f not in _seen_text:
            fact_issues.append({
                "claim": f"需求点名文件 {f}", "verdict": "false", "grounded": True,
                "detail": "磁盘核验：该文件在项目中不存在",
                "suggestion": f"近似候选：{fc['candidates']}" if fc.get("candidates") else "无近似文件",
            })
    # 标注既有 verdict=false 的 grounded：引用了磁盘确认缺失的文件名 且 非栈/框架差异 → 坐实
    for fi in fact_issues:
        if not isinstance(fi, dict) or fi.get("verdict") != "false" or "grounded" in fi:
            continue
        _t = f"{fi.get('claim', '')} {fi.get('detail', '')}"
        _refs_missing = any(b and b in _t for b in det_missing_bases)
        fi["grounded"] = bool(_refs_missing and not _is_stack_mismatch_issue(fi))
    return fact_issues


def _verify_named_files_exist(task_description: str, project_path: str | None) -> list[dict]:
    """事实核验（多源仲裁 + 可信度）：从需求提取被点名文件，多源核实是否存在。

    第二批-2 动态可信度：事实源按可信度仲裁，避免单源滞后误杀——
      - 工作区磁盘（os.walk）：当前实地状态，ground truth。
      - git 已跟踪（git ls-files）：已 commit 的产出（VERIFY_L2 reset 临时改工作区时，git 仍是真相）。
    两源任一命中即视为存在；双源都命中 → confidence=high；仅一源 → medium；
    都未命中 → exists=False（confidence=high，可触发澄清——已 ground truth 双查）。
    这样"知识库索引滞后说不存在"不会误杀（这里根本不靠索引，靠磁盘+git 两个 ground truth）。

    返回 [{"file", "exists", "confidence", "sources":[...], "candidates":[...]}]。
    """
    if not project_path:
        return []
    import os
    import re
    import subprocess
    import time as _time
    # D57：短 TTL memo（30s）——review×3/replan 短窗重入不重复整树 walk + git ls-files。
    # 规划期项目树静态；30s 窗把"apply 后新文件被 stale 判不存在"的误判窗压到复审突发内。
    _memo_key = (str(project_path), hash(task_description or ""))
    _hit = _VERIFY_MEMO.get(_memo_key)
    if _hit is not None and (_time.monotonic() - _hit[0]) < _VERIFY_MEMO_TTL_S:
        return [dict(r) for r in _hit[1]]  # 浅拷贝防调用方原地改缓存
    named = re.findall(r"\b([A-Za-z_][\w./-]*\.[A-Za-z]{1,5})\b", task_description or "")
    if not named:
        return []

    # 确定性排除（防误判虚假前提，配合 prompt 边界）：
    #  - 文档/附件类扩展名（PRD.md 等需求载体，非项目源文件）；
    #  - 代码调用形态（Map.of / log.info / X.builder()）——"标识符.方法"不是文件名。
    # 这样 _verify 只核验真正像"项目源文件路径"的 token，不把示例代码/附件当被点名文件。
    _DOC_EXTS = {"md", "markdown", "txt", "text", "docx", "doc", "pdf", "png", "jpg", "jpeg", "webp"}
    _CODE_METHOD_TAIL = {  # 常见标准库/框架方法名尾段（点后），出现即判为代码调用非文件
        "of", "info", "debug", "warn", "error", "builder", "build", "out", "println",
        "format", "valueof", "tostring", "get", "set", "put", "add", "stream",
        "collect", "map", "filter", "join", "now", "parse", "send", "sendmsg",
    }

    def _looks_like_code_or_doc(tok: str) -> bool:
        ext = tok.rsplit(".", 1)[-1].lower()
        if ext in _DOC_EXTS:
            return True  # 附件/文档
        # 路径形态（含 /）更像真实文件，保留核验
        if "/" in tok:
            return False
        # 单段 "标识符.尾段"：尾段是已知方法名 → 代码调用；或尾段非典型源码扩展名
        if ext in _CODE_METHOD_TAIL:
            return True
        # 源码扩展名白名单：只有这些才当文件核验，其余（如 .builder、.of）视为代码调用
        if ext not in {"java", "js", "ts", "vue", "py", "xml", "html", "css", "sql",
                       "json", "yml", "yaml", "go", "rs", "kt", "tsx", "jsx"}:
            return True
        return False

    named = [t for t in named if not _looks_like_code_or_doc(t)]
    if not named:
        return []

    # 源1：工作区磁盘 basename → 路径
    disk_files: dict[str, list[str]] = {}
    try:
        for root, dirs, files in os.walk(project_path):
            dirs[:] = [d for d in dirs if d not in (
                ".git", "node_modules", "target", "dist", "build", ".venv",
                "__pycache__", ".idea", ".codegraph")]
            for f in files:
                disk_files.setdefault(f.lower(), []).append(
                    os.path.relpath(os.path.join(root, f), project_path))
    except Exception:  # noqa: BLE001
        return []

    # 源2：git 已跟踪文件 basename（已 commit 的产出，工作区被 reset 时仍是真相）
    git_files: dict[str, list[str]] = {}
    try:
        r = subprocess.run(
            ["git", "-C", project_path, "ls-files"],
            capture_output=True, text=True, timeout=20,
        )
        if r.returncode == 0:
            for p in r.stdout.splitlines():
                if p.strip():
                    git_files.setdefault(os.path.basename(p).lower(), []).append(p.strip())
    except Exception:  # noqa: BLE001
        pass

    results: list[dict] = []
    for token in set(named):
        base = os.path.basename(token).lower()
        in_disk = base in disk_files
        in_git = base in git_files
        sources = ([("disk", disk_files[base][:2])] if in_disk else []) + \
                  ([("git", git_files[base][:2])] if in_git else [])
        if in_disk or in_git:
            conf = "high" if (in_disk and in_git) else "medium"
            paths = (disk_files.get(base) or git_files.get(base) or [])[:2]
            results.append({
                "file": token, "exists": True, "confidence": conf,
                "sources": [s[0] for s in sources], "candidates": paths,
            })
        else:
            # 双 ground truth 源都未命中 → 高可信度判定不存在（非索引滞后）
            stem = base.rsplit(".", 1)[0]
            cands = [p for fn, ps in disk_files.items()
                     for p in ps
                     if fn.rsplit(".", 1)[0].startswith(stem[:3]) and len(stem) >= 3][:3]
            results.append({
                "file": token, "exists": False, "confidence": "high",
                "sources": [], "candidates": cands,
            })
    _VERIFY_MEMO[_memo_key] = (_time.monotonic(), [dict(r) for r in results])  # D57
    return results


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

【定级铁律——宁高勿低】定级偏低的代价远大于偏高：低判会让技术方案变浅、子任务拆得粗、
文件产出严重不足，导致需求做不完、交付不是生产级。命中以下任一【强信号】，至少定 ultra：
- 【新建一个完整业务模块/平台/子系统】（不是在既有功能上加点东西，而是从 0 搭一块新业务），
  尤其需求里出现"平台/系统/中心/引擎/编排"等成体系词；
- 【多模块 + 全栈】：既要后端（实体/Mapper/Service/Controller/配置）又要前端（页面/交互），
  且横跨 2 个以上模块或业务域；
- 【多渠道/多策略/可配置编排】这类需要抽象接口 + 多实现 + 统一调度的设计；
- 澄清后确认要做【完整前后端】（别因为某一问被答"先做后端"就降级——除非用户明确只要单点小改）。
只有当需求确属【在既有系统上做有限的、单一模块内的改动】时，才用 complex 及以下。
判不准时上调，不下调。

严格输出 JSON：
{
  "complexity": "simple|medium|complex|ultra",
  "reason": "定级理由（基于澄清后信息；若命中强信号必须指出命中哪条）",
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
        # Wave 1/TD2606-B1：复杂度走类型边界（result 保留供下游读取）。形状非法 → 显式降级 MEDIUM（不静默错形）。
        try:
            comp = ComplexityAssessmentResponse.model_validate(result).complexity
        except Exception as _ve:  # noqa: BLE001
            logger.warning("[ASSESS] 复杂度评估输出形状非法，显式降级 MEDIUM（不静默错形）: %s", str(_ve)[:120])
            comp = Complexity.MEDIUM
        # 新建项目至少 complex（需技术方案）
        if greenfield and comp in (Complexity.SIMPLE, Complexity.MEDIUM):
            comp = Complexity.COMPLEX
        # 治本(task 2e187366)：ASSESS 不得把 complexity【下调】到 analyze 初判之下。
        # 现场：ANALYZE 正确判 ultra(企业级全栈多模块平台)，ASSESS 据澄清信息降到 complex →
        # complex 的 tech_design 更浅 → 只产 27 文件(对照 auto 同 PRD 出 98 文件)，全栈需求做不完。
        # ASSESS 的价值是【纠正低估】(可上调)，绝不该【削薄已正确判定的大任务】。取两者较高档兜底：
        # 过度配给(慢但全)远比配给不足(方案太薄无法交付)安全。
        # 注意：checkpoint resume 后 state["complexity"] 可能是【字符串】而非 Complexity 枚举
        # （LangGraph 序列化），故 isinstance 判断会漏 → 必须同时兼容字符串与枚举（治本 308cd191：
        # 上次只兼容枚举，实跑是字符串 → 守卫没触发 → 仍降级 complex/27 文件）。
        _RANK_S = {"simple": 0, "medium": 1, "complex": 2, "ultra": 3}
        _S2C = {
            "simple": Complexity.SIMPLE, "medium": Complexity.MEDIUM,
            "complex": Complexity.COMPLEX, "ultra": Complexity.ULTRA,
        }
        _analyze_raw = state.get("complexity")
        _av = (_analyze_raw.value if hasattr(_analyze_raw, "value") else str(_analyze_raw or "")).lower()
        if _RANK_S.get(_av, -1) > _RANK_S.get(comp.value, 0):
            logger.info(
                "[ASSESS] 定级 %s 低于 analyze 初判 %s → 守住初判(不削薄大任务，治本 2e187366/308cd191)",
                comp.value, _av,
            )
            comp = _S2C.get(_av, comp)
        logger.info("[ASSESS] 澄清后定级: %s (%s)", comp.value, result.get("reason", "")[:60])
        return {"assessed_complexity": comp, "complexity": comp}
    except Exception as exc:  # noqa: BLE001
        logger.warning("[ASSESS] LLM 失败，沿用 analyze 初判: %s", exc)
        # TD2606-B3：ASSESS 失败时只能沿用 analyze 初判（无法重新定级），但打 degraded 标记让
        # 交付/确认环节看得见"复杂度未经 ASSESS 校正"，不静默当作已校正（ASSESS 本职是纠正低估）。
        return {
            "assessed_complexity": state.get("complexity", Complexity.MEDIUM),
            "degraded_reasons": list(state.get("degraded_reasons") or []) + ["assess_skipped_llm_failed"],
        }


# ══════════════════════════════════════════════
# 节点 2.7：detect_stack — 技术栈/架构识别（plan 前预处理，磁盘 ground truth）
# ══════════════════════════════════════════════

STACK_ADJUDICATE_SYSTEM = """你是资深架构师。下面是对一个代码仓库的【磁盘客观证据】，"""\
"""请据此判定它的真实技术栈（不要靠框架名先验，只看证据）。严格输出 JSON：
{"frontend":"前端栈(如 Vue / React / 服务端模板(Thymeleaf) / 无)",
 "frontend_kind":"server-template|spa|separated|none",
 "backend":"后端栈(如 Spring Boot (java) / Django (python))","build":"构建工具",
 "confidence":0.0-1.0,"reason":"判定依据(引用证据)"}"""

# 栈画像 schema 版本：探测逻辑/画像字段变更时递增，使按指纹缓存的旧画像失效重探。
# 仅指纹（repo 内容）相同不足以复用——画像结构变了（如新增 infra_symbols），旧缓存缺字段。
# v2: 新增 infra_symbols（基建符号锚点，治本 worker 臆造不存在的框架类如 RedisCache）。
# v3: jvm 新增 lombok_available/lombok_source（R65TR-T5 基线注解处理器在位性）——不 bump
#     则已缓存画像永缺该键、硬约束永不渲染（猎手 F3，前例 108676a 同纪律）。
# v4: 新增 infra_symbol_methods（R65E8-T5 基建类 public 方法签名 grounding，治 method 级幻觉
#     如缓存类调裸 .set/.get）——不 bump 则已缓存画像永缺该键、方法签名永不渲染（同 F3 纪律）。
_STACK_SCHEMA_VERSION = 4


async def detect_stack(state: BrainState) -> dict:
    """技术栈/架构识别（plan 前预处理）：磁盘事实为准，确定性优先、模型仅兜底、按 repo 指纹缓存 DB。

    治本 task 8537fa5e：tech_design 曾因无栈事实而用"RuoYi=Vue"先验在 Thymeleaf 单体产 Vue 死代码。
    本节点把"项目是什么栈"做成 plan 前的单一权威事实（project_stack），由 tech_design/plan/worker 统一消费。
    流程：① 命中 (project_id, repo 指纹) 缓存即复用（零成本）；② 否则确定性磁盘探测；
    ③ 仅当置信低/信号歧义才调【一次】大模型据证据裁决；④ 落 projects.config 按指纹缓存。
    """
    from swarm.brain.stack_detect import (
        compute_repo_fingerprint,
        detect_stack_deterministic,
        extract_stack_hints_from_knowledge,
    )

    proj_path = _resolve_project_path(state)
    pid = state.get("project_id") or ""
    if not proj_path:
        return {}  # 无磁盘路径（如纯 greenfield 未落盘）→ 跳过，tech_design 回退原有 project_facts

    try:
        fingerprint = compute_repo_fingerprint(proj_path)
    except Exception:  # noqa: BLE001
        fingerprint = ""

    # ① 缓存命中（同 repo 指纹）→ 复用
    from swarm.project import store as _pstore
    proj_rec = None
    try:
        proj_rec = _pstore.get_project(pid) if pid else None
        cached = (proj_rec or {}).get("config", {}).get("project_stack") if proj_rec else None
        if (isinstance(cached, dict) and cached.get("fingerprint")
                and cached["fingerprint"] == fingerprint
                and cached.get("schema_version") == _STACK_SCHEMA_VERSION):
            logger.info("[DETECT_STACK] 命中缓存（指纹 %s, schema v%s）：前端=%s 后端=%s",
                        fingerprint, _STACK_SCHEMA_VERSION, cached.get("frontend"), cached.get("backend"))
            return {"project_stack": cached}
    except Exception as exc:  # noqa: BLE001
        logger.warning("[DETECT_STACK] 读缓存失败（不致命，继续探测）: %s", exc)

    # ② 确定性磁盘探测
    profile = detect_stack_deterministic(proj_path)
    # 合流 KB 已爬的项目架构/技术栈知识（如"[RuoYi规范] SpringBoot+Shiro+Thymeleaf"）——
    # 我们爬了 wiki/规范进库，这里显式拎出来作高优先证据，别让它埋在 query-dependent 层（8537fa5e 续）。
    kb_hints = extract_stack_hints_from_knowledge(state.get("knowledge_context"))
    if kb_hints:
        profile.setdefault("evidence", []).append("KB 已收录的项目架构/技术栈知识：")
        profile["evidence"].extend("  · " + h for h in kb_hints)
        profile["kb_stack_hints"] = kb_hints
    logger.info(
        "[DETECT_STACK] 确定性探测：前端=%s(%s) 后端=%s 构建=%s 置信=%.2f%s（KB 架构线索 %d 条）",
        profile.get("frontend"), profile.get("frontend_kind"), profile.get("backend"),
        profile.get("build"), profile.get("confidence"),
        "（需模型兜底）" if profile.get("needs_model_adjudication") else "",
        len(kb_hints),
    )

    # ③ 仅低置信/歧义才调一次大模型裁决（据证据，不靠先验）
    if profile.get("needs_model_adjudication"):
        try:
            llm = _get_brain_llm()
            ev = "\n".join(profile.get("evidence") or [])
            resp = await llm.ainvoke([
                {"role": "system", "content": STACK_ADJUDICATE_SYSTEM},
                {"role": "user", "content": f"磁盘证据：\n{ev}\n\n确定性初判：{profile.get('frontend')} / "
                                            f"{profile.get('backend')}（置信 {profile.get('confidence')}）。请裁决。"},
            ])
            # Wave 1/TD2606-B1：裁决响应走类型边界（confidence 强制 float，frontend 为载荷关键）。
            # 形状非法 → 抛出 → 下方 except 沿用确定性结果（不静默吞错形裁决）。
            adj = parse_and_validate(resp.content, StackAdjudicateResponse)
            if adj.frontend:
                profile.update({
                    "frontend": adj.frontend or profile["frontend"],
                    "frontend_kind": adj.frontend_kind or profile["frontend_kind"],
                    "backend": adj.backend or profile["backend"],
                    "build": adj.build or profile["build"],
                    "confidence": adj.confidence or profile["confidence"],
                    "source": "deterministic+model",
                })
                logger.info("[DETECT_STACK] 大模型裁决后：前端=%s 后端=%s 置信=%.2f",
                            profile["frontend"], profile["backend"], profile["confidence"])
        except Exception as exc:  # noqa: BLE001
            logger.warning("[DETECT_STACK] 模型裁决失败，沿用确定性结果: %s", exc)

    profile["fingerprint"] = fingerprint
    profile["schema_version"] = _STACK_SCHEMA_VERSION

    # ④ 落 projects.config 按指纹缓存（合并写，不clobber其它config）
    try:
        if pid and proj_rec is not None:
            cfg = dict(proj_rec.get("config") or {})
            cfg["project_stack"] = profile
            _pstore.update_project(pid, config=cfg)
            logger.info("[DETECT_STACK] 画像已缓存到 projects.config（指纹 %s）", fingerprint)
    except Exception as exc:  # noqa: BLE001
        logger.warning("[DETECT_STACK] 写缓存失败（不致命）: %s", exc)

    return {"project_stack": profile}


# ══════════════════════════════════════════════
# 节点 3：tech_design — 技术方案 + 接口先行（Q6/B）
# ══════════════════════════════════════════════

TECH_DESIGN_SYSTEM = """你是资深技术负责人。基于【真实产品需求】+ 项目知识库 + 项目真实结构，\
产出一份可执行的技术方案。你的核心职责有两个：

【职责一：事实核验】先核对需求的事实依据，不要基于虚假前提设计：
- 需求点名的文件/类/表是否在项目里真实存在？（见下方"被点名文件的真实存在性核验"）
- 若需求说"在 X 文件加内容/修改 X"但 X 不存在 → 这是【虚假前提】，必须在输出标记 fact_issues，不要硬编方案。
- 若需求说"新增 Y 模块"但 Y 已存在 → 标记 already_exists，避免重复创建。

【虚假前提的严格边界——以下【绝不】算虚假前提，禁止标记 fact_issues=false】：
1. **上传的需求附件/文档**（如 PRD.md、需求.docx、设计图）：这是需求【载体】，不是项目源文件，
   它"不在项目里"是正常的，绝不能因此判虚假前提。
2. **需求要【新建】的文件/表/类/模块**：新功能本来就是从零创建，"不存在"正是要新建的理由，
   归入 file_plan 的 create，不是虚假前提。
3. **示例代码/伪代码里的标准库或第三方 API**（如 Map.of、log.info、System.out、@Autowired、
   工具类调用等）：这是用法示意，不是"被点名要核验的项目文件"，绝不标记。
4. **PRD 里描述的接口路径/SDK 方法名/术语**：是要实现的目标，不是已存在事实。
   只有当需求【明确说"修改某个已有文件/类"】且该文件确实不存在时，才算真正的虚假前提。
   判断准则：fact_issues=false 仅用于"需求假定某个【已存在的项目文件】存在但实际不存在"——
   产品经理式的【新建功能需求】几乎不会有虚假前提（都是新建），不要无中生有。
5. **需求文档与项目实际【技术栈/框架】不一致**：绝不算虚假前提、绝不标 false。
   上方【项目磁盘事实】给的是 ground truth，【优先级高于需求文档的任何框架/技术假设】。
   你必须先据磁盘事实（扩展名分布/构建清单/前端形态信号/样例文件）判定项目的【真实技术栈】，
   再按真实栈落地：把 stack 各项填真实栈、file_plan 的路径与文件形态走真实栈约定。
   若需求文档假定了与磁盘不同的框架/技术（例：文档说前端 X 但磁盘是 Y），以磁盘为准【适配落地】，
   可在 architecture 一句话注明"文档提及的 X 按项目实际栈 Y 落地"。这是【适配】不是阻断，不要终止设计。

【职责二：需求转化（产品话→技术方案）】真实产品经理只说"要个功能管设备"，不说表/字段/类名。\
你要基于项目真实结构与分层规范，把模糊的产品需求翻译成明确的【文件级技术方案】：
- 设计数据模型（要建什么表、字段、类型）。
- 据项目分层规范（见"项目分层样例文件"，学其命名/路径规律）设计要【新建/修改】哪些文件，给出【完整相对路径】+ 每个文件的职责。
- 这就是后续 worker 要照着干的清单——务必具体到文件路径，不要只给抽象架构。

要求：
- 既有项目：沿用既有技术栈与分层规范（从样例文件学）。
- file_plan 的路径必须符合项目真实结构（参照样例文件的目录/命名规律推导，勿凭空捏造路径）。

严格输出 JSON：
{
  "fact_issues": [{"claim": "需求中的事实主张", "verdict": "false|already_exists|uncertain", "detail": "说明", "suggestion": "近似候选或建议"}],
  "stack": {"frontend": "...", "backend": "...", "storage": "...", "rationale": "沿用说明"},
  "architecture": "架构概述",
  "data_model": "数据模型（表/字段/类型，文字或 mermaid）",
  "flow": "业务流程（文字）",
  "file_plan": [{"path": "完整相对路径", "action": "create|modify", "module": "该文件所属的【物理构建模块】=它所在的、含单一构建清单(pom.xml/build.gradle/go.mod/package.json 等)的目录；同一构建单元用同一名。棕地里新功能若加进既有模块，就填【既有模块名】而非另造新模块。绝不按功能域把一个模块拆成多个各带构建清单的子模块", "responsibility": "该文件职责", "depends_on": ["前置文件路径"]}],
  "risks": ["风险1"],
  "acceptance": ["验收标准1"],
  "comment_requirements": "代码注释要求",
  "shared_contract": {"apis": [...], "data_structures": [...]}
}

注意：file_plan 是方案的核心产出——必须列全实现该功能所需的所有文件（新建+修改），路径完整、职责清晰。\
data_model 中每张【新表/新列/schema 变更】都必须在 file_plan 里有对应的 DDL/migration 文件\
（路径按项目自身真实约定：已有 sql/、migrations/、flyway/liquibase/alembic 等目录就沿用，磁盘事实优先）——\
没有 DDL 的新表=应用启动即缺表，交付不可运行。\
若 fact_issues 中有 verdict=false 的虚假前提，file_plan 可留空或仅列确定的部分。"""

TECH_DESIGN_USER = """需求：
{task_description}

澄清摘要：
{clarify_summary}

复杂度：{complexity}　是否新建项目：{greenfield}

项目知识库（既有技术栈/结构）：
{knowledge}

项目真实结构（事实依据，ground truth）：
{project_facts}

被点名文件的真实存在性核验（事实依据，标记 exists）：
{file_verification}

{review_feedback}请先做事实核验，再产出技术方案与文件级 file_plan。"""


# ── ultra 超大需求两阶段 tech_design（DESIGN 第八节 A+B）──
# 阶段1：只产出模块清单 + 数据模型 + 架构（短输出），不展开到上百具体文件路径。
TECH_DESIGN_STAGE1_SYSTEM = """你是资深技术负责人。面对一个【超大需求】，先做【顶层方案】——
不要急着列出所有文件，而是先划分模块、定数据模型、定架构。这是第一阶段（短输出）。

职责：
1. 事实核验：需求是否基于不存在的已有文件（虚假前提）？产品经理式新建需求几乎无虚假前提。
   注意：上传附件(PRD.md)、示例代码标准库(Map.of/log.info)、要新建的文件，都【不算】虚假前提。
2. 把需求划分为若干【物理构建模块】，每个模块给：名称、职责、预估文件数、模块间依赖。
   ★"模块"=一个【物理构建单元】★：即一个将包含【单一构建清单】的目录（构建清单指该技术栈的
   工程描述文件，如 pom.xml / build.gradle / Cargo.toml / go.mod / package.json 之一）。它
   **不等于功能域**：一个功能域（如"告警""用户中心"）通常应落在【一个】物理模块内、用子包/
   子目录区分子功能，**绝不要**拆成 xxx-core / xxx-schedule / xxx-callback 等多个各带构建清单的
   模块（构建清单数爆炸 + 父级 <modules>/settings 注册极易漏 = 静默丢模块）。棕地【优先复用既有
   模块目录】，仅当某子功能确有【独立打包/独立发布/独立生命周期】的真实需求时才新建物理模块。
   功能分组表达在【职责描述】里，不靠拆模块表达。

严格输出 JSON：
{"fact_issues": [{"claim","verdict":"false|already_exists|uncertain","detail","suggestion"}],
 "stack": {"frontend","backend","storage","rationale"},
 "architecture": "架构概述",
 "data_model": "数据模型(表/字段)",
 "modules": [{"name":"物理构建模块名(=一个含单一构建清单的目录名，一个功能域尽量落一个模块、勿按子功能拆多模块)","responsibility":"职责","est_files":12,"depends_on":["前置模块名"]}]}"""

TECH_DESIGN_STAGE1_USER = """需求：
{task_description}

澄清摘要：{clarify_summary}
复杂度：{complexity}　是否新建项目：{greenfield}

项目知识库：
{knowledge}

项目真实结构（ground truth）：
{project_facts}

被点名文件核验：
{file_verification}

{review_feedback}请先做事实核验，再产出【模块清单 + 数据模型 + 架构】（不要列具体文件）。"""

# 阶段2：按单个模块产出该模块的 file_plan（短输出，只列这一个模块的文件）。
TECH_DESIGN_STAGE2_SYSTEM = """你是资深技术负责人，正在为【一个模块】产出文件级方案。
整体架构/数据模型已定，你只负责【当前这一个模块】的文件清单——不要管其他模块。

据项目分层规范设计该模块要【新建/修改】的文件，给完整相对路径 + 职责 + 依赖。
所有文件的 module 字段都填【当前模块名】。
本模块若涉及数据模型中的新表/新列/schema 变更，必须包含对应 DDL/migration 文件
（路径按项目自身真实约定：已有 sql/、migrations/ 等目录就沿用，磁盘事实优先）。

严格输出 JSON：
{"file_plan": [{"path":"完整相对路径","action":"create|modify","module":"当前模块名","responsibility":"职责","depends_on":["前置文件路径"]}]}"""

TECH_DESIGN_STAGE2_USER = """总需求（背景）：{task_description}

整体架构：{architecture}
数据模型：{data_model}

项目真实结构（参照其分层/命名规律）：
{project_facts}

## 当前要产出 file_plan 的模块（第 {mod_idx}/{mod_total} 个）
模块名：{mod_name}
职责：{mod_responsibility}
预估文件数：{mod_est_files}
{cont_section}
只为这个模块产出 file_plan（完整路径），module 字段统一填 "{mod_name}"。
本批【最多输出 {batch_cap} 个文件】：按依赖序先输出最基础的文件；若超出上限，
剩余文件我会继续分批向你请求——绝不要为塞进一批而省略文件或截断单个文件项。"""

# R65-T1 分批续写参数：round65 实测 sdk 模块 12 文件 53.5s，30 文件 ~2-4min，
# 远离单调用超时；批次硬上限触顶 = WARNING+机读 incomplete 标记收下已产出（绝不静默截断）。
_STAGE2_BATCH_FILES = 30   # 单批文件数上限（提示词约束 + 续写收敛判据用确定性空批/0新增，不猜批大小）
_STAGE2_MAX_BATCHES = 10   # 单模块产出批次硬上限（10×30=300 文件，病理性无限产出的止损阀）
# R65-T1 猎手 F3：失败预算改双轨——「连续失败」判模块死（同构失败快收敛），
# 「累计失败」只作硬上限（大模块批多，零星瞬时失败不该跨批累积成死刑，
# 否则恰好惩罚本机制要救的大模块）。
_STAGE2_MAX_TOTAL_FAILURES = 8

# 猎手 F4：est_files 是 LLM 自报字段，宽松抽数字（"~15"/"10-20" 取首段数字）
_RE_EST_DIGITS = re.compile(r"\d+")


def _fileplan_path_key(path: str) -> str:
    """跨批去重键归一（R65-T1 猎手 F6）：strip + 反斜杠→斜杠 + 剥 ./ 前缀。
    模型续批把 src/A.x 复读成 ./src/A.x 时不得当新文件（污染计数+重复条目）。
    只归一键，条目里保留原始 path（下游有自己的规范化管线）。"""
    q = str(path or "").strip().replace("\\", "/")
    while q.startswith("./"):
        q = q[2:]
    return q


def _is_token_limit_error(exc: BaseException) -> bool:
    """R38-C：识别账本拒绝（isinstance 优先 + 字符串兜底——langchain/fallback 链路
    有时把异常包成普通 Exception 只剩 message，对齐 models/errors.py 双保险模式）。"""
    from swarm.models.errors import TaskTokenLimitExceeded as _TTLE
    return isinstance(exc, _TTLE) or "token limit exceeded" in str(exc)


_ADMISSION_POLL_S = 2.0  # R38-C：等待在飞结算的轮询间隔
# R38 复核 F8：准入等待后的重试独立配额（probe→reserve 竞态输家不烧能力配额），有界防死循环
_ADMISSION_RETRY_MAX = 5


async def _await_token_admission(task_id: str | None, usage: dict | None, *,
                                 max_wait_s: float, poll_s: float | None = None) -> bool:
    """R38-C：账本拒绝后的准入等待——替代"固定次数/固定退避"空转重试。

    round38 实测：STAGE2 零退避 33ms 烧完 3 次重试、CONTRACT 退避 2s/4s，而在飞调用
    settle 要 103-408s——结构上等不到预留释放，"暂时性预留紧张"被固化为模块永久丢失。

    轮询 ledger.admission_probe：fit→True（可重试）；hopeless（在飞全释放也不够，
    等待无意义——真出路是 widen_budget/escalate）或超时→False（调用方立即确定性放弃）。
    usage 缺 requested_est（异常被包装）→ 放行 True，退回既有重试路径不误杀。
    ★必须在并发信号量外调用——等待不占并发槽★"""
    import asyncio as _aio
    import time as _time
    est = int((usage or {}).get("requested_est") or 0)
    if not task_id or est <= 0:
        # R38 复核 F2：无信息放行必须可观测——退回旧的立即重试路径，运维要能从日志
        # 看出准入等待被跳过（异常被包装丢 usage 是真实链路，非纯防御）。
        if task_id:
            logger.warning(
                "[LEDGER-ADMISSION] 任务 %s 的 token 拒绝缺 requested_est（异常被包装？）"
                "→ 跳过准入等待放行既有重试", task_id)
        return True
    from swarm.models import ledger as _adm_ledger
    _poll = _ADMISSION_POLL_S if poll_s is None else poll_s
    deadline = _time.monotonic() + max(0.0, float(max_wait_s))
    while True:
        verdict = _adm_ledger.admission_probe(task_id, est)
        if verdict == "fit":
            return True
        if verdict == "hopeless":
            logger.warning(
                "[LEDGER-ADMISSION] 任务 %s 预留 est=%d 在飞全释放也不够（hopeless）"
                "→ 立即放弃等待（出路是预算放宽/escalate，非重试）", task_id, est)
            return False
        if _time.monotonic() >= deadline:
            logger.warning(
                "[LEDGER-ADMISSION] 任务 %s 预留 est=%d 等待在飞结算超时(%.0fs) → 放弃",
                task_id, est, max_wait_s)
            return False
        await _aio.sleep(_poll)


async def _tech_design_staged(llm, task_desc, comp_str, greenfield, state,
                              project_facts, file_verification, review_feedback,
                              preset_stage1: dict | None = None):
    """ultra 超大需求两阶段 tech_design（DESIGN 第八节 A+B）。

    阶段1：LLM 出模块清单+数据模型+架构（短输出）。
    阶段2：按模块逐个 LLM 出该模块 file_plan（每次短输出）→ 合并。
    每阶段短输出，规避单次生成上百文件超长 JSON 卡死。
    返回 (result_dict, file_plan, fact_issues, contract)，与单次路径同结构供后续复用。

    R38-F：preset_stage1 非空 = 外科补齐模式——跳过 STAGE1 重生成，用给定的
    architecture/data_model/modules（通常只含缺失模块子集）直接进 STAGE2，
    绝不全量重拆已成模块（P1 外科补丁先例）。
    """
    import time as _time

    _t0 = _time.monotonic()
    if preset_stage1 is not None:
        stage1 = dict(preset_stage1)
        modules = stage1.get("modules", []) or []
        architecture = stage1.get("architecture", "")
        data_model = stage1.get("data_model", "")
        fact_issues = stage1.get("fact_issues", []) or []
        contract = {}  # 补齐模式不动契约草案（节点层保留 state 既有值）
        logger.info(
            "[TECH_DESIGN-STAGE1] 外科补齐模式：跳过顶层方案重生成，仅补 %d 个模块",
            len(modules))
    else:
        # ── 阶段1：顶层方案（模块清单 + 数据模型 + 架构）──
        resp1 = await llm.ainvoke([
            {"role": "system", "content": TECH_DESIGN_STAGE1_SYSTEM},
            {"role": "user", "content": TECH_DESIGN_STAGE1_USER.format(
                task_description=task_desc,
                clarify_summary=state.get("clarify_summary", "") or "（无澄清）",
                complexity=comp_str, greenfield="是" if greenfield else "否",
                knowledge=_format_knowledge(state),
                project_facts=project_facts, file_verification=file_verification,
                review_feedback=review_feedback,
            )},
        ])
        stage1 = _parse_json_from_llm(resp1.content)
        if not isinstance(stage1, dict):
            stage1 = {}
        modules = stage1.get("modules", []) or []
        architecture = stage1.get("architecture", "")
        data_model = stage1.get("data_model", "")
        fact_issues = stage1.get("fact_issues", []) or []
        contract = stage1.pop("shared_contract", {}) if isinstance(stage1, dict) else {}
        logger.info(
            "[TECH_DESIGN-STAGE1] 顶层方案：%d 个模块，数据模型 %d 字，耗时 %.1fs",
            len(modules), len(str(data_model)), _time.monotonic() - _t0,
        )
    if not modules:
        # 阶段1 没给模块 → 退回单次（小需求或 LLM 没按格式）
        _fb_fp = validate_file_plan(stage1.get("file_plan", []))
        # R65B-LOW1：回退路径不是无规模信号——stage1 自带 file_plan 长度就是信号。
        # 配置了 per-file 弹性时按文件数放宽，否则大 file_plan 走此路零弹性撞基线预算
        # （round65b 同病异构面）。窄 try 隔离：记账旁路失败绝不弄丢设计产出。
        _fb_task_id = state.get("task_id")
        if _fb_task_id and _fb_fp:
            try:
                _fb_cfg = get_config()
                _fb_base = int(getattr(_fb_cfg, "max_task_tokens", 0) or 0)
                _fb_per_file = int(getattr(
                    _fb_cfg, "max_task_tokens_per_planned_file", 0) or 0)
                if _fb_base > 0 and _fb_per_file > 0:
                    from swarm.models import ledger as _fb_ledger
                    _fb_budget = _fb_base + _fb_per_file * len(_fb_fp)
                    _fb_ledger.widen_budget(_fb_task_id, _fb_budget)
                    logger.info(
                        "[TECH_DESIGN] 无模块回退路径预算弹性：%d 文件 → "
                        "widen_budget(base %d + %d×%d = %d)",
                        len(_fb_fp), _fb_base, _fb_per_file, len(_fb_fp), _fb_budget)
            except Exception as _fb_exc:  # noqa: BLE001
                logger.warning(
                    "[TECH_DESIGN] 回退路径预算弹性记账失败（%s）——设计产出保留继续，"
                    "预算未放宽（大 file_plan 可能撞基线预算，请查 ledger/config）",
                    _fb_exc)
        return stage1, _fb_fp, fact_issues, contract

    # R38-A：模块数是规划期第一个规模信号——立即按 base+per_module×n 放宽 ledger 预算
    # （runner 的 on_chain_end 弹性钩子在节点结束才触发，救不了本节点内的阶段2；per_subtask
    # 弹性在规划期恒 0）。widen_budget 单调只增不减，且对 track-only（闸关）任务 no-op。
    _r38_task_id = state.get("task_id")
    if _r38_task_id:
        # 猎手 R65B (e)：记账旁路窄 try 隔离——异常漏进节点级 catch-all 会被误标
        # 「LLM 失败」（STAGE1 这里代价小，但同病同治保持口径一致）。
        try:
            _r38_cfg = get_config()
            _r38_base = int(getattr(_r38_cfg, "max_task_tokens", 0) or 0)
            _r38_per_mod = int(getattr(_r38_cfg, "max_task_tokens_per_module", 0) or 0)
            if _r38_base > 0 and _r38_per_mod > 0:
                from swarm.models import ledger as _r38_ledger
                _r38_budget = _r38_base + _r38_per_mod * len(modules)
                _r38_ledger.widen_budget(_r38_task_id, _r38_budget)
                logger.info(
                    "[TECH_DESIGN-STAGE1] 预算弹性：%d 模块 → widen_budget(base %d + %d×%d = %d)",
                    len(modules), _r38_base, _r38_per_mod, len(modules), _r38_budget,
                )
        except Exception as _r38_exc:  # noqa: BLE001
            logger.warning(
                "[TECH_DESIGN-STAGE1] 预算弹性记账失败（%s）——继续规划不阻断，"
                "预算未放宽（ultra 规划期可能撞基线预算，请查 ledger/config）",
                _r38_exc)

    # ── 阶段2：按模块并行产出 file_plan（每次短输出）──
    # P1-DEBT-12 修复（并行 + 双护栏）：
    #   ① 并行：各模块只读阶段1 已定的 architecture/data_model（共享契约在阶段1 已 pop，
    #     模块间在阶段2 无数据依赖），故可 asyncio.gather 并发。Semaphore 限并发=3
    #     （单云端 key 友好，防限流/KV 压满）。
    #   ② 单模块 500s 超时（asyncio.wait_for）——防某模块 LLM hang。有超时托底，并行最坏
    #     封顶 = ceil(N/并发)×500s，正常一波 ~500s 即过，远优于串行累加。
    #   ③ 失败/超时模块记入 failed_modules 并硬告警（ERROR）——非静默跳过，便于事实核验对账。
    # 产出顺序：gather 保序返回，按模块原始顺序聚合 file_plan，保证稳定可复现。
    import asyncio as _asyncio

    mod_total = len(modules)
    _STAGE2_MODULE_TIMEOUT = _stage2_module_timeout()  # 秒/模块（R64-T5 排序，见函数注释）
    _STAGE2_CONCURRENCY = 3         # 单云端 key 友好的并发上限
    _sem = _asyncio.Semaphore(_STAGE2_CONCURRENCY)

    _STAGE2_MAX_ATTEMPTS = 3  # 单模块失败重试：LLM 返空(char0)/瑕疵/超时多为瞬时，重试治本
                              # (RUN12 实证 alarm-config 'Expecting value: char0' 空响应 → 整模块丢失 → 欠 PRD)

    async def _gen_one_module(mi: int, mod: dict) -> dict:
        """产出单个模块的 file_plan（R65-T1 分批续写）。返回 {idx,name,file_plan,error}。

        round65 死因：大模块（est_files=92）单次调用枚举全部文件 → 单流 500s 必超时，
        重试同构必复现 → 整模块丢失。治本：
        - 每批带上限（提示词约束）短输出；续批带「已产出清单勿重复」；
        - 收敛判据是确定性的：空批 或 0 新增（不猜批大小——模型自行提前停会被
          est_files 覆盖率 WARNING 暴露，绝不静默）；
        - 批失败只重试该批（累积成果不丢）；失败预算双轨（猎手 F3）：连续
          _STAGE2_MAX_ATTEMPTS 次判死（同构失败快收敛），累计 _STAGE2_MAX_TOTAL_FAILURES
          硬上限（零星瞬时失败不跨批累积成死刑——那会恰好惩罚要救的大模块）；
        - 超时/输出上限截断重试自适应缩批（大响应超时 → 更小的批才可能过）；
        - 批次硬上限触顶 → WARNING + incomplete 机读标记收下已产出（绝不静默截断亦不丢弃）；
        - 模块级 all-or-nothing：预算烧尽 → file_plan=[] 走 stage2_failed_modules
          对账（半截 plan 静默当成功 = silent-pass 禁令）。

        R38 复核 F8：token 拒绝后等到准入的重试不占能力配额（admission_probe 无副作用，
        probe→reserve 竞态输家不该烧配额），独立计数有界（_ADMISSION_RETRY_MAX）防死循环。"""
        mod_name = mod.get("name") or f"module-{mi}"
        _t_mod = _time.monotonic()
        _acc: dict[str, dict] = {}  # 归一路径键 → 条目（插入序 = 依赖序），跨批去重合并
        _last_err = "unknown"
        _consec_fail = 0    # 连续失败（成功批清零）——判模块死的主判据（猎手 F3）
        _total_fail = 0     # 累计失败硬上限（防长序列失败无界）
        _adm_retries = 0    # 准入等待后的重试次数（不占能力配额）
        _ok_batches = 0     # 产出了新文件的成功批数
        _batch_cap = _STAGE2_BATCH_FILES
        _converged = False
        _topped_out = False
        while (_consec_fail < _STAGE2_MAX_ATTEMPTS
               and _total_fail < _STAGE2_MAX_TOTAL_FAILURES
               and _adm_retries <= _ADMISSION_RETRY_MAX):
            if _ok_batches >= _STAGE2_MAX_BATCHES:
                _topped_out = True
                logger.warning(
                    "[TECH_DESIGN-STAGE2] 模块 %d/%d '%s' 触批次上限（%d 批，已产出 %d 文件）"
                    "——收下已产出并打 incomplete 机读标记，绝不静默截断",
                    mi, mod_total, mod_name, _STAGE2_MAX_BATCHES, len(_acc))
                break
            if _acc:
                _done_lines = "\n".join(f"- {p}" for p in _acc)
                _cont_section = (
                    f"\n已产出的文件（共 {len(_acc)} 个，【绝不要】重复输出，只列剩余）：\n"
                    f"{_done_lines}\n"
                    '若上述清单已覆盖该模块全部文件：直接输出 {"file_plan": []}'
                    "——【绝不虚构】凑数文件。\n")
            else:
                _cont_section = ""
            _tm = _time.monotonic()
            _token_denied: dict | None = None  # R38-C：账本拒绝时带 usage，信号量外等准入
            async with _sem:
                try:
                    resp2 = await _asyncio.wait_for(
                        llm.ainvoke([
                            {"role": "system", "content": TECH_DESIGN_STAGE2_SYSTEM},
                            {"role": "user", "content": TECH_DESIGN_STAGE2_USER.format(
                                task_description=task_desc[:2000],
                                architecture=str(architecture)[:1500],
                                data_model=str(data_model)[:2500],
                                project_facts=project_facts,
                                mod_idx=mi, mod_total=mod_total,
                                mod_name=mod_name,
                                mod_responsibility=mod.get("responsibility", ""),
                                mod_est_files=mod.get("est_files", "?"),
                                cont_section=_cont_section,
                                batch_cap=_batch_cap,
                            )},
                        ]),
                        timeout=_STAGE2_MODULE_TIMEOUT,
                    )
                    # R65-T1 猎手 F7（尽力而为面）：max_tokens 截断的响应 json_repair 会
                    # "修好"成含幻影残路径的合法 JSON 静默入账——有 finish_reason 元数据时
                    # 在解析前拦截判失败并缩批（无元数据的静默截断残量靠录制带观察）。
                    _fin = str(((getattr(resp2, "response_metadata", None) or {})
                                .get("finish_reason") or "")).lower()
                    if _fin in ("length", "max_tokens"):
                        _batch_cap = max(10, _batch_cap // 2)
                        raise ValueError(f"响应被输出上限截断(finish_reason={_fin})，缩批重试")
                    r2 = _parse_json_from_llm(resp2.content)
                    # 复核 R-1（CONFIRMED HIGH）：合法 JSON 但缺 file_plan 数组 = off-schema
                    # 退化响应，绝不作收敛信号——落失败重试路径（首批/续批同律）。
                    if not isinstance(r2, dict) or not isinstance(r2.get("file_plan"), list):
                        raise ValueError("响应缺 file_plan 数组（off-schema，不作收敛信号）")
                    _raw_list = r2["file_plan"]
                    # Wave 1/TD2606-B1：清洗 file_plan——丢弃无有效 path 的 malformed 项（不让其流向 dispatch），
                    # 并按模块名补全缺失的 module 字段。
                    fp = validate_file_plan(_raw_list, module=mod_name)
                    if _raw_list and not fp:
                        raise ValueError("file_plan 全为无效项（模块未产出有效文件）")
                    # 猎手 F1/F6：归一键去重；同路径冲突复读（字段不同）保首见但必须留痕
                    _new: dict[str, dict] = {}
                    _n_conflict = 0
                    _conf_sample: list[str] = []
                    for e in fp:
                        k = _fileplan_path_key(e.get("path", ""))
                        _kept = _acc.get(k) if k in _acc else _new.get(k)
                        if _kept is None:
                            _new[k] = e
                        elif e != _kept:
                            _n_conflict += 1
                            if len(_conf_sample) < 3:
                                _conf_sample.append(str(e.get("path")))
                    if _n_conflict:
                        logger.warning(
                            "[TECH_DESIGN-STAGE2] 模块 %d/%d '%s' 同路径冲突复读 %d 处"
                            "（保首见弃后见，样本 %s）——若为模型自我修正(如 create→modify)"
                            "该修正已被丢弃，反复出现需核对 action 口径",
                            mi, mod_total, mod_name, _n_conflict, _conf_sample)
                    if not _new:
                        # 空批 / 全复读 = 收敛信号（有累积）；首批就空 = 模块 0 产出，计失败重试
                        if _acc:
                            _converged = True
                            break
                        raise ValueError("file_plan 为空（模块未产出有效文件）")
                    _acc.update(_new)
                    _ok_batches += 1
                    _consec_fail = 0  # 猎手 F3：成功批清零连续失败计数
                    logger.info(
                        "[TECH_DESIGN-STAGE2] 模块 %d/%d '%s' 批 %d → +%d 文件（累计 %d，耗时 %.1fs）",
                        mi, mod_total, mod_name, _ok_batches, len(_new), len(_acc),
                        _time.monotonic() - _tm,
                    )
                    continue  # 下一批（或空批确认收敛）
                except _asyncio.TimeoutError:
                    _last_err = "timeout"
                    # 超时 = 响应太大/太慢的结构信号，重试前缩批（同构重试必复现 round65 死法）
                    _batch_cap = max(10, _batch_cap // 2)
                except Exception as exc:  # noqa: BLE001
                    _last_err = str(exc)[:200]
                    if _is_token_limit_error(exc):
                        _token_denied = getattr(exc, "usage", None) or {}
            # R38-C：账本拒绝 → 信号量外等在飞结算释放预留再重试（不占能力配额）；
            # hopeless/超时 → 立即确定性放弃（round38 实测 33ms 空转 3 次 vs settle 103-408s）。
            if _token_denied is not None:
                if not await _await_token_admission(
                        state.get("task_id"), _token_denied,
                        max_wait_s=_STAGE2_MODULE_TIMEOUT):
                    _last_err = f"admission_denied:{_last_err}"
                    break
                _adm_retries += 1
                continue
            _consec_fail += 1
            _total_fail += 1
            if (_consec_fail < _STAGE2_MAX_ATTEMPTS
                    and _total_fail < _STAGE2_MAX_TOTAL_FAILURES):
                logger.warning(
                    "[TECH_DESIGN-STAGE2] 模块 %d/%d '%s' 第 %d 次失败(%s)，重试"
                    "（连续 %d/累计 %d；累积 %d 文件不丢，只重试当前批）",
                    mi, mod_total, mod_name, _total_fail, _last_err,
                    _consec_fail, _total_fail, len(_acc),
                )
        if _acc and (_converged or _topped_out):
            # 猎手 F4：est_files 是 LLM 自报字段（"~15"/"10-20"/"?" 都出现过），
            # int() 直转失败会静默废掉完备性观察面——宽松抽数字，抽不出也要留痕。
            _est = 0
            _est_raw = mod.get("est_files")
            _m = _RE_EST_DIGITS.search(str(_est_raw or ""))
            if _m:
                _est = int(_m.group())
            elif _est_raw not in (None, "", "?"):
                logger.warning(
                    "[TECH_DESIGN-STAGE2] 模块 %d/%d '%s' est_files=%r 不可解析"
                    "——无法校验收敛完备性，下游覆盖闸注意对账",
                    mi, mod_total, mod_name, _est_raw)
            if _est > 0 and len(_acc) * 2 < _est:
                # 收敛判据依赖模型自报"已完整"——产出远低于其 stage1 自估时必须可观测
                logger.warning(
                    "[TECH_DESIGN-STAGE2] 模块 %d/%d '%s' 产出 %d 文件 < 自估 est_files=%d 的一半"
                    "——模型可能提前收敛，下游覆盖闸/事实核验注意对账",
                    mi, mod_total, mod_name, len(_acc), _est)
            _n_calls = _ok_batches + _total_fail + _adm_retries + (1 if _converged else 0)
            logger.info(
                "[TECH_DESIGN-STAGE2] 模块 %d/%d '%s' → %d 文件（自估 %s），耗时 %.1fs"
                "（%d 批/%d 次调用%s）",
                mi, mod_total, mod_name, len(_acc), _est_raw if _est_raw is not None else "?",
                _time.monotonic() - _t_mod, _ok_batches, _n_calls,
                "，触顶 incomplete" if _topped_out else "",
            )
            return {"idx": mi, "name": mod_name, "file_plan": list(_acc.values()),
                    "error": None, "incomplete": _topped_out}
        # R38 复核 F3：报真实次数与放弃原因（早退时旧日志谎报"重试 MAX 次"误导排障）
        logger.error(
            "[TECH_DESIGN-STAGE2] 模块 %d/%d '%s' 失败放弃（连续失败 %d/%d，累计 %d/%d，"
            "准入重试 %d，已产出 %d 文件按 all-or-nothing 弃置走失败对账，硬告警，"
            "该模块文件丢失）: %s",
            mi, mod_total, mod_name, _consec_fail, _STAGE2_MAX_ATTEMPTS,
            _total_fail, _STAGE2_MAX_TOTAL_FAILURES, _adm_retries, len(_acc), _last_err,
        )
        return {"idx": mi, "name": mod_name, "file_plan": [], "error": _last_err}

    _valid = [(mi, mod) for mi, mod in enumerate(modules, start=1) if isinstance(mod, dict)]
    # R65-F8：单模块协程的未预期逃逸异常（_gen_one_module 的 try 覆盖 LLM 调用，但如
    # _await_token_admission 在 except 块之外，自身故障会逃逸）绝不连坐兄弟模块——
    # return_exceptions 隔离后映射为该模块失败走 stage2_failed_modules 对账；
    # 取消（CancelledError）是关停语义，必须原样上抛。
    _results_raw = await _asyncio.gather(
        *[_gen_one_module(mi, mod) for mi, mod in _valid], return_exceptions=True)
    _results = []
    for (mi, mod), r in zip(_valid, _results_raw):
        if isinstance(r, BaseException):
            if isinstance(r, _asyncio.CancelledError):
                raise r
            _mod_name = mod.get("name") or f"module-{mi}"
            logger.error(
                "[TECH_DESIGN-STAGE2] 模块 %d '%s' 未预期异常逃逸"
                "（gather 已隔离，兄弟模块不连坐，该模块走失败对账）: %r",
                mi, _mod_name, r)
            _results.append({"idx": mi, "name": _mod_name, "file_plan": [],
                             "error": f"unhandled:{r!r}"[:200]})
        else:
            _results.append(r)
    # gather 保序：按模块原始顺序聚合，保证 file_plan 稳定可复现
    _results.sort(key=lambda r: r["idx"])

    all_file_plan: list[dict] = []
    failed_modules: list[dict] = []
    incomplete_modules: list[dict] = []  # 猎手 F2：触批次上限的模块必须机读可辨，不能只靠日志
    for r in _results:
        if r["error"]:
            failed_modules.append({"name": r["name"], "idx": r["idx"], "reason": r["error"]})
        else:
            all_file_plan.extend(r["file_plan"])
            if r.get("incomplete"):
                incomplete_modules.append(
                    {"name": r["name"], "idx": r["idx"], "files": len(r["file_plan"])})

    if failed_modules:
        _failed_names = [m["name"] for m in failed_modules]
        logger.error(
            "[TECH_DESIGN-STAGE2] ⚠ %d/%d 模块产出失败 %s——file_plan 不完整，"
            "下游事实核验/计划校验应据此对账，勿当成功",
            len(failed_modules), mod_total, _failed_names,
        )

    # R65B-T1：第二级预算弹性——STAGE2 聚合揭示 file_plan 规模后按文件数再放宽。
    # R38-A 模块弹性标定于「单模块≈75k」旧成本模型；R65-T1 分批协议 + STAGE1「少物理
    # 模块」导向后，规划成本随文件数走（round65b 实锤：2 模块 171 文件拿 1.1M 干 10+
    # 模块的活，plan 顶格 577.5k 烧穿）。widen_budget 单调只增，外科补齐子集重跑的
    # 小值天然 no-op。
    # 猎手 (e) 整改：本块跑在昂贵 gather 之后——记账旁路的任何异常若漏进节点级
    # catch-all，会把已产出的完整 file_plan 扔掉并误标成「LLM 失败」。窄 try 隔离：
    # 记账失败只 WARNING（预算不放宽=最坏退回 round65b 形态，仍有闸兜），设计产出必须保住。
    if _r38_task_id and all_file_plan:
        try:
            _r65_cfg = get_config()
            _r65_base = int(getattr(_r65_cfg, "max_task_tokens", 0) or 0)
            _r65_per_mod = int(getattr(_r65_cfg, "max_task_tokens_per_module", 0) or 0)
            _r65_per_file = int(getattr(_r65_cfg, "max_task_tokens_per_planned_file", 0) or 0)
            if _r65_base > 0 and _r65_per_file > 0:
                from swarm.models import ledger as _r65_ledger
                _r65_budget = (_r65_base + _r65_per_mod * len(modules)
                               + _r65_per_file * len(all_file_plan))
                _r65_ledger.widen_budget(_r38_task_id, _r65_budget)
                logger.info(
                    "[TECH_DESIGN-STAGE2] 预算二级弹性：%d 文件 → widen_budget"
                    "(base %d + %d×%d 模块 + %d×%d 文件 = %d)",
                    len(all_file_plan), _r65_base, _r65_per_mod, len(modules),
                    _r65_per_file, len(all_file_plan), _r65_budget,
                )
        except Exception as _r65_exc:  # noqa: BLE001
            logger.warning(
                "[TECH_DESIGN-STAGE2] 预算二级弹性记账失败（%s）——设计产出保留继续，"
                "预算未放宽（规划期可能复现 round65b 预算饿死，请查 ledger/config）",
                _r65_exc)

    result = {
        "architecture": architecture, "data_model": data_model,
        "stack": stage1.get("stack", {}), "modules": modules,
        "file_plan": all_file_plan, "fact_issues": fact_issues,
        "stage2_failed_modules": failed_modules,
        "stage2_incomplete_modules": incomplete_modules,
    }
    logger.info(
        "[TECH_DESIGN-STAGED] 两阶段完成：%d 模块（%d 失败，并发=%d）→ 合计 %d 文件",
        mod_total, len(failed_modules), _STAGE2_CONCURRENCY, len(all_file_plan),
    )
    return result, all_file_plan, fact_issues, contract


def _package_tech_design_output(state: "BrainState", result, file_plan,
                                fact_issues, contract) -> dict:
    """tech_design 终段打包（全量/R38-F 外科补齐两路径共用）。

    W1.1：ultra 两阶段产出里 phase-2 失败的模块（文件丢失）必须被下游看见——
    既写入专用 state 字段（confirm 闸门据此阻断静默 auto_accept；R38-F review_design
    自动模式据此打回外科补齐），又追加到 degraded_reasons（透传交付/通知）。"""
    _failed_mods = (result.get("stage2_failed_modules") or []) if isinstance(result, dict) else []
    _out: dict = {
        "tech_design": result,
        "shared_contract_draft": contract or {},
        "tech_design_fact_issues": fact_issues or [],
        "tech_design_file_plan": file_plan or [],
        "tech_design_failed_modules": _failed_mods,
    }
    if _failed_mods:
        _names = [m.get("name", "?") for m in _failed_mods if isinstance(m, dict)]
        _reason = (
            f"tech_design 阶段 {len(_failed_mods)} 个模块设计生成失败 {_names}"
            "——这些模块的文件未进入 file_plan，交付不完整，需人工介入"
        )
        logger.error("[TECH_DESIGN] %s", _reason)
        _out["degraded_reasons"] = list(state.get("degraded_reasons") or []) + [_reason]
    # R65-T1 猎手 F2：触批次上限的模块（file_plan 收下但可能未列全）同走 degraded 透传，
    # 让覆盖闸/交付面机读可辨，而非只留 WARNING 日志。
    _inc_mods = (result.get("stage2_incomplete_modules") or []) if isinstance(result, dict) else []
    if _inc_mods:
        _inc_names = [m.get("name", "?") for m in _inc_mods if isinstance(m, dict)]
        _inc_reason = (
            f"tech_design 阶段 {len(_inc_mods)} 个模块触产出批次上限 {_inc_names}"
            "——file_plan 已收下但可能未列全，下游覆盖闸/事实核验须对账"
        )
        logger.warning("[TECH_DESIGN] %s", _inc_reason)
        _out["degraded_reasons"] = list(
            _out.get("degraded_reasons") or state.get("degraded_reasons") or []) + [_inc_reason]
    # ── C3 哨兵（round38c：设计了十几张 alarm_* 新表，全 diff 零 .sql，2FA 列无 DDL）──
    # data_model 设计了表 ∧ file_plan 无任何 schema-migration 形态文件 → warn+degraded
    # （栈无关模式集启发式，warn 级不阻断；prompt 侧已同批加 DDL 硬要求）。
    _dm = str((result or {}).get("data_model") or "") if isinstance(result, dict) else ""
    if _dm.strip() and ("表" in _dm or "table" in _dm.lower()):
        _MIG_HINTS = (".sql", "migration", "flyway", "liquibase", "alembic",
                      "schema.prisma", "changelog")
        _has_ddl = any(
            any(h in str((it or {}).get("path") or "").lower() for h in _MIG_HINTS)
            for it in (file_plan or []) if isinstance(it, dict))
        if not _has_ddl:
            _ddl_reason = ("tech_design 数据模型设计了表结构但 file_plan 无任何 "
                           "DDL/migration 文件——新表无建表脚本=交付不可运行（C3 哨兵）")
            logger.warning("[TECH_DESIGN] %s", _ddl_reason)
            _out["degraded_reasons"] = list(
                _out.get("degraded_reasons") or state.get("degraded_reasons") or []
            ) + [_ddl_reason]
    return _out


async def tech_design(state: BrainState) -> dict:
    """产出技术方案 + 共享契约草案。打回重做时带上评审反馈。"""
    greenfield = bool((state.get("session_metadata") or {}).get("greenfield"))
    prev_review = state.get("design_review") or {}
    review_feedback = ""
    if prev_review.get("decision") == "reject" and prev_review.get("feedback"):
        review_feedback = f"【上一版被打回，评审反馈】{prev_review.get('feedback')}\n请据此改进。\n\n"

    comp = state.get("assessed_complexity") or state.get("complexity", Complexity.MEDIUM)
    comp_str = comp.value if hasattr(comp, "value") else str(comp)

    # 事实依据采集（ground truth = 真实磁盘，不靠可能滞后的索引）
    proj_path = _resolve_project_path(state)
    task_desc = state.get("task_description", "")
    project_facts = _gather_project_facts(proj_path)
    # detect_stack 预处理已产出权威技术栈画像 → 置顶为权威栈指令（磁盘优先于文档框架假设，
    # 治本 8537fa5e）；缺画像（如跳过 detect_stack）时回退仅用 _gather_project_facts 原始事实。
    _stack = state.get("project_stack")
    if _stack:
        from swarm.brain.stack_detect import format_stack_for_prompt
        _stack_directive = format_stack_for_prompt(_stack)
        if _stack_directive:
            project_facts = _stack_directive + "\n\n" + project_facts
    file_checks = _verify_named_files_exist(task_desc, proj_path)
    if file_checks:
        _fv_lines = []
        for fc in file_checks:
            if fc["exists"]:
                # 存在 → 给出【真实路径】，强制 file_plan 用它（不许 LLM 重猜路径）。
                # 这是"事实库不滞后"的关键：已 commit 的产出，定位时必须读真实路径。
                real = fc["candidates"][0] if fc["candidates"] else "(路径未知)"
                src = "+".join(fc.get("sources", [])) or "?"
                _fv_lines.append(
                    f"  - {fc['file']}: ✓已存在【真实路径={real}】(来源:{src}) "
                    f"→ 若需修改此文件，file_plan 必须用这个真实路径，禁止另猜目录")
            else:
                cand = f" 近似候选:{fc['candidates']}" if fc["candidates"] else ""
                _fv_lines.append(f"  - {fc['file']}: ✗不存在(疑似虚假前提!){cand}")
        file_verification = "\n".join(_fv_lines)
    else:
        file_verification = "（需求未点名具体文件，或无项目路径——无需文件存在性核验）"

    try:
        llm = _get_brain_llm()
        # ── R38-F 外科补齐：review_design 对账打回（repair_only）→ 只重生成缺失模块，
        # 与已成模块的 file_plan 合并，绝不全量重拆（round37 R35-C 全量重拆=费用黑洞教训；
        # 人工 reject 无 repair_only 标记 → 保留下方全量重做语义）。──
        _prior_td = state.get("tech_design")
        _prior_failed = state.get("tech_design_failed_modules") or []
        if (prev_review.get("repair_only") and _prior_failed
                and isinstance(_prior_td, dict) and _prior_td.get("modules")):
            _failed_names = {str(m.get("name")) for m in _prior_failed
                             if isinstance(m, dict)}
            _retry_mods = [m for m in (_prior_td.get("modules") or [])
                           if isinstance(m, dict) and str(m.get("name")) in _failed_names]
            if _retry_mods:
                _r2, _fp2, _fi2, _c2 = await _tech_design_staged(
                    llm, task_desc, comp_str, greenfield, state,
                    project_facts, file_verification, review_feedback,
                    preset_stage1={
                        "modules": _retry_mods,
                        "architecture": _prior_td.get("architecture", ""),
                        "data_model": _prior_td.get("data_model", ""),
                    },
                )
                _kept_fp = list(state.get("tech_design_file_plan")
                                or _prior_td.get("file_plan") or [])
                _residual = (_r2.get("stage2_failed_modules") or []) \
                    if isinstance(_r2, dict) else []
                result = dict(_prior_td)
                result["file_plan"] = _kept_fp + list(_fp2 or [])
                result["stage2_failed_modules"] = _residual
                file_plan = result["file_plan"]
                fact_issues = list(state.get("tech_design_fact_issues") or [])
                contract = state.get("shared_contract_draft") or {}
                logger.info(
                    "[TECH_DESIGN] R38-F 外科补齐：%d 缺失模块补回 %d 个（残余 %d），"
                    "file_plan %d → %d 文件（已成模块未重拆）",
                    len(_retry_mods), len(_retry_mods) - len(_residual),
                    len(_residual), len(_kept_fp), len(file_plan),
                )
                return _package_tech_design_output(state, result, file_plan,
                                                   fact_issues, contract)
        # ── ultra 超大需求走两阶段产出（规避单次生成上百文件超长 JSON 卡死）──
        if comp == Complexity.ULTRA or comp_str == "ultra":
            result, file_plan, fact_issues, contract = await _tech_design_staged(
                llm, task_desc, comp_str, greenfield, state,
                project_facts, file_verification, review_feedback,
            )
        else:
            resp = await llm.ainvoke([
                {"role": "system", "content": TECH_DESIGN_SYSTEM},
                {"role": "user", "content": TECH_DESIGN_USER.format(
                    task_description=task_desc,
                    clarify_summary=state.get("clarify_summary", "") or "（无澄清）",
                    complexity=comp_str,
                    greenfield="是" if greenfield else "否",
                    knowledge=_format_knowledge(state),
                    project_facts=project_facts,
                    file_verification=file_verification,
                    review_feedback=review_feedback,
                )},
            ])
            result = _parse_json_from_llm(resp.content)
            contract = result.pop("shared_contract", {}) if isinstance(result, dict) else {}
            fact_issues = result.get("fact_issues", []) if isinstance(result, dict) else []
            # Wave 1/TD2606-B1：清洗 file_plan，丢弃无有效 path 的 malformed 项。
            file_plan = validate_file_plan(result.get("file_plan", []) if isinstance(result, dict) else [])

        # ── 确定性路径校正（治本：用核验到的真实路径覆盖 LLM 猜的路径）──
        # bug(task 9bd1d5b5)：LLM file_plan 把已存在文件的路径猜错（monitor/→common/），
        # 导致 worker 去错目录找不到文件→拒答→任务失败。事实库不滞后的关键是【定位用真实路径】。
        # 对核验出"已存在"的文件，按 basename 匹配，强制 file_plan 里对应项用真实路径。
        import os as _os
        real_by_base = {}
        for fc in file_checks:
            if fc["exists"] and fc.get("candidates"):
                real_by_base[_os.path.basename(fc["file"]).lower()] = fc["candidates"][0]
        if real_by_base and isinstance(file_plan, list):
            for fp in file_plan:
                if not isinstance(fp, dict) or not fp.get("path"):
                    continue
                base_name = _os.path.basename(fp["path"]).lower()
                real = real_by_base.get(base_name)
                if real and fp["path"] != real:
                    logger.info("[TECH_DESIGN] 路径校正(事实优先): file_plan %s → 真实路径 %s",
                                fp["path"], real)
                    fp["path"] = real
                    # 已存在的文件必然是 modify 而非 create
                    if fp.get("action") == "create":
                        fp["action"] = "modify"

        # ── 治本：虚假前提【block 必须确定性坐实】（用户原则"不以文档为准"+"确定性兜住小模型"）──
        # 唯一可坐实虚假前提的依据 = 磁盘核验"需求点名的具体文件/类是否真不存在"(file_checks)。
        # 框架/技术栈维度由 detect_stack 权威拥有；tech_design 的 LLM 仍会把"PRD 提到 Vue 但项目是
        # Thymeleaf"标 verdict=false（prompt 软约束压不住），但那是【已被 project_stack 解决的栈差异】，
        # 绝不能因 LLM 自由文本而 block。故给每条 verdict=false 标 grounded：
        #   grounded=True  ← 磁盘坐实点名文件缺失（真虚假前提，after_tech_design 据此 block）
        #   grounded=False ← 纯 LLM 判定无磁盘佐证（框架/栈差异、语义臆测）→ advisory，不阻断
        # A2（2026-07-09 登记册）：grounded 判定抽出纯函数 + 豁免 file_plan action=create
        # 的计划新建文件（磁盘不存在是工作本身，不是虚假前提）。
        fact_issues = _label_grounded_fact_issues(fact_issues, file_checks, file_plan)
        _advisory = [fi for fi in fact_issues
                     if isinstance(fi, dict) and fi.get("verdict") == "false" and not fi.get("grounded")]
        if _advisory:
            # 未坐实的 LLM verdict=false 降级 advisory：记日志 + 透传 degraded_reasons（人可见，不阻断）
            logger.info(
                "[TECH_DESIGN] %d 个未确定性坐实的 verdict=false 降级为 advisory（不阻断；框架/栈差异或语义臆测，"
                "project_stack 权威定栈=%s）：%s",
                len(_advisory), (state.get("project_stack") or {}).get("frontend"),
                [str(a.get("claim", ""))[:50] for a in _advisory],
            )
        logger.info(
            "[TECH_DESIGN] 技术方案已产出 (file_plan=%d 文件, fact_issues=%d)",
            len(file_plan or []), len(fact_issues or []),
        )
        return _package_tech_design_output(state, result, file_plan, fact_issues, contract)
    except Exception as exc:  # noqa: BLE001
        logger.warning("[TECH_DESIGN] LLM 失败，产出空方案安全继续: %s", exc)
        # LLM 失败仍保留确定性磁盘核验结果（虚假前提不能因 LLM 挂了就漏过）
        det_false = [fc for fc in file_checks if not fc["exists"]]
        det_issues = [{
            "claim": f"需求点名文件 {fc['file']}", "verdict": "false",
            "detail": "磁盘核验：该文件在项目中不存在",
            "suggestion": f"近似候选：{fc['candidates']}" if fc["candidates"] else "无近似文件",
        } for fc in det_false]
        # W1.2：tech_design 整体 LLM 失败 → file_plan 为空、方案为占位。绝不能让 auto_accept
        # 把这种"无设计"的降级计划静默放行当成功。打 fail-fast 标记 + degraded_reasons，
        # 供 gates.can_auto_accept_plan 拦下升级人工审核。
        _reason = f"tech_design 整体生成失败（LLM 异常 {type(exc).__name__}），方案/file_plan 为空，须人工介入"
        logger.error("[TECH_DESIGN] %s", _reason)
        return {
            "tech_design": {"architecture": "（自动生成失败，降级直接规划）", "risks": [], "notes": []},
            "shared_contract_draft": {},
            "tech_design_fact_issues": det_issues,
            "tech_design_file_plan": [],
            "tech_design_generation_failed": True,
            "degraded_reasons": list(state.get("degraded_reasons") or []) + [_reason],
        }


# ══════════════════════════════════════════════
# 节点 3.5：contract_design — 共享契约设计（T1，DESIGN_multiworker_collaboration）
# ══════════════════════════════════════════════

# 三段式（治本 runaway）：契约不再一次性全局生成（云端 reasoning 模型实测 GLM-5.2/Kimi 均 20+min/
# 6w chunk 才 stall），改 Stage A 全局骨架(小) + Stage B 逐模块并发(各自 owns 的片) + Stage C 确定性合并。
# 每调用小而有界、可并发，runaway 从根上消失，且随模块数水平扩展。镜像 _tech_design_staged 的成熟模式。
# P1-E（996db614 实测）：慢 brain 模型（GLM-5.2 单调用 100-270s）+ 并发 3 争抢单端点 →
# 2/10 模块契约片撑爆 300s 超时丢失 → 下游缺契约靠重试自愈、代价巨大。
# 治本：降并发（每调用更快、超时更少）+ 上调单调用超时（给慢模型留空间）+ 重试退避。均可 env 调。
_CONTRACT_CONCURRENCY = int(os.environ.get("SWARM_CONTRACT_CONCURRENCY", "2") or "2")
_CONTRACT_MAX_ATTEMPTS = int(os.environ.get("SWARM_CONTRACT_MAX_ATTEMPTS", "3") or "3")
_CONTRACT_STAGE_TIMEOUT = float(os.environ.get("SWARM_CONTRACT_STAGE_TIMEOUT", "600") or "600")
# 治本 B（996db614 数据驱动）：Stage A 全局骨架是【consumer_map（跨模块消费关系→确定性连
# depends_on 的唯一来源）的单点故障】。实测两组数据：2026-06-27 run 骨架【正常 75s 就完】(15 模块、
# consumer_map=13)；2026-06-28 run 却 600s 没完被墙钟掐断 → consumer_map 整个丢 → ② 跨模块依赖没连。
# 即骨架正常 ~75s，那次 600s 超时是【异常】(模型 runaway/端点抖动)，不是"生成本来就大"。故真正的
# 修复是【重试】(换一次新生成大概率 75s 完成)——而非放宽超时(更慢检测异常、最坏更久)。timeout 保持
# 600s(对 75s 正常值已 8x 余量、06-27 实测从未误杀健康生成)，靠重试兜异常。
_CONTRACT_SKELETON_TIMEOUT = float(
    os.environ.get("SWARM_CONTRACT_SKELETON_TIMEOUT", "600") or "600"
)
# 骨架重试次数独立（默认 2=1 次重试）：异常多为瞬时(runaway/端点抖动)，1 次新生成即大概率恢复到
# 正常 75s；重跑同 prompt 若仍超时，第 3 次纯浪费 → 封顶 2 次即快速降级。
_CONTRACT_SKELETON_MAX_ATTEMPTS = int(
    os.environ.get("SWARM_CONTRACT_SKELETON_MAX_ATTEMPTS", "2") or "2"
)

# ── Stage A：全局骨架（只定没有单一模块归属、必须全局统一的部分）──
CONTRACT_SKELETON_SYSTEM = """你是系统架构师，为一个【多模块大型需求】定【全局骨架】——只定那些
【没有单一模块归属、必须全局统一】的部分，给各模块后续细化当锚点。绝不写任何模块内部细节。

只定三类全局事实：
1. conventions：命名/路径约定（包名前缀、模块目录命名规范），防各 worker 撞名重复创建。
2. constants：跨模块共享的常量/枚举（渠道类型、状态码、回调类型等），全局唯一一份。
3. consumer_map：跨模块消费关系——每个模块【被哪些模块消费】、对方大致期望它暴露什么 surface
   （接口/DTO/API 的方向）。据模块 depends_on 反转细化，让各模块知道"谁要用我、我该 expose 什么"。

严格输出 JSON：
{"skeleton": {
  "conventions": ["命名/路径约定1", "..."],
  "constants": [{"name","values":["..."]}],
  "consumer_map": [{"module":"模块目录名","consumed_by":["消费它的模块名"],"expected_surface":"对方期望它暴露的接口/DTO/API 概述"}]
}}"""

CONTRACT_SKELETON_USER = """需求：
{task_description}

模块清单（含依赖关系 depends_on）：
{modules}

数据模型：
{data_model}

请只产出【全局骨架】JSON（conventions + 全局 constants + consumer_map）。求精准、勿写模块内部细节。"""

# ── Stage B：单模块视角，只产该模块 owns 的契约片（仿 TECH_DESIGN_STAGE2）──
CONTRACT_MODULE_SYSTEM = """你是系统架构师，正在为【一个模块】产出它在【全局共享契约】里负责的那一片。
全局骨架（命名约定/全局常量/谁消费你）已定，你只负责【当前这一个模块 owns 的契约】——
即【本模块对外暴露、供其他模块消费】的部分。不要管别的模块、不要重复全局常量。

只定本模块 owns 的共享契约：
1. interfaces：本模块对外暴露的跨模块接口名 + 完整方法签名（参数/返回类型）+ purpose。
2. dtos：本模块定义、被其他模块引用的数据结构 + 字段。
3. apis：本模块对外的 URL 路径 + HTTP 方法 + 请求/响应结构。
4. dependencies：本模块 pom.xml/构建文件【必须声明的全部】第三方依赖（编译期硬约束）。
   多个 worker 并行写本模块，谁建 pom 谁就得把整模块用到的依赖一次声明全——漏一个
   （如用了 RedisTemplate/@Slf4j/Validation 而 pom 没声明）整模块 mvn compile 必败、全量返工。
   - Java/Maven：用 artifactId（spring-boot-starter-data-redis、lombok、
     spring-boot-starter-validation、hutool-all、fastjson2…），跨 group 同名写 groupId:artifactId。
   - 契约阶段【代码尚未写】→ 据本模块【职责/功能】推断需要哪些库（非靠 import）。常见映射：
     JWT/令牌鉴权→io.jsonwebtoken:jjwt-api、缓存/会话→spring-boot-starter-data-redis、
     定时/调度→quartz、JSON 序列化→fastjson2 或 jackson、工具类→hutool-all、参数校验→
     spring-boot-starter-validation、日志/样板→lombok、HTTP 客户端→okhttp/httpclient。
     按职责把【所有】会用到的第三方库一次列全，宁多勿漏（漏一个整模块编译失败）。

所有条目的 module 字段都填【当前模块名】。严格输出 JSON：
{"interfaces":[{"name","module","signature":"完整方法签名","purpose"}],
 "dtos":[{"name","module","fields":["类型 字段名"]}],
 "apis":[{"path","method","request","response"}],
 "dependencies":[{"module":"当前模块名","artifacts":["artifactId 或 groupId:artifactId 并集"]}]}"""

CONTRACT_MODULE_USER = """总需求（背景）：{task_description}

数据模型：{data_model}

全局骨架（命名约定/全局常量——遵守它，保持一致）：
{skeleton}

## 当前要产出契约片的模块（第 {mod_idx}/{mod_total} 个）
模块名：{mod_name}
职责：{mod_responsibility}
被这些模块消费：{consumed_by}
对方期望你暴露：{expected_surface}

本模块已规划的文件清单（真实落盘文件名）：
{module_files}

★命名铁律★：interfaces/dtos 的 name 必须与上述清单里某个文件的主名一致（文件名去扩展名）——
清单里已有承载该概念的文件时【绝不】另起新名字（那会造出同一概念的重复文件）；只有清单里
确实没有对应文件的全新概念才允许新名字。I 前缀与去前缀视为同一概念（IAlarmService 与
AlarmService 是一个东西）：接口条目优先用接口形态的文件名，绝不把 *Impl 实现类名当接口名。

只产出【这个模块 owns 的契约片】JSON，module 字段统一填 "{mod_name}"。
务必填全 dependencies：列出本模块编译期需声明的全部第三方依赖并集（漏一个即整模块编译失败）。"""


def _contract_module_files_block(file_plan: list, mod_name: str, cap: int = 60) -> str:
    """R64-T4 源头预防：把本模块已规划文件的【文件名】注入契约 prompt。

    round64 实锤（cassette seq8-13）：契约与 file_plan 是两次独立 LLM 调用且契约 prompt
    不含 file_plan（has_file_plan=False）——两个命名空间独立产生，56 个契约符号 30 个 name
    对不上任何 file_plan basename（AlarmSimpleRequest↔SimpleNotifyRequest.java、
    AlarmComposeUtil 无文件），下游 R48b-1 被迫为幻影名造重复文件、R62-Task5 每轮重新归一
    （40→59 条）。治本=让契约命名对齐真实文件（栈中立：只注文件名，不注任何栈约定）。"""
    names: list[str] = []
    want = str(mod_name or "").strip().rstrip("/")
    for it in file_plan or []:
        mod = (it.get("module") if isinstance(it, dict) else getattr(it, "module", "")) or ""
        path = (it.get("path") if isinstance(it, dict) else getattr(it, "path", "")) or ""
        if str(mod).strip().rstrip("/") == want and path:
            base = str(path).replace("\\", "/").rstrip("/").rsplit("/", 1)[-1]
            if base and base not in names:
                names.append(base)
    if not names:
        if file_plan:
            # 猎手批2 F1：file_plan 非空却零匹配=模块名漂移（大小写/别名），铁律静默失效
            # 必须留痕——与 pin_contract_symbol_paths 的 full_miss 观察面同律对称。
            logger.info(
                "[R64-T4] 模块 %r 在 file_plan（共 %d 条）零匹配——命名铁律对该模块静默失效"
                "（疑模块名漂移，round65 观察面）", want, len(file_plan))
        return "（本模块暂无已规划文件清单——命名铁律不适用，按概念命名即可）"
    shown = names[:cap]
    tail = f"\n…（共 {len(names)} 个，仅列前 {cap}）" if len(names) > cap else ""
    return "\n".join(f"- {n}" for n in shown) + tail


def _normalize_contract_dependencies(raw) -> list[dict]:
    """把 LLM 产出的 dependencies 规整成 Rule5 可消费的 [{"module","artifacts":[...]}]。

    容错：① 标准 list[dict]；② dict 形式 {模块名: [artifacts]}；其余忽略。
    逐项去空白/去重，module 去尾斜杠；artifacts 全空的条目丢弃。纯函数、可单测。
    """
    out: list[dict] = []

    def _clean_arts(arts) -> list[str]:
        seen: list[str] = []
        for a in arts or []:
            s = str(a).strip()
            if s and s not in seen:
                seen.append(s)
        return seen

    if isinstance(raw, list):
        for entry in raw:
            if not isinstance(entry, dict):
                continue
            mod = str(entry.get("module") or "").strip().rstrip("/")
            arts = _clean_arts(entry.get("artifacts"))
            if mod and arts:
                out.append({"module": mod, "artifacts": arts})
    elif isinstance(raw, dict):
        for mod, arts in raw.items():
            mod = str(mod or "").strip().rstrip("/")
            arts = _clean_arts(arts if isinstance(arts, list) else [arts])
            if mod and arts:
                out.append({"module": mod, "artifacts": arts})
    return out


def _union_contract_member(cur, new) -> tuple:
    """并集合并两个契约成员值（同名接口/DTO/常量的 sig_field），返回 (合并值, 是否有变化)。

    - 任一为 list（dtos.fields / constants.values）→ 按出现顺序并集去重；
    - 否则按 str（interfaces.signature）→ 不同签名按行并集（已有行不重复并入），合并成多行串。
    keep-first 会丢方法；并集保证同名接口的所有方法/字段都进共享契约。
    """
    if isinstance(cur, list) or isinstance(new, list):
        def _as_list(v):
            if isinstance(v, list):
                return list(v)
            return [v] if v not in (None, "") else []
        out_l = _as_list(cur)
        changed = False
        for x in _as_list(new):
            if x not in out_l:
                out_l.append(x)
                changed = True
        return out_l, changed

    cur_s = str(cur or "").strip()
    new_s = str(new or "").strip()
    if not new_s or new_s == cur_s:
        return cur_s, False
    if not cur_s:
        return new_s, True
    existing = {ln.strip() for ln in cur_s.splitlines() if ln.strip()}
    new_lines = [ln.strip() for ln in new_s.splitlines() if ln.strip() and ln.strip() not in existing]
    if not new_lines:
        return cur_s, False
    return cur_s + "\n" + "\n".join(new_lines), True


def _merge_module_contracts(skeleton: dict, slices: list[dict]) -> dict:
    """Stage C：把全局骨架 + 各模块契约片【确定性合并】成全局共享契约（0 LLM，纯函数可单测）。

    - union：interfaces/dtos/apis/dependencies 各模块片并集；conventions/constants 取自骨架。
    - 冲突告警（决策2）：同名不同定义（interfaces 比 signature、dtos 比 fields、constants 比 values）
      → logger.warning + 保留首个定义，graceful degrade，不阻断、不调 LLM。
    - dependencies：合并 + 现有 _normalize_contract_dependencies 归一 + 按模块并集成一条/模块。
    输出与单体版【完全相同】的 schema（interfaces/dtos/constants/apis/conventions/dependencies），
    下游 contract_symbols/Rule5/worker 注入零改动。
    """
    skeleton = skeleton if isinstance(skeleton, dict) else {}
    slices = [s for s in slices if isinstance(s, dict)]

    def _merge_named(groups: list, key_label: str, sig_field: str) -> list[dict]:
        """按 name 并集合并；同名项的 sig_field 成员【取并集，不丢方法/字段】。

        治本（keep-first 隐患）：大模型大块结构化生成时常把同一接口重复吐多遍、每遍签名略有
        出入。旧实现"保留首版、丢弃其余"：若被丢版含首版没有的方法 → 全局共享契约对该接口
        【不完整】→ 下游消费方调缺失方法 → worker cannot-find-method。改为按成员并集合并：
          - sig_field 为 list（dtos.fields / constants.values）→ 成员并集（保序去重）；
          - sig_field 为 str（interfaces.signature 完整方法签名）→ 不同签名串并集（按行去重后
            合并成多行），确保所有方法都进共享契约。
          - 完全相同 → 静默去重，不告警。
        """
        out: list[dict] = []
        seen: dict[str, dict] = {}
        # P6（round37b）：同模块自并计数——U1/U3 bisect 把一个模块拆成 ~a/~b 子批，各自生成该
        # 模块的共享接口 → 同名同模块重复（round37 实证 IAlarmTaskService "alarm-core 并入
        # alarm-core" 并 8 次）。这不是"跨模块同名多版冲突"而是模块边界重叠/bisect 产物：仍并集
        # 保方法（安全），但按【同模块 vs 跨模块】分流日志——同模块聚合成一条边界重叠告警（surface
        # 真信号、去 churn），跨模块才是真多版 INFO。栈无关：只比 module 字段，不含任何框架词汇。
        _intra_module_dups: dict[str, int] = {}
        for group in groups:
            for item in (group or []):
                if not isinstance(item, dict):
                    continue
                name = str(item.get("name") or "").strip()
                if not name:
                    out.append(item)  # 无名项不去重，直接并入
                    continue
                # D10（阶段6，登记册 §五）：合并键改 (module, name)——旧裸 name 键把
                # 跨模块同名接口强行合体（module 归属取首版，round37 实测 168→148 的
                # 来源）。同模块同名（bisect/边界重叠）照旧并集；跨模块同名=不同契约，
                # 各自独立成条（无 module 字段归入 "" 桶=保留旧全局并集语义兜底）。
                _key = (str(item.get("module") or "").strip(), name)
                if _key not in seen:
                    merged = dict(item)
                    seen[_key] = merged
                    out.append(merged)  # out 与 seen 共享同一引用，后续并集就地生效
                    continue
                base = seen[_key]
                _base_mod = str(base.get("module") or "").strip()
                _item_mod = str(item.get("module") or "").strip()
                _intra = _base_mod and _item_mod and _base_mod == _item_mod
                merged_val, changed = _union_contract_member(
                    base.get(sig_field), item.get(sig_field)
                )
                if changed:
                    base[sig_field] = merged_val
                if _intra:
                    # 同模块自并（边界重叠/bisect）：聚合计数，循环后出一条告警，不逐次 churn
                    _intra_module_dups[f"{_base_mod}:{name}"] = \
                        _intra_module_dups.get(f"{_base_mod}:{name}", 0) + 1
                elif changed:
                    # 6.9-Rp7：D10 后本分支仅 ""-桶（双方缺 module 字段的兜底并集）可达，
                    # 措辞如实（跨模块同名已各自独立成条，不再进这里）。
                    logger.info(
                        "[CONTRACT_MERGE] %s '%s' 无 module 归属的同名多版 → ""桶并集合并"
                        "(不丢方法/字段，兜底旧全局语义)",
                        key_label, name,
                    )
        if _intra_module_dups:
            _total = sum(_intra_module_dups.values())
            _top = sorted(_intra_module_dups.items(), key=lambda kv: -kv[1])[:5]
            logger.warning(
                "[CONTRACT_MERGE] P6 模块边界重叠：%s 出现 %d 处【同模块同名】自并去重"
                "（bisect 子批/边界重叠使一个模块的接口被多片重复生成，已并集保方法）"
                "，Top: %s", key_label, _total,
                ", ".join(f"{k}×{v + 1}" for k, v in _top))
        return out

    interfaces = _merge_named([s.get("interfaces") for s in slices], "interfaces", "signature")
    dtos = _merge_named([s.get("dtos") for s in slices], "dtos", "fields")
    constants = _merge_named([skeleton.get("constants")], "constants", "values")

    apis_out: list[dict] = []
    _api_seen: set = set()
    for s in slices:
        for a in (s.get("apis") or []):
            if not isinstance(a, dict):
                continue
            k = (str(a.get("path") or ""), str(a.get("method") or ""))
            if k in _api_seen:
                continue
            _api_seen.add(k)
            apis_out.append(a)

    # 每片【先各自归一】（容 list 与 dict 两种形态），再按模块并集成【一条/模块】（防重复 acceptance 注入）
    by_mod: dict[str, list[str]] = {}
    order: list[str] = []
    norm_deps: list[dict] = []
    for s in slices:
        norm_deps.extend(_normalize_contract_dependencies(s.get("dependencies")))
    for d in norm_deps:
        m = d["module"]
        if m not in by_mod:
            by_mod[m] = []
            order.append(m)
        for a in d["artifacts"]:
            if a not in by_mod[m]:
                by_mod[m].append(a)
    dependencies = [{"module": m, "artifacts": by_mod[m]} for m in order]

    return {
        "interfaces": interfaces,
        "dtos": dtos,
        "constants": constants,
        "apis": apis_out,
        "conventions": [c for c in (skeleton.get("conventions") or []) if c],
        "dependencies": dependencies,
    }


async def contract_design(state: BrainState) -> dict:
    """共享契约设计节点（T1）——三段式：骨架 → 逐模块并发 → 确定性合并。

    单体一次性生成全局契约会让云端 reasoning 模型 runaway（实测 GLM-5.2/Kimi 均 20+min/6w chunk
    才 stall→failover）。治本：契约的"全局一致"只需一次【确定性对账】，不需一次性吐完所有字。
      Stage A 骨架（1 次小调用）：全局 conventions/constants/consumer_map。
      Stage B 逐模块并发（仿 _tech_design_staged Stage2）：每模块只产自己 owns 的契约片，小而有界。
      Stage C 确定性合并（0 LLM）：union + 冲突告警 + 依赖归一 → 全局契约。
    每调用小、有界、可并发，runaway 从根上消失，随模块数水平扩展。仅 ultra+多模块走三段式；
    其余直通沿用 tech_design 的 shared_contract_draft。输出 schema 与单体版一致，下游零改动。
    产出的 shared_contract 会：① 注入每个 worker 作只读契约；② 作为"契约子任务"最先落盘（dispatch 层）。
    """
    import asyncio as _asyncio
    import time as _time

    td = state.get("tech_design") or {}
    modules = td.get("modules") or []
    comp = state.get("assessed_complexity") or state.get("complexity")
    comp_str = comp.value if hasattr(comp, "value") else str(comp)

    # 仅 ultra 多模块才需要全局契约（简单/单模块沿用 draft，零开销）
    if comp_str != "ultra" or len(modules) < 2:
        # C4-8 复核补漏：always-emit 含此早退——否则上一轮（replan 重入降级场景）的
        # contract_failed_modules 粘滞跨轮。
        return {"contract_failed_modules": []}

    task_desc = state.get("task_description", "") or ""
    data_model = str(td.get("data_model", ""))
    _valid_mods = [m for m in modules if isinstance(m, dict)]
    mod_total = len(_valid_mods)
    llm = _get_brain_llm()

    # ── Stage A：全局骨架（1 次小调用）──
    _t0 = _time.monotonic()
    logger.info("[CONTRACT_SKELETON] 启动全局骨架（%d 模块：conventions/constants/consumer_map）…", mod_total)
    # 治本 B：Stage A 加【重试 + 独立更大预算】（镜像 Stage B 逐模块的成熟模式，此前 Stage A
    # 独缺重试且共用 600s → consumer_map 单点故障一次超时即全丢）。拿到有效骨架(尤其 consumer_map)
    # 即成功；耗尽重试才降级。
    skeleton: dict = {}
    _skel_ok = False
    _skel_err = "unknown"
    for _attempt in range(1, _CONTRACT_SKELETON_MAX_ATTEMPTS + 1):
        _ta = _time.monotonic()
        try:
            respA = await _asyncio.wait_for(llm.ainvoke([
                {"role": "system", "content": CONTRACT_SKELETON_SYSTEM},
                {"role": "user", "content": CONTRACT_SKELETON_USER.format(
                    task_description=task_desc[:2500],
                    modules=json.dumps(_valid_mods, ensure_ascii=False)[:2500],
                    data_model=data_model[:2000],
                )},
            ]), timeout=_CONTRACT_SKELETON_TIMEOUT)
            skel_raw = _parse_json_from_llm(respA.content)
            skeleton = skel_raw.get("skeleton", skel_raw) if isinstance(skel_raw, dict) else {}
            if not isinstance(skeleton, dict):
                skeleton = {}
            # 成功返回并解析为 dict 即视为成功（空骨架也合法——模型可能无全局 conventions/constants/
            # consumer_map，Stage B 逐模块照常跑；只重试【超时/异常】这类"没拿到结果"，不重试空内容）。
            _skel_ok = True
            break
        except _asyncio.TimeoutError:
            # 治本 A：错因可见——旧代码 `%s % TimeoutError()` 渲染为空串，运维永远看不出是【超时】
            # 还是模型报错。显式记超时 + 预算 + 提示"模型可能仍在生成被掐断"。
            _skel_err = f"超时 {_CONTRACT_SKELETON_TIMEOUT:.0f}s（模型可能仍在生成被墙钟掐断）"
        except Exception as exc:  # noqa: BLE001
            _skel_err = f"{type(exc).__name__}: {str(exc)[:200]}"
            # R38-C：账本拒绝 → 等在飞结算再重试；hopeless/超时 → 立即降级（不空转）。
            if _is_token_limit_error(exc):
                if not await _await_token_admission(
                        state.get("task_id"), getattr(exc, "usage", None) or {},
                        max_wait_s=_CONTRACT_SKELETON_TIMEOUT):
                    _skel_err = f"admission_denied:{_skel_err}"
                    break
                continue
        if _attempt < _CONTRACT_SKELETON_MAX_ATTEMPTS:
            logger.warning(
                "[CONTRACT_SKELETON] 第 %d/%d 次失败(%s)，退避重试（耗时 %.1fs）",
                _attempt, _CONTRACT_SKELETON_MAX_ATTEMPTS, _skel_err, _time.monotonic() - _ta,
            )
            await _asyncio.sleep(min(2.0 * _attempt, 10.0))
    if not _skel_ok:
        # consumer_map 丢失 = ② 跨模块 depends_on 无从确定性连线（靠 worker `_build_blocked_on_
        # unbuilt_internal` BLOCKED 退避兜症状）。记 error 级（非 warning）+ 明确错因，别再静默空消息。
        # R38 复核 F3：报真实尝试数（admission hopeless 早退时旧日志谎报"重试 MAX 次"）
        logger.error(
            "[CONTRACT_SKELETON] %d 次尝试后失败放弃，降级沿用 tech_design draft（consumer_map 丢失"
            "→跨模块依赖靠 worker BLOCKED 退避兜底）: %s", _attempt, _skel_err,
        )
        # C4-8 复核补漏：骨架失败=【全部】模块契约片丢失（最坏场景），机读账必须如实
        # ——旧 `return {}` 恰在最坏场景无账（VALIDATE/交付面全盲）。
        return {"contract_failed_modules":
                    [str(m.get("name") or f"module-{i}") for i, m in
                     enumerate(_valid_mods, start=1)],
                "degraded_reasons": list(state.get("degraded_reasons") or []) + [
                    f"共享契约骨架生成失败（{_skel_err}）——全部 {mod_total} 模块契约片丢失，"
                    "跨模块依赖靠 worker BLOCKED 退避兜底"]}
    cmap: dict[str, dict] = {}
    for entry in (skeleton.get("consumer_map") or []):
        if isinstance(entry, dict) and entry.get("module"):
            cmap[str(entry["module"]).strip()] = entry
    logger.info(
        "[CONTRACT_SKELETON] 骨架产出：conventions=%d constants=%d consumer_map=%d，耗时 %.1fs",
        len(skeleton.get("conventions") or []), len(skeleton.get("constants") or []),
        len(skeleton.get("consumer_map") or []), _time.monotonic() - _t0,
    )

    # ── Stage B：逐模块并发产契约片（仿 _tech_design_staged Stage2：Semaphore + gather + 重试）──
    _sem = _asyncio.Semaphore(_CONTRACT_CONCURRENCY)

    async def _gen_one_module_contract(mi: int, mod: dict) -> dict:
        # R38 复核 F8：与 STAGE2 同款——准入重试不占能力配额，独立有界。
        mod_name = mod.get("name") or f"module-{mi}"
        cm = cmap.get(mod_name.strip(), {})
        _last_err = "unknown"
        _attempt = 0
        _adm_retries = 0
        while _attempt < _CONTRACT_MAX_ATTEMPTS and _adm_retries <= _ADMISSION_RETRY_MAX:
            _tm = _time.monotonic()
            _token_denied: dict | None = None  # R38-C：账本拒绝时带 usage，信号量外等准入
            # C4-7（round38c alarm-engine 3×600s 白烧 30min）：反馈式重试——原样重放
            # 超时输入必然再超时。第 2 次收缩 data_model 配额、第 3 次再去 skeleton，
            # 并注入上次失败原因+精简要求；账本准入重试不变形（非能力问题）。
            _dm_quota = 2000 if _attempt == 0 else (1000 if _attempt == 1 else 600)
            _sk_payload = (json.dumps(
                {"conventions": skeleton.get("conventions") or [],
                 "constants": skeleton.get("constants") or []},
                ensure_ascii=False)[:1500] if _attempt < 2 else "{}")
            _retry_brief = ("" if _attempt == 0 else (
                f"\n\n注意：上次生成失败（{_last_err}），本次输入已精简。请输出【更精简】的"
                "契约：只给接口签名与必要 DTO 字段，省略一切描述性文字，控制输出长度。"))
            async with _sem:
                try:
                    resp = await _asyncio.wait_for(llm.ainvoke([
                        {"role": "system", "content": CONTRACT_MODULE_SYSTEM},
                        {"role": "user", "content": CONTRACT_MODULE_USER.format(
                            task_description=task_desc[:1500],
                            data_model=data_model[:_dm_quota],
                            skeleton=_sk_payload,
                            mod_idx=mi, mod_total=mod_total, mod_name=mod_name,
                            mod_responsibility=mod.get("responsibility", ""),
                            consumed_by="、".join(cm.get("consumed_by") or []) or "（无）",
                            expected_surface=cm.get("expected_surface", "") or "（无特别约定）",
                            # R64-T4：契约命名对齐真实 file_plan（源头预防命名漂移）
                            module_files=_contract_module_files_block(
                                state.get("tech_design_file_plan") or [], mod_name),
                        ) + _retry_brief},
                    ]), timeout=_CONTRACT_STAGE_TIMEOUT)
                    r = _parse_json_from_llm(resp.content)
                    if not isinstance(r, dict):
                        raise ValueError("非 JSON dict")
                    # 兜底 module 字段（owner 归属，供 Stage C 合并 + 下游 Rule5 按模块注入）
                    for k in ("interfaces", "dtos", "dependencies"):
                        for it in (r.get(k) or []):
                            if isinstance(it, dict) and not it.get("module"):
                                it["module"] = mod_name
                    logger.info(
                        "[CONTRACT_MODULE] 模块 %d/%d '%s' → 接口=%d DTO=%d API=%d 依赖=%d，耗时 %.1fs%s",
                        mi, mod_total, mod_name, len(r.get("interfaces") or []),
                        len(r.get("dtos") or []), len(r.get("apis") or []),
                        len(r.get("dependencies") or []), _time.monotonic() - _tm,
                        f"（第 {_attempt + _adm_retries + 1} 次调用成功）"
                        if (_attempt + _adm_retries) else "",
                    )
                    return {"idx": mi, "name": mod_name, "slice": r, "error": None}
                except _asyncio.TimeoutError:
                    _last_err = "timeout"
                except Exception as exc:  # noqa: BLE001
                    _last_err = str(exc)[:200]
                    if _is_token_limit_error(exc):
                        _token_denied = getattr(exc, "usage", None) or {}
            # R38-C：账本拒绝 → 等在飞结算释放预留再重试（固定退避 2s/4s 等不到 408s 的
            # settle，且不占能力配额）；hopeless/超时 → 立即确定性放弃。
            if _token_denied is not None:
                if not await _await_token_admission(
                        state.get("task_id"), _token_denied,
                        max_wait_s=_CONTRACT_STAGE_TIMEOUT):
                    _last_err = f"admission_denied:{_last_err}"
                    break
                _adm_retries += 1
                continue  # 已按准入等待，跳过固定退避直接重试
            _attempt += 1
            if _attempt < _CONTRACT_MAX_ATTEMPTS:
                logger.warning(
                    "[CONTRACT_MODULE] 模块 %d/%d '%s' 第 %d 次失败(%s)，退避重试",
                    mi, mod_total, mod_name, _attempt, _last_err,
                )
                # P1-E：退避——超时/瞬时拥塞多因端点争抢，给它喘息再试（指数，封顶 10s）。
                await _asyncio.sleep(min(2.0 * _attempt, 10.0))
        # R38 复核 F3：报真实次数与放弃原因（早退时旧日志谎报"重试 MAX 次"误导排障）
        logger.error(
            "[CONTRACT_MODULE] 模块 %d/%d '%s' 失败放弃（能力重试 %d/%d，准入重试 %d，"
            "该模块契约片丢失）: %s",
            mi, mod_total, mod_name, _attempt, _CONTRACT_MAX_ATTEMPTS, _adm_retries, _last_err,
        )
        return {"idx": mi, "name": mod_name, "slice": {}, "error": _last_err}

    _results = await _asyncio.gather(
        *[_gen_one_module_contract(mi, mod) for mi, mod in enumerate(_valid_mods, start=1)]
    )
    _results.sort(key=lambda r: r["idx"])
    slices = [r["slice"] for r in _results if not r["error"]]
    failed = [r["name"] for r in _results if r["error"]]
    _degraded: list[str] = list(state.get("degraded_reasons") or [])
    if failed:
        logger.error(
            "[CONTRACT_MODULE] ⚠ %d/%d 模块契约片产出失败 %s——合并将缺这些模块的接口/依赖，"
            "下游缺依赖编译失败靠定向恢复兜底",
            len(failed), mod_total, failed,
        )
        # #22：契约片缺失透传人工可见（非硬闸——契约仅辅助，worker BLOCKED 退避+定向恢复兜底），
        # 让交付/通知能看到"共享契约不完整"。
        _degraded.append(
            f"共享契约 {len(failed)}/{mod_total} 模块契约片生成失败 {failed}"
            "——这些模块的接口/依赖未进契约，下游靠定向恢复兜底"
        )
    if not slices:
        logger.warning("[CONTRACT_DESIGN] 全部模块契约片失败，降级沿用 tech_design draft")
        # C4-8：缺片机读化（always-emit，含空清空防粘滞）——旧实现只进 log+degraded
        # 人读字符串，VALIDATE/交付面对"契约少一片"全盲（round38c alarm-engine 实证）。
        return {"degraded_reasons": _degraded, "contract_failed_modules": failed} if failed else {
            "contract_failed_modules": []}

    # ── Stage C：确定性合并（0 LLM）──
    merged = _merge_module_contracts(skeleton, slices)
    logger.info(
        "[CONTRACT_MERGE] 合并完成：接口=%d DTO=%d 常量=%d API=%d 约定=%d 模块依赖=%d（%d/%d 模块成功）",
        len(merged["interfaces"]), len(merged["dtos"]), len(merged["constants"]),
        len(merged["apis"]), len(merged["conventions"]), len(merged["dependencies"]),
        len(slices), mod_total,
    )
    if not merged["dependencies"]:
        logger.warning(
            "[CONTRACT_MERGE] 契约未含 dependencies——Rule5 将空转，缺依赖编译失败只能靠定向恢复兜底",
        )
    _out_contract: dict = {"shared_contract_draft": merged or state.get("shared_contract_draft") or {}}
    if failed:
        _out_contract["degraded_reasons"] = _degraded
    # C4-8：契约缺片机读键 always-emit（成功路径清空防跨轮粘滞；失败路径供
    # VALIDATE/交付面消费——旧实现只有人读 degraded 字符串，机器对缺片全盲）。
    _out_contract["contract_failed_modules"] = failed
    return _out_contract


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
        # R38-F：自动通过前对账 tech_design 失败清单——round38 实测 7/9 模块设计丢失后
        # 2ms"方案通过"，残缺 file_plan 静默流向 PLAN。非空 → 打回外科补齐（repair_only
        # 标记让 tech_design 只补缺失模块，绝不全量重拆）；受 design_rejects 上限约束，
        # 到顶带 degraded（TECH_DESIGN 已写）强制继续，不死循环。
        _failed = state.get("tech_design_failed_modules") or []
        if _failed and reject_count < _tier_limits()["design_rejects"]:
            _names = [m.get("name", "?") for m in _failed if isinstance(m, dict)]
            _fb = (f"tech_design {len(_failed)} 个模块设计缺失 {_names}"
                   "——只补齐缺失模块的 file_plan，勿全量重拆已成模块")
            logger.warning("[REVIEW_DESIGN] R38-F 对账：%s → 打回外科补齐（第 %d 次）",
                           _fb, reject_count + 1)
            return {"design_review": {"decision": "reject", "feedback": _fb,
                                      "reject_count": reject_count + 1,
                                      "repair_only": True}}
        if _failed:
            logger.error(
                "[REVIEW_DESIGN] R38-F：外科补齐 %d 次后仍缺 %d 模块，带 degraded 强制继续"
                "（交付/通知可见不完整）", reject_count, len(_failed))
        return {"design_review": {"decision": "approve", "feedback": "自动化模式自动通过", "reject_count": reject_count}}

    if reject_count >= _tier_limits()["design_rejects"]:
        _mdr = _tier_limits()["design_rejects"]
        logger.warning("[REVIEW_DESIGN] 打回达上限 %d，强制通过并标记需人工关注", _mdr)
        return {"design_review": {"decision": "approve", "feedback": f"打回{reject_count}次达上限，强制继续", "reject_count": reject_count, "forced": True}}

    decision = interrupt({
        "type": "review_design",
        "task_id": state.get("task_id"),
        "tech_design": state.get("tech_design"),
        "shared_contract": state.get("shared_contract_draft"),
        "reject_count": reject_count,
        "message": "请评审技术方案：通过则进入任务拆解，打回请填写反馈（最多打回 3 次）。",
    })

    # fail-closed：仅【显式 approve】才放行方案，其余（reject / 未知 / 畸形 payload）一律按打回。
    # 安全前提已核实：submit_design_review(api/routers/task.py) 强校验 decision∈{approve,reject}
    # 才会推进，故合法 approve 必是 {"decision":"approve"}；未知只可能来自非 API 入口/损坏 resume，
    # 按打回再评审一轮（reject_count 有上限，到顶 review_design 强制通过兜底，不会死循环）。
    _dec = decision.get("decision") if isinstance(decision, dict) else None
    _fb = decision.get("feedback", "") if isinstance(decision, dict) else ""
    if _dec == "approve":
        logger.info("[REVIEW_DESIGN] 方案通过")
        return {"design_review": {"decision": "approve", "feedback": _fb, "reject_count": reject_count}}

    if _dec != "reject":
        logger.warning("[REVIEW_DESIGN] 未知决策 payload=%r → fail-closed 按打回处理", decision)
    logger.info("[REVIEW_DESIGN] 方案被打回（第 %d 次）: %s", reject_count + 1, _fb[:60])
    return {"design_review": {"decision": "reject", "feedback": _fb, "reject_count": reject_count + 1}}


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
    # 脚手架排序边【单一权威判据】——见 contract_utils.is_structural_scaffold_dep 的不变量。
    from swarm.brain.contract_utils import is_structural_scaffold_dep
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
            # 条件0（R62 治本·结构性边不可剥）：目标是【脚手架子任务】的边，是确定性构建顺序
            # 约束（Maven 继承地基：父 pom 先落地、本模块 pom 先落地），绝非"LLM 误加的假依赖"。
            # decouple 的"文件重叠+契约耦合"启发式对它天然盲（pom↔代码零文件重叠），剥了就是
            # round62 死因（module 脚手架空 depends_on → 与聚合父同一并行 wave → module_registered_
            # before_scaffold → 连坐放弃）。判据用【单一权威】is_structural_scaffold_dep（=结构性
            # _is_scaffold_subtask，非 id 前缀）——同时覆盖注入器脚手架 + R58-3 LLM 认领 pom 者
            # （对抗复核实锤：后者无 st-scaffold- id 但同样被误剥）。无条件保留。
            if is_structural_scaffold_dep(dep):
                kept.append(dep_id)
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
    _max_resplit = _tier_limits()["elaborate_resplit"]

    # ★R65E-T3（效率治本，round65e4 Agent A ~22min 浪费实测）★ 二次拆分的【LLM 环】（round65e4
    # 每轮 ~11min）结构性改不了模块 coherence 违例——LLM resplit 只在【模块内】拆子任务，绝不动
    # module 归属 / tech_design_file_plan。若 plan 已带【file_plan 通道】的 G1 硬违例，validate_plan
    # 必打回本轮、全量重产，这一轮的 LLM resplit 是注定被丢弃的烧钱空转 → 跳过。
    #
    # ★复核 HIGH CONFIRMED 整改（soundness 要害）★ 预检【只】据 file_plan 通道（tech_design_file_plan
    # 驱动的 fp_ambiguous），用【空 subtasks 探针】隔离——该通道 elaborate 全程不碰（state 级），
    # 故"预检 invalid ⟹ validate_plan invalid"铁成立（validate_plan 的 fp_ambiguous 读同一份 file_plan，
    # 有此违例 ambiguous 必非空、必 invalid，与 cands 无关）。★绝不据 plan.subtasks 的 cands 通道★：
    # normalize_plan_scopes Rule-0 幻觉路径重定位会在【尾段】把 writable 迁到真目录、令 cands 违例
    # invalid→valid 翻转（复核实测复现）；据它抢跳=对一个尾段会自愈的好 plan 误跳 resplit、超预算
    # 子任务未拆直奔 dispatch。round65e4 死因本体正是 file_plan 通道 fp_ambiguous，覆盖无损。
    # ★只跳 LLM 环★：确定性文件拆 `_split_oversized_by_files` 照跑（省的只是 LLM ~11min，且 doomed
    # plan 必被 validate_plan 打回不进 dispatch）。gate 关（SWARM_MODULE_COHERENCE_GATE=0）→ validate
    # 不据 coherence 打回 → 本预检亦不跳（严格与闸同步）。异常保守照常 resplit（不炸主链）。
    _skip_llm_resplit_doomed = False
    _mc_gate_on = os.environ.get(
        "SWARM_MODULE_COHERENCE_GATE", "1").strip().lower() not in ("0", "false", "no", "off")
    if _mc_gate_on:
        try:
            from types import SimpleNamespace as _NS

            from swarm.brain.contract_utils import _resolve_module_dirs as _e_rmd
            # 空 subtasks 探针 → resolver 的 cands 通道被清空，`ambiguous` 纯由 tech_design_file_plan
            # 的 fp_ambiguous 驱动。shared_contract 保留以维持模块宇宙。★只据 ambiguous①（fp_ambiguous）★：
            # 它源自 state 级 file_plan、elaborate 全程不碰 → 100% 不变；validate_plan 的 ambiguous ⊇
            # 本 fp_ambiguous（读同一 file_plan），故非空 ⟹ validate_plan 必 invalid，铁成立。★绝不据
            # collision②★：collision 由 out[mod] 物理目录解析得来，加入 plan.subtasks 的代码证据可能改
            # out[mod]、把 probe 的 collision 解掉 → probe-only collision 非 elaborate-invariant、据它抢
            # 跳会误伤尾段自愈的好 plan。project_path/base_ref 必须与 validate_plan 同源（#82/R65E-T1
            # 既有基线接线的 fp_ambiguous 缩减要一致，否则 None 更保守=可能多判 ambiguous 致误跳）。
            _probe = _NS(subtasks=[], shared_contract=getattr(plan_obj, "shared_contract", None) or {})
            _e_out, _e_amb, _e_coll = _e_rmd(
                _probe,
                _get_project_path(state.get("project_id") or ""),
                state.get("tech_design_file_plan") or [],
                base_ref=state.get("base_commit"))
            if _e_amb:
                _skip_llm_resplit_doomed = True
                logger.warning(
                    "[ELABORATE] R65E-T3 plan 带 file_plan 通道 G1 一对多违①（%d 模块跨多物理目录）→ "
                    "跳过本轮二次拆分的 LLM 环（validate_plan 必打回、LLM resplit 改不了 file_plan 归属，"
                    "省一整轮重产烧钱）；确定性文件拆照跑、validate_plan 仍为唯一权威: %s",
                    len(_e_amb), dict(list(_e_amb.items())[:2]))
        except Exception as exc:  # noqa: BLE001 — 预检失败保守照常 resplit，绝不炸主链
            logger.warning("[ELABORATE] R65E-T3 coherence 预检异常，保守照常 resplit（含 LLM 环）: %s",
                           exc, exc_info=True)

    # 多轮：每轮找出"需再拆"的子任务，二次拆分替换，重新检查
    while resplit_rounds < _max_resplit:
        need_resplit = [st for st in plan_obj.subtasks if _needs_resplit(st, budget)]
        if not need_resplit:
            break
        resplit_rounds += 1
        new_subtasks = list(plan_obj.subtasks)
        changed = False
        for st in need_resplit:
            # 文件数超标 → 确定性按层拆(可复现、精准投喂)；纯上下文预算超标(文件少但大)→ LLM 拆。
            if _oversized_by_files(st):
                children = _split_oversized_by_files(st)
            elif _skip_llm_resplit_doomed:
                # R65E-T3：file_plan-doomed 本轮 → 跳过 LLM 二次拆分（~11min 空转），确定性文件拆已在上
                # 分支照跑；该 plan 必被 validate_plan 打回、不进 dispatch，未拆的 token 超预算项无外溢风险。
                continue
            else:
                children = await _resplit_subtask(st, state, budget)
            if children and len(children) > 1:
                idx = next((i for i, x in enumerate(new_subtasks) if x.id == st.id), None)
                if idx is not None:
                    new_subtasks[idx:idx + 1] = children
                    changed = True
                    logger.info("[ELABORATE] 子任务 %s 二次拆分为 %d 个", st.id, len(children))
                    # P0-1 修复：二次拆分替换了节点，但其它子任务可能仍 depends_on 旧 id
                    # （如 st-2 depends_on st-1，st-1 被拆成 st-1-1/st-1-2 后 st-1 已不存在）。
                    # 必须把所有指向旧 id 的下游依赖重映射到子链尾节点（children[-1]），
                    # 否则 VALIDATE_PLAN 结构校验必报"依赖未知任务"，陷入规划死循环。
                    # 映射到【终端节点集】：串行链=尾节点；平行 fan-out=全部终端 leaf/协调者
                    # （下游依赖父=依赖整批完成，取 children[-1] 会让平行拆下的下游提前 ready）。
                    remapped = _remap_dependents_to_terminals(new_subtasks, st.id, children)
                    if remapped:
                        logger.info(
                            "[ELABORATE] 重映射 %d 条下游依赖: %s → %s（避免悬空依赖）",
                            remapped, st.id, children[-1].id,
                        )
        if not changed:
            break  # LLM 拆不动了，避免空转
        plan_obj = _rebuild_plan(plan_obj, new_subtasks)

    # ── I6：剥离 LLM 误加的假 depends_on，提升 dispatch 并行度 ──
    # 注意顺序：decouple 必须在 normalize【之前】跑。decouple 用"文件重叠"判真假依赖，
    # 需要看到子任务【归一前】的原始写意图（task 0f93f1fc 真实场景：st-2 readable
    # st-1 产出的 NumberUtils.java 是真依赖）。若 normalize 先跑把 st-1 子链尾节点的
    # 写权降级，decouple 会误判 st-2 与尾节点"零文件重叠"而错误剥离真依赖。
    decoupled = _decouple_independent_subtasks(plan_obj)

    # ── P1-1：scope 归一（同文件写权唯一 + 降级者依赖首写者，Bug-3 防并发写冲突）──
    # 放在 decouple 之后。Bug-3 的并发安全由 normalize 的"降级者依赖首写者"独立保证，
    # 不依赖与 decouple 的相对顺序——normalize 加的 st-1-1→st-1-2 依赖因两者文件重叠
    # 不会被（已跑完的）decouple 剥离。
    from swarm.brain.contract_utils import (
        correct_misclassified_intent,
        enrich_context_snippets,
        enrich_java_package_readable,
        inject_api_knowledge,
        prune_empty_scope_subtasks,
        resolve_plan_conflicts,
        wire_readable_provenance,
    )
    # project_path 先解析：normalize 需据"文件是否已存在于 repo"区分聚合修改 vs 新建撞车
    # （治本文件争抢——已存在聚合文件多写者串行保留写权，不静默降级丢贡献）。
    _proj_path = None
    try:
        from swarm.project import store as _store
        _pid = state.get("project_id") or ""
        if _pid:
            _proj = _store.get_project(_pid)
            _proj_path = _proj.get("path") if _proj else None
    except Exception as exc:  # noqa: BLE001
        logger.debug("[ELABORATE] 获取 project_path 失败，scope 归一退化为 demote 行为: %s", exc)

    # ── 计划冲突解决【唯一事实源】：dedupe → fix_dep → normalize → bump_difficulty（顺序是治本要害） ──
    # 顺序固化在 resolve_plan_conflicts（contract_utils），_elaborate 与离线 plan-quality 评测共用同一条
    # 代码，杜绝调用点各写一份导致漂移。RUN17/18/19 三轮治本(脚手架合并/依赖序/单一写者/难度)全在此收口。
    _resolve = resolve_plan_conflicts(plan_obj, project_path=_proj_path,
                                      base_ref=state.get("base_commit"))  # B6 #2：钉扎 base 非实时 HEAD
    if _resolve["dep_reordered"]:
        logger.info("[ELABORATE] 依赖序修正：脚手架置根 + SQL 依赖实体跑最后（杜绝 SQL 巨任务成全局根瓶颈卡死）")
    if _resolve["difficulty_bumped"]:
        logger.info("[ELABORATE] 脚手架难度提升：%d 个脚手架/根pom写者 trivial→MEDIUM（避开单发拒答）",
                    _resolve["difficulty_bumped"])

    # ── G3（Task#9 审计③ GAP2）：demote 之后【重跑】空 scope 剪除 ──
    # 唯一的 prune 跑在 plan 节点、在 ELABORATE 之前；而 normalize_plan_scopes（在
    # resolve_plan_conflicts 内）把撞车的非首写者 demote 成 readable，可能把子任务 scope 收成空
    # → 空写 scope 死任务漏到 dispatch（round62 empty-diff churn 仍可达那条路）。此处 demote 之后
    # 重剪，幂等、栈中立、绝不剪空计划（守卫见 prune_empty_scope_subtasks）。
    _repruned = prune_empty_scope_subtasks(plan_obj)
    if _repruned:
        logger.info("[ELABORATE] G3: demote 后重剪 %d 个空写 scope 死子任务（防漏到 dispatch）: %s",
                    len(_repruned), _repruned)

    # ── 意图校正(task dbfc265f)：LLM 把功能需求误判 AUDIT 但 scope 有写文件 → 纠正为
    # MODIFY/CREATE，避免走 security_audit 不产 diff → findings=0 假失败 → retry 死循环。
    if correct_misclassified_intent(plan_obj):
        logger.info("[ELABORATE] 意图校正：AUDIT 子任务含写文件 → 纠正为 MODIFY/CREATE（确定性信号覆盖 LLM 误判）")

    # ── P2-1：Java 同 package 类自动入 readable，避免同模块编译因可读范围不全必败 ──
    java_enriched = enrich_java_package_readable(plan_obj, _proj_path)
    if java_enriched:
        logger.info("[ELABORATE] P2-1: 已将 Java 同 package 类纳入相关子任务 readable")

    # ── 方案A(task 34fab09e)：上下文预注入。readable 补全后抽取 scope 文件关键代码片段，
    # 注入子任务 context_snippets，随 worker prompt 下发 → worker 不必 cat 探索耗尽步数。
    try:
        snippets_injected = enrich_context_snippets(plan_obj, _proj_path)
        if snippets_injected:
            logger.info("[ELABORATE] 方案A: 已为子任务预注入 scope 文件代码片段（worker 免 cat 探索）")
    except Exception as exc:  # noqa: BLE001
        logger.warning("[ELABORATE] 上下文预注入失败（非致命，worker 仍可自行探索）: %s", exc)

    # ── D4(b): 按 plan 声明依赖命中"常幻觉库→正确 API 签名"知识表，注入相关子任务 context_snippets，
    # 消除本地小模型对第三方库(如 okhttp3.OkHttpClient)类名/方法名的幻觉退化死循环（round18 st-16）。
    try:
        if inject_api_knowledge(plan_obj):
            logger.info("[ELABORATE] D4(b): 已按声明依赖注入外部库正确 API 签名（消库名幻觉）")
    except Exception as exc:  # noqa: BLE001
        logger.warning("[ELABORATE] 外部库 API 知识注入失败（非致命）: %s", exc)

    # ── Bug-1 根治：plan 成型后全局悬空依赖兜底（单一收口点）──
    # 二次拆分 + 多轮 replan 可能残留指向不存在子任务的 depends_on，
    # _remap_dependents 只兜单次 resplit 映射，这里收口所有路径，杜绝
    # VALIDATE_PLAN 结构校验 "依赖未知任务" 死循环（task 0f93f1fc 实证）。
    dangling_fixed = _prune_dangling_dependencies(plan_obj.subtasks)
    if dangling_fixed:
        logger.info("[ELABORATE] 悬空依赖兜底：修正 %d 个子任务的 depends_on", dangling_fixed)

    # ── T4（round63）：契约符号钉权威落点 + 跨子任务类型引用布线 ──
    # 必须在 G2 之前：本 pass 只把 producer 路径布进 consumer readable+upstream_artifacts，
    # 依赖边交下方既有 G2 按 readable∩create 补（复用其环守卫，不造第二套加边逻辑）。
    # import 不进 try（hunter#2）：模块级回归（循环导入/语法错）要 fail-loud 于此，
    # 绝不与"单个 plan 数据怪癖"共享同一条 WARNING 被当瞬时失败无限吞掉。
    from swarm.brain.symbol_provenance import (
        pin_contract_symbol_paths,
        wire_created_type_references,
    )
    _t4_pinned, _t4_wired = 0, []
    # pin 与 wire 分开 try（hunter#1 HIGH）：pin 已就地提交 defined_in 后 wire 再抛，
    # 共用一条"跳过"日志=半应用状态被谎报成全无事发生——worker 拿到权威落点指令却没有
    # readable/依赖边，比 round63 前的裸猜更害人。分开 catch、各自留痕、带栈（hunter#4）。
    try:
        _t4_pinned = pin_contract_symbol_paths(plan_obj)
        if _t4_pinned:
            logger.info("[ELABORATE] T4: 契约符号钉权威落点 defined_in %d 条（worker import 依此推导）",
                        _t4_pinned)
    except Exception as exc:  # noqa: BLE001 — 自愈尽力而为；未钉=退回 worker 事后符号接地
        logger.warning("[ELABORATE] T4: 契约符号钉落点失败（非致命，本轮无新增 defined_in）: %s",
                       exc, exc_info=True)
    try:
        _t4_wired = wire_created_type_references(plan_obj).get("wired") or []
        if _t4_wired:
            logger.info("[ELABORATE] T4: 跨子任务类型引用布线 %d 条（readable+upstream_artifacts，"
                        "依赖边交 G2）: %s", len(_t4_wired), _t4_wired[:5])
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "[ELABORATE] T4: 类型引用布线异常中断（非致命；注意半应用状态：本轮已钉 defined_in "
            "%d 条但 consumer 未获 readable/依赖边——worker 侧靠 seed 闸/符号接地兜底）: %s",
            _t4_pinned, exc, exc_info=True)

    # ── G2（Task#9 审计③ GAP1）：readable 消费 → 补 provenance 依赖边 ──
    # readable 里出现某 producer 的 create_files（精确路径匹配）= 确定性消费其产物 → 必须
    # depends_on 该 producer（同沙箱产物才在/跨沙箱 B1 才注入已完成产物），否则同波派发 →
    # worker 伪造未见符号。加边前查环，成环则不加、告警交结构闸。放在悬空兜底之后（我的边只指
    # 现存任务，不被 dangling-fix 误删），全 scope/readable 变换收敛后再补序。
    _prov_added: list = []
    try:
        _prov_added, _prov_cycle = wire_readable_provenance(plan_obj)
        if _prov_added:
            logger.info("[ELABORATE] G2: 补 %d 条 provenance 依赖边（readable 消费者→产物 owner）: %s",
                        len(_prov_added), _prov_added[:5])
        if _prov_cycle:
            logger.warning("[ELABORATE] G2: %d 对 readable 消费加边会成环（更深计划错，未补边、未制造环）: %s",
                           len(_prov_cycle), _prov_cycle[:5])
    except Exception as exc:  # noqa: BLE001 — 自愈尽力而为，绝不因它拖垮规划（与兄弟 pass 对称）
        logger.warning("[ELABORATE] G2: provenance 接线失败（非致命，跳过）: %s", exc)

    # 最终检查：仍超预算/缺验收的标记出来（人工介入信号）
    oversized: list[str] = []
    for st in plan_obj.subtasks:
        est = _effective_est_tokens(st)   # G6：含注入 snippet 的有效预算（注入后重算，读时算不漂移）
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
    if (resplit_rounds > 0 or decoupled > 0 or any(_resolve.values()) or java_enriched
            or dangling_fixed or _t4_pinned or _t4_wired or _prov_added):
        # 拆分 / 剥离假依赖 / 冲突解决(合并·依赖序·归一·难度) / Java 同包入域 / 悬空依赖兜底 /
        # T4 钉落点·布线 / G2 补边 改变了 plan，回写。（T4 之前 G2 加的依赖边靠 plan_obj
        # 就地变异侥幸存活——LangGraph 只保证【返回键】进 state，checkpoint 恢复语义下
        # 未回写的变异不可依赖，此处一并治了。）
        out["plan"] = plan_obj
    if _t4_pinned:
        # dispatch.py:528 读 state["shared_contract"] 优先于 plan.shared_contract——
        # 钉住的 defined_in 必须随 state 键回写，否则派发面读到旧契约=白钉。
        out["shared_contract"] = plan_obj.shared_contract or {}
    return out


def _effective_est_tokens(st) -> int:
    """★G6（Task#9 审计④）★ 子任务的【有效】上下文预算 = 声明 est + 已注入 context_snippets
    的 token 量（~4 char/token）。context_snippets 是真实吃 worker 上下文的注入内容，注入前的
    est 完全没算它——"注入后重算"在此【读时计算】（不回写 est，故 elaborate 多轮 replan 重注入
    也不漂移/双计）。栈中立、确定性。est 缺省=0 时仍只回 snippet 贡献（下界，不假绿）。"""
    est = getattr(st, "est_context_tokens", 0) or 0
    snip = getattr(st, "context_snippets", "") or ""
    return est + len(snip) // 4


def _needs_resplit(st, budget: int) -> bool:
    """子任务是否需二次拆分：超上下文预算（INVEST 缺验收不强制拆，仅标记）。

    关键守卫：若子任务只改【单个文件】(writable≤1)，绝不二次拆分——拆了也只能让
    多个子任务都改同一文件，各自产出针对同一文件的 diff，MERGE 时行号冲突拼成
    损坏 patch(git apply --check failed)，契约符号永远落不了地、任务无法闭环
    (实测 task 8c9782b4：单文件任务拆 4 子任务 → merged_diff 5399字符是坏 patch →
    apply 失败 → defaultIfEmpty 方法没落地)。单文件超预算靠 worker 的 pre_model_hook
    历史裁剪在单子任务内消化，而非拆分。
    """
    scope = getattr(st, "scope", None)
    n_writable = len(getattr(scope, "writable", []) or []) if scope else 0
    n_create = len(getattr(scope, "create_files", []) or []) if scope else 0
    # 单文件修改(恰好1个writable、0个新建)不拆：拆了多个子任务改同一文件→diff冲突坏 patch。
    # 注意 0 文件(greenfield/scope未明)不在此守卫内——仍按预算判，可拆。
    if n_writable == 1 and n_create == 0:
        return False
    # RUN13 治本：文件数超标也需拆（即使上下文预算够）。9 文件子任务 est_tokens 可能没超
    # 150k 预算，但 CODING 工作量(逐个写 9 个文件)远超时间预算。按文件数拆，确定性投喂。
    if _oversized_by_files(st):
        return True
    return _effective_est_tokens(st) > budget   # G6：含注入 snippet 的有效预算


def _oversized_by_files(st) -> bool:
    """子任务涉及文件数(create + writable)是否超 MAX_FILES_PER_SUBTASK。

    与上下文预算正交：文件多但每个小，est_tokens 可能没超预算，可 CODING 逐文件写仍撞
    时间墙(RUN13 实测 9 文件 CODING 560s)。这是【时间/工作量】维度的超标，须拆。
    """
    scope = getattr(st, "scope", None)
    if not scope:
        return False
    n = len(getattr(scope, "create_files", []) or []) + len(getattr(scope, "writable", []) or [])
    return n > MAX_FILES_PER_SUBTASK


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
                creates=", ".join(getattr(getattr(st, "scope", None), "create_files", []) or []) or "（无）",
                readables=", ".join(getattr(getattr(st, "scope", None), "readable", []) or []) or "（无）",
            )},
        ])
        result = _parse_json_from_llm(resp.content)
        subs = result.get("subtasks") or []
        if len(subs) < 2:
            return [st]
        children = []
        base_scope = getattr(st, "scope", None) or FileScope(writable=[], readable=[])
        _parent_w = set(getattr(base_scope, "writable", []) or [])
        _parent_r = set(getattr(base_scope, "readable", []) or [])
        # #31-P1-fix（F1a·治根因）：父 create_files（=必建文件）此前从不窄化——每个子块
        # 深拷贝继承父的【完整】create_files → 串行子块 1 只产自己那片，#31 完整性闸却拿
        # 父全集查它 → 冤杀（缺的文件其实归还没跑的兄弟）。这里按 LLM 的 create_files 分派
        # 把父待建集【无重叠】拆到各子块，每个必建文件恰好归一个子块（first-claim wins）。
        _parent_c = set(getattr(base_scope, "create_files", []) or [])
        _claimed_c: set[str] = set()
        for i, s in enumerate(subs[:4]):
            # 修复别名 bug：每个子节点必须用【独立深拷贝】的 scope，绝不能共享同一
            # base_scope 对象。否则 normalize_plan_scopes 原地改 scope.create_files
            # 时会污染所有兄弟节点（同一引用），导致 scope 错乱变空 → Worker 无写权
            # 创建文件（task 39f7be5a 现场：子任务 writable/create_files 全空）。
            child_scope = base_scope.model_copy(deep=True)
            # P0-1 修复：按 LLM 给的 writable_files/readable_files 收窄子任务 scope，
            # 不再全量继承父 scope（否则每个子任务都面对整个大文件 → 输入累积撞上下文上限）。
            # 取与父 scope 的【交集】防越权；LLM 没给或给空时回退父 scope（保守不阻断）。
            _cw = [f for f in (s.get("writable_files") or []) if f in _parent_w]
            _cr = [f for f in (s.get("readable_files") or []) if f in (_parent_r | _parent_w)]
            if _cw:
                child_scope.writable = _cw
            if _cr:
                child_scope.readable = _cr
            # #31-P1-fix：create_files【总是】窄化（不像 writable 回退父全集——那正是 F1a 病根）。
            # 只认父待建集内、且尚未被前序子块认领的文件（first-claim wins，杜绝一文件挂多子块）。
            _cc = [f for f in (s.get("create_files") or [])
                   if f in _parent_c and f not in _claimed_c]
            child_scope.create_files = _cc
            _claimed_c.update(_cc)
            children.append(SubTask(
                id=f"{st.id}-{i + 1}",
                description=s.get("description", "")[:500],
                difficulty=getattr(st, "difficulty", SubTaskDifficulty.MEDIUM),
                modality=getattr(st, "modality", SubTaskModality.TEXT),
                scope=child_scope,
                depends_on=list(getattr(st, "depends_on", []) or []) + (
                    [f"{st.id}-{i}"] if i > 0 else []  # 子任务间默认串行(保守，避免同 scope 并行写冲突)
                ),
                acceptance_criteria=s.get("acceptance_criteria", []) or [],
                # S2-5：covers 继承——父任务的需求覆盖声明复制到拆出子任务，防 elaborate
                # 拆分丢 covers 白烧覆盖矩阵校验重试（task#24 覆盖校验按 subtask.covers 对账）
                covers=list(getattr(st, "covers", []) or []),
                est_context_tokens=int(s.get("est_context_tokens", budget // 2) or budget // 2),
            ))
        # #31-P1-fix（F1a）：LLM 未分派给任何子块的必建文件 → 分给【串行末子块】（它在所有
        # 兄弟之后跑，兄弟不承接=不会被完整性闸问及该文件，末块作为唯一 owner 诚实负责创建）。
        # 杜绝"无主必建文件蒸发"（无人产=下游 BLOCKED）与"挂多子块"（冤杀 earlier 子块）两头。
        if children:
            _unclaimed = [f for f in (getattr(base_scope, "create_files", []) or [])
                          if f not in _claimed_c]
            if _unclaimed:
                _last = children[-1].scope
                _last.create_files = list(_last.create_files or []) + _unclaimed
        return children
    except Exception as exc:  # noqa: BLE001
        logger.warning("[ELABORATE] 子任务 %s 二次拆分失败，保留原样: %s", getattr(st, "id", "?"), exc)
        return [st]


def _layer_rank(rel: str) -> tuple[int, str]:
    """文件按分层秩排序键：数据模型(0)→VO→DTO→mapper→xml→service→impl→controller(7)→未知(90)。

    仅用于【批内文件排序】(描述里数据层在前、Web 层在后，读着自然)，不再作为拆分边界——
    拆分边界已改为【实体词干】(见 _split_oversized_by_files)。次级键 rel 保证同层稳定有序。
    """
    from swarm.brain.contract_utils import _infer_create_layer
    info = _infer_create_layer(rel)
    if not info:
        return (90, rel)
    return (_LAYER_ORDER.get(info[0], 80), rel)


# 实体词干提取用：剥离 RuoYi 分层后缀 + 接口 I 前缀，让同一实体的 entity/vo/mapper/xml/
# service/impl/controller 归一到同一词干(AlarmApp*)。顺序敏感(ServiceImpl 须在 Service 前匹配)。
# 仅列【极不可能是实体名一部分】的纯分层后缀，避免误伤(如不剥 Task/Job/Config，"AlarmTask"是实体)。
_LAYER_SUFFIXES = ("ServiceImpl", "Service", "Controller", "MapperImpl", "Mapper",
                   "Repository", "Vo", "VO", "Dto", "DTO", "Bo", "BO")


def _entity_stem(rel: str) -> str:
    """从文件路径提取【实体/特性词干】。同实体全栈共享词干(AlarmApp.java / AlarmAppMapper.xml /
    IAlarmAppService.java / AlarmAppController.java → 都是 AlarmApp)。

    治本 RUN14 死循环：按层拆把 service 接口与调用它的 controller 拆进不同子任务 → 两个 worker
    各自臆测方法签名 → 跨子任务契约漂移 → 整模块编译失败(st-3 处爆出、改不了上游 → 死循环)。
    改按实体词干分组，同一实体全栈留在【一个子任务】，由一个 worker 一次写完、签名自洽。
    """
    import re as _re
    name = rel.replace("\\", "/").split("/")[-1]
    name = _re.sub(r"\.(java|xml|sql|vue|js|ts|go|py)$", "", name)
    if len(name) > 1 and name[0] == "I" and name[1].isupper():  # IAlarmAppService → AlarmAppService
        name = name[1:]
    for suf in _LAYER_SUFFIXES:
        if name.endswith(suf) and len(name) > len(suf):
            name = name[: -len(suf)]
            break
    return name or "misc"


def _split_single_entity_by_layer(core: list[str], max_files: int) -> list[list[str]]:
    """round10 #3 治本：单实体大 core 按【层】拆——数据层(domain/DTO/mapper/xml,boilerplate 快)
    → 逻辑层(service/impl/controller,复杂慢)，串行(逻辑依赖数据)。共享契约(CONTRACT_MODULE)已锚定
    接口/DTO 签名，故按层拆+契约注入不致 RUN14 漂移。数据层若仍超 max_files 再按块拆(DTO/domain
    低耦合可分)。返回 ≥2 批=拆成功；无清晰层 seam(纯逻辑/纯数据)返回 [core] 不拆(避免空批)。"""
    def _is_logic(f: str) -> bool:
        b = f.replace("\\", "/").rsplit("/", 1)[-1].rsplit(".", 1)[0]
        return b.endswith("Controller") or b.endswith("Service") or b.endswith("ServiceImpl")
    logic = [f for f in core if _is_logic(f)]
    data = [f for f in core if not _is_logic(f)]
    if not logic or not data:
        return [core]   # 无层 seam → 不拆
    batches: list[list[str]] = []
    if len(data) > max_files:
        batches += [data[i:i + max_files] for i in range(0, len(data), max_files)]
    else:
        batches.append(data)
    batches.append(logic)
    return batches


def _basename_noext(f: str) -> str:
    b = f.replace("\\", "/").rsplit("/", 1)[-1]
    return b.rsplit(".", 1)[0] if "." in b else b


def _longest_common_suffix(strs: list[str]) -> str:
    """一组字符串的最长公共后缀(用于识别 XxxHandler/XxxSender 这类同后缀兄弟)。"""
    if not strs:
        return ""
    shortest = min(strs, key=len)
    suf = ""
    for i in range(1, len(shortest) + 1):
        c = shortest[-i]
        if all(len(s) >= i and s[-i] == c for s in strs):
            suf = c + suf
        else:
            break
    return suf


def _is_interface_like(base: str) -> bool:
    """Java 接口约定：I + 大写(INotifyService)。这类是共享抽象,不算平行实现。"""
    return len(base) > 1 and base[0] == "I" and base[1].isupper()


def _has_camel_word_prefix(base: str, prefix: str) -> bool:
    """base 是否以【完整 CamelCase 词】prefix 开头。D35 治本：裸 startswith 会把
    BaseballXxx 当 Base*、Abstraction 当 Abstract* 误判成共享抽象——prefix 命中后
    必须正好结束或紧跟大写字母（新词边界）才算。跨栈通用纯命名判据。"""
    if not base.startswith(prefix):
        return False
    return len(base) == len(prefix) or base[len(prefix)].isupper()


def _is_upstream_shared(base: str) -> bool:
    """【共享抽象】——被各平行实现依赖，须【先建】(归上游首批,各 leaf readable 含它)。
    含：接口(I 前缀)、抽象/基类(Abstract*/Base*/*Base/*Support)。对抗审计 #1 治本：基类若被当
    平行兄弟拆到 leaf 批，前向 readable 不含它 → 子批 `cannot find symbol`。
    D35：前缀按 CamelCase 词边界匹配（BaseballScoreService/Abstraction 不是基类）。"""
    return (
        _is_interface_like(base)
        or _has_camel_word_prefix(base, "Abstract")
        or _has_camel_word_prefix(base, "Base")
        or base.endswith(("Base", "Support"))
    )


def _is_downstream_coordinator(base: str) -> bool:
    """【协调者】——实例化/引用【全部实现】，须【后建】(归下游末批,readable 含全部 leaf)。
    含：工厂/注册表/分发器/路由(*Factory/*Registry/*Dispatcher/*Router)。对抗审计 #1 治本：
    工厂若被当平行兄弟拆，前向 readable 不含晚序 leaf → `cannot find symbol`。"""
    return base.endswith(("Factory", "Registry", "Dispatcher", "Router"))


def _is_downstream_consumer(base: str) -> bool:
    """跨目录【消费者】——依赖(读取/调用)平行 leaf 实现的表现/协调层，须【后建】(归下游末批,
    readable 含全部 leaf)。含协调者(工厂/注册表/分发/路由) + 表现层(Controller/Endpoint/Resource/
    Facade/Aggregator/Gateway)。#7 治本：这类若被误当"共享上游类型"归首批→先于 leaf 编译→
    cannot find symbol。fail-closed：消费者名即便非真消费者，归下游(后建、下游可读全部)也安全；
    反向(真消费者归上游)必炸。纯命名判据、跨栈通用、不绑 Java/项目名。"""
    return (
        _is_downstream_coordinator(base)
        or base.endswith(("Controller", "Endpoint", "Resource", "Facade",
                          "Aggregator", "Gateway"))
    )


def _detect_parallel_impls(core: list[str]) -> tuple[list[str], list[str], list[str]] | None:
    """检测【同层平行独立实现家族】(策略/插件模式：N 个兄弟实现共享一个抽象、彼此无编译依赖)。

    治本 round18 st-16：6 个渠道通知 impl(SlackNotifyService/DingTalkNotifyService/…)塞进一个
    子任务 → worker 被迫在一个上下文里串烧 6 套异构外部集成(各自 HTTP 客户端)→ 迭代/预算耗尽 +
    对某库 API 幻觉即拖垮整批。这类实现【彼此独立】(只共享接口，不互相引用)，天然可一实现一子任务。

    判据(两信号任一，且【剥离共享抽象/协调者后】的 leaf 数 ≥ MIN_PARALLEL_IMPL_SIBLINGS)：
      - 目录信号：同一约定实现目录(impl/handler/channel/strategy/sender/…)下 ≥N 个 leaf；或
      - 命名信号：同目录 ≥N 个 leaf 基名共享 ≥4 字符公共后缀且前缀各异(XxxHandler/XxxSender…)。
    返回 (leaves, upstream, downstream)：
      - leaves    = 彼此独立的平行实现(各成一批,一实现一子任务)；
      - upstream  = 共享抽象(接口/抽象/基类) + 家族目录外的其它 core(共享类型) → 首批(先建)；
      - downstream= 协调者(工厂/注册表/…) → 末批(后建,读全部 leaf)。
    不构成家族返回 None。纯路径+命名判据，跨语言/跨栈通用，不绑 Java、不写死项目/模块名。
    """
    from collections import defaultdict
    # D35：分组键 = 【完整父目录路径】而非目录 basename——只按 basename 分组会把
    # a/impl 与 b/impl 两个互不相干的目录并成一个假家族（跨目录 leaf 被错连成兄弟批）。
    # 目录信号(_IMPL_DIR_NAMES)仍只看最后一段目录名，语义不变。
    by_dir: dict[str, list[str]] = defaultdict(list)
    for f in core:
        norm = f.replace("\\", "/")
        parent_path = norm.rsplit("/", 1)[0].lower() if "/" in norm else ""
        by_dir[parent_path].append(f)

    best_files: list[str] = []
    best_leaves: list[str] = []
    for dirpath, files in by_dir.items():
        dirname = dirpath.rsplit("/", 1)[-1]
        # 剥离共享抽象/协调者后，剩下的才是【平行 leaf】——据此判家族规模与命名信号
        leaves = [
            f for f in files
            if not _is_upstream_shared(_basename_noext(f))
            and not _is_downstream_coordinator(_basename_noext(f))
        ]
        if len(leaves) < MIN_PARALLEL_IMPL_SIBLINGS:
            continue
        bases = [_basename_noext(f) for f in leaves]
        dir_sig = dirname in _IMPL_DIR_NAMES
        suf = _longest_common_suffix(bases)
        name_sig = (
            len(suf) >= 4
            and len(set(bases)) == len(bases)          # 各文件名互异
            and all(len(b) > len(suf) for b in bases)   # 每个都有各自前缀(非纯后缀)
        )
        if (dir_sig or name_sig) and len(leaves) > len(best_leaves):
            best_files, best_leaves = files, leaves
    if len(best_leaves) < MIN_PARALLEL_IMPL_SIBLINGS:
        return None

    fam_dir = set(best_files)
    leaves = list(best_leaves)
    upstream = [f for f in best_files if _is_upstream_shared(_basename_noext(f))]
    downstream = [f for f in best_files if _is_downstream_coordinator(_basename_noext(f))]
    # 家族目录【外】的其它 core 分两类(#7 治本)：① 共享类型(接口/DTO/消息类，leaf 依赖它们)→
    # 上游首批先建；② 消费者(Controller/工厂等表现协调层，依赖 leaf)→下游末批后建(readable 含全 leaf)。
    # 原码把②也塞进上游→消费者先于 leaf 编译→cannot find symbol。
    _extra = [f for f in core if f not in fam_dir]
    # round21 对抗审计修：共享抽象优先——名字以 Gateway/Resource/Facade 等消费者后缀结尾【但同时是
    # 接口/Abstract*/Base*/*Support 共享抽象】的类(IPaymentGateway/AbstractGateway/BaseFacade)必须
    # 归 upstream(leaf 依赖它先建)，绝不能因消费者后缀降到 downstream→leaf fan-out cannot find symbol。
    _extra_consumers = [f for f in _extra
                        if _is_downstream_consumer(_basename_noext(f))
                        and not _is_upstream_shared(_basename_noext(f))]
    _extra_shared = [f for f in _extra if f not in set(_extra_consumers)]
    upstream = _extra_shared + upstream
    downstream = downstream + _extra_consumers
    return leaves, upstream, downstream


def _split_parallel_impl_core(core: list[str]) -> tuple[list[list[str]], int, int] | None:
    """把平行独立实现家族拆成【每个实现一批】。拆不出返回 None。

    返回 (batches, n_upstream, n_downstream)：
      批序 = 共享抽象(upstream,前 n_upstream 批,先建) → 各 leaf 批(彼此独立,fan-out 依赖上游) →
             协调者(downstream,后 n_downstream 批,fan-in 依赖全 leaf,读全 leaf)。
    调用方据 n_upstream/n_downstream 做 fan-out/fan-in 依赖布线（leaf 彼此不串行→一个卡住不连坐兄弟，
    治本 round15 head-of-line）。leaf 内按实体词干归组(一实现可含多文件，同词干同批)。
    """
    detected = _detect_parallel_impls(core)
    if detected is None:
        return None
    leaves, upstream, downstream = detected
    batches: list[list[str]] = []
    n_up = 0
    if upstream:
        batches.append(sorted(upstream, key=_layer_rank))     # 共享抽象/类型 → 首批(上游)
        n_up = 1
    stem_groups: dict[str, list[str]] = {}
    for f in sorted(leaves, key=_layer_rank):
        stem_groups.setdefault(_entity_stem(f), []).append(f)
    for stem in sorted(stem_groups):
        batches.append(stem_groups[stem])                     # 各平行实现 → 独立一批
    n_down = 0
    if downstream:
        batches.append(sorted(downstream, key=_layer_rank))   # 协调者 → 末批(下游,读全 leaf)
        n_down = 1
    if len(batches) < 2:
        return None
    return batches, n_up, n_down


def _split_oversized_by_files(st, max_files: int = MAX_FILES_PER_SUBTASK) -> list:
    """确定性按【实体词干】把文件数超标的子任务拆成多个子任务(不调 LLM，可复现)。

    治本 RUN13(预算) + RUN14(契约漂移)双约束：
    - 拆分边界 = 实体之间；【绝不拆穿一个实体的全栈】(entity+mapper+xml+service+impl+controller
      必须同批，否则接口与调用方分家 → 签名漂移 → 编译失败死循环)。
    - 多实体子任务 → 按实体打包(小实体可同批，单实体超 max_files 仍【整批原子】不拆，靠 A=900s 兜底)。
    - 单实体大切片(如 6 文件全 AlarmApp) → 不拆，返回 [st]，靠 A=900s 预算容纳(实测 9 文件≈560s)。
    - writable(改既有文件，如注册/pom)垫最后批；子链串行 child-(i) depends_on child-(i-1)。
    - 每个子节点独立深拷贝 scope(防别名污染)。下游依赖重映射由调用方 _remap_dependents 统一处理。
    返回 children(len≥2)；若拆不出≥2 个内聚批(单实体/总数不超)返回 [st] 不变。
    """
    from swarm.types import SubTask, SubTaskDifficulty, SubTaskModality

    scope = getattr(st, "scope", None)
    creates = list(getattr(scope, "create_files", []) or []) if scope else []
    writables = list(getattr(scope, "writable", []) or []) if scope else []
    if len(creates) + len(writables) <= max_files:
        return [st]
    # 子任务验收里的构建命令跟随 harness（栈感知），无则留空走通用表述
    _split_build_cmd = (getattr(getattr(st, "harness", None), "build_command", "") or "").strip()

    def _basename(f: str) -> str:
        return f.replace("\\", "/").rsplit("/", 1)[-1]

    def _layer_of(f: str) -> str:
        low = f.replace("\\", "/").lower()
        b = _basename(low)
        if b.endswith(".html") or "/static/" in low or "/templates/" in low:
            return "web"          # Thymeleaf 模板 / 静态 js/css —— 不经 javac 编译
        if b.endswith(".sql"):
            return "sql"          # 建表/seed —— 完全独立
        return "core"            # .java / mybatis .xml —— 编译耦合核心

    # 治本 RUN16(st-10 16 文件过大)：先按【是否参与 javac 编译】分层。
    # web(.html/static) 不经 javac、sql 独立 —— 与 java 核心【无编译耦合】,安全剥成独立批,
    # 既减体积又不引入契约漂移。只有 java 核心受"接口↔控制器签名"约束须谨慎按特性拆。
    core = [f for f in creates if _layer_of(f) == "core"]
    web = [f for f in creates if _layer_of(f) == "web"]
    sql = [f for f in creates if _layer_of(f) == "sql"]

    # core 按 Controller 锚点拆特性(≥2 个 Controller 才拆,杜绝 RUN14 单特性接口/控制器分家);
    # <2 个 → core 整体一批(契约自洽优先,靠 A=900s 预算)。
    anchors = sorted({_entity_stem(f) for f in core
                      if _basename(f).rsplit(".", 1)[0].endswith("Controller")}, key=len, reverse=True)
    core_batches: list[list[str]] = []
    parallel_core = False          # 平行独立实现拆分？(决定 fan-out/fan-in 而非串行链)
    _p_up = _p_down = 0            # 平行拆分的上游/下游批数(共享抽象/协调者)
    if len(anchors) >= 2:
        feat: dict[str, list[str]] = {a: [] for a in anchors}
        for f in sorted(core, key=_layer_rank):
            s = _entity_stem(f)
            feat[next((a for a in anchors if s.startswith(a)), anchors[-1])].append(f)
        core_batches = [feat[a] for a in sorted(anchors, key=len) if feat[a]]
    elif core:
        # D4(a) 治本 round18 st-16：先判【同层平行独立实现家族】(N 个渠道/handler/strategy 兄弟,
        # 无 Controller 锚点、彼此无依赖)→ 一实现一子任务(共享接口成前置上游批)。这在单实体分层之前,
        # 因这类兄弟各是【不同实体】(不同渠道),不该被当单实体整批。检测不中(None)才落回单实体逻辑。
        parallel_meta = _split_parallel_impl_core(core)
        if parallel_meta is not None:
            core_batches, _p_up, _p_down = parallel_meta
            parallel_core = True
        # round10 #3 治本：单实体 core 超文件数上界 → 按层拆(数据→逻辑,契约锚定不漂移)消除 900s
        # 超时；正常实体(≤上界)仍整批守 RUN14。
        elif len(core) > MAX_SINGLE_ENTITY_FILES:
            core_batches = _split_single_entity_by_layer(core, max_files)
        else:
            core_batches = [core]

    # web/sql 各自成批(web 多则按 max_files 分块);批序：core → web(引用控制器URL,放其后) → sql(独立,末)
    web_batches = [web[i:i + max_files] for i in range(0, len(web), max_files)] if web else []
    sql_batches = [sql] if sql else []
    all_batches = core_batches + web_batches + sql_batches

    norm_batches: list[list[tuple[str, str]]] = [[(p, "create") for p in b] for b in all_batches]
    if writables:
        if norm_batches and len(norm_batches[-1]) + len(writables) <= max_files:
            norm_batches[-1] += [(w, "write") for w in writables]
        else:
            norm_batches.append([(w, "write") for w in writables])

    if len(norm_batches) <= 1:
        return [st]   # 拆不出≥2 个内聚批(纯单特性 java) → 不拆,靠 A=900s 预算

    # 治本(ELABORATE 截断 → P6b 误判重拆)：保留父任务【完整实现指引】(原仅取 300 字成裸 stub →
    # VALIDATE_PLAN 误标"描述截断/缺完整指引" → P6b 误判缺功能触发徒劳全量重拆)。给每个子块一段
    # 自洽描述：父描述全文 + 明确"本批负责哪些文件、其余批由兄弟完成、接口以共享契约为准"。
    base_desc = (getattr(st, "description", "") or "").strip()[:2000]
    n = len(norm_batches)
    core_n = len(core_batches)

    # ── 依赖布线：默认【串行链】(本批依赖上一批)——实体/分层拆的下游确需上游产物。
    # 平行独立实现拆分(parallel_core)则改【fan-out/fan-in】：leaf 批各依赖【上游共享抽象批】、彼此
    # 不串行（一个渠道卡住不再连坐放弃兄弟渠道，治本 round15 head-of-line）；协调者批 fan-in 依赖全 leaf。
    # batch_dep[i] = 本批依赖的【批下标列表】(空=仅依赖父任务原 depends_on)。
    _L = core_n - _p_up - _p_down          # leaf 批数
    batch_dep: list[list[int]] = []
    for i in range(n):
        if parallel_core and i < core_n:
            if i < _p_up:                                  # 上游共享抽象批：其间串行(通常仅 1 批)
                batch_dep.append([i - 1] if i > 0 else [])
            elif i < _p_up + _L:                           # leaf：fan-out 依赖最后一个上游批(或父)
                batch_dep.append([_p_up - 1] if _p_up > 0 else [])
            else:                                          # 协调者：fan-in 依赖全部 leaf
                batch_dep.append(list(range(_p_up, _p_up + _L)))
        else:
            batch_dep.append([i - 1] if i > 0 else [])     # 串行(实体/分层/web/sql)

    # 平行 leaf 批的 readable 只给【上游共享抽象产物】(不给并行兄弟——兄弟并发未落盘、且本就无引用)；
    # 其余(串行批/协调者批)用【前向累积】上游产物(协调者末批据此读全 leaf)。
    _shared_up_creates: list[str] = [p for b in all_batches[:_p_up] for p in b] if parallel_core else []

    children = []
    _upstream_creates: list[str] = []   # 已拆出上游批的 create_files 累积(供串行/协调者批 readable)
    for i, grp in enumerate(norm_batches):
        child_scope = scope.model_copy(deep=True)
        child_scope.create_files = [p for p, k in grp if k == "create"]
        child_scope.writable = [p for p, k in grp if k == "write"]
        # ── A1 治本(round11)：下游批常 import 上游批产出的类型(数据层 domain/DTO/mapper →
        # 逻辑层 service/impl/controller)。把上游 create_files 并入本批 readable，一处同治
        # ① _decouple_independent_subtasks 不误剥真依赖 ② worker bootstrap 注入兄弟产物(杜绝
        # `package …domain does not exist` 空转)。平行 leaf 只读共享抽象、协调者/串行读累积。
        _is_parallel_leaf = parallel_core and _p_up <= i < _p_up + _L
        _up_for_readable = _shared_up_creates if _is_parallel_leaf else _upstream_creates
        if _up_for_readable:
            _existing_r = list(getattr(child_scope, "readable", []) or [])
            child_scope.readable = list(dict.fromkeys(_existing_r + _up_for_readable))
            # #12 治本(B fail-closed seed)：记下【哪些 readable 是上游/兄弟产物】(provenance)，
            # 供 worker bootstrap 判据——这些产物缺失于本地=上游未就绪/被 revert，不空烧交 worker。
            # 只标 provenance，不改 readable 语义；基线只读上下文不入此集，杜绝误 BLOCKED。
            _existing_ua = list(getattr(child_scope, "upstream_artifacts", []) or [])
            child_scope.upstream_artifacts = list(dict.fromkeys(_existing_ua + _up_for_readable))
        files_label = "、".join(p.rsplit("/", 1)[-1] for p, _ in grp)
        child_desc = (
            f"{base_desc}\n\n【按文件分批 · 第 {i + 1}/{n} 批】本子任务是上述父任务按文件分层"
            f"拆分的一批，仅负责创建/修改这些文件：{files_label}。父任务的完整实现目标见上述描述；"
            f"其余文件由兄弟子任务（同一父任务的其它批）完成——跨文件接口以共享契约为准，"
            f"勿重复实现不属于本批的文件。"
        )
        children.append(SubTask(
            id=f"{st.id}-{i + 1}",
            description=child_desc,
            difficulty=getattr(st, "difficulty", None) or SubTaskDifficulty.MEDIUM,
            modality=getattr(st, "modality", None) or SubTaskModality.TEXT,
            scope=child_scope,
            depends_on=list(getattr(st, "depends_on", []) or []) + [
                f"{st.id}-{j + 1}" for j in batch_dep[i]  # fan-out/fan-in 或串行(见 batch_dep)
            ],
            acceptance_criteria=[
                # CODEWALK 根因C：构建命令从子任务 harness 取（栈感知），不再写死 mvn compile
                # 误导非 Java 栈；harness 无命令时留通用表述（L1 按项目栈探测真实构建命令）。
                f"本子任务 scope 内 {len(grp)} 个文件全部创建/修改完成，且模块可编译/构建通过"
                + (f"（{_split_build_cmd}）" if _split_build_cmd else ""),
            ],
            # S2-5：covers 继承（同 _resplit_subtask——elaborate 两条拆分路径都不丢覆盖声明）
            covers=list(getattr(st, "covers", []) or []),
            est_context_tokens=int((getattr(st, "est_context_tokens", 0) or 0) * len(grp) /
                                   max(1, len(creates) + len(writables))) or 1,
        ))
        # 本批 create_files 入累积，供后续批 readable 引用(A1 治本，见上)
        _upstream_creates += [p for p, k in grp if k == "create"]
    logger.info("[ELABORATE] 子任务 %s 文件数 %d → 分层/特性拆为 %d 批"
                "(core:%d批+web:%d+sql:%d，Controller 锚点 %d，单特性java不拆穿)",
                st.id, len(creates) + len(writables), n,
                len(core_batches), len(web_batches), len(sql_batches), len(anchors))
    return children


def _terminal_child_ids(children: list) -> list[str]:
    """子拆分的【终端节点 id 集】——没有任何【同批子节点】依赖它们（下游重映射的正确目标）。

    串行链 → [尾节点]（单个）；平行 fan-out 无协调者 → 全部 leaf；有协调者 fan-in → [协调者]。
    下游依赖父任务=依赖"整批完成"，故须映射到终端集（全部无后继的子节点），不能只取 children[-1]
    ——平行 fan-out 下 children[-1] 只是某个 leaf，取它会让下游在兄弟 leaf 未完成时就 ready。"""
    ids = {getattr(c, "id", "") for c in children}
    depended: set[str] = set()
    for c in children:
        for d in (getattr(c, "depends_on", []) or []):
            if d in ids:
                depended.add(d)
    terminals = [getattr(c, "id", "") for c in children if getattr(c, "id", "") not in depended]
    return terminals or [getattr(children[-1], "id", "")]


def _remap_dependents_to_terminals(subtasks: list, old_id: str, children: list) -> int:
    """把下游对 old_id 的依赖重映射到【被拆子集的终端节点集】(可能多个,见 _terminal_child_ids)。

    统一收口 ELABORATE/dispatch/恢复阶梯的拆分后重映射：串行链退化为 [尾节点]（与旧
    _remap_dependents(children[-1]) 行为一致）；平行 fan-out 则映射到全部终端 leaf/协调者，杜绝
    下游提前 ready。原地修改，跳过被拆子节点自身，去重。"""
    terminals = _terminal_child_ids(children)
    child_ids = {getattr(c, "id", "") for c in children}
    child_prefix = f"{old_id}-"
    remapped = 0
    for st in subtasks:
        sid = getattr(st, "id", "")
        if sid in child_ids or sid.startswith(child_prefix):
            continue
        deps = list(getattr(st, "depends_on", []) or [])
        if old_id not in deps:
            continue
        rewritten: list[str] = []
        seen: set[str] = set()
        for d in deps:
            for t in (terminals if d == old_id else [d]):
                if t not in seen:
                    seen.add(t)
                    rewritten.append(t)
        st.depends_on = rewritten
        remapped += 1
    return remapped


def _prune_dangling_dependencies(subtasks: list) -> int:
    """全局悬空依赖兜底清理：把任何指向【不存在子任务】的 depends_on 修正。

    Bug-1 根治（task 0f93f1fc 实证）：ELABORATE 二次拆分 + 多轮 replan 后，
    下游子任务的 depends_on 可能残留指向已不存在的旧 id（如 st-1 被拆成
    st-1-1/st-1-2 后某轮 replan 又重置，st-2 仍 depends_on 旧 "st-1" 或旧
    "st-1-2"）。_remap_dependents 只在单次 resplit 的 old→new 映射时生效，
    兜不住跨轮累积的悬空依赖 → VALIDATE_PLAN 结构校验报 "依赖未知任务" 死循环。

    本函数是 plan 成型后的【单一收口点】，对每个悬空 dep：
      1. 若存在以该 dep 为前缀的现存子链（dep="st-1"，存在 st-1-1/st-1-2）→
         重映射到该子链尾节点（id 最大者，语义=全链完成）。
      2. 否则（无任何前缀匹配）→ 直接剥离该依赖（保守：宁可少一条依赖让其
         并行，也不要悬空依赖卡死规划。剥离不影响正确性，最多并行度判定偏激进，
         由 scope 归一 + worker 串行 reset 兜底）。
    返回被修正的子任务数。
    """
    existing = {getattr(st, "id", "") for st in subtasks}
    fixed = 0
    for st in subtasks:
        deps = list(getattr(st, "depends_on", []) or [])
        if not deps:
            continue
        new_deps = []
        seen = set()
        changed = False
        for d in deps:
            if d in existing:
                target = d
            else:
                # 悬空：找以 d 为前缀的现存子链尾节点
                children = sorted(
                    [e for e in existing if e.startswith(f"{d}-")]
                )
                if children:
                    target = children[-1]
                    changed = True
                    logger.info(
                        "[ELABORATE] 悬空依赖兜底: %s 的 depends_on %s → %s（子链尾节点）",
                        getattr(st, "id", "?"), d, target,
                    )
                else:
                    # 无前缀匹配 → 剥离
                    changed = True
                    logger.warning(
                        "[ELABORATE] 悬空依赖剥离: %s 的 depends_on %s 指向不存在子任务，已移除",
                        getattr(st, "id", "?"), d,
                    )
                    continue
            if target not in seen and target != getattr(st, "id", ""):
                seen.add(target)
                new_deps.append(target)
        if changed:
            st.depends_on = new_deps
            fixed += 1
    return fixed


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

关键原则（防小模型上下文爆炸）：
- 每个子任务只圈定它【真正需要改动】的最小文件子集（writable_files），从"涉及文件"里挑，绝不全量继承。
- 若多个子任务都要改同一个大文件，说明拆分维度错了——应按【文件】或【独立功能点】拆，让每个子任务面对尽量少的文件，而不是让它们都盯着同一个大文件。
- readable_files 只列该子任务真正要读的依赖文件，宁少勿多。
- #31-P1-fix：需【新建】的文件(create_files)也要拆——把"待新建文件"分派给【负责创建它的那一个】
  子任务的 create_files 字段，每个新建文件【只归一个子任务】（绝不让多个子任务都声明创建同一新文件）。
  拿不准归属的新建文件不写（系统会兜底分派给串行末子块），但绝不重复分派。

严格输出 JSON：
{{
  "subtasks": [
    {{"description": "子任务描述", "acceptance_criteria": ["验收1"],
      "writable_files": ["该子任务真正要改的文件(父scope子集)"],
      "readable_files": ["该子任务真正要读的依赖文件"],
      "create_files": ["该子任务负责【新建】的文件(父待新建集子集，每个新文件只归一个子任务)"],
      "est_context_tokens": 数字}}
  ]
}}"""

RESPLIT_USER = """需二次拆分的子任务：
{desc}

预估上下文：{est} tokens（预算 {budget}）
可写文件(writable)：{files}
待新建文件(create)：{creates}
可读文件(readable)：{readables}

请拆成 2-4 个各自在预算内的子任务。为每个子任务圈定最小必要的 writable_files/create_files/readable_files
（从上面列表里挑子集），让每个子任务面对尽量少的文件。每个【待新建文件】只分派给一个子任务的 create_files。"""


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
