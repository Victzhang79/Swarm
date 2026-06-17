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
        # 预算基于【worker 主力池】窗口——三个本地模型都是 worker(不按难度绑死)，
        # 但预算该用主力(Qwen3.6-40B-Claude 256K / MiniMax 196K)窗口算，
        # 不被次级 Saka(112K,只跑trivial小活+fallback)拖低。
        # 安全性：budget×0.75×0.7≈103K < Saka 112K，即使降级到 Saka，worker 的
        # pre_model_hook 裁剪后输入也装得下——三个 worker 模型通吃，无需额外重拆。
        candidate_models = list(getattr(wcfg, "worker_parallel_pool", []) or [])
        if not candidate_models:
            # 池空兜底：退回各档 primary（向后兼容）
            candidate_models = [cfg.routing_trivial, cfg.routing_medium, cfg.routing_complex]
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

    # ── 虚假前提优先处理（需求转化层 tech_design 检出，覆盖 auto_accept）──
    # 用户决策："涉及事实需澄清，就不能 auto_accept"。虚假前提是【事实错误】非信息不足，
    # auto 模式也【绝不能用默认假设硬跑】——基于虚假前提产出的都是垃圾、纯烧算力。
    false_premises = [
        fi for fi in (state.get("tech_design_fact_issues") or [])
        if isinstance(fi, dict) and fi.get("verdict") == "false"
    ]
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
            return {"clarify_history": history, "clarify_round": int(state.get("clarify_round", 0)) + 1}
        except Exception:  # noqa: BLE001
            return {"clarify_done": True, "clarify_blocked_by_facts": True, "clarify_summary": summary}

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


def _gather_project_facts(project_path: str | None, max_dirs: int = 60) -> str:
    """采集项目真实结构事实供 tech_design 核验【事实依据】（ground truth=磁盘，不靠可能滞后的索引）。

    产出：① 顶层目录树（前若干层，识别分层规范如 RuoYi 的 controller/service/mapper/domain）；
    ② 各典型层下的样例文件名（让 LLM 学到命名/路径规律，设计新文件路径时照此推导）。
    这样 tech_design 能核验"需求点名的文件是否真实存在"+ 据真实结构设计新文件路径。
    """
    if not project_path:
        return "（无项目路径，无法核验文件事实——方案中的文件存在性需 worker 沙箱实地确认）"
    import os
    try:
        lines: list[str] = []
        # 识别典型分层目录的样例文件（帮 LLM 学命名规律）
        sample_patterns = ("controller", "service", "mapper", "domain", "entity",
                            "model", "dao", "repository", "api", "views", "components")
        seen_samples: dict[str, list[str]] = {}
        dir_count = 0
        for root, dirs, files in os.walk(project_path):
            # 跳过噪音目录
            dirs[:] = [d for d in dirs if d not in (
                ".git", "node_modules", "target", "dist", "build", ".venv",
                "__pycache__", ".idea", ".codegraph")]
            rel = os.path.relpath(root, project_path)
            if rel == ".":
                rel = ""
            depth = rel.count(os.sep) if rel else 0
            if depth > 4:
                dirs[:] = []
                continue
            dir_count += 1
            if dir_count > max_dirs * 5:
                break
            low = rel.lower()
            for pat in sample_patterns:
                if pat in low and len(seen_samples.get(pat, [])) < 3:
                    for f in files[:3]:
                        seen_samples.setdefault(pat, []).append(os.path.join(rel, f))
        if seen_samples:
            lines.append("项目分层样例文件（据此学习命名/路径规律，新文件路径照此推导）：")
            for pat, fs in list(seen_samples.items())[:8]:
                for f in fs[:2]:
                    lines.append(f"  - {f}")
        return "\n".join(lines) if lines else "（项目结构已扫描，未识别到典型分层目录）"
    except Exception as exc:  # noqa: BLE001
        return f"（项目结构扫描失败：{exc}；文件存在性需 worker 沙箱实地确认）"


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
  "file_plan": [{"path": "完整相对路径", "action": "create|modify", "module": "所属业务模块(如 alarm-task/alarm-channel/schedule/auth/system，同一功能模块用同一名)", "responsibility": "该文件职责", "depends_on": ["前置文件路径"]}],
  "risks": ["风险1"],
  "acceptance": ["验收标准1"],
  "comment_requirements": "代码注释要求",
  "shared_contract": {"apis": [...], "data_structures": [...]}
}

注意：file_plan 是方案的核心产出——必须列全实现该功能所需的所有文件（新建+修改），路径完整、职责清晰。\
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
2. 把需求划分为若干【业务模块】，每个模块给：名称、职责、预估文件数、模块间依赖。
3. 设计数据模型（要建哪些表/核心字段）。
4. 定技术栈与架构（沿用项目既有分层）。

严格输出 JSON：
{"fact_issues": [{"claim","verdict":"false|already_exists|uncertain","detail","suggestion"}],
 "stack": {"frontend","backend","storage","rationale"},
 "architecture": "架构概述",
 "data_model": "数据模型(表/字段)",
 "modules": [{"name":"模块名(如 alarm-task)","responsibility":"职责","est_files":12,"depends_on":["前置模块名"]}]}"""

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

只为这个模块产出 file_plan（完整路径），module 字段统一填 "{mod_name}"。"""


async def _tech_design_staged(llm, task_desc, comp_str, greenfield, state,
                              project_facts, file_verification, review_feedback):
    """ultra 超大需求两阶段 tech_design（DESIGN 第八节 A+B）。

    阶段1：LLM 出模块清单+数据模型+架构（短输出）。
    阶段2：按模块逐个 LLM 出该模块 file_plan（每次短输出）→ 合并。
    每阶段短输出，规避单次生成上百文件超长 JSON 卡死。
    返回 (result_dict, file_plan, fact_issues, contract)，与单次路径同结构供后续复用。
    """
    import time as _time

    # ── 阶段1：顶层方案（模块清单 + 数据模型 + 架构）──
    _t0 = _time.monotonic()
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
        return stage1, stage1.get("file_plan", []) or [], fact_issues, contract

    # ── 阶段2：按模块逐个产出 file_plan（每次短输出）──
    all_file_plan: list[dict] = []
    mod_total = len(modules)
    for mi, mod in enumerate(modules, start=1):
        if not isinstance(mod, dict):
            continue
        mod_name = mod.get("name") or f"module-{mi}"
        _tm = _time.monotonic()
        try:
            resp2 = await llm.ainvoke([
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
                )},
            ])
            r2 = _parse_json_from_llm(resp2.content)
            fp = r2.get("file_plan", []) if isinstance(r2, dict) else []
            # 确保 module 字段（小模型/LLM 可能漏填）
            for item in fp:
                if isinstance(item, dict) and not item.get("module"):
                    item["module"] = mod_name
            all_file_plan.extend(fp)
            logger.info(
                "[TECH_DESIGN-STAGE2] 模块 %d/%d '%s' → %d 文件，耗时 %.1fs",
                mi, mod_total, mod_name, len(fp), _time.monotonic() - _tm,
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "[TECH_DESIGN-STAGE2] 模块 %d/%d '%s' 产出失败（降级跳过）: %s",
                mi, mod_total, mod_name, exc,
            )

    result = {
        "architecture": architecture, "data_model": data_model,
        "stack": stage1.get("stack", {}), "modules": modules,
        "file_plan": all_file_plan, "fact_issues": fact_issues,
    }
    logger.info(
        "[TECH_DESIGN-STAGED] 两阶段完成：%d 模块 → 合计 %d 文件",
        mod_total, len(all_file_plan),
    )
    return result, all_file_plan, fact_issues, contract


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
            file_plan = result.get("file_plan", []) if isinstance(result, dict) else []

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

        # 确定性兜底：磁盘核验出"点名文件不存在"，即便 LLM 没标 fact_issues 也补上（事实优先于 LLM）
        det_false = [fc for fc in file_checks if not fc["exists"]]
        if det_false and not any(
            (fi.get("verdict") == "false") for fi in (fact_issues or []) if isinstance(fi, dict)
        ):
            for fc in det_false:
                fact_issues.append({
                    "claim": f"需求点名文件 {fc['file']}",
                    "verdict": "false",
                    "detail": "磁盘核验：该文件在项目中不存在",
                    "suggestion": f"近似候选：{fc['candidates']}" if fc["candidates"] else "无近似文件",
                })
        logger.info(
            "[TECH_DESIGN] 技术方案已产出 (file_plan=%d 文件, fact_issues=%d)",
            len(file_plan or []), len(fact_issues or []),
        )
        return {
            "tech_design": result,
            "shared_contract_draft": contract or {},
            "tech_design_fact_issues": fact_issues or [],
            "tech_design_file_plan": file_plan or [],
        }
    except Exception as exc:  # noqa: BLE001
        logger.warning("[TECH_DESIGN] LLM 失败，产出空方案安全继续: %s", exc)
        # LLM 失败仍保留确定性磁盘核验结果（虚假前提不能因 LLM 挂了就漏过）
        det_false = [fc for fc in file_checks if not fc["exists"]]
        det_issues = [{
            "claim": f"需求点名文件 {fc['file']}", "verdict": "false",
            "detail": "磁盘核验：该文件在项目中不存在",
            "suggestion": f"近似候选：{fc['candidates']}" if fc["candidates"] else "无近似文件",
        } for fc in det_false]
        return {
            "tech_design": {"architecture": "（自动生成失败，降级直接规划）", "risks": [], "notes": []},
            "shared_contract_draft": {},
            "tech_design_fact_issues": det_issues,
            "tech_design_file_plan": [],
        }


# ══════════════════════════════════════════════
# 节点 3.5：contract_design — 共享契约设计（T1，DESIGN_multiworker_collaboration）
# ══════════════════════════════════════════════

CONTRACT_DESIGN_SYSTEM = """你是系统架构师，负责为一个【多模块大型需求】设计【跨模块共享契约】。

背景：这个需求会被拆成多个子任务，由多个 worker【并行】实现。为防止各 worker 各写各的、
接口对不上（如两人各建一个 INotifyService、DTO 字段不一致、API 路径冲突），
你要先把【所有 worker 都必须遵守的共享契约】定下来——这是全局唯一的一份基石。

只定【跨模块/跨子任务共享】的部分（模块内部细节由各 worker 自决，不要管）：
1. 共享接口：跨模块调用的接口名 + 完整方法签名（参数/返回类型）。
2. 共享 DTO/实体：被多个模块引用的数据结构 + 字段。
3. 共享常量/枚举：渠道类型、状态码、回调类型等。
4. API 路径规范：对外接口的 URL 路径 + HTTP 方法 + 请求/响应结构。
5. 命名/路径约定：包名前缀、模块目录规范，避免同名重复创建。

严格输出 JSON：
{"shared_contract": {
  "interfaces": [{"name","module","signature":"完整方法签名","purpose"}],
  "dtos": [{"name","module","fields":["类型 字段名"]}],
  "constants": [{"name","values":["..."]}],
  "apis": [{"path","method","request","response"}],
  "conventions": ["命名/路径约定1", "..."]
}}"""

CONTRACT_DESIGN_USER = """需求：
{task_description}

模块清单（来自技术方案）：
{modules}

数据模型：
{data_model}

文件级方案概览（看哪些文件跨模块被引用）：
{file_plan_summary}

请产出【跨模块共享契约】JSON。只定共享部分，求精准（这是所有 worker 的基石）。"""


async def contract_design(state: BrainState) -> dict:
    """共享契约设计节点（T1）：多模块大需求并行实现前，用 Brain 大模型产出全局共享契约。

    决策（Q-T1-1/Q-T1-2）：独立节点 + Brain 大模型直接生成（全局基石求准）。
    仅对【ultra 且多模块】需求产契约；其余直通（沿用 tech_design 的 shared_contract_draft）。
    产出的 shared_contract 会：① 注入每个 worker 作只读契约；② 作为"契约子任务"最先落盘（dispatch 层）。
    """
    file_plan = state.get("tech_design_file_plan") or []
    td = state.get("tech_design") or {}
    modules = td.get("modules") or []
    comp = state.get("assessed_complexity") or state.get("complexity")
    comp_str = comp.value if hasattr(comp, "value") else str(comp)

    # 仅 ultra 多模块才需要全局契约（简单/单模块沿用 draft，零开销）
    if comp_str != "ultra" or len(modules) < 2:
        return {}

    try:
        llm = _get_brain_llm()
        fp_summary = "\n".join(
            f"  - {fp.get('path')} [{fp.get('module', '?')}] {fp.get('responsibility', '')}"
            for fp in file_plan[:120]
        )
        resp = await llm.ainvoke([
            {"role": "system", "content": CONTRACT_DESIGN_SYSTEM},
            {"role": "user", "content": CONTRACT_DESIGN_USER.format(
                task_description=(state.get("task_description", ""))[:2500],
                modules=json.dumps(modules, ensure_ascii=False)[:2000],
                data_model=str(td.get("data_model", ""))[:2000],
                file_plan_summary=fp_summary[:3000],
            )},
        ])
        result = _parse_json_from_llm(resp.content)
        contract = result.get("shared_contract", result) if isinstance(result, dict) else {}
        n_if = len(contract.get("interfaces", []) or []) if isinstance(contract, dict) else 0
        n_dto = len(contract.get("dtos", []) or []) if isinstance(contract, dict) else 0
        n_api = len(contract.get("apis", []) or []) if isinstance(contract, dict) else 0
        logger.info(
            "[CONTRACT_DESIGN] 共享契约已产出：接口=%d DTO=%d API=%d（Brain 大模型，全局基石）",
            n_if, n_dto, n_api,
        )
        return {"shared_contract_draft": contract or state.get("shared_contract_draft") or {}}
    except Exception as exc:  # noqa: BLE001
        logger.warning("[CONTRACT_DESIGN] 契约生成失败，沿用 tech_design draft 继续: %s", exc)
        return {}


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
    _max_resplit = _tier_limits()["elaborate_resplit"]
    # 多轮：每轮找出"需再拆"的子任务，二次拆分替换，重新检查
    while resplit_rounds < _max_resplit:
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
                    # P0-1 修复：二次拆分替换了节点，但其它子任务可能仍 depends_on 旧 id
                    # （如 st-2 depends_on st-1，st-1 被拆成 st-1-1/st-1-2 后 st-1 已不存在）。
                    # 必须把所有指向旧 id 的下游依赖重映射到子链尾节点（children[-1]），
                    # 否则 VALIDATE_PLAN 结构校验必报"依赖未知任务"，陷入规划死循环。
                    # 选尾节点：子链内部已串行（见 _resplit_subtask），尾节点完成 ⇒ 全链完成，
                    # 语义最简且不破坏依赖驱动调度的并行度判定。
                    remapped = _remap_dependents(new_subtasks, st.id, children[-1].id)
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
        normalize_plan_scopes,
    )
    scope_normalized = normalize_plan_scopes(plan_obj)

    # ── 意图校正(task dbfc265f)：LLM 把功能需求误判 AUDIT 但 scope 有写文件 → 纠正为
    # MODIFY/CREATE，避免走 security_audit 不产 diff → findings=0 假失败 → retry 死循环。
    if correct_misclassified_intent(plan_obj):
        logger.info("[ELABORATE] 意图校正：AUDIT 子任务含写文件 → 纠正为 MODIFY/CREATE（确定性信号覆盖 LLM 误判）")

    # ── P2-1：Java 同 package 类自动入 readable，避免同模块编译因可读范围不全必败 ──
    _proj_path = None
    try:
        from swarm.project import store as _store
        _pid = state.get("project_id") or ""
        if _pid:
            _proj = _store.get_project(_pid)
            _proj_path = _proj.get("path") if _proj else None
    except Exception as exc:  # noqa: BLE001
        logger.debug("[ELABORATE] 获取 project_path 失败，跳过 Java 同包入域: %s", exc)
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

    # ── Bug-1 根治：plan 成型后全局悬空依赖兜底（单一收口点）──
    # 二次拆分 + 多轮 replan 可能残留指向不存在子任务的 depends_on，
    # _remap_dependents 只兜单次 resplit 映射，这里收口所有路径，杜绝
    # VALIDATE_PLAN 结构校验 "依赖未知任务" 死循环（task 0f93f1fc 实证）。
    dangling_fixed = _prune_dangling_dependencies(plan_obj.subtasks)
    if dangling_fixed:
        logger.info("[ELABORATE] 悬空依赖兜底：修正 %d 个子任务的 depends_on", dangling_fixed)

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
    if resplit_rounds > 0 or decoupled > 0 or scope_normalized or java_enriched or dangling_fixed:
        # 拆分 / 剥离假依赖 / scope 归一 / Java 同包入域 / 悬空依赖兜底改变了 plan，回写
        out["plan"] = plan_obj
    return out


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
                est_context_tokens=int(s.get("est_context_tokens", budget // 2) or budget // 2),
            ))
        return children
    except Exception as exc:  # noqa: BLE001
        logger.warning("[ELABORATE] 子任务 %s 二次拆分失败，保留原样: %s", getattr(st, "id", "?"), exc)
        return [st]


def _remap_dependents(subtasks: list, old_id: str, new_id: str) -> int:
    """把所有子任务 depends_on 中指向 old_id 的项重映射到 new_id（原地修改）。

    用于 ELABORATE 二次拆分后修复悬空依赖：st-1 被拆成 st-1-1/st-1-2 后，
    原先 depends_on=[st-1] 的下游 st-2 需改为 depends_on=[st-1-2]（子链尾节点）。

    跳过被拆出的子节点自身（它们 id 以 old_id 为前缀，内部串行已由
    _resplit_subtask 建好，不应再被重映射到自己的尾节点造成自依赖/环）。

    返回重映射的依赖条数（去重后按"涉及的子任务数"计，便于日志可读）。
    """
    remapped = 0
    child_prefix = f"{old_id}-"
    for st in subtasks:
        sid = getattr(st, "id", "")
        # 被拆出的子节点自身不参与重映射（避免 st-1-2 depends_on st-1 → 自指）
        if sid == new_id or sid.startswith(child_prefix):
            continue
        deps = list(getattr(st, "depends_on", []) or [])
        if old_id not in deps:
            continue
        # 替换 old_id → new_id，并去重（防止已存在 new_id 造成重复）
        rewritten = []
        seen = set()
        for d in deps:
            target = new_id if d == old_id else d
            if target not in seen:
                seen.add(target)
                rewritten.append(target)
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

严格输出 JSON：
{{
  "subtasks": [
    {{"description": "子任务描述", "acceptance_criteria": ["验收1"],
      "writable_files": ["该子任务真正要改的文件(父scope子集)"],
      "readable_files": ["该子任务真正要读的依赖文件"],
      "est_context_tokens": 数字}}
  ]
}}"""

RESPLIT_USER = """需二次拆分的子任务：
{desc}

预估上下文：{est} tokens（预算 {budget}）
可写文件(writable)：{files}
可读文件(readable)：{readables}

请拆成 2-4 个各自在预算内的子任务。为每个子任务圈定最小必要的 writable_files/readable_files
（从上面列表里挑子集），让每个子任务面对尽量少的文件。"""


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
