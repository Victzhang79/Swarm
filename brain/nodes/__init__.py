"""Brain 节点函数 — LangGraph 状态机的所有节点实现

每个节点是一个函数: (BrainState) -> dict
返回的 dict 会被 merge 回 BrainState。

真实 LLM 调用 + mock fallback：每个节点优先调用 Brain LLM，
失败时回退到原有 mock 逻辑。

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
Theme A / A7 — god-file 拆解后续清单（本文件 ~4000 行，round24 只做去重/去环，未拆体）

拆解【必须逐簇一次一个、每步全绿】，且守住两条硬约束，否则回归：
  1. 可 patch 的有状态符号（_get_brain_llm/_dispatch_to_worker/_get_project_path/
     _try_l2_*/_verify_l2_via_llm/_run_reactor_build_in_sandbox 等）**必须仍以
     `swarm.brain.nodes.X` 可寻址**——移动后要在本 __init__ re-export，且测试 patch 的
     是 __init__ 命名空间。移错位置→大批 patch 失效（本轮 A4/A6 已踩过两次）。
  2. 抽出的模块**不得反向 import 本 __init__**（会重建 A6 刚破的环）；共享纯 helper 先下沉
     到 nodes/shared.py（干净 sink），抽出模块只依赖 shared。

已识别的内聚簇（建议拆出顺序，风险从低到高）：
  A. ✅[已拆] 恢复/阻断分析簇 → brain/nodes/recovery.py（_producers_of / _package_in_baseline /
     _blocked_pkg_unrecoverable / _is_missing_dependency_failure + _det_of / _INTERNAL_BLOCKED_KINDS）。
  B-1. ✅[已拆] Maven 缺失依赖补全簇（纯 pom/path，未被 patch）→ brain/nodes/maven_repair.py
     （_pkg_match_tokens / _extract_missing_pkgs / _iter_project_poms / _find_maven_dep_for_pkg /
     _inject_dep_into_pom / _inject_missing_maven_deps + 5 个 maven 正则常量）。
  B-2. ✅[已拆] 恢复阶梯 + pom/模块脚手架连通分量 → brain/nodes/planning_core.py（round25 主线1）。
     取证修正：_rebuild_plan/_resplit_subtask/_split_oversized_by_files/_remap_dependents_to_terminals/
     _context_budget 早已在 brain/planning_nodes.py（经 _targeted_redecompose 内 lazy import 消费），
     不在本 __init__。真正的非叶簇是【就地改 plan / 依赖闭包】的恢复阶梯 18 函数 + 3 常量：
     _widen_scope_for_compile_repair / _grant_module_pom_writable / _serialize_pom_writers /
     _local_tree_revert_subtask / _git_diff_for_paths / _proj_path_from_state / _targeted_redecompose /
     _redecompose_timeout_subtasks / _generate_compile_stub / _give_up_preserve_build + 纯图/足迹
     helper(_module_of/_reaches/_add_dep_safe/_transitive_abandon/_subtask_footprint/
     _files_owned_by_completed/_has_stream_stall/_is_timeout_oversize_failure)。整体原子迁移：
     planning_core 禁 eager import __init__（对 _get_brain_llm/planning_nodes 均 lazy），__init__
     re-export 保可寻址。测试 patch 陷阱：_give_up_preserve_build 内部同簇互调
     (_proj_path_from_state/_generate_compile_stub) 在 planning_core 命名空间解析——patch 目标已迁
     planning_core（见 test_ladder_giveup_preserve_build）。
  D. ✅[已拆] AUDIT 安全审计节点（叶，未被 patch）→ brain/nodes/audit_node.py（_run_security_audit；round27 改名——audit.py 子模块会遮蔽父包的 audit 函数绑定致 6 处调用点 TypeError）。
  C. ✅[已拆·round26] handle_failure 族 → brain/nodes/failure.py：_handle_failure_impl（~660 行）
     + _l1_details_of 已外置；薄包装 handle_failure 仍留本 __init__（round24 A4 plan 持久化 seam），
     其 bare 调用 + patch("swarm.brain.nodes._handle_failure_impl") 经底部 re-export 解析保可寻址。
  后续候选：把 dispatch/verify/audit/planning_core 之外的其余节点助手按【叶簇优先】继续下沉。
每簇拆前先补【行为测试】锁外部契约（禁 inspect.getsource 结构焊死），再迁移。
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
"""

from __future__ import annotations

import json
import logging
import os
import re
import time
from pathlib import Path

from langgraph.types import interrupt

from swarm.audit import audit
from swarm.brain.context_log import init_task_context, sliding_context_prompt, touch_context
from swarm.brain.prompts import (
    ANALYZE_SYSTEM,
    ANALYZE_USER,
    LEARN_FAILURE_SYSTEM,
    LEARN_FAILURE_USER,
    LEARN_SUCCESS_SYSTEM,
    LEARN_SUCCESS_USER,
    PLAN_SYSTEM,
    PLAN_USER,
    REVISION_SYSTEM,
    REVISION_USER,
    VALIDATE_PLAN_SYSTEM,
    VALIDATE_PLAN_USER,
    VERIFY_L2_SYSTEM,
    VERIFY_L2_USER,
    VERIFY_L3_SYSTEM,
    VERIFY_L3_USER,
)
from swarm.brain.state import BrainState, effective_complexity
from swarm.config.settings import get_config
from swarm.memory.sliding_window import PRIORITY_WORKER
from swarm.models.router import ModelRouter

# B1 批3: dispatch/verify 域已抽出；re-export 节点保 swarm.brain.nodes.X 路径不变。
from swarm.brain.nodes.dispatch import dispatch, monitor  # noqa: E402,F401
from swarm.brain.nodes.verify import verify_l2, verify_l3  # noqa: E402,F401

# B1 批2: 无状态纯 helper + 常量已抽到 shared.py；此处 re-export 保持
# swarm.brain.nodes.X 的 import / patch 路径 100% 不变。
from swarm.brain.nodes.shared import (  # noqa: E402,F401
    _CREATE_HINTS,
    _DELETE_HINTS,
    _FILE_EXT,
    _FILE_PAT,
    _L2_CMD_RE,
    _brain_profile_prompt,
    _build_simple_plan,
    _classify_file_ops,
    _complexity_str,
    _format_project_structure,
    _guess_target_files,
    _infer_harness,
    _infer_intent,
    _l2_test_command_from_criteria,
    _match_files_by_description,
    l1_passed,
    _parse_json_from_llm,
    _planning_triage,
    _worker_profile_prompt,
    parse_and_validate,
)
# A7: re-export 恢复簇，保 swarm.brain.nodes.X 可寻址 + 测试 patch 目标不变
from swarm.brain.nodes.recovery import (  # noqa: E402,F401
    _INTERNAL_BLOCKED_KINDS,
    _MISSING_DEP_PATTERNS,
    _blocked_pkg_unrecoverable,
    _det_of,
    _is_missing_dependency_failure,
    _package_in_baseline,
    _producers_of,
)
# god-file 簇B-1：re-export Maven 缺失依赖补全簇（保 swarm.brain.nodes.X 可寻址；未被 patch）
from swarm.brain.nodes.maven_repair import (  # noqa: E402,F401
    _ARTIFACT_RE,
    _DEP_BLOCK_RE,
    _GROUP_RE,
    _MAVEN_GENERIC_SEG,
    _MISSING_PKG_BRAIN_RE,
    _extract_missing_pkgs,
    _find_maven_dep_for_pkg,
    _inject_dep_into_pom,
    _inject_missing_maven_deps,
    _iter_project_poms,
    _pkg_match_tokens,
)
# god-file 簇D：re-export AUDIT 安全审计节点（保 swarm.brain.nodes.X 可寻址；未被 patch）
from swarm.brain.nodes.audit_node import _run_security_audit  # noqa: E402,F401
# god-file 主线1：re-export 规划/恢复核心簇（恢复阶梯 + B-2 pom 脚手架连通分量）。
# 保 swarm.brain.nodes.X 可寻址；__init__ 内调用点(handle_failure/_handle_failure_impl)以此绑定解析，
# patch(swarm.brain.nodes.X) 对其生效。planning_core 内部同簇互调须 patch planning_core 命名空间。
from swarm.brain.nodes.planning_core import (  # noqa: E402,F401
    _add_dep_safe,
    _files_owned_by_completed,
    _generate_compile_stub,
    _git_diff_for_paths,
    _give_up_preserve_build,
    _grant_module_pom_writable,
    _has_stream_stall,
    _is_timeout_oversize_failure,
    _local_tree_revert_subtask,
    _module_of,
    _proj_path_from_state,
    _reaches,
    _redecompose_timeout_subtasks,
    _serialize_pom_writers,
    _subtask_footprint,
    _targeted_redecompose,
    _transitive_abandon,
    _widen_scope_for_compile_repair,
)
from swarm.brain.llm_schemas import (  # noqa: E402
    ComplexityAssessmentResponse,
)
from swarm.types import (
    Complexity,
    Confidence,
    FileScope,
    HumanDecision,
    KnowledgeContext,
    SubTask,
    SubTaskDifficulty,
    SubTaskModality,
    TaskHarness,
    TaskIntent,
    TaskPlan,
    WorkerOutput,
)

logger = logging.getLogger(__name__)


# 文件名匹配：要求左边界是非文件名字符（含中文/空格/标点），杜绝中文粘连。
# 旧正则 [\w./-]+ 会把中文也算进 \w，导致 "输出readme.md" → "输出readme.md"。
# (?<![\w/.\-]) 前面不能是文件名字符（ASCII），中文不在此类→自然成为边界
# 末尾用 (?![A-Za-z0-9_./\-]) 而非 \b：中文是 \w，\b 在 ".md出" 处不成立会漏匹配。

# 操作意图关键词


# 子任务 writable 文件数告警阈值：超过则疑似规划过度圈定(把整个模块塞进 scope)。
_SCOPE_WRITABLE_WARN_THRESHOLD = 20


# ══════════════════════════════════════════════
# 辅助工具
# ══════════════════════════════════════════════

def _get_brain_llm():
    """获取 Brain LLM 实例。

    P2（可选 JSON mode，SWARM_BRAIN_JSON_MODE=true 开启，默认关）：provider 支持时让模型直接产
    合法 JSON，减少 brain 输出脏逗号触发 json_repair。默认关——provider 若不支持 response_format
    会拒整个调用（毁掉所有 brain 调用），故待确认端点支持再开；`_parse_json_from_llm` 的 json_repair
    仍是恒在兜底（关着安全、开了无损）。绑定失败优雅回退不绑。"""
    router = ModelRouter()
    llm = router.get_brain_llm()
    if os.environ.get("SWARM_BRAIN_JSON_MODE", "false").lower() in ("true", "1", "yes"):
        try:
            return llm.bind(response_format={"type": "json_object"})
        except Exception:  # noqa: BLE001
            return llm
    return llm


# ══════════════════════════════════════════════
# 节点函数
# ══════════════════════════════════════════════


async def analyze(state: BrainState) -> dict:
    """ANALYZE 节点 — 任务初判 & 检索知识上下文

    输入: task_description, project_id
    输出: complexity(初判，终复杂度由 assess 澄清后定), knowledge_context,
          is_micro_task, needs_clarify
    """
    from swarm.knowledge.service import (
        empty_knowledge_context,
        format_brain_knowledge_prompt,
        retrieve_knowledge,
    )

    task_description = state.get("task_description", "")
    project_id = state.get("project_id", "")
    logger.info(f"[ANALYZE] 分析任务: {task_description[:80]}...")

    work_state: BrainState = dict(state)
    context_patch: dict = {}
    if not work_state.get("context_log"):
        context_patch = init_task_context(work_state)
        work_state.update(context_patch)

    from swarm.memory.task_digest import format_recent_tasks_for_brain, load_recent_task_summaries

    recent_summaries = state.get("recent_task_summaries")
    if recent_summaries is None and project_id:
        # 最终推演·连续性护栏(round21)：DB/store 抖动不应让 ANALYZE 早退 FAILED——近期任务摘要仅是
        # 规划的软上下文，取不到降级空即可(与 retrieve_knowledge 的 N-12 护栏同口径),不中断主干。
        try:
            recent_summaries = await load_recent_task_summaries(project_id)
        except Exception as _rtexc:  # noqa: BLE001
            logger.warning("[ANALYZE] 近期任务摘要加载失败(降级空,不中断规划): %s", _rtexc)
            from swarm.infra.degrade import record_degrade
            record_degrade("brain.analyze.summary_load")  # E1
            recent_summaries = []
    recent_tasks_prompt = format_recent_tasks_for_brain(recent_summaries or [])
    session_meta = state.get("session_metadata") or {}
    session_prompt = json.dumps(session_meta, ensure_ascii=False, indent=2) if session_meta else "（无）"

    knowledge_context: KnowledgeContext = empty_knowledge_context()
    if project_id:
        knowledge_context, stats = await retrieve_knowledge(task_description, project_id)
        # N-12 修复：检索整体崩溃时 service 返回空知识 + stats['error']，但下游只用 context
        # → "检索崩溃"与"真无知识"不可区分，Brain 在零知识上规划还以为正常。显式告警区分。
        if stats.get("retrieval_failed") or stats.get("error"):
            logger.error(
                "[ANALYZE] ⚠️ 知识检索崩溃(非'无知识')，Brain 将在零知识上下文上规划: %s",
                stats.get("error") or "retrieval_failed",
            )
        logger.info(
            "[ANALYZE] 知识检索完成: struct=%s semantic=%s norms=%s "
            "summary=%s mistakes=%s successes=%s",
            stats.get("struct_count", 0),
            stats.get("semantic_count", 0),
            stats.get("norms_count", 0),
            stats.get("has_project_summary", False),
            stats.get("mistakes_count", 0),
            stats.get("successes_count", 0),
        )
    else:
        logger.warning("[ANALYZE] 无 project_id，跳过知识检索")

    sliding_ctx = sliding_context_prompt(work_state)

    # 复杂度判定：一律走【带知识库检索结果的云端 Brain 大模型】判定。
    # （历史曾有 _heuristic_complexity 关键词短路抢在 LLM 前拦截——命中"注释/typo"等词就
    # 直接判死、连大模型都不调，导致跨文件/新建类任务被误降级成 simple。已废弃该短路：
    # 复杂度是语义判断，应由拿到 struct/semantic/norms/项目摘要 的大模型来定，而非脆弱关键词。）

    # ── LLM 复杂度分类 ──
    knowledge_prompt = format_brain_knowledge_prompt(
        knowledge_context, task_description
    )
    _analyze_degraded: str | None = None  # LLM 降级原因（audit #12），非降级保持 None
    try:
        llm = _get_brain_llm()
        prompt_user = ANALYZE_USER.format(
            task_description=task_description,
            knowledge_context=knowledge_prompt,
            user_profile=_brain_profile_prompt(state),
            recent_tasks=recent_tasks_prompt,
            session_metadata=session_prompt,
            sliding_context=sliding_ctx,
        )
        response = await llm.ainvoke([
            {"role": "system", "content": ANALYZE_SYSTEM},
            {"role": "user", "content": prompt_user},
        ])
        result = _parse_json_from_llm(response.content)
        # N-26：JSON 合法但缺 complexity 键时，原 result["complexity"] KeyError 会落到下方
        # 泛 except 被误标"通用失败"。与 JSONDecodeError 分支一致回退 MEDIUM（而非崩溃）。
        if not result.get("complexity"):
            logger.warning("[ANALYZE] LLM 输出缺 complexity 键，回退 MEDIUM（N-26）")
            result["complexity"] = "medium"
        # Wave 1/TD2606-B1：经类型边界提取 complexity（容忍非字符串形状）；非法 → 显式回退 MEDIUM。
        try:
            complexity = ComplexityAssessmentResponse.model_validate(result).complexity
        except Exception as _ve:  # noqa: BLE001
            logger.warning("[ANALYZE] complexity 形状非法，显式回退 MEDIUM（B1）: %s", str(_ve)[:120])
            result["complexity"] = "medium"
            complexity = Complexity.MEDIUM
    except json.JSONDecodeError as e:
        logger.warning(f"[ANALYZE] LLM 输出 JSON 解析失败: {e}")
        result = {
            "complexity": "medium",
            "reasoning": f"Mock: JSON 解析失败回退默认中等复杂度 — {e}",
            "key_risks": [],
            "suggested_subtask_count": 2,
        }
        complexity = Complexity(result["complexity"])
        _analyze_degraded = f"analyze LLM 输出解析失败，复杂度静默回退 MEDIUM（{e}）"
    except Exception as e:
        logger.warning(f"[ANALYZE] LLM 调用失败，回退到 medium: {e}")
        complexity = Complexity.MEDIUM
        result = {
            "complexity": "medium",
            "reasoning": f"LLM 调用失败，回退: {e}",
            "key_risks": [],
            "suggested_subtask_count": 2,
        }
        _analyze_degraded = f"analyze LLM 调用失败，复杂度静默回退 MEDIUM（{e}）"

    logger.info(f"[ANALYZE] 复杂度判定: {complexity.value}")
    affected_files = list(knowledge_context.get("affected_files") or [])
    if not affected_files:
        affected_files = [
            r.get("file_path", "")
            for r in knowledge_context.get("struct", [])
            if r.get("file_path")
        ]

    # ── 修复 A：单文件改动后置降级 medium → SIMPLE ──
    # 背景(task dab669bb/16098179)："给某个类加一个方法"这类【单文件单点改动】被 LLM 判
    # medium → 走 PLAN/ELABORATE/VALIDATE 四道慢 LLM 规划(~11min) + 拆成「实现+测试」多子任务
    # → 多子任务改同文件 MERGE 冲突 / 测试子任务写错 → replan 死循环。
    # 单文件任务由【一个 worker 一次写完】，有完整上下文、不拆强耦合坏子任务，SIMPLE 快速路径
    # 跳过四道大模型规划。
    # 信号源关键：必须用【任务描述里显式点名的文件】(_classify_file_ops)，【不能】用知识库
    # 检索的 affected_files——后者是"相关上下文文件"(常 25+ 个 struct)，不是"要改的文件"，
    # 用它判单文件永远不成立(踩坑 task 94f41bec：struct=25 导致降级失效)。
    # 仅在【LLM 判 medium】+【任务描述明确点名恰好 1 个 modify 文件】+【无 create/delete】时生效，
    # 不降级 complex，不误伤跨文件(点名 >1 文件不触发)、不误伤新建文件任务。
    if complexity == Complexity.MEDIUM:
        from swarm.brain.nodes.shared import _classify_file_ops
        _ops = _classify_file_ops(task_description)
        # 合并点名的所有文件（modify/create/delete 去重）。注意：_classify_file_ops 会把
        # "新增方法"误归类为 create（"新增"关键词），但"给已有类加方法"实质是改单个已存在文件。
        # 故不区分 modify/create，只看【任务点名的不同文件总数】——恰好 1 个即单文件任务。
        _named_all = list(dict.fromkeys(
            [f for f in (_ops.get("modify", []) + _ops.get("create", []) + _ops.get("delete", [])) if f]
        ))
        if len(_named_all) == 1:
            logger.info(
                "[ANALYZE] 单文件改动后置降级 medium → SIMPLE（任务点名唯一文件=%s，"
                "走快速路径：单 worker 一次写完，跳过四道慢规划，避免多子任务同文件冲突）",
                _named_all[0],
            )
            complexity = Complexity.SIMPLE
            if isinstance(result, dict):
                result["complexity"] = "simple"
                result["reasoning"] = (
                    f"[后置降级] 原判 medium，但任务仅点名单文件 {_named_all[0]}，"
                    f"降级 SIMPLE 走快速路径。原因：{result.get('reasoning', '')}"
                )
    reasoning = str(result.get("reasoning", ""))[:300] if isinstance(result, dict) else ""
    analyze_touch = touch_context(
        work_state,
        "analyze",
        f"复杂度={complexity.value}; 理由={reasoning}",
    )
    return {
        "complexity": complexity,
        "knowledge_context": knowledge_context,
        "affected_files": affected_files,
        "recent_task_summaries": recent_summaries or [],
        "degraded_reasons": list(state.get("degraded_reasons") or []) + (
            [_analyze_degraded] if _analyze_degraded else []
        ),
        **_planning_triage(task_description, complexity, state),
        **context_patch,
        **analyze_touch,
    }


def _format_tech_design_for_plan(state: BrainState) -> str:
    """把 tech_design 产出（file_plan + 数据模型 + 契约）格式化给 PLAN，作为定 scope 的权威依据。

    空（未经 tech_design 或失败）→ 返回提示让 PLAN 回退自推导。
    """
    td = state.get("tech_design") or {}
    file_plan = state.get("tech_design_file_plan") or []
    if not file_plan and not td.get("data_model"):
        return "（无技术设计方案——请据项目结构/知识库自行推导要建/改的文件）"
    lines: list[str] = []
    if td.get("data_model"):
        lines.append(f"【数据模型】{td.get('data_model')}")
    if file_plan:
        lines.append("【文件级方案 file_plan】（据此确定子任务 scope 的文件，路径已经过事实核验）：")
        for fp in file_plan:
            if not isinstance(fp, dict):
                continue
            act = fp.get("action", "?")
            dep = f" 依赖:{fp.get('depends_on')}" if fp.get("depends_on") else ""
            lines.append(f"  - [{act}] {fp.get('path', '?')} — {fp.get('responsibility', '')}{dep}")
    contract = state.get("shared_contract_draft") or {}
    if contract:
        import json as _json
        lines.append(f"【共享契约】{_json.dumps(contract, ensure_ascii=False)[:600]}")
    if td.get("architecture"):
        lines.append(f"【架构概述】{str(td.get('architecture'))[:300]}")
    return "\n".join(lines)


async def _invoke_llm_abortable(llm, messages, total_timeout: float):
    """R34-1：流式优先 LLM 调用——僵尸生成缓解（token 黑洞治本客户端侧杠杆）。

    非流式 ainvoke 被 wait_for 取消后，网关/后端可能继续整段生成（僵尸占 GPU →
    越烧越慢死亡螺旋，round34 规划期 ~71 次 300s 超时实证形态）。流式请求客户端
    断开时主流推理后端会 abort 生成；且 chunk 间隔看门狗在停滞早期即断连，不干等
    总超时。llm 无 astream（测试桩/不支持流）→ 原 wait_for(ainvoke) 行为逐字节不变。
    chunk 间隔上限 SWARM_PLAN_BATCH_CHUNK_GAP（默认 120s，非法回退）。超时统一抛
    asyncio.TimeoutError（调用方按既有 timeout 分支处理）。
    """
    import asyncio as _aio
    astream = getattr(llm, "astream", None)
    if astream is None:
        return await _aio.wait_for(llm.ainvoke(messages), timeout=total_timeout)
    try:
        _gap = float(os.environ.get("SWARM_PLAN_BATCH_CHUNK_GAP", "120") or "120")
    except ValueError:
        logger.error("[PLAN-BATCH] SWARM_PLAN_BATCH_CHUNK_GAP 配置非法(%r)——回退默认 120",
                     os.environ.get("SWARM_PLAN_BATCH_CHUNK_GAP"))
        _gap = 120.0
    parts: list[str] = []
    loop = _aio.get_running_loop()  # 协程内惯用法（复核 INFO：避 get_event_loop 弃用面）
    deadline = loop.time() + total_timeout
    agen = astream(messages)
    try:
        while True:
            remaining = deadline - loop.time()
            if remaining <= 0:
                raise _aio.TimeoutError()
            try:
                chunk = await _aio.wait_for(
                    agen.__anext__(), timeout=min(_gap, remaining))
            except StopAsyncIteration:
                break
            # hunter CONFIRMED：list content-block（部分 provider 流式形态）绝不能 str()
            # ——那产出 Python repr 污染 JSON。按 langchain 口径抽取 text 分片。
            c = getattr(chunk, "content", None)
            if isinstance(c, str):
                if c:
                    parts.append(c)
            elif isinstance(c, list):
                for _blk in c:
                    if isinstance(_blk, str) and _blk:
                        parts.append(_blk)
                    elif isinstance(_blk, dict):
                        _t = _blk.get("text") or _blk.get("content") or ""
                        if isinstance(_t, str) and _t:
                            parts.append(_t)
    finally:
        # 断连即弃：aclose 促使传输层关闭，流式后端据此 abort 生成（僵尸源头治理）
        _aclose = getattr(agen, "aclose", None)
        if _aclose is not None:
            try:
                # 复核 INFO：aclose 本身有界（饱和态传输 wedge 时不无限干等，5s 足够本地关流）
                await _aio.wait_for(_aclose(), timeout=5.0)
            except Exception:  # noqa: BLE001 — 关闭失败/超时不掩盖主结果/主异常
                pass
    return type("R", (), {"content": "".join(parts)})()


def _previous_plan_repair_block(prev_plan, prev_baseline) -> str:
    """R31-3 T3：D09 重试的增量修补块——上一版 plan 摘要 + 修补纪律。

    round31 实证：feedback 只列 issue 时 PLAN 每轮全量重拆，issue 集合 13→15→12→14
    震荡不收敛（第 2 轮把第 1 轮已覆盖条目又随机丢了）。给出上一版摘要并显式要求
    "保留已通过部分"，把重试从重掷骰子变成定向修补。prev_plan 缺失/畸形 → ""（首次
    规划/降级路径零注入）。有界：每子任务一行 desc[:60]，总量 6000 字符封顶。
    """
    subtasks = getattr(prev_plan, "subtasks", None) or []
    if not subtasks:
        return ""
    lines = []
    for st in subtasks:
        desc = (getattr(st, "description", "") or "").replace("\n", " ")[:60]
        covers = getattr(st, "covers", None) or []
        lines.append(f"  {getattr(st, 'id', '?')}: {desc} covers={list(covers)}")
    bl = [f"{e.get('id')}({str(e.get('reason') or '')[:40]})"
          for e in (prev_baseline or []) if isinstance(e, dict)]
    full = "\n".join(lines)
    body = full[:6000]
    if len(full) > 6000:
        # 复核 L-4：截断必须自述——否则"保留未点名子任务"指令会反向诱导 LLM 丢掉未展示的
        body += "\n  （摘要已截断：未列出的子任务同样存在且需保留）"
    _bl_text = ", ".join(bl)[:1200]  # hunter F3：申报摘要独立定界（拼在 body 截断之后）
    return (
        "\n上一版计划摘要（子任务 → covers 声明）：\n" + body
        + (f"\nbaseline_covered 申报: {_bl_text}" if _bl_text else "")
        + "\n\n增量修补纪律：以上一版计划为基础定向修补——保留校验未点名问题的子任务拆分、"
        "covers 声明与 baseline_covered 申报，只修正上面点名的问题；"
        "不要全量重拆（重拆会把已覆盖的条目再次随机丢失，白烧重试预算）。\n"
    )


def _requirement_coverage_prompt_block(requirement_items, *, batched: bool = False) -> str:
    """S2-3（task#24）：PLAN prompt 的需求条目清单 + covers 声明纪律注入块。

    加法式注入（不改 PLAN_USER/PLAN_BATCH_USER 模板本体）：requirement_items 缺失/空
    → 返回 ""，拼接后 prompt 与老行为【一字不差】（抽取降级/老任务零变化）。
    非空 → 追加条目清单（id+kind+text）与"每个条目必须被至少一个子任务 covers"纪律，
    供 validate_plan 的覆盖矩阵确定性对账（plan_validator.validate_requirement_coverage）。
    batched=True（ultra 分批路径）：单批只见部分文件，追加"本批只声明相关条目"说明——
    全覆盖由 merge 后的整体校验兜底。通用多栈多领域：块内无任何语言/框架/领域词汇。
    """
    items = [
        it for it in (requirement_items or [])
        if isinstance(it, dict) and str(it.get("id") or "").strip()
    ]
    if not items:
        return ""
    lines = [
        f"- {str(it['id']).strip()} [{it.get('kind', 'other')}] {str(it.get('text') or '')[:200]}"
        for it in items
    ]
    batch_note = (
        "\n- 分批拆解提示：本批只为与【本批文件清单】相关的条目声明 covers，"
        "无关条目留给其他批次（系统会在所有批次合并后整体校验全覆盖）。"
        if batched else ""
    )
    return (
        "\n\n## 需求条目清单（PRD 覆盖矩阵 —— 覆盖声明为硬性要求）\n"
        "以下是从需求文本确定性抽取的结构化需求条目。每个子任务的 JSON 对象必须包含 "
        "\"covers\" 字段（字符串列表），声明该子任务负责实现哪些需求条目 ID，例如 "
        "\"covers\": [\"req-xxxxxxxx\"]：\n"
        + "\n".join(lines)
        + "\n\n覆盖声明纪律：\n"
        "- 每个需求条目必须被至少一个子任务的 covers 覆盖——未覆盖的条目会被计划校验拒绝并要求重新规划；\n"
        "- covers 只能引用上面清单中存在的 ID，绝不编造 ID；\n"
        "- 一个子任务可以覆盖多个条目；与该子任务无关的条目不要写进它的 covers。\n"
        "- R31：若某条目已被【现有代码】完整满足、本任务无需为它做任何改动，不要硬造子任务——"
        "改在计划 JSON 顶层用 baseline_covered 字段申报，例如 "
        "\"baseline_covered\": [{\"id\": \"req-xxxxxxxx\", \"reason\": \"现有代码何处/如何已满足（简要依据）\"}]；\n"
        "- baseline_covered 同样只能引用清单中的 ID，且 reason 必填非空；\n"
        "- ★baseline_covered 仅指仓库中【当前已存在】的代码——本计划将要新建的模块/"
        "由其他子任务或其他批次实现的功能【绝不】申报（那属于对应子任务的 covers）；"
        "reason 必须指向可在现有代码中核实的位置★；\n"
        "- 申报是对现状的承诺：交付前会对需求条目做运行时验收核查——可自动执行的断言若与"
        "申报不符会导致验收失败并回灌整改；无法自动核实的申报（如需鉴权的能力）会被降级"
        "标记并呈报人工审核。只申报你能从现有代码中确认的能力。"
        + batch_note
    )


async def _plan_ultra_batched(
    llm, state, task_description, knowledge_context, sliding_ctx, file_plan,
):
    """ultra 超大需求【按模块分批】拆解（DESIGN 第九节治本 P1-P6）。

    治本核心：按【功能模块】分批（每模块一批，批内垂直切片），替代旧的 10% 机械文件切片。
    - P1 垂直切片：批内 prompt 要求"一个完整功能=一个子任务"（含 Entity+Mapper+Service+Controller）
    - P2 跨批依赖：批间按 tech_design 模块 depends_on 排序 + merge 批间串行
    - P3 模块脚手架：新模块(无现有目录)加前置脚手架子任务
    - P4 路径规范：prompt 强制模块路径前缀统一
    - P5 去重：分批前 dedupe_file_plan 去同名文件
    - P6 验收：prompt 强制每子任务给 acceptance(首选 mvn compile)
    """
    import time as _time

    from swarm.brain.plan_batch import (
        batch_progress_line,
        batch_signature,
        dedupe_file_plan,
        group_into_module_batches,
        merge_subtask_batches,
        split_oversized_batches,
    )
    from swarm.brain.prompts import PLAN_BATCH_SYSTEM, PLAN_BATCH_USER

    td = state.get("tech_design") or {}
    # P1-DEBT-02 修复：① 键名 tech_design_result→tech_design（原键全项目无人写，td 恒空
    #   导致 module_deps 批间依赖排序失效 + data_model 注入空）；② shared_contract 不在
    #   tech_design dict 里——它被 tech_design 节点单独 pop 为 shared_contract_draft，须从
    #   state.shared_contract_draft 取（contract_design 节点产出也落此键）。
    tech_design_extra = json.dumps(
        {
            "data_model": td.get("data_model", ""),
            "shared_contract": state.get("shared_contract_draft") or {},
        },
        ensure_ascii=False,
    )[:3000]
    proj_struct = _format_project_structure(knowledge_context)
    # sliding_ctx 头部带 plan() 注入的"上轮 replan/校验失败根因 + R31-3 增量修补块"——
    # 此前分批路径把它静默丢弃，ULTRA replan 退化为盲重规划（反复产同样的坏计划）。
    # 复核 H-1：原 [:2000] 在 D09 覆盖 issues（round31 量级 12-15 条 ≈3000+ 字符）+
    # 修补块（≤6.2K，拼在 issues 之后）下会把修补块整块截没——T3 为 ULTRA 分批而生
    # 却到不了主战场。上界放到 14000（feedback 8K 帽 + 修补块 6.2K 帽，两处上游已
    # 各自定界，此处仅兜底）。
    sliding_ctx_text = (sliding_ctx or "").strip()[:14000] or "（无）"
    # S2-3：分批路径同样注入需求条目清单（items 空=一字不加）。batched=True 提示本批只
    # 声明相关条目——全覆盖由 merge 后 validate_plan 的覆盖矩阵整体校验兜底。
    _cov_block = _requirement_coverage_prompt_block(
        state.get("requirement_items"), batched=True)

    # P5：分批前全局去重同名文件
    _before = len(file_plan)
    file_plan = dedupe_file_plan(file_plan)
    if len(file_plan) < _before:
        logger.info("[PLAN-BATCH] P5 去重：%d → %d 文件（移除 %d 个同名重复）",
                    _before, len(file_plan), _before - len(file_plan))

    # 模块依赖（tech_design 阶段1 modules.depends_on）供批间排序
    module_deps = {}
    for m in (td.get("modules") or []):
        if isinstance(m, dict) and m.get("name"):
            module_deps[m["name"]] = m.get("depends_on") or []

    # P1/P2：按模块分批（每模块一批，批间依赖序）
    module_batches = group_into_module_batches(file_plan, module_deps or None)
    # R32-2 U1：超大模块批二次切分（round32：大批 4 轮 16 次确定性超时 >300s，小批全成；
    # FINDING-10 降级只兜底不治"批太大"）。子批保原序，串行门控沿用 merge 既有机制。
    try:
        _PLAN_BATCH_MAX_FILES = int(
            os.environ.get("SWARM_PLAN_BATCH_MAX_FILES", "20") or "20")
    except ValueError:
        logger.error(
            "[PLAN-BATCH] SWARM_PLAN_BATCH_MAX_FILES 配置非法(%r)——回退默认 20",
            os.environ.get("SWARM_PLAN_BATCH_MAX_FILES"))
        _PLAN_BATCH_MAX_FILES = 20
    _before_split = len(module_batches)
    module_batches = split_oversized_batches(module_batches, _PLAN_BATCH_MAX_FILES)
    if len(module_batches) > _before_split:
        logger.info("[PLAN-BATCH] U1 超大批切分：%d → %d 批（单批 ≤%d 文件）",
                    _before_split, len(module_batches), _PLAN_BATCH_MAX_FILES)
    total = len(module_batches)
    logger.info(
        "[PLAN-BATCH] 按模块分批拆解启动：%d 文件 → %d 个模块批（垂直切片，非机械10%%切）",
        len(file_plan), total,
    )

    # P3：识别新模块（项目里无该模块目录前缀的）→ 需脚手架前置
    existing_dirs = set()
    proj_path = _get_project_path(state.get("project_id") or "")
    if proj_path:
        import os as _os
        try:
            for d in _os.listdir(proj_path):
                if _os.path.isdir(_os.path.join(proj_path, d)):
                    existing_dirs.add(d)
        except Exception:  # noqa: BLE001
            pass

    # FINDING-10(task 25a6d83c)：每批 LLM 调用加【总墙钟上限】(asyncio.wait_for)——与 TECH_DESIGN
    # stage2 单模块 500s 超时同构。否则 brain 模型(GLM-5.2)某批失控持续生成时,无 chunk 看门狗抓不到、
    # read-timeout 不管总时长 → PLAN 单批挂死整个任务(实测挂 16min)。超时按已有 except 分支降级跳过。
    import asyncio as _asyncio
    # round29 真因4 配套：墙钟 env 可调（默认 300s 不变）——原硬码使降级路径无法被行为测试覆盖。
    # 复核 A：非法配置值（如 "abc"）不得裸穿被外层 except 误诊成"LLM 调用失败"（系统级配置错
    # 会打挂每一个 ULTRA 任务，排障方向全错）——显式按配置错误归因，ERROR 留痕后回退默认。
    try:
        _PLAN_BATCH_TIMEOUT = float(os.environ.get("SWARM_PLAN_BATCH_TIMEOUT", "300") or "300")
    except ValueError:
        logger.error(
            "[PLAN-BATCH] SWARM_PLAN_BATCH_TIMEOUT 配置非法(%r)——系统级配置错误请修 env，"
            "本次回退默认 300s", os.environ.get("SWARM_PLAN_BATCH_TIMEOUT"),
        )
        _PLAN_BATCH_TIMEOUT = 300.0
    # 秒/批（正常 ≤171s，留 ~1.7x 余量，失控时 5min 截断降级）
    # P6a（治本，996db614 实测 2/9 模块批分解失败→那俩模块零子任务永不构建→交付残缺）：批分解
    # timeout/error/空 此前【无重试静默丢】，与骨架曾犯同病。失败多为 GLM-5.2 瞬时 timeout，1 次
    # 重试大概率恢复（镜像骨架/Stage B 成熟模式）。耗尽才计 failed_batches。env 可调。
    _PLAN_BATCH_MAX_ATTEMPTS = int(os.environ.get("SWARM_PLAN_BATCH_MAX_ATTEMPTS", "2") or "2")
    batch_results: list[list[dict]] = []
    failed_batches = 0
    # 各模块批【独立分解】(生成时无跨批依赖，跨批串行依赖在 merge 阶段才加)→ 并发执行。
    # 实测本地 40B 后端(2×5090 连续批处理)8 并发仍近线性(1.46×)，保守取 4 并发，留足余量。
    # 串行 11 批 ~20min → 并发4 ~5min。gather 保序：结果按 module_batches 顺序收集，merge 全局
    # 编号与串行版一致。逐批超时/异常降级与原行为逐字节一致(返回标记，主循环统一计 failed_batches)。
    _plan_sem = _asyncio.Semaphore(4)
    # R31-1 T1：各批 LLM 顶层 baseline_covered 申报的收集器（成功批才收，闭包单线程安全）
    _baseline_decls: list = []
    # R32-1 U2：成功批缓存。★只在"上一轮有失败批"的补齐型重试复用（round32 证据：覆盖
    # issue 集合与批失败集合同构）；上一轮批全成的纯覆盖分歧重试（round31 形态）绝不吃
    # 缓存——复用=产出同一 plan，T3 增量修补/baseline 申报永远无法生效★。签名不吃
    # feedback（正要跨 feedback 复用）；file_plan 变更签名天然不同。缓存整体重建
    # （last-write-wins：本轮成功批集合），不增量累积防陈旧条目滚雪球。
    _batch_cache_prev: dict = state.get("plan_batch_cache") or {}
    # 复核 F-3：执行失败 replan（replan_feedback 非空）禁用缓存——人工闸放行残缺计划后
    # 执行失败，缓存批必须带 replan 教训真跑（宁慢勿错，回退 pre-U2 行为）。
    _repair_retry = bool(state.get("plan_batch_failed_modules")) and not (
        state.get("replan_feedback") or "").strip()
    _batch_cache_new: dict = {}

    async def _decompose_batch(i: int, mod_name: str, batch: list) -> tuple:
        # R32-1 U2：补齐型重试的缓存命中——签名一致的成功批直接回放（零 LLM/零信号量），
        # 申报随缓存回放（不丢）；回放件重新入本轮缓存（下轮重试仍可用）。
        _sig = batch_signature(mod_name, batch)
        if _repair_retry and _sig in _batch_cache_prev:
            _hit = _batch_cache_prev[_sig]
            # R34-2 哨兵：上一轮已证实确定性超时并 bisect 过的批 → 直接判 timeout 触发
            # 切分（半批各自缓存命中/真跑），省掉每 attempt 整批重烧 600s（round34 实证
            # 4 慢批×4 attempt ≈40min 纯重复）。哨兵随轮重写防陈旧。
            if _hit.get("bisected") and len(batch) >= 2:
                _batch_cache_new[_sig] = {"module": mod_name, "bisected": True}
                logger.info("[PLAN-BATCH] U2 哨兵命中：批'%s'已知确定性超时 → 直接切分",
                            mod_name)
                return ("timeout", i, mod_name, None, 0.0, len(batch))
            _cached_subs = json.loads(json.dumps(_hit.get("subtasks") or []))
            _cached_bl = list(_hit.get("baseline") or [])
            if _cached_subs:
                if _cached_bl:
                    _baseline_decls.extend(_cached_bl)
                _batch_cache_new[_sig] = {"module": mod_name,
                                          "subtasks": _hit.get("subtasks") or [],
                                          "baseline": _cached_bl}
                logger.info("[PLAN-BATCH] U2 缓存命中：模块'%s'（%d 子任务，跳过 LLM 分解）",
                            mod_name, len(_cached_subs))
                return ("ok", i, mod_name, _cached_subs, 0.0, len(batch))
        batch_fp_text = "\n".join(
            f"  - {fp.get('path')} [{fp.get('action', 'create')}] {fp.get('responsibility', '')}"
            for fp in batch
        )
        # P3：判断该模块是否为新模块（文件路径顶层目录不在现有目录里）
        top_dirs = {(fp.get("path") or "").replace("\\", "/").split("/")[0]
                    for fp in batch if fp.get("path")}
        new_module_dirs = [d for d in top_dirs if d and d not in existing_dirs]
        # 复核 F-2：U1 切分后的非首子批（mod#i/k, i>1）不触发脚手架提示——每子批各造
        # 一份脚手架时，通用去重网兜不住非 Maven 栈（dedupe_module_scaffolds 只认 pom.xml）。
        if "#" in mod_name and not mod_name.split("#", 1)[1].startswith("1/"):
            new_module_dirs = []
        # R33-1 U3 同理：bisect 半批只有纯 ~a 链保留脚手架（~b 及其后代不重复触发）
        if "~" in mod_name and any(p != "a" for p in mod_name.split("~")[1:]):
            new_module_dirs = []
        scaffold_hint = ""
        if new_module_dirs:
            scaffold_hint = (
                f"\n\n【重要-P3 新模块脚手架】本模块涉及新建模块目录 {new_module_dirs}，"
                f"项目中尚不存在。请在本批【第一个子任务】先创建该模块的基础设施"
                f"（如 Maven 模块的 pom.xml 并注册到父 pom 的 <modules>、基础目录结构），"
                f"该模块其他子任务 depends_on 这个脚手架子任务。")
        try:
            prompt_user = PLAN_BATCH_USER.format(
                task_description=task_description[:2000],
                sliding_context=sliding_ctx_text,
                batch_idx=i, total_batches=total,
                batch_file_plan=f"模块 '{mod_name}'：\n{batch_fp_text}{scaffold_hint}",
                project_structure=proj_struct,
                tech_design_extra=tech_design_extra,
            )
        except (KeyError, IndexError, ValueError) as exc:
            # 模板占位符与传参漂移是确定性代码 bug：重试无意义，按"批失败"降级并高可见
            # 记录；不让 KeyError 裸穿 gather 炸掉全部批（外层 except 会把它伪装成一次
            # 普通 LLM 失败，排障方向全错）。全批失败仍由下方 RuntimeError 兜底。
            logger.error(
                "[PLAN-BATCH] 模块'%s' prompt 模板占位符与传参不匹配（代码 bug，非 LLM 故障）：%r",
                mod_name, exc,
            )
            return ("error", i, mod_name, exc, None, len(batch))
        # S2-3：追加需求条目清单 + covers 纪律（items 空时 _cov_block=""，一字不加）
        prompt_user += _cov_block
        async with _plan_sem:
            # P6a：timeout/error/空 重试（镜像骨架/Stage B），耗尽才返回失败标记。拿到非空子任务即成功。
            last_fail: tuple = ("error", i, mod_name, None, None, len(batch))
            for _attempt in range(1, _PLAN_BATCH_MAX_ATTEMPTS + 1):
                _t0 = _time.monotonic()
                try:
                    # R34-1：流式+chunk 看门狗（无 astream 的桩/客户端=原 wait_for 行为）
                    response = await _invoke_llm_abortable(
                        llm,
                        [
                            {"role": "system", "content": PLAN_BATCH_SYSTEM},
                            {"role": "user", "content": prompt_user},
                        ],
                        _PLAN_BATCH_TIMEOUT,
                    )
                    _dt = _time.monotonic() - _t0
                    result = _parse_json_from_llm(response.content)
                    subs = result.get("subtasks", []) if isinstance(result, dict) else []
                    for _st in subs:
                        if isinstance(_st, dict):
                            for _opt in ("harness", "contract"):
                                if _opt in _st and _st[_opt] is None:
                                    _st.pop(_opt)
                    if subs:
                        # R31-1 T1：本批的 baseline_covered 申报进闭包收集（gather 单线程安全），
                        # merge 后统一 normalize 去重——批间重复申报同一条目保首即可。
                        _bl = result.get("baseline_covered") if isinstance(result, dict) else None
                        if isinstance(_bl, list) and _bl:
                            _baseline_decls.extend(_bl)
                        # R32-1 U2：成功批入缓存（深拷贝——下游 merge/模块标记会变异 subs；
                        # 复核 F-5c：baseline 同深拷贝，不赌 normalize 不变异入参的实现细节）
                        _batch_cache_new[_sig] = {
                            "module": mod_name,
                            "subtasks": json.loads(json.dumps(subs)),
                            "baseline": json.loads(json.dumps(_bl))
                            if isinstance(_bl, list) else [],
                        }
                        return ("ok", i, mod_name, subs, _dt, len(batch))
                    # 复核 L-1：空子任务批的 baseline 申报不收（批失败面），申报蒸发须留痕
                    # ——信号未失联（该模块进 failed 记账→fail-fast 闸），但审计要能看见。
                    _bl_lost = result.get("baseline_covered") if isinstance(result, dict) else None
                    if isinstance(_bl_lost, list) and _bl_lost:
                        logger.warning(
                            "[PLAN-BATCH] 模块'%s' 批无子任务但含 %d 条 baseline 申报——"
                            "失败批申报不收（未覆盖条目将走覆盖校验重试）", mod_name, len(_bl_lost))
                    last_fail = ("ok", i, mod_name, [], _dt, len(batch))  # 空 → 可重试
                except _asyncio.TimeoutError:
                    last_fail = ("timeout", i, mod_name, None, None, len(batch))
                except Exception as exc:  # noqa: BLE001
                    last_fail = ("error", i, mod_name, exc, None, len(batch))
                if _attempt < _PLAN_BATCH_MAX_ATTEMPTS:
                    logger.warning(
                        "[PLAN-BATCH] 模块'%s' 第 %d/%d 次分解失败(%s)，退避重试",
                        mod_name, _attempt, _PLAN_BATCH_MAX_ATTEMPTS, last_fail[0],
                    )
                    await _asyncio.sleep(min(2.0 * _attempt, 8.0))
            return last_fail

    # gather 按输入顺序返回 → 保持 module_batches(模块依赖序)的批次顺序
    _outcomes = await _asyncio.gather(*[
        _decompose_batch(i, mod_name, batch)
        for i, (mod_name, batch) in enumerate(module_batches, start=1)
    ])
    # R33-1 U3：bisect-on-timeout——确定性慢批（内容特异性生成失控，非纯体量：round33
    # 实证 12 文件批也超时 4 次）对半切分重试，有界两轮（最小 1/4 批，1 文件不再切）。
    # 半批 ~a/~b 后缀命名：独立签名入 U2 缓存、独立失败记账、按原位置替换保批间序。
    _batch_files: dict[str, list] = {n: f for n, f in module_batches}
    # 泄压阀（对照 SWARM_PLAN_COVERAGE_GATE 先例）：默认开；关闭回退整批记账旧行为。
    _bisect_on = os.environ.get("SWARM_PLAN_BATCH_BISECT", "1").strip().lower() \
        not in ("0", "false", "no", "off")
    for _bisect_round in range(2 if _bisect_on else 0):
        _to_split = [(idx, oc) for idx, oc in enumerate(_outcomes)
                     if oc[0] == "timeout" and len(_batch_files.get(oc[2]) or []) >= 2]
        if not _to_split:
            break
        _specs = []
        for _idx, _oc in _to_split:
            _name, _files = _oc[2], _batch_files[_oc[2]]
            _mid = (len(_files) + 1) // 2
            logger.warning(
                "[PLAN-BATCH] U3 批'%s'超时耗尽重试 → 对半切分重试（%d+%d 文件）",
                _name, _mid, len(_files) - _mid)
            # R34-2：整批签名落哨兵入本轮缓存（下一 attempt 免整批重烧）
            _batch_cache_new[batch_signature(_name, _files)] = {
                "module": _name, "bisected": True}
            for _suf, _part in (("~a", _files[:_mid]), ("~b", _files[_mid:])):
                _batch_files[_name + _suf] = _part
                _specs.append((_idx, _oc[1], _name + _suf, _part))
        # R34-1 退避：超时批 bisect 前给饱和后端冷却窗（env 可调，0=关）
        try:
            _cooldown = float(os.environ.get(
                "SWARM_PLAN_BATCH_TIMEOUT_COOLDOWN", "15") or "15")
        except ValueError:
            _cooldown = 15.0
        if _cooldown > 0:
            await _asyncio.sleep(_cooldown)
        _half_ocs = await _asyncio.gather(*[
            _decompose_batch(_i, _hname, _part)
            for (_x, _i, _hname, _part) in _specs
        ])
        _replace: dict[int, list] = {}
        for (_idx, _i, _hname, _part), _hoc in zip(_specs, _half_ocs):
            _replace.setdefault(_idx, []).append(_hoc)
        _outcomes = [
            _o for idx, oc in enumerate(_outcomes)
            for _o in (_replace.get(idx) or [oc])
        ]

    # round29 真因4 治本：失败模块【结构化记账】而非只计数——d37a52a3 实测 'system-enhance'
    # 14 文件两次 timeout 被降级跳过后无任何 state 痕迹，任务其余成功则记 DONE 但交付物静默缺
    # 整模块 + LEARN_SUCCESS 学成成功模式（伪装成功）。记账供 plan 节点落 state
    # （plan_batch_failed_modules）→ can_auto_accept_plan fail-fast 升人工 + degraded_reasons
    # 拦 L6 假成功学习。降级容错语义不变（幸存批照常合并）。
    plan_batch_failed_modules: list[dict] = []
    for kind, i, mod_name, payload, _dt, _nfiles in _outcomes:
        if kind == "ok" and payload:
            # 复核 B：给每个子任务 dict 打模块标记（merge 的 {**st} 拷贝保留额外键），使末端
            # SubTask 构造失败能按模块归因记账，而非裸穿外层 except 连坐丢弃全部记账。
            for _st in payload:
                if isinstance(_st, dict):
                    _st["_plan_batch_module"] = mod_name
            batch_results.append(payload)
            logger.info("%s 模块'%s' 拆出 %d 个子任务",
                        batch_progress_line(i, total, _nfiles, _dt), mod_name, len(payload))
        elif kind == "ok":
            failed_batches += 1
            plan_batch_failed_modules.append(
                {"name": mod_name, "files": _nfiles, "reason": "empty"})
            logger.warning("%s 模块'%s' 未拆出子任务（降级跳过）",
                           batch_progress_line(i, total, _nfiles, _dt), mod_name)
        elif kind == "timeout":
            failed_batches += 1
            plan_batch_failed_modules.append(
                {"name": mod_name, "files": _nfiles, "reason": "timeout"})
            logger.warning(
                "%s 模块'%s' LLM 调用超时 >%.0fs（降级跳过，防 PLAN 无限挂 — FINDING-10）",
                batch_progress_line(i, total, _nfiles), mod_name, _PLAN_BATCH_TIMEOUT)
        else:
            failed_batches += 1
            plan_batch_failed_modules.append(
                {"name": mod_name, "files": _nfiles, "reason": f"error: {payload!r}"[:200]})
            logger.warning("%s 模块'%s' 拆解异常（降级跳过）: %s",
                           batch_progress_line(i, total, _nfiles), mod_name, payload)

    merged = merge_subtask_batches(batch_results)
    logger.info(
        "[PLAN-BATCH] 按模块分批完成：%d/%d 模块成功，合并出 %d 个子任务（失败 %d）",
        total - failed_batches, total, len(merged), failed_batches,
    )
    if not merged:
        # round27：全部批次失败时绝不静默返回空 scope 兜底计划——那会绕过 plan_generation_failed
        # 标记（TD2606-A5），在 auto_accept 下被 confirm_plan 放行 → worker 无文件可写 → 假失败。
        # 抛出让 plan() 的 except Exception 走与单发路径同构的 _plan_degraded 降级
        # （兜底计划 + plan_generation_failed=True，can_auto_accept_plan fail-fast 拦下）。
        raise RuntimeError(
            f"ultra 分批拆解全部 {total} 批失败（LLM 超时/异常），无可用子任务")
    # N-03 兼容：万一 LLM 仍吐旧键 acceptance（SubTask 字段是 acceptance_criteria，
    # extra=ignore 会静默丢弃致验收恒空），重映射后再构造。
    for st in merged:
        if isinstance(st, dict) and "acceptance" in st and "acceptance_criteria" not in st:
            st["acceptance_criteria"] = st.pop("acceptance")
    # 复核 B：逐子任务构造——个别字段畸形（pydantic ValidationError）不得裸穿外层通用 except
    # 把【全部】记账与幸存模块产出连坐丢弃（归因退化成"LLM 调用失败"）。畸形子任务按模块
    # 记入 plan_batch_failed_modules（reason=真实校验错误）+ WARNING，其余照常交付。
    _subtasks: list[SubTask] = []
    _invalid_by_module: dict[str, list[str]] = {}
    for st in merged:
        _mod = st.pop("_plan_batch_module", None) if isinstance(st, dict) else None
        try:
            _subtasks.append(SubTask(**st))
        except Exception as exc:  # noqa: BLE001  pydantic ValidationError 及同类构造错误
            _invalid_by_module.setdefault(str(_mod or "?"), []).append(f"{st.get('id', '?')}: {exc}")
            logger.warning(
                "[PLAN-BATCH] 模块'%s' 子任务 %s 字段畸形被剔除（记账不静默）: %s",
                _mod or "?", st.get("id", "?") if isinstance(st, dict) else "?", exc,
            )
    for _mod, _errs in _invalid_by_module.items():
        plan_batch_failed_modules.append({
            "name": _mod, "files": 0,
            "reason": f"invalid_subtasks({len(_errs)}): {_errs[0][:150]}",
        })
    if _invalid_by_module:
        # 复核 F-1 [M]：畸形批逐出缓存——否则补齐重试签名命中确定性回放同一畸形产物，
        # LLM 永远不被重问（U2 前重试有自愈机会，绝不让缓存把瞬时错误变成死局）。
        _batch_cache_new = {k: v for k, v in _batch_cache_new.items()
                            if v.get("module") not in _invalid_by_module}
    if not _subtasks:
        raise RuntimeError("ultra 分批拆解合并后子任务全部构造失败（字段畸形），无可用子任务")
    # round29 真因4：失败模块清单随 plan 一起返回（调用方落 state + 闸门消费），不再只留日志。
    # R31-1 T1：baseline 申报并集第三元返回——绝不挂 TaskPlan 字段（结构性防"变异路径丢字段"，
    # v0.9.23 F1 同类教训），由 plan() 落独立 state 键。
    # R32-1 U2：本轮成功批缓存第四元返回，plan() 落 state 供补齐型重试复用。
    from swarm.brain.plan_validator import normalize_baseline_covered
    return (TaskPlan(subtasks=_subtasks), plan_batch_failed_modules,
            normalize_baseline_covered(_baseline_decls), _batch_cache_new)


def _subtask_signature(st) -> tuple:
    """子任务签名（id+描述+写权 scope）——replan 前后【完全一致】才可复用旧完成态。"""
    sc = getattr(st, "scope", None)
    writable = tuple(sorted(getattr(sc, "writable", []) or [])) if sc else ()
    creates = tuple(sorted(getattr(sc, "create_files", []) or [])) if sc else ()
    return (getattr(st, "id", ""), (getattr(st, "description", "") or "").strip(), writable, creates)


def _surgical_replan_reset(old_results: dict, old_plan, new_plan,
                           old_recovery_counts: dict | None = None,
                           old_retry_counts: dict | None = None,
                           old_redecompose_counts: dict | None = None,
                           old_abandoned_ids: list | None = None,
                           old_give_up_ids: list | None = None) -> dict:
    """R1b（治本·纵深防御）：replan 重入时【按签名保留】完成态，不再无条件 clobber。

    新 plan 中 id+描述+写权 scope 与旧子任务【完全一致】且旧结果 L1 通过 → 保留其 subtask_results
    （dispatch 据 completed_ids 自动跳过、不重跑）；新增/变更/失败 的清空重派。premature victory 由
    "签名完全一致才保留"杜绝（旧 id 语义变=签名变→不保留→重派）。无旧完成态→空 reset（首规划）。

    遗漏项#2 复核 MEDIUM：targeted_recovery_counts 同签名纪律修剪——replan 分批重编号使 id 复用
    是【默认情形】（merge_subtask_batches 顺序重编 st-N），旧 id 的耗尽配额若粘滞会饿死语义全新的
    同名子任务（把 round29 治的"被别人用量饿死"换个形态复发）。签名完全一致才保留配额记账。

    D08（治本）：同签名纪律扩到【全部 replan 敏感记账表】——此前只清 subtask_results/
    targeted_recovery_counts，漏了 subtask_retry_counts / subtask_redecompose_count /
    abandoned_subtask_ids / give_up_isolated_ids。id 复用是默认情形，粘滞的旧账会：
      · 陈旧 retry_counts → 新 st-N 首败即 `_next>max_retries` 跳过重试直接 escalate；
      · 陈旧 redecompose_count>=1 → 阶梯二对语义全新的子任务永拒（拆小预算=0）；
      · 陈旧 abandoned/give_up id 命中新子任务 → get_dispatch_batch 排除 → 永不派发 →
        after_monitor 判"全不可派发"提前 MERGE = 假 PARTIAL。
    dict 记账（retry/redecompose/recovery）按签名一致保留，list 放弃标记（abandoned/give_up）
    只保留在新 plan 且签名一致者——签名变=语义新子任务，绝不继承旧放弃/旧配额。"""
    if not any((old_results, old_recovery_counts, old_retry_counts,
                old_redecompose_counts, old_abandoned_ids, old_give_up_ids)):
        return {}
    old_sig = {st.id: _subtask_signature(st) for st in (getattr(old_plan, "subtasks", []) or [])}
    new_sig = {st.id: _subtask_signature(st) for st in (getattr(new_plan, "subtasks", []) or [])}

    def _sig_unchanged(sid: str) -> bool:
        """签名完全一致（且在新 plan）——唯一可继承旧记账/旧放弃的条件。"""
        return sid in new_sig and old_sig.get(sid) == new_sig.get(sid)

    preserved = {
        sid: out for sid, out in (old_results or {}).items()
        if _sig_unchanged(sid) and l1_passed(out)
    }
    pruned_counts = {
        sid: n for sid, n in (old_recovery_counts or {}).items() if _sig_unchanged(sid)
    }
    pruned_retry = {
        sid: n for sid, n in (old_retry_counts or {}).items() if _sig_unchanged(sid)
    }
    pruned_redecompose = {
        sid: n for sid, n in (old_redecompose_counts or {}).items() if _sig_unchanged(sid)
    }
    pruned_abandoned = [sid for sid in (old_abandoned_ids or []) if _sig_unchanged(sid)]
    pruned_give_up = [sid for sid in (old_give_up_ids or []) if _sig_unchanged(sid)]
    logger.info(
        "[PLAN] replan 重入：按签名保留 %d/%d 个已完成子任务（其余清空重派），不再全量 clobber"
        "；记账保留 recovery=%d/%d retry=%d/%d redecompose=%d/%d；放弃标记保留 abandoned=%d/%d "
        "give_up=%d/%d（签名不一致=语义新子任务，清空旧账不饿死）",
        len(preserved), len(old_results or {}),
        len(pruned_counts), len(old_recovery_counts or {}),
        len(pruned_retry), len(old_retry_counts or {}),
        len(pruned_redecompose), len(old_redecompose_counts or {}),
        len(pruned_abandoned), len(old_abandoned_ids or []),
        len(pruned_give_up), len(old_give_up_ids or []),
    )
    return {
        "subtask_results": preserved,
        "dispatch_remaining": [],
        "failed_subtask_ids": [],
        "targeted_recovery_counts": pruned_counts,
        # D08：补清余下三张 replan 敏感记账/放弃表（同签名纪律，防旧账饿死/误弃新子任务）
        "subtask_retry_counts": pruned_retry,
        "subtask_redecompose_count": pruned_redecompose,
        "abandoned_subtask_ids": pruned_abandoned,
        "give_up_isolated_ids": pruned_give_up,
        # 批4c 补漏（外部复核）：replan 重入=新一轮规划，清历史 escalate 粘滞
        # （confirm/deliver REVISE→PLAN 路径不经 revision()/handle_failure，此处是汇合点）
        "failure_escalated": False,
    }


async def plan(state: BrainState) -> dict:
    """PLAN 节点 — 将任务拆解为子任务 DAG

    输入: task_description, complexity, knowledge_context
    输出: plan
    """
    task_description = state.get("task_description", "")
    # 复杂度走单一真值入口（assess 优先→analyze 初判→MEDIUM 兜底 + resume 字符串归一枚举，
    # task 8537fa5e）。CODEWALK 根因A纪律②：此处原为手写内联版，绕开入口即漂移温床。
    complexity = effective_complexity(state)
    knowledge_context = state.get("knowledge_context", {})

    # I3 防 premature victory：检测 replan 重入——若 state 已有 subtask_results（说明这是
    # handle_failure(replan) / confirm(revise) 触发的重新规划，非首次），则旧的完成态事实表
    # 不可信（新 plan 可能复用旧子任务 id 但语义已变，旧"成功"结果会让新子任务被误判已完成
    # 而跳过执行 = premature victory）。replan 语义 = 一切重来，确定性清空完成态 + 派发队列，
    # 让新 plan 的所有子任务都重新派发。完成态只由 dispatch 基于真实 WorkerOutput 重新写。
    # R1b：replan 重入时改【按签名外科手术式保留】完成态（见 _surgical_replan_reset），不再无条件
    # clobber 全部（旧行为把 34 个已完成全清空从头重跑=996db614 主失控）。新 plan 在各路径构建后，
    # 于 return 处用 _surgical_replan_reset(旧结果, 旧plan, 新plan) 算保留集。此处仅捕获旧态（将被覆盖）。
    _replan_old_results = state.get("subtask_results") or {}
    _replan_old_plan = state.get("plan")

    logger.info(f"[PLAN] 拆解任务 (复杂度={complexity.value})")

    if complexity == Complexity.SIMPLE:
        affected_files = state.get("affected_files") or []
        _proj_path = _get_project_path(state.get("project_id") or "")
        task_plan = _build_simple_plan(task_description, affected_files, project_path=_proj_path)
        logger.info(
            "[PLAN] SIMPLE 快速路径 — 1 个 trivial 子任务 (scope=%d 文件)",
            len(affected_files),
        )
        # D51：不再把 shared_contract enrich 进每个子任务（plan 体积病灶——N 份 ~42K 内联
        # 副本随每次 checkpoint 序列化）。完整契约在派发面 build_worker_prompt 合成，
        # worker 可见契约与旧行为逐字节一致。
        # 测试剔除（同主路径，task 744316e7）：SIMPLE 路径也防 Brain 塞测试
        from swarm.brain.nodes.shared import _strip_unrequested_tests
        task_plan = _strip_unrequested_tests(task_plan, task_description)
        plan_touch = touch_context(
            state,
            "plan",
            f"生成 {len(task_plan.subtasks)} 个子任务（SIMPLE 快速路径）",
        )
        return {
            "plan": task_plan,
            "shared_contract": task_plan.shared_contract or {},
            # R2-1：PLAN=新一轮规划起点，无条件清历史 escalate 粘滞（与 merge 干净轮对称；
            # 堵"首次 REVISE→PLAN 无 old_results 时 _surgical_replan_reset 返回空"的漏清线）
            "failure_escalated": False,
            # round29 真因4 always-emit（复核 LOW）：SIMPLE 路径不走分批，恒发 []，保不变量字面自洽。
            "plan_batch_failed_modules": [],
            # R31-1 T1 always-emit：SIMPLE 单 trivial 子任务自证覆盖（覆盖校验早退），无申报面。
            "baseline_covered": [],
            # R32-1 U2 always-emit：SIMPLE 不走分批，恒 {}
            "plan_batch_cache": {},
            **_surgical_replan_reset(_replan_old_results, _replan_old_plan, task_plan,
                                 old_recovery_counts=state.get("targeted_recovery_counts"),
                                 old_retry_counts=state.get("subtask_retry_counts"),
                                 old_redecompose_counts=state.get("subtask_redecompose_count"),
                                 old_abandoned_ids=state.get("abandoned_subtask_ids"),
                                 old_give_up_ids=state.get("give_up_isolated_ids")),
            **plan_touch,
        }

    from swarm.knowledge.service import format_brain_knowledge_prompt
    from swarm.memory.task_digest import format_recent_tasks_for_brain

    knowledge_prompt = format_brain_knowledge_prompt(
        knowledge_context, task_description
    )
    recent_tasks_prompt = format_recent_tasks_for_brain(
        state.get("recent_task_summaries") or []
    )
    _plan_degraded: str | None = None  # LLM 降级原因（audit #13），非降级保持 None
    # round29 真因4：分批拆解失败模块清单。always-emit（非分批/全成功路径发 []）——last-write-wins
    # 使 replan 成功后自动清空，不粘滞（「仅条件写无人清」是历史 bug 模式）。
    _plan_batch_failed: list[dict] = []
    # R31-1 T1：本轮 baseline_covered 申报（独立 state 键 always-emit——LLM 未申报/降级
    # 兜底路径恒 []，last-write-wins 刷掉上一轮申报防跨重试粘滞）
    _baseline_covered: list[dict] = []
    # R32-1 U2：本轮成功批缓存（非分批路径恒 {}——last-write-wins 覆写防陈旧）
    _plan_batch_cache: dict = {}
    sliding_ctx = sliding_context_prompt(state)

    # P0-2：replan 重入时把上轮失败原因拼进上下文，引导 LLM 避开同样的坏计划
    # （见 task 0f93f1fc：replan 后 LLM 看不到"依赖悬空/scope 冲突"原因 → 原样重生成）。
    _replan_feedback = (state.get("replan_feedback") or "").strip()
    if _replan_feedback:
        sliding_ctx = (
            f"⚠️ 上一轮规划执行失败，本次为重新规划（第 {state.get('replan_count', 1)} 次）。\n"
            f"上轮失败根因（务必规避，不要重复同样的拆分/依赖/scope 错误）：\n"
            f"{_replan_feedback}\n"
            # 复核 M-1：replan 分支与校验重试分支对称注入上一版摘要——否则 replan LLM
            # 看不到"上一版已通过的 covers/baseline 申报"，always-emit 会用漏申报的新
            # 输出覆写 state，覆盖闸门重新失败白烧 D09 重试（最坏复现 round31 式烧光）。
            + _previous_plan_repair_block(
                state.get("plan"), state.get("baseline_covered"))
            + "\n"
            + (sliding_ctx or "")
        )
        logger.info("[PLAN] replan 重入 — 已注入上轮失败原因+上一版计划摘要供 LLM 规避")

    # D09：VALIDATE_PLAN 失败原因回灌——after_validate 失败→increment_retry→plan 是重试循环，
    # 上轮校验（结构/P6b 完整性）为何被否绝不能对 LLM 隐藏，否则盲重生成同样坏计划烧光重试预算。
    _validation_feedback = (state.get("plan_validation_feedback") or "").strip()
    if _validation_feedback:
        sliding_ctx = (
            f"⚠️ 上一轮生成的执行计划【校验未通过】（第 {state.get('plan_retry_count', 1)} 次重试）。\n"
            f"校验失败的具体问题（本次务必逐条修正，不要重复同样的结构/依赖/缺功能错误）：\n"
            f"{_validation_feedback}\n"
            # R31-3 T3：上一版摘要 + 增量修补纪律（治全量重拆掷骰子不收敛）
            + _previous_plan_repair_block(
                state.get("plan"), state.get("baseline_covered"))
            + "\n"
            + (sliding_ctx or "")
        )
        logger.info("[PLAN] 校验失败重试 — 已注入上轮校验 issues 供 LLM 修正")

    # ── LLM 任务拆解 ──
    try:
        llm = _get_brain_llm()
        router = ModelRouter()
        routing_table = router.get_routing_table()
        # 需求转化层产出注入：把 tech_design 的 file_plan/数据模型/契约喂给 PLAN，
        # PLAN 据此定 scope（不再从零猜文件）。空则提示回退自推导。
        tech_design_plan = _format_tech_design_for_plan(state)

        # ── ultra 超大需求分批拆解（DESIGN_plan_batch_decompose）──
        # tech_design 产出 file_plan 上百文件时，单次 LLM 拆全量 DAG 会卡死（stream chunk 不超时 +
        # 超长 JSON 极慢）。改：按 10% 比例分批，逐批 LLM 拆解，每批规模可控 + 进度日志。
        _file_plan = state.get("tech_design_file_plan") or []
        _BATCH_TRIGGER = 30  # file_plan 超过此数才分批（中小需求单次最优，零回归）
        # Q5 判定点（第二阶段）：超大到一定程度应切成串行主任务（主任务间依赖，A 合格才走 B）。
        # 本批先留判定与告警，完整串行主任务编排是后续迭代（见 DESIGN_plan_batch_decompose 七）。
        _SERIAL_MASTER_TRIGGER = 200
        if len(_file_plan) > _SERIAL_MASTER_TRIGGER:
            logger.warning(
                "[PLAN] file_plan=%d 文件超过串行主任务阈值(%d)：建议切分为多个串行主任务"
                "（A 产出合格→B），当前仍用单任务 10%% 分批拆解兜底。Q5 串行主任务编排待第二阶段实现。",
                len(_file_plan), _SERIAL_MASTER_TRIGGER,
            )
        if complexity == Complexity.ULTRA and len(_file_plan) > _BATCH_TRIGGER:
            (task_plan, _plan_batch_failed, _baseline_covered,
             _plan_batch_cache) = await _plan_ultra_batched(
                llm, state, task_description, knowledge_context,
                sliding_ctx, _file_plan,
            )
        else:
            prompt_user = PLAN_USER.format(
                task_description=task_description,
                complexity=complexity.value,
                routing_table=json.dumps(routing_table, ensure_ascii=False, indent=2),
                project_structure=_format_project_structure(knowledge_context),
                knowledge_context=knowledge_prompt,
                user_profile=_brain_profile_prompt(state),
                recent_tasks=recent_tasks_prompt,
                sliding_context=sliding_ctx,
                tech_design_plan=tech_design_plan,
            )
            # S2-3：需求条目清单 + covers 声明纪律（加法式注入；items 空=一字不加，老行为零变化）
            prompt_user += _requirement_coverage_prompt_block(state.get("requirement_items"))
            response = await llm.ainvoke([
                {"role": "system", "content": PLAN_SYSTEM},
                {"role": "user", "content": prompt_user},
            ])
            result = _parse_json_from_llm(response.content)
            # 健壮性(task 88d69519)：LLM 可能输出 "harness": null / "model_preference" 等可选字段为
            # null。SubTask.harness 类型是 TaskHarness（非 Optional，靠 default_factory），显式传
            # None 会触发 pydantic validation error → 整个 plan 解析失败 → 降级空 scope 兜底。
            # 这里剔除值为 None 的可选字段，让 default_factory 生效。
            if isinstance(result, dict):
                for _st in result.get("subtasks", []) or []:
                    if isinstance(_st, dict):
                        for _opt in ("harness", "contract"):
                            if _opt in _st and _st[_opt] is None:
                                _st.pop(_opt)
                # TD2606-B17：create-signature 去重（dedupe_subtasks）此前只在批量 ultra 路径
                # （merge_subtask_batches）跑。单发 plan 路径同样可能 LLM 吐重复脚手架子任务
                # （RUN6 根因类）→ 在此对单发路径也做去重，使去重成为全路径不变量。
                from swarm.brain.plan_batch import dedupe_subtasks, prune_parallel_groups
                result["subtasks"] = dedupe_subtasks(result.get("subtasks", []) or [])
                # D10：去重删子任务后同步 parallel_groups（否则悬空引用 → plan_validator 硬失败
                # → 叠加 D09 盲重试死循环）。valid_ids = 去重后存活子任务 id。
                if result.get("parallel_groups"):
                    _valid_ids = {st.get("id") for st in result["subtasks"] if isinstance(st, dict)}
                    result["parallel_groups"] = prune_parallel_groups(
                        result.get("parallel_groups"), _valid_ids)
                # R31-1 T1：顶层 baseline_covered 摘出走独立 state 键（绝不进 TaskPlan——
                # 变异重构造路径 merge/resplit/revision 天然碰不到，结构性防丢字段）
                from swarm.brain.plan_validator import normalize_baseline_covered
                _baseline_covered = normalize_baseline_covered(
                    result.pop("baseline_covered", None))
            task_plan = TaskPlan(**result)
    except json.JSONDecodeError as e:
        logger.error(f"[PLAN] LLM 输出 JSON 解析失败，使用空 scope 兜底 plan（Worker 可能失败）: {e}")
        _plan_degraded = f"plan LLM 输出解析失败，产出空 scope 兜底计划（Worker 大概率失败，需人工关注）（{e}）"
        task_plan = TaskPlan(
            subtasks=[
                SubTask(
                    id="st-1",
                    description=task_description,
                    difficulty=SubTaskDifficulty.MEDIUM,
                    modality=SubTaskModality.TEXT,
                    scope=FileScope(writable=[], readable=[]),
                    contract={"input": "原始需求", "output": "实现代码"},
                    acceptance_criteria=["代码编译通过", "基本功能验证"],
                    depends_on=[],
                    model_preference=None,
                )
            ],
            parallel_groups=[["st-1"]],
        )
    except Exception as e:
        logger.error(f"[PLAN] LLM 调用失败: {e}")
        _plan_degraded = f"plan LLM 调用失败，产出最简空验证兜底计划（Worker 大概率失败，需人工关注）（{e}）"
        # 创建最简单的回退计划
        task_plan = TaskPlan(
            subtasks=[
                SubTask(
                    id="st-1",
                    description=state.get("task_description", "未知任务"),
                    difficulty=SubTaskDifficulty.MEDIUM,
                    modality=SubTaskModality.TEXT,
                    scope=FileScope(),
                    contract={},
                    acceptance_criteria=["无验证"],
                    depends_on=[],
                )
            ],
            parallel_groups=[["st-1"]],
        )

    logger.info(f"[PLAN] 生成 {len(task_plan.subtasks)} 个子任务")

    # ── 垂直切片守卫（确定性硬兜底，方向A）──
    # PLAN prompt 已软引导"按垂直功能切片、同语言不按文件/层拆"，但 LLM 是软约束，可能仍
    # 把同语言无依赖的功能水平切成多个子任务（task 5c17c464/94334785 实证：两文件拆两子任务）。
    # 这里在代码层硬合并：同沙箱语言 + 无相互依赖 + 同 modality 的多个子任务 → 合并成 1 个，
    # 消除水平切分带来的子任务依赖/MERGE 冲突/失败面放大。详见 _merge_horizontal_subtasks。
    from swarm.brain.nodes.shared import _merge_horizontal_subtasks
    task_plan = _merge_horizontal_subtasks(task_plan)

    # T1：把 contract_design 节点产出的全局共享契约(state.shared_contract_draft)注入 plan。
    # D51：不再 enrich 进每个子任务 contract（N 份 ~42K 内联副本 = plan/checkpoint 体积病灶）；
    # worker 可见的完整契约由派发面 build_worker_prompt 以同一 merge 语义（shared 打底 +
    # 子任务覆盖）现场合成，行为等价。
    _contract = state.get("shared_contract_draft") or {}
    if _contract and not (task_plan.shared_contract or {}):
        task_plan.shared_contract = _contract
    elif _contract and isinstance(task_plan.shared_contract, dict):
        # PLAN LLM 自带了 shared_contract（无 dependencies）会盖掉 contract_design 的草案。
        # dependencies 是编译期硬契约（Rule5 据此把模块依赖并集落进 pom owner 验收），
        # 绝不能被丢——草案有、plan 自身没有时补进去（其余键以 plan 自身为准，不动）。
        if _contract.get("dependencies") and not task_plan.shared_contract.get("dependencies"):
            task_plan.shared_contract["dependencies"] = _contract["dependencies"]

    # T3：同文件写权唯一——消除"同一文件被多个子任务并发写"的冲突（写权保留首个，
    # 其余降级为 readable）+ 被依赖产物自动入域。防多 worker 同时编辑同一文件互相覆盖。
    from swarm.brain.contract_utils import normalize_plan_scopes
    # 复核 L-1：与 elaborate 同源传 project_path+钉扎 base，aggregate-vs-新建撞车判定不读实时 HEAD。
    if normalize_plan_scopes(task_plan, project_path=_get_project_path(state.get("project_id") or ""),
                             base_ref=state.get("base_commit")):
        logger.info("[PLAN] T3 scope 归一：消除同文件并发写冲突（写权唯一化 + 依赖产物入域）")

    # harness 兜底：LLM 未给出 harness 的子任务，按语言推断一个，确保 Worker 有
    # 项目特定的构建/测试命令 + 命令白名单可用（否则又退化成"口头自报通过"）。
    for st in task_plan.subtasks:
        h = getattr(st, "harness", None)
        if h is None or not (h.build_command or h.test_command or h.verify_commands or h.extra_whitelist):
            st.harness = _infer_harness(st.description or task_description, st.scope)
        # intent 兜底：LLM 未显式给出(默认 MODIFY) 时按描述启发式推断，
        # 让 AUDIT/DEBUG/REFACTOR 等差异化意图也能在 LLM 漏标时被识别。
        if st.intent == TaskIntent.MODIFY:
            inferred = _infer_intent(st.description or task_description)
            if inferred != TaskIntent.MODIFY:
                st.intent = inferred
        # scope 过度圈定守卫：弱规划模型常把整个模块塞进 writable(实测 RuoYi 暴露:
        # "加一个方法"却圈了 88 个文件)。超阈值时告警 + 审计，便于排查"diff 巨大且脏"
        # 的根因。不自动裁剪(可能误删真需要的文件)，但把信号显式暴露出来。
        _writable = list(getattr(st.scope, "writable", []) or [])
        if len(_writable) > _SCOPE_WRITABLE_WARN_THRESHOLD:
            logger.warning(
                "[PLAN] 子任务 %s scope 过度圈定: writable=%d 个文件(阈值 %d)，"
                "可能导致上传/拉回大量无关文件、diff 巨大。建议规划时只圈真正改动的文件。",
                getattr(st, "id", "?"), len(_writable), _SCOPE_WRITABLE_WARN_THRESHOLD,
            )
        # est_context_tokens 兜底(Q7 上下文预算)：LLM 未估时按难度+scope 文件数启发式估算，
        # 让 elaborate 的二次拆分有真实信号(否则字段恒 0，预算检测形同虚设)。
        if not getattr(st, "est_context_tokens", 0):
            _diff = getattr(st, "difficulty", SubTaskDifficulty.MEDIUM)
            _base = {SubTaskDifficulty.TRIVIAL: 8000, SubTaskDifficulty.MEDIUM: 50000,
                     SubTaskDifficulty.COMPLEX: 120000}.get(_diff, 50000)
            # 每个 writable 文件按 ~6k token 估(读+改)，叠加难度基线
            st.est_context_tokens = _base + len(_writable) * 6000

    # ── 测试剔除（task 744316e7 根因·单一事实源）──
    # 此处 scope + harness 都已齐备。任务未明确要求测试时，统一剔除 scope 里的测试文件
    # + 清空 harness.test_command，杜绝"Brain 擅自塞测试 → 测试用 junit 但项目无依赖 →
    # 测试类编译失败 → mvn compile 过了却被 L1 判死 + worker 修 junit 绕圈"病根链。
    from swarm.brain.nodes.shared import _strip_unrequested_tests
    task_plan = _strip_unrequested_tests(task_plan, task_description)

    # R34-8 确定性无害化：申报与 covers 重叠的条目丢申报保 covers——分批 LLM 会把
    # "本计划其他批实现"误当"存量已满足"申报（round34 实证 31 条批间推卸型申报）。
    # 兄弟批真用 covers 声明了的重叠项无害化；仅申报无 covers 的（真基线或真漏洞）
    # 留给验收断言+人工闸裁决。放在全部 plan 变异（merge/strip）之后保对账一致。
    if _baseline_covered:
        _covered_ids = {rid for st in task_plan.subtasks
                        for rid in (getattr(st, "covers", None) or [])}
        _overlap = [e for e in _baseline_covered if e.get("id") in _covered_ids]
        if _overlap:
            logger.info(
                "[PLAN] baseline 申报与子任务 covers 重叠 %d 条 → 丢申报保 covers"
                "（R34-8 批间推卸无害化）: %s",
                len(_overlap), ",".join(e.get("id", "?") for e in _overlap[:10]))
            _baseline_covered = [
                e for e in _baseline_covered if e.get("id") not in _covered_ids]

    plan_touch = touch_context(
        state,
        "plan",
        f"生成 {len(task_plan.subtasks)} 个子任务",
    )
    return {
        "plan": task_plan,
        "shared_contract": task_plan.shared_contract or {},
        "degraded_reasons": list(state.get("degraded_reasons") or []) + (
            [_plan_degraded] if _plan_degraded else []
        ) + (
            # round29 真因4：丢模块=交付范围残缺，必须进 degraded（should_write_success 据此
            # 拦 L6 假成功学习；人工 accept 放行后终态仍诚实带痕）。reducer 追加去重。
            [f"plan_batch_module_dropped:{','.join(m.get('name', '?') for m in _plan_batch_failed)}"]
            if _plan_batch_failed else []
        ),
        # round29 真因4：always-emit（空也发）——防粘滞 + 供 can_auto_accept_plan 闸门消费。
        "plan_batch_failed_modules": _plan_batch_failed,
        # R31-1 T1 always-emit：本轮申报（LLM 未申报/降级兜底=[]），validate_plan 覆盖校验消费
        "baseline_covered": _baseline_covered,
        # R32-1 U2 always-emit：本轮成功批缓存（补齐型重试复用；非分批/降级路径恒 {}）。
        # 复核 F-4：批全成时缓存按自身规则永远无人消费——落 {} 不把数十 KB 死重灌进
        # 每次 checkpoint（D51 plan 体积病灶同族）。
        "plan_batch_cache": _plan_batch_cache if _plan_batch_failed else {},
        # TD2606-A5：规划 LLM 失败时上面产出的是空 scope「无验证」兜底假计划。打专用标记，
        # 让 can_auto_accept_plan fail-fast 拦下，绝不让它静默 dispatch → 空 diff → 假 DONE。
        # （_plan_degraded 仅在两条 except 失败分支被赋值，故等价于"规划生成失败"。）
        "plan_generation_failed": _plan_degraded is not None,
        # R2-1：同 SIMPLE 路径——PLAN 起点无条件清历史 escalate 粘滞
        "failure_escalated": False,
        **_surgical_replan_reset(_replan_old_results, _replan_old_plan, task_plan,
                                 old_recovery_counts=state.get("targeted_recovery_counts"),
                                 old_retry_counts=state.get("subtask_retry_counts"),
                                 old_redecompose_counts=state.get("subtask_redecompose_count"),
                                 old_abandoned_ids=state.get("abandoned_subtask_ids"),
                                 old_give_up_ids=state.get("give_up_isolated_ids")),
        **plan_touch,
    }


_COMPLETENESS_MISSING_MARKERS = (
    "缺失", "缺核心", "缺少", "未覆盖", "missing", "incomplete",
)
# 描述质量类问题（截断/措辞/指引不全）——绝不触发徒劳全量重拆：根因在 ELABORATE 拆分逻辑，
# 重拆后仍会再截断；ultra 项目全量重拆=11 模块 TECH_DESIGN/CONTRACT/PLAN-BATCH 重跑，成本极高。
_COMPLETENESS_DESC_QUALITY_MARKERS = (
    "截断", "描述", "指引", "措辞", "模糊", "不清", "表述", "truncat", "description", "wording",
)


def _filter_completeness_missing(llm_issues: list) -> list:
    """从 LLM 计划校验 issues 中筛出【缺功能子任务】(结构完整性缺陷,该触发补齐重规划)。

    治本(ELABORATE 截断 → P6b 误判重拆)：命中 missing 关键词但同时带【描述质量】标记的 issue
    （如"描述截断…缺少完整实现指引"）按描述质量放过——它不是少了功能子任务，顶多本地补描述，
    绝不该触发徒劳的全量重拆。只有【真缺功能/缺文件/缺表 DDL】才进补齐。
    """
    return [
        s for s in (llm_issues or [])
        if any(k in str(s) for k in _COMPLETENESS_MISSING_MARKERS)
        and not any(k in str(s) for k in _COMPLETENESS_DESC_QUALITY_MARKERS)
    ]


def _confirm_coverage_summary(state: BrainState) -> dict:
    """hunter F5：PLAN 人工闸的覆盖对账摘要（现算派生，承诺不抛——闸 payload 增强面，
    矩阵算失败绝不挡人工闸本体）。"""
    try:
        from swarm.brain.plan_validator import build_coverage_matrix
        matrix = build_coverage_matrix(
            state.get("plan"), state.get("requirement_items"),
            state.get("baseline_covered"))
        return {
            "total": matrix["total_items"],
            "covered": matrix["covered_items"],
            "uncovered": [
                {"id": u.get("id"), "text": str(u.get("text") or "")[:120]}
                for u in matrix["uncovered"][:30]
            ],
            "baseline_covered_count": len(matrix["baseline_covered"]),
        }
    except Exception as exc:  # noqa: BLE001
        logger.warning("[CONFIRM] 覆盖摘要现算失败(payload 降级为空): %s", exc)
        return {}


def _format_validation_feedback(issues: list) -> str:
    """D09：把校验 issues 列表压成回灌 PLAN 的简明反馈文本（逐条 bullet，去空/去重保序）。

    空 issues → 空串（PLAN 侧据此不注入，不产生"莫名警告"噪声）。"""
    seen: set[str] = set()
    lines: list[str] = []
    for it in (issues or []):
        s = str(it).strip()
        if s and s not in seen:
            seen.add(s)
            lines.append(f"- {s}")
    out = "\n".join(lines)
    if len(out) > 8000:
        # 复核 L-5/hunter F3：feedback 无界会随条目数放大（60 条 ≈15K）并联动
        # 下一轮 prompt/分批 sliding_ctx 膨胀——定界并自述截断（不静默）
        kept = out[:8000].rsplit("\n", 1)[0]
        dropped = len(lines) - kept.count("\n") - 1
        out = kept + f"\n- （反馈已截断，另有 {dropped} 条同类问题未列出）"
    return out


async def validate_plan(state: BrainState) -> dict:
    """VALIDATE_PLAN 节点 — PlanValidator 硬校验 + 可选 LLM 补充

    输入: plan, task_description, affected_files
    输出: plan_valid, plan_validation_issues, plan_validation_feedback（失败原因回灌 PLAN，D09）
    """
    from swarm.brain.plan_validator import (
        MAX_LLM_VALIDATION_PLAN_CHARS,
        slim_plan_json_for_llm_validation,
        validate_plan_structure,
        validate_requirement_coverage,
    )

    plan_obj = state.get("plan")
    task_description = state.get("task_description", "")
    retry_count = state.get("plan_retry_count", 0)
    affected_files = state.get("affected_files") or []

    logger.info(f"[VALIDATE_PLAN] 验证计划 (重试次数={retry_count})")

    if plan_obj is None:
        return {
            "plan_valid": False,
            "plan_retry_count": retry_count,
            "plan_validation_issues": ["计划为空"],
            "plan_validation_feedback": "- 计划为空（PLAN 未产出任何子任务，请重新生成完整的子任务 DAG）",
        }

    struct_result = validate_plan_structure(
        plan_obj,
        affected_files=affected_files if affected_files else None,
    )
    for w in struct_result.warnings:
        logger.info("[VALIDATE_PLAN] 警告: %s", w)

    if not struct_result.valid:
        logger.warning(
            "[VALIDATE_PLAN] 结构校验未通过: %s",
            "; ".join(struct_result.issues),
        )
        return {
            "plan_valid": False,
            "plan_retry_count": retry_count,
            "plan_validation_issues": struct_result.issues,
            # D09：结构校验失败原因回灌 PLAN（悬空依赖/环/parallel_groups 悬空引用等）供重试修正
            "plan_validation_feedback": _format_validation_feedback(struct_result.issues),
        }

    if effective_complexity(state) == Complexity.SIMPLE:  # 修复 12.3：澄清后定级优先
        logger.info("[VALIDATE_PLAN] SIMPLE 快速路径 — 结构验证通过")
        return {
            "plan_valid": True,
            "plan_retry_count": retry_count,
            "plan_validation_issues": [],
            "plan_validation_feedback": "",  # 通过即清空，防跨轮粘滞
        }

    # ── S2-3 PRD 覆盖矩阵（确定性维度，ACCEPTANCE_DESIGN 定案3/§2.5，task#24）──
    # 接缝：结构校验后、SIMPLE 早退后（单 trivial 子任务自证覆盖，强校验只会误伤）、
    # LLM 软校验前。requirement_items 缺失/空（抽取降级/老 checkpoint）→ 跳过校验 +
    # degraded 留痕（诚实降级，绝不阻塞主链）；未覆盖条目/悬空 covers → plan_valid=False
    # 走现成 D09 回灌通道（feedback 逐条列条目 id+text，PLAN 重试 :864-872 注入 LLM）。
    # 熔断复用 plan_retry_count/MAX_PLAN_RETRY（graph.after_validate），绝不另起计数器；
    # 覆盖失败即返回、不再跑 LLM 软校验（§2.3：与 P6b 共用重试预算，不各自烧一轮）。
    _coverage_degraded: list[str] = []
    _req_items = state.get("requirement_items") or []
    # S2 复核 S2：覆盖闸门杀开关（对照 SWARM_RUNTIME_SMOKE_ENABLED 先例）——covers 是
    # 新上线的确定性硬闸，存量任务/抽取噪声导致 LLM 反复不服从时，运维需要一个不改代码
    # 的泄压阀（否则一条坏条目烧光 MAX_PLAN_RETRY 必进人工）。默认 "1"=闸门全开；
    # 关闭走跳过+degraded 留痕，绝不静默。
    _coverage_gate_on = os.environ.get(
        "SWARM_PLAN_COVERAGE_GATE", "1").strip().lower() not in ("0", "false", "no", "off")
    if not _coverage_gate_on:
        logger.info("[VALIDATE_PLAN] SWARM_PLAN_COVERAGE_GATE 关闭 — 跳过覆盖矩阵校验（degraded 留痕）")
        _coverage_degraded = ["plan_coverage:skipped(disabled)"]
    elif not _req_items:
        logger.info("[VALIDATE_PLAN] requirement_items 缺失/空 — 跳过覆盖矩阵校验（degraded 留痕）")
        _coverage_degraded = ["plan_coverage:skipped(no_requirement_items)"]
    else:
        cov_result = validate_requirement_coverage(
            plan_obj, _req_items, state.get("baseline_covered"))
        for w in cov_result.warnings:
            logger.info("[VALIDATE_PLAN] 覆盖矩阵警告: %s", w)
        if not cov_result.valid:
            logger.warning(
                "[VALIDATE_PLAN] 覆盖矩阵校验未通过（%d 条 issue）: %s",
                len(cov_result.issues), "; ".join(cov_result.issues),
            )
            # R34-3 观测面：跨 attempt 覆盖集增量（round34 实证 18→12→18 震荡无人可见）
            _prev_ids = set(re.findall(
                r"req-[0-9a-f]{8}",
                " ".join(str(i) for i in (state.get("plan_validation_issues") or []))))
            _cur_ids = set(re.findall(
                r"req-[0-9a-f]{8}", " ".join(cov_result.issues)))
            if _prev_ids and _prev_ids != _cur_ids:
                logger.info(
                    "[VALIDATE_PLAN] 覆盖增量：本轮新增未覆盖 %s；较上轮已修复 %s",
                    sorted(_cur_ids - _prev_ids) or "无",
                    sorted(_prev_ids - _cur_ids) or "无")
            return {
                "plan_valid": False,
                "plan_retry_count": retry_count,
                "plan_validation_issues": cov_result.issues,
                # D09：未覆盖条目 id+text / 悬空 covers 清单回灌 PLAN 重规划
                "plan_validation_feedback": _format_validation_feedback(cov_result.issues),
            }
        _bl_n = len([e for e in (state.get("baseline_covered") or [])
                     if isinstance(e, dict) and str(e.get("reason") or "").strip()])
        logger.info(
            "[VALIDATE_PLAN] 覆盖矩阵校验通过：%d 个需求条目全部被覆盖"
            "（含 baseline_covered 申报 %d 条）", len(_req_items), _bl_n,
        )

    # ── LLM 计划验证（结构已通过后的【软建议】，不阻断）──
    # Bug-2 根治（task 92ff8a71/70543ea2/37460a5b 实证）：过去 llm_valid =
    # result.get("valid", False) 是 fail-closed —— LLM 没明确返回 valid:true 就否决，
    # 叠加 GLM-5.1 流式超时返回截断/畸形 JSON（无 valid 键）→ 反复否决 → 耗尽 3 重试
    # → 主流程卡死在 PLAN，根本走不到 DISPATCH。而异常路径反而 fail-open，策略自相矛盾。
    #
    # 新策略：【结构校验通过即放行】。LLM 验证仅作软建议——收集 issues/suggestions
    # 记日志供观测，绝不阻断流程。结构校验（validate_plan_structure）已硬保证 DAG/
    # scope/依赖可执行性，这是确定性闸门；LLM 的"质量"判断是主观软信号，不该一票否决。
    # SWARM_VALIDATE_PLAN_LLM_GATE=true 可恢复旧的硬否决行为（默认 false=软建议）。
    llm_gate_hard = os.environ.get(
        "SWARM_VALIDATE_PLAN_LLM_GATE", "false"
    ).lower() in ("true", "1", "yes")
    llm_valid = True
    llm_issues: list[str] = []
    try:
        # P16-2 治本：喂给软校验 LLM 的是【瘦身 plan_json】（剥离每子任务约 42K 的 contract
        # 副本 + 注入代码）。原 model_dump_json 达 ~1MB（~260K token），把推理模型 GLM-5.2
        # 拖进 84K chunk / 25min reasoning runaway（撞 1500s wall-clock 上限才放行，且结果软
        # 建议被丢弃）→ 卡在到 DISPATCH 之前。结构确定性闸门已保证 DAG/scope/依赖，软校验无需
        # 内联 contract 副本（契约完整性由 plan 级 shared_contract 一次性体现）。
        plan_json = slim_plan_json_for_llm_validation(plan_obj)
        if len(plan_json) > MAX_LLM_VALIDATION_PLAN_CHARS:
            # 瘦身后仍超上限（异常巨 plan）→ 跳过 LLM 软建议：结构确定性闸门已放行，绝不把
            # 超大 prompt 喂推理模型再次 wall-clock runaway。default 放行（软信号缺失=不阻断）。
            logger.info(
                "[VALIDATE_PLAN] 瘦身后 plan_json %d 字符 > %d 上限 → 跳过 LLM 软建议（结构已通过放行）",
                len(plan_json), MAX_LLM_VALIDATION_PLAN_CHARS,
            )
            result = {"valid": True, "issues": []}
        else:
            llm = _get_brain_llm()
            prompt_user = VALIDATE_PLAN_USER.format(
                task_description=task_description,
                plan_json=plan_json,
                user_profile=_brain_profile_prompt(state),
            )
            response = await llm.ainvoke([
                {"role": "system", "content": VALIDATE_PLAN_SYSTEM},
                {"role": "user", "content": prompt_user},
            ])
            result = _parse_json_from_llm(response.content)
        llm_says_valid = bool(result.get("valid", False))
        llm_issues = list(result.get("issues", []) or [])
        if not llm_says_valid:
            if llm_gate_hard:
                llm_valid = False
            else:
                # 软建议：记录 LLM 的顾虑但放行（结构已通过）
                logger.info(
                    "[VALIDATE_PLAN] LLM 软建议（不阻断）：valid=false, issues=%s",
                    llm_issues or "(未给出具体问题)",
                )
        # P6b（治本，996db614 实测 VALIDATE_PLAN 报"缺核心功能子任务"却软放行→交付缺核心引擎）：
        # 「缺子任务/未覆盖核心功能」是【结构完整性】缺陷（非主观顾虑），区别于一般软建议——
        # 在【小预算】内触发一次重规划补齐（与 P6a plan-batch 重试组合：重规划时失败模块批被重试恢复），
        # 耗尽预算才放行（不无限阻断自动流）。env SWARM_VALIDATE_PLAN_COMPLETENESS_GATE=false 可关。
        _completeness_on = os.environ.get(
            "SWARM_VALIDATE_PLAN_COMPLETENESS_GATE", "true"
        ).lower() not in ("false", "0", "no")
        _completeness_budget = int(os.environ.get("SWARM_PLAN_COMPLETENESS_RETRIES", "1") or "1")
        if _completeness_on and llm_valid and llm_issues and retry_count < _completeness_budget:
            _missing = _filter_completeness_missing(llm_issues)
            if _missing:
                llm_valid = False
                logger.warning(
                    "[VALIDATE_PLAN] 检出【缺核心功能子任务】(结构完整性缺陷,非软建议)→触发重规划"
                    "补齐(完整性重试 %d/%d): %s", retry_count + 1, _completeness_budget, _missing,
                )
    except json.JSONDecodeError as e:
        logger.warning(f"[VALIDATE_PLAN] LLM JSON 解析失败，结构已通过则放行: {e}")
        llm_valid = True
    except Exception as e:
        logger.warning(f"[VALIDATE_PLAN] LLM 验证异常，结构已通过则放行: {e}")
        llm_valid = True

    plan_valid = llm_valid
    logger.info(
        f"[VALIDATE_PLAN] 结果: {'通过' if plan_valid else '未通过'} "
        f"(LLM门={'硬否决' if llm_gate_hard else '软建议'})"
    )
    _final_issues = [] if plan_valid else (llm_issues or ["LLM 计划验证未通过"])
    return {
        "plan_valid": plan_valid,
        "plan_retry_count": retry_count,
        "plan_validation_issues": _final_issues,
        # D09：LLM/P6b 完整性校验失败原因回灌 PLAN（通过则清空，防跨轮粘滞）
        "plan_validation_feedback": "" if plan_valid else _format_validation_feedback(_final_issues),
        # S2-3：覆盖矩阵因 items 缺失被跳过时诚实留痕（degraded_reasons 是 reducer 键，
        # 追加去重；无跳过时不发键，零噪声）。
        **({"degraded_reasons": _coverage_degraded} if _coverage_degraded else {}),
    }


def confirm_plan(state: BrainState) -> dict:
    """CONFIRM 节点 — 人工确认点（ultra 复杂度 / 计划校验失败 / 显式人工确认）

    使用 langgraph.types.interrupt 实现挂起等待人工输入。
    输入: plan, task_description, complexity, plan_valid
    输出: human_decision

    auto_accept 模式语义（P0-3 修复）：
    - plan_valid=True  → 自动接受（原行为，纯自动 API 场景顺畅放行）。
    - plan_valid=False → **不得自动接受非法计划**（task 0f93f1fc：auto_accept 把
      校验失败 4 次的计划直接放行，送进 dispatch 后 scope 冲突 + 悬空依赖必败）。
      按产品决策(Q2)：有人工监听 → interrupt 等人工出选项/输入框；纯自动无监听
      (auto_accept) → 降级 fail-fast(REJECT)，给出清晰原因，而非蒙混放行。
    """
    # P2-2 修复：进入 confirm 有三种原因，文案/日志按 reason 区分，
    # 不再无条件打印"ultra 复杂度"（误导：medium 校验失败也会进这里）。
    # 用 effective_complexity（已归一枚举）：resume 后 state["complexity"] 是字符串，
    # 直接 == Complexity.ULTRA 会静默 False、把 ultra 闸门误标成普通校验失败。
    _complexity = effective_complexity(state)
    # 与 can_auto_accept_plan / after_validate 一致缺省 False（缺标记=按未校验处理，
    # 文案归到 validation_failed 而非误标 ultra/manual）。实际到此 plan_valid 必已设置。
    _plan_valid = state.get("plan_valid", False)
    if not _plan_valid:
        _reason = "validation_failed"
        _msg = "此任务的执行计划多次自动校验未通过，需人工审核后决定是否继续。"
    elif _complexity == Complexity.ULTRA:
        _reason = "ultra"
        _msg = "此任务为架构级变更（ultra），请审核执行计划并决定是否继续。"
    else:
        _reason = "manual_confirm"
        _msg = "此任务需人工确认执行计划，请审核后决定是否继续。"

    logger.info("[CONFIRM] 等待人工确认 (reason=%s)", _reason)

    auto_accept = state.get("auto_accept", False) or os.environ.get("SWARM_AUTO_ACCEPT", "").lower() in ("1", "true", "yes")

    if auto_accept:
        # P0-3 闸门：auto_accept 只对合法计划生效。非法计划纯自动场景 fail-fast。
        # 放行判据收敛在 brain.gates 单一事实源（与 DELIVER 同构，杜绝"修一个漏一个"）。
        from swarm.brain.gates import can_auto_accept_plan

        allow, reason = can_auto_accept_plan(state)
        if not allow:
            logger.warning(
                "[CONFIRM] auto_accept 模式拒绝放行（fail-fast）：%s", reason,
            )
            # W1.1：tech_design 有失败模块时，auto_accept 不得静默成功——
            # 升级人工(failure_escalated)，与"计划非法"一样走 fail-fast，但归因区分。
            if reason.startswith("tech_design_incomplete"):
                _vf = "tech_design_incomplete"
            elif reason.startswith("plan_generation_failed"):
                _vf = "plan_generation_failed"  # TD2606-A5
            elif reason.startswith("plan_batch_failed"):
                # round29 真因4 归因补漏：PLAN-BATCH 丢模块≠计划非法——误标 plan_invalid 会
                # 污染 L5 错题归因（与 tech_design_incomplete 单列同理）。
                _vf = "plan_batch_failed"
            else:
                _vf = "plan_invalid"
            _patch = {
                "human_decision": HumanDecision.REJECT,
                "confirm_reason": _reason,
                "verification_failure": _vf,
            }
            # tech_design 残缺 / 规划生成失败 / PLAN-BATCH 丢模块 → 升级人工(escalate)，
            # 与"计划非法"一样 fail-fast 但归因区分，绝不静默成功。
            if _vf in ("tech_design_incomplete", "plan_generation_failed", "plan_batch_failed"):
                _patch["failure_escalated"] = True
                _patch["failure_strategy"] = "escalate"
            return _patch
        logger.info("[CONFIRM] 自动接受 (auto_accept 模式，计划合法)")
        return {"human_decision": HumanDecision.ACCEPT}

    # 有人工监听：interrupt 暂停图执行，等待外部 Command(resume=...) 提供决策。
    plan_obj = state.get("plan")
    decision = interrupt(
        {
            "type": "confirm_plan",
            "confirm_reason": _reason,
            "task_id": state.get("task_id"),
            "task_description": state.get("task_description"),
            "complexity": _complexity.value if hasattr(_complexity, "value") else str(_complexity),
            "plan": plan_obj.model_dump() if plan_obj is not None and hasattr(plan_obj, "model_dump") else {},
            "plan_validation_issues": state.get("plan_validation_issues") or [],
            # W1.1：把失败模块/降级原因带进 interrupt，人工审核时能看到"设计不完整"
            "tech_design_failed_modules": state.get("tech_design_failed_modules") or [],
            # round29 真因4（复核 C，与 W1.1 对称）：人工审核须看到结构化的丢失模块明细
            # （name/files/reason），不只 degraded_reasons 里的压缩字符串。
            "plan_batch_failed_modules": state.get("plan_batch_failed_modules") or [],
            "degraded_reasons": state.get("degraded_reasons") or [],
            # hunter F5：baseline 申报刻意不挂 TaskPlan（防变异丢字段）的副作用是
            # plan.model_dump() 里没有它——PLAN 人工闸是最廉价的否决点，审核者必须
            # 看到"PLAN 声称哪些条目存量已有"及覆盖对账，否则失明到 DELIVER（全量
            # 执行成本之后）。矩阵现算不进 state（两份事实必漂移先例）。
            "baseline_covered": state.get("baseline_covered") or [],
            "coverage_matrix": _confirm_coverage_summary(state),
            "message": _msg,
        }
    )

    # decision 可能是字符串 "accept"/"reject"、dict{"decision":...} 或 HumanDecision。
    _raw = decision.get("decision") if isinstance(decision, dict) else decision
    try:
        human_decision = _raw if isinstance(_raw, HumanDecision) else HumanDecision(_raw)
    except (ValueError, TypeError):
        # 畸形/未知 resume payload → fail-closed：不再静默默认 ACCEPT（原 bug：把不确定的
        # 人工意图当"通过"放行），也不让非法字符串抛异常把整图打成 FAILED。按 REJECT 处理 + 告警。
        logger.warning("[CONFIRM] 无法解析人工决策 payload=%r → fail-closed 按 REJECT 处理", decision)
        human_decision = HumanDecision.REJECT

    logger.info(f"[CONFIRM] 人工决策: {human_decision.value}")
    _patch_out: dict = {"human_decision": human_decision}
    # ★对抗复核 3rd-P1a 治本★：REVISE 时把用户填写的修改意见带进 replan_feedback，供 PLAN 节点
    # 定向重规划（plan 节点读 state["replan_feedback"]）。此前只取 decision 字段、丢弃 feedback →
    # confirm 修订退化成"盲重规划"（与 DELIVER 修订链路不对称）。仅 REVISE 且有反馈时注入。
    if human_decision == HumanDecision.REVISE and isinstance(decision, dict):
        _fb = (decision.get("feedback") or "").strip()
        if _fb:
            _patch_out["replan_feedback"] = _fb
    return _patch_out


async def _dispatch_to_worker(
    subtask: SubTask,
    knowledge_context: KnowledgeContext,
    project_id: str = "",
    task_id: str = "",
    *,
    use_alternate: bool = False,
    user_profile_prompt: str = "",
    shared_contract: dict | None = None,
    model_override: str | None = None,
    recursion_boost: int = 0,
    base_ref: str | None = None,
) -> WorkerOutput:
    """将子任务派发给 Worker 执行 — 真实调用 WorkerExecutor"""
    from swarm.knowledge.service import compact_knowledge_context, set_worker_context

    # 解析项目路径（AUDIT 分支与 Worker 都需要）
    project_path = None
    if project_id:
        try:
            from swarm.project import store
            proj = store.get_project(project_id)
            if proj and proj.get("path"):
                project_path = proj["path"]
        except Exception as exc:
            logger.warning("[DISPATCH] 获取项目路径失败: %s", exc)

    # ── AUDIT 意图：走安全审计分支(不产 diff，产结构化报告) ──
    # 必须在 ModelRouter 初始化之前短路：审计不需要 Worker LLM，
    # 否则无模型凭证的环境(如 CI)会在此误初始化 ChatOpenAI 而崩。
    if subtask.intent == TaskIntent.AUDIT:
        return await _run_security_audit(
            subtask, project_path, project_id=project_id, task_id=task_id
        )

    router = ModelRouter()
    difficulty = subtask.difficulty.value if hasattr(subtask.difficulty, "value") else str(subtask.difficulty)
    modality = subtask.modality.value if hasattr(subtask.modality, "value") else str(subtask.modality)
    if use_alternate:
        # audit #34：用公共方法替代直接调 ModelRouter 私有方法，恢复封装边界。
        worker_llm, model_name = router.get_alternate_llm_for_subtask(difficulty, modality)
        logger.info(f"[DISPATCH] 子任务 {subtask.id} 使用备选模型: {model_name}")
    elif model_override and modality != "multimodal":
        # 主力并行轮转：把同难度子任务分到不同本地主力(worker_parallel_pool)，
        # 两个主力同时干、分散负载、产出更快；仍带该难度 fallback 链兜底。
        worker_llm = router.get_llm_by_name(model_override, difficulty=difficulty)
        model_name = model_override
        logger.info(f"[DISPATCH] 子任务 {subtask.id} 主力并行轮转 → {model_name}")
    else:
        worker_llm = router.get_llm_for_subtask(
            difficulty=difficulty,
            modality=modality,
        )
        model_name = getattr(worker_llm, 'model_name', None) or getattr(worker_llm, 'model', None)
        if not model_name:
            # audit #35：两个属性都取不到 → 丧失模型追踪能力，显式告警而非静默用 'routed'
            model_name = 'routed'
            logger.warning(
                "[DISPATCH] 子任务 %s 无法从 LLM 对象读取模型名(model_name/model 均缺)，"
                "追踪降级为 'routed'", subtask.id,
            )
        logger.info(f"[DISPATCH] 子任务 {subtask.id} 使用模型: {model_name}")

    set_worker_context(project_id or None)
    worker_knowledge = compact_knowledge_context(
        knowledge_context,
        limits={"mistakes": 3, "successes": 3, "struct": 8, "semantic": 3, "norms": 5, "behavior": 3},
    )

    audit(
        "dispatch_handoff",
        orchestrator="Brain",
        executor="Worker",
        task_id=task_id,
        subtask_id=subtask.id,
        model=model_name,
        difficulty=subtask.difficulty.value if hasattr(subtask.difficulty, "value") else str(subtask.difficulty),
    )

    try:
        from swarm.infra.worker_dispatcher import get_worker_dispatcher
        dispatcher = get_worker_dispatcher()
        t0 = time.monotonic()
        output = await dispatcher.dispatch(
            subtask,
            model_name=model_name if isinstance(model_name, str) else None,
            knowledge=worker_knowledge,
            project_id=project_id or None,
            project_path=project_path,
            task_id=task_id or None,
            user_profile_prompt=user_profile_prompt,
            shared_contract=shared_contract or {},
            recursion_boost=recursion_boost,
            base_ref=base_ref,  # 3rd#2：worker diff 基线相对钉扎 base
        )
        duration_ms = int((time.monotonic() - t0) * 1000)
        audit(
            "worker_complete",
            orchestrator="Brain",
            executor="Worker",
            task_id=task_id,
            subtask_id=subtask.id,
            model=model_name,
            duration_ms=duration_ms,
            diff_len=len(output.diff or ""),
            l1_passed=output.l1_passed,
            confidence=output.confidence.value if hasattr(output.confidence, "value") else str(output.confidence),
        )
        return output
    except Exception as e:
        logger.error(f"[DISPATCH] Worker 执行异常: {e}")
        return WorkerOutput(
            subtask_id=subtask.id,
            diff="",
            summary=f"执行失败: {e}",
            confidence=Confidence.LOW,
            l1_passed=False,
            l1_details={"error": str(e)},
        )


# god-file 主线1：恢复阶梯 + B-2 pom 脚手架连通分量(18 函数 + 3 常量)已抽出 →
# brain/nodes/planning_core.py（re-export 见文件顶部；调用点仍以 swarm.brain.nodes.X 解析）。


async def handle_failure(state: BrainState) -> dict:
    """HANDLE_FAILURE 节点 — 处理子任务失败。

    brain#3(round24 A4) 不可变持久化：_handle_failure_impl 会【就地修改】state["plan"]
    的 SubTask（注入 retry_guidance、_grant_module_pom_writable 补 pom 写权、
    _widen_scope_for_compile_repair 扩 scope）。这些就地改动只有在 plan channel 被【写回
    返回 dict】时才随 LangGraph checkpoint 持久化；否则 resume 后 plan channel 回滚到改前
    版本 → 诊断/写权/scope 全丢（原 8 个再派发返回里仅 2 个带 plan）。故：凡再派发失败子
    任务(dispatch_remaining)的返回，统一回传当前 plan。plan 为 replace 语义、回传同一对象
    幂等无副作用；已自带 plan 的返回(_targeted_redecompose 的 new_plan)不覆盖。
    """
    result = await _handle_failure_impl(state)
    if isinstance(result, dict) and "dispatch_remaining" in result and "plan" not in result:
        _p = state.get("plan")
        if _p is not None:
            result["plan"] = _p
    return result


# round26 god-file 治理：_handle_failure_impl(~660行)+_l1_details_of 已外置 brain/nodes/failure.py。
# re-export 回本命名空间：上面薄包装 handle_failure 的 bare 调用与
# patch("swarm.brain.nodes._handle_failure_impl") 的 seam 契约均经此解析。
from swarm.brain.nodes.failure import (  # noqa: E402,F401
    _handle_failure_impl,
    _l1_details_of,
)


def _make_base_reader(state: BrainState):
    """从项目【git HEAD 基线】读取 base 文件内容，供 3-way merge / is_new 权威判定。

    ★round21 治本（apply_ok=False 真死因，Agent B 交付链路取证）★：原先读【工作区 project_path】，
    但 pull-back 已把完成子任务产物 materialize 进工作区 → 新模块文件(ruoyi-alarm/pom.xml 等)被
    base_reader 读到 → `is_new=False` → 发带 worker 沙箱相对 base 偏移的 modify hunk → 纯净 git HEAD
    无此文件 → `git apply --check` 必失败(round19 merged diff 88 文件全 modify、0 create 的根因)。
    worker 的 diff 本就相对 git HEAD 生成(executor `_snapshot_from_git_head`)，故 merge base 必须同源
    读 HEAD 才能 3-way 对齐 + 让 HEAD 无的文件正确判 is_new→纯新建补丁。非 git 仓/git 异常→退回工作区
    读(greenfield 与旧行为不回归)。纯读、通用跨栈、非项目写死。"""
    import subprocess

    project_id = state.get("project_id") or ""
    project_path = _get_project_path(project_id)
    _is_git = bool(project_path) and (Path(project_path) / ".git").exists()
    # 3rd#2：merge 3-way base 读【任务钉扎的 base commit】而非实时 HEAD——运行期 HEAD 若被
    # 用户/兄弟任务推进，读 HEAD 会与 worker 相对 base 生成的 hunk 不同源→apply 失败。base=None
    # （非 git/greenfield/未钉扎）→ "HEAD"（零回归）。
    from swarm.git_base import resolve_base_ref
    _base_ref = resolve_base_ref(state.get("base_commit"))

    def _read_worktree(rel: str) -> str | None:
        full = Path(project_path) / rel
        try:
            if full.is_file():
                return full.read_text(encoding="utf-8", errors="replace")
        except OSError as exc:
            logger.debug("[MERGE] read base %s: %s", full, exc)
        return None

    _cache: dict[str, str | None] = {}  # 单次 merge 内 HEAD 稳定：memo 掉 88×git fork（对抗审计·perf）

    def read(file_path: str) -> str | None:
        if not project_path:
            return None
        rel = file_path.lstrip("/")
        if rel.startswith("a/") or rel.startswith("b/"):
            rel = rel[2:]
        if rel in _cache:
            return _cache[rel]
        if _is_git:
            try:
                r = subprocess.run(
                    ["git", "-C", str(project_path), "show", f"{_base_ref}:{rel}"],
                    capture_output=True, text=True, encoding="utf-8",
                    errors="replace", timeout=30,
                )
                # errors="replace"（对抗审计 round21 必修）：HEAD 里的二进制文件(favicon/.jar/图片)不会
                # 抛 UnicodeDecodeError 崩 MERGE（原 text=True 默认 strict 解码会冒泡 ValueError 死整包交付，
                # 原工作区读用 errors="replace" 从不崩）。returncode==0→HEAD 有此文件(返回提交版)；
                # 非 0（"exists on disk, but not in HEAD"）→ HEAD 无=新文件→None→is_new=True→create 补丁。
                val = r.stdout if r.returncode == 0 else None
            except (OSError, ValueError, subprocess.SubprocessError) as exc:
                # git 缺失/超时/解码等异常 → 退回工作区读（保守，不误判所有文件为新）。ValueError 兜底。
                logger.debug("[MERGE] git show HEAD:%s 失败，退回工作区读: %s", rel, exc)
                val = _read_worktree(rel)
        else:
            val = _read_worktree(rel)
        _cache[rel] = val
        return val

    return read


def merge(state: BrainState) -> dict:
    """MERGE 节点 — 合并所有子任务的 diff

    输入: subtask_results
    输出: merged_diff, merge_conflicts (如有硬冲突), rebase_subtask_ids (如有 rebase)
    """
    from swarm.brain.merge_engine import (
        filter_orphan_module_patches,
        merge_diffs,
        verify_merged_patch_applies,
    )

    subtask_results: dict = state.get("subtask_results", {})

    logger.info(f"[MERGE] 合并 {len(subtask_results)} 个子任务的 diff")

    subtask_diffs: list[tuple[str, str]] = []
    for subtask_id, output in subtask_results.items():
        if isinstance(output, WorkerOutput):
            subtask_diffs.append((subtask_id, output.diff or ""))
        elif isinstance(output, dict):
            subtask_diffs.append((subtask_id, output.get("diff", "") or ""))

    # #11(c) 硬门控：module-defining 子任务(建 <dir>/pom.xml 等骨架)不在成功集时，剔除
    # 引用该骨架缺失模块的兄弟补丁——否则合并 patch 有模块目录文件却无该模块骨架 →
    # git apply/reactor 崩(No such file / Child module does not exist)，整包交付死于门口。
    _merge_proj_path = _get_project_path(state.get("project_id") or "")

    def _base_has_module(_dir: str) -> bool:
        if not _merge_proj_path:
            return False
        for _mf in ("pom.xml", "build.gradle", "build.gradle.kts", "Cargo.toml", "go.mod"):
            if (Path(_merge_proj_path) / _dir / _mf).is_file():
                return True
        _md = Path(_merge_proj_path) / _dir
        return _md.is_dir() and any(_md.glob("*.csproj"))

    # #11(c) 护栏(round21 对抗审计)：base 项目路径不可用时传 None → filter 跳过过滤，
    # 绝不把既有模块误判孤儿→补丁全剔→误杀交付（真问题仍由 VERIFY_L2/apply 护栏兜）。
    subtask_diffs, _dropped_orphans = filter_orphan_module_patches(
        subtask_diffs,
        base_module_exists=_base_has_module if _merge_proj_path else None)
    if _dropped_orphans:
        logger.error(
            "[MERGE] ⚠️ 模块骨架缺失(module-defining 子任务未成功) → 剔除引用其的补丁，"
            "保其余模块交付；缺骨架模块=%s（非模型问题，交付需该模块脚手架落盘）",
            {d: sids for d, sids in _dropped_orphans.items()},
        )

    # A-P1-26c：传入依赖拓扑序，让 rebase 策略以【上游子任务】为 base（非 hunk 出现序）。
    plan = state.get("plan")
    subtask_order = plan.topological_order() if plan is not None else None

    result = merge_diffs(
        subtask_diffs,
        base_reader=_make_base_reader(state),
        subtask_order=subtask_order,
    )

    if result.conflicts:
        for conflict in result.conflicts:
            logger.warning(
                "[MERGE] 冲突: %s — %s",
                conflict.file_path,
                conflict.message,
            )

    if result.rebase_subtask_ids:
        logger.info(
            "[MERGE] rebase 重生成: %s（保留 base 方 diff，重跑冲突子任务）",
            result.rebase_subtask_ids,
        )

    # ── Fix 1c·fail-closed 护栏：仅在【终局干净合并】（无冲突、无待 rebase → 即将进 VERIFY_L2）时，
    # 对最终 merged_diff 跑 git apply --check，让 "success=True" 诚实反映"补丁真能落盘"。
    # rebase 循环中的中间态不校验（文件将被重生成，避免假阴性）。校验失败＝确定性组装缺陷，
    # 在 MERGE 出口就打醒目诊断（区别于 VERIFY_L2 的"集成失败"），不再靠 success=True 蒙混。
    _apply_ok, _apply_err = True, ""
    if result.success and not result.rebase_subtask_ids and result.merged_diff.strip():
        _proj_path = _get_project_path(state.get("project_id") or "")
        # round29 治本：校验对齐 diff 生成基线（base_reader 读钉扎 base commit）——否则 pull-back
        # 污染工作树后，本应新建的文件被 `git apply --check` 误判 "already exists" → 假 apply_ok=False
        # → 本应 PARTIAL 的任务被 escalate 成 FAILED（task d37a52a3 实测 77 文件全中）。
        from swarm.git_base import resolve_base_ref
        _base_ref = resolve_base_ref(state.get("base_commit"))
        _apply_ok, _apply_err = verify_merged_patch_applies(_proj_path, result.merged_diff, _base_ref)
        if not _apply_ok:
            logger.error(
                "[MERGE] ⚠️ 合并 patch 组装非法：git apply --check 失败 → %s"
                "（确定性 diff 组装缺陷，非模型/非集成问题；VERIFY_L2 将阻断交付）",
                _apply_err,
            )
            # Fix 0（round17 诊断落盘）：apply 失败时把 merged_diff 完整落盘供离线定位组装缺陷。
            # verify_merged_patch_applies 用 delete=True 临时文件跑完即删 → 否则每轮只能靠 agent
            # 逆推。fail-safe：helper 内吞异常返回 None，绝不影响主流程。
            from swarm.brain.merge_engine import dump_merged_diff_for_diagnosis
            _dump_path = dump_merged_diff_for_diagnosis(state.get("task_id") or "", result.merged_diff)
            if _dump_path:
                logger.error("[MERGE] merged_diff 已落盘供诊断: %s", _dump_path)
    logger.info(
        "[MERGE] 合并完成, 总长度=%d, 冲突=%d, 自动消解=%d, rebase=%d, success=%s, apply_ok=%s",
        len(result.merged_diff),
        len(result.conflicts),
        len(result.auto_resolved_files),
        len(result.rebase_subtask_ids),
        result.success,
        _apply_ok,
    )
    merge_touch = touch_context(
        state,
        "merge",
        (
            f"合并 {len(subtask_results)} 个子任务; "
            f"diff={len(result.merged_diff)} chars; "
            f"冲突={len(result.conflicts)}; "
            f"rebase={len(result.rebase_subtask_ids)}"
        ),
    )
    out: dict = {"merged_diff": result.merged_diff, **merge_touch}

    # H3 纪律：BrainState 无 reducer（last-write-wins），clean merge 必须显式回写
    # rebase_subtask_ids=[]，否则上一轮的非空 rebase 列表会残留，导致 after_merge
    # 误判仍需 rebase → MERGE→DISPATCH 死循环至 recursion_limit。
    # 仅在下方 rebase 路径命中时被覆盖为非空。
    out["rebase_subtask_ids"] = []
    # round27 同族补漏：merge_conflicts 与 failed_subtask_ids 也是"仅冲突路径写、无人清"的
    # 粘滞键——第 1 轮冲突 → HANDLE_FAILURE 重试成功 → 第 2 轮 clean merge 不回写 → after_merge
    # 读到上轮残留冲突再次路由 HANDLE_FAILURE（空失败集喂 LLM，可能 escalate 把已成功任务判
    # FAILED / replan 推倒重来）。与 H3 同法：每轮 merge 先清，仅冲突路径覆盖为非空。
    out["merge_conflicts"] = []
    out["failed_subtask_ids"] = []
    # 批4c 补漏（外部复核）：failure_escalated 同为"仅条件写"粘滞键——干净轮显式清，
    # 本函数下方 apply-check 失败 / rebase 超限硬冲突两条 escalate 路径在同一 out 覆盖为
    # True（A6 每轮独立判定，语义不变）。与上面 merge_conflicts 的 round27 修法对称。
    out["failure_escalated"] = False

    # #1(a) fail-closed：终局干净合并但 merged_diff `git apply --check` 失败＝确定性组装缺陷。
    # 绝不能只诊断后默认落 VERIFY_L2（project_path 空时 L2 复核整块跳过 → 非法 patch 假绿放行）。
    # 复用既有 escalate 路径（after_merge:285 → DELIVER 人工审核；交付 gate 拒绝放行、不学成成功）。
    if not _apply_ok:
        out["failure_escalated"] = True
        out["failure_strategy"] = "escalate"
        out["l2_passed"] = False
        out["verification_failure"] = "merge_apply_invalid"
        logger.warning(
            "[MERGE] → 升级人工(escalate)：合并 patch 组装非法，fail-closed 阻断交付（不进 VERIFY_L2 假绿）"
        )

    # ── 硬冲突路径（无 base_reader 可用或单子任务冲突）──
    if result.conflicts:
        out["merge_conflicts"] = [
            {
                "file_path": c.file_path,
                "subtask_ids": c.subtask_ids,
                "message": c.message,
            }
            for c in result.conflicts
        ]
        out["failed_subtask_ids"] = sorted(
            {sid for c in result.conflicts for sid in c.subtask_ids}
        )

    # ── Rebase 重生成路径 ──
    # 将 rebase 子任务从 subtask_results 移除，加入 dispatch_remaining 重跑
    # 不增加重试计数（rebase 是策略性重生成，不是失败重试）
    # audit #30：但用独立的 rebase 计数设上限，防 rebase→fail→rebase 无限循环。
    if result.rebase_subtask_ids:
        rebase_counts = dict(state.get("subtask_rebase_counts", {}))
        max_rebase = get_config().model.max_retries + 1  # 与重试上限同量级，独立计数
        next_rebase = {sid: rebase_counts.get(sid, 0) + 1 for sid in result.rebase_subtask_ids}
        over_limit = [sid for sid, n in next_rebase.items() if n > max_rebase]
        if over_limit:
            # 杠杆B(交付韧性·止血，round9 治本)：rebase 达上限但【整体合并干净】(无硬冲突、merged_diff
            # 有效)时，不该把"全子任务过/0 冲突"的近完整产物整体判 FAILED。result.merged_diff 已是 base
            # 写者版本(超限的下游【聚合清单】加性变更——如多写者向根 pom 各加不同 <module>/<dependency>
            # ——未并入)；接受它继续走 VERIFY_L2→交付(PARTIAL 质量)。聚合清单成员完整性由交付前 post-pass
            # reconcile(integration_review/learn_success 的 reconcile_workspace_manifests)据 ground-truth
            # 兜底补回(如缺失的 <module> 注册)。仅当存在【真硬冲突】时才升级人工 fail-fast(原行为)。
            # round9 实测：35 子任务/失败 0/冲突 0/360KB 干净合并，仅因 st-30 根 pom rebase 超限被误判
            # FAILED——本支挽回。
            if not result.conflicts and result.merged_diff.strip():
                logger.warning(
                    "[MERGE] rebase 达上限(%d) 但整体合并干净(冲突=0) → 接受 base 版干净合并继续交付，"
                    "超限聚合清单加性变更交 post-pass reconcile 据 ground-truth 兜底，不整体判 FAILED: %s",
                    max_rebase, over_limit,
                )
                out["subtask_rebase_counts"] = {**rebase_counts, **next_rebase}
                out["merge_rebase_dropped"] = over_limit
                # rebase_subtask_ids 维持 [](上方默认)、不设 failure_escalated → after_merge 路由 VERIFY_L2
                return out
            # rebase 已达上限【且有真硬冲突】→ 升级人工，不再无限重生成
            logger.warning(
                "[MERGE] 子任务 rebase 达上限(%d)且存在硬冲突，升级人工: %s", max_rebase, over_limit,
            )
            out["failure_escalated"] = True
            out["failure_strategy"] = "escalate"
            out["l2_passed"] = False
            out["subtask_rebase_counts"] = {**rebase_counts, **next_rebase}
            return out
        out["rebase_subtask_ids"] = result.rebase_subtask_ids
        out["subtask_rebase_counts"] = {**rebase_counts, **next_rebase}
        dispatch_remaining = list(state.get("dispatch_remaining", []))
        remaining_results = dict(subtask_results)
        for sid in result.rebase_subtask_ids:
            remaining_results.pop(sid, None)
            if sid not in dispatch_remaining:
                dispatch_remaining.append(sid)
        out["subtask_results"] = remaining_results
        out["dispatch_remaining"] = dispatch_remaining

    return out


def _get_project_path(project_id: str) -> str | None:
    if not project_id:
        return None
    try:
        from swarm.project import store

        proj = store.get_project(project_id)
        if proj and proj.get("path"):
            return proj["path"]
    except Exception as exc:
        # P2-5：本函数被 PLAN/MERGE/DISPATCH/VERIFY_L2/HANDLE_FAILURE 全线共用，
        # 前缀不得写死单一节点名误导排障
        logger.warning("[PROJECT] 获取项目路径失败: %s", exc)
    return None


def _sandbox_available() -> bool:
    cfg = get_config().sandbox
    return bool(cfg.use_for_worker and cfg.api_url)


def _run_l2_local(project_path: str, merged_diff: str, test_cmd: str, *, timeout: int = 180,
                  base_ref: str | None = None) -> bool:
    from swarm.project.diff_apply import apply_git_diff

    apply_result = apply_git_diff(project_path, merged_diff)
    if not apply_result.get("ok"):
        logger.warning(
            "[VERIFY_L2] 本地 git apply 失败: stage=%s stderr=%s",
            apply_result.get("stage"),
            apply_result.get("stderr", ""),
        )
        return False

    import subprocess

    try:
        proc = subprocess.run(
            test_cmd,
            cwd=project_path,
            shell=True,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
        if proc.returncode != 0:
            logger.warning(
                "[VERIFY_L2] 本地测试失败 (rc=%s): %s",
                proc.returncode,
                (proc.stderr or proc.stdout or "")[:500],
            )
        return proc.returncode == 0
    except subprocess.TimeoutExpired:
        logger.warning("[VERIFY_L2] 本地测试超时 (%ss): %s", timeout, test_cmd)
        return False
    except Exception as exc:
        logger.warning("[VERIFY_L2] 本地测试异常: %s", exc)
        return False
    finally:
        # H1 修复：L2 验证用的是临时 apply，验证完必须还原工作树——否则脏改动残留，
        # 污染下一任务的事实核验 ground truth(git ls-files/os.walk) 和 learn_success 的 commit。
        # R1 治本：限定回滚到 merged_diff 涉及的文件（scoped _reset_worktree_to_head），
        # 不再用整库 `checkout -- .` + `clean -fd`——后者会抹掉用户在该项目里无关的未提交改动。
        try:
            from swarm.brain.integration_review import _reset_worktree_to_head
            _reset_worktree_to_head(project_path, merged_diff, base_ref=base_ref)
        except Exception as exc:  # noqa: BLE001
            logger.warning("[VERIFY_L2] 工作树回滚失败(非致命): %s", exc)


def _run_l2_in_sandbox(
    project_path: str,
    merged_diff: str,
    test_cmd: str,
    *,
    project_id: str = "",
    timeout: int = 180,
) -> bool | None:
    """沙箱 L2 功能测试。返回 True/False=测试【真跑了】的结论；None=infra 失败（沙箱不可用/
    命令没跑成），交调用方走既有降级路径（本地 L2 → LLM 兜底），绝不误判为测试失败。

    D31 治本：改走 run_command（shell 端点，所有镜像通用）——旧实现用 run_code 打 Jupyter
    kernel 端点，自建语言镜像无 kernel 必 502，且 502 被当测试失败误杀整任务。对齐
    _run_reactor_build_in_sandbox 的 ran/ok 区分与 __RC__ 退出码口径；create 与 reactor 版
    同款传 project_id（_resolve_template 据此匹配项目专属镜像模板）。
    """
    from pathlib import Path

    from swarm.worker.sandbox import get_sandbox_manager, write_file_to_sandbox

    cfg = get_config().sandbox
    workdir = cfg.sandbox_remote_workdir
    manager = get_sandbox_manager()
    run_command = getattr(manager, "run_command", None)
    if run_command is None:
        return None
    sandbox = None

    try:
        sandbox = manager.create(
            project_id=project_id or None,
            source="verify_l2",
        )
        manager.sync_project_to_sandbox(sandbox, Path(project_path), workdir)

        # patch 走 envd 文件端点写入（不依赖 Jupyter kernel，也不受 shell 命令行长度限制）
        patch_path = "/tmp/__swarm_l2_merged.patch"
        write_file_to_sandbox(sandbox, patch_path, merged_diff, manager=manager)

        apply_result = run_command(
            sandbox,
            f"cd {workdir} && git apply {patch_path}; echo __APPLY_RC__$?",
            timeout=90,
        )
        apply_out = (getattr(apply_result, "stdout", "") or "") + (getattr(apply_result, "stderr", "") or "")
        if "__APPLY_RC__" not in apply_out:
            # 命令没跑成（网关 5xx/沙箱死）= infra 失败 → None 降级，不判测试失败
            logger.warning(
                "[VERIFY_L2] 沙箱 git apply 未执行(infra，降级本地/LLM): %s",
                (getattr(apply_result, "error", None) or apply_out)[:300],
            )
            return None
        if "__APPLY_RC__0" not in apply_out:
            logger.warning("[VERIFY_L2] 沙箱 git apply 失败: %s", apply_out[:500])
            return False

        test_result = run_command(
            sandbox,
            f"cd {workdir} && ({test_cmd}); echo __RC__$?",
            timeout=timeout + 30,
        )
        test_out = (getattr(test_result, "stdout", "") or "") + (getattr(test_result, "stderr", "") or "")
        if "__RC__" not in test_out:
            logger.warning(
                "[VERIFY_L2] 沙箱测试未执行(infra，不判测试失败，降级本地/LLM): %s",
                (getattr(test_result, "error", None) or test_out)[:300],
            )
            return None
        ok = "__RC__0" in test_out
        if not ok:
            logger.warning("[VERIFY_L2] 沙箱测试未通过: %s", test_out[-1000:])
        return ok
    except Exception as exc:  # noqa: BLE001 — infra 异常 → 降级，不误判失败也不炸 verify 节点
        logger.warning("[VERIFY_L2] 沙箱 L2 验证异常(infra，降级本地/LLM): %s", exc)
        return None
    finally:
        if sandbox is not None:
            try:
                manager.kill(sandbox.sandbox_id)
            except Exception as exc:
                logger.debug("[VERIFY_L2] 销毁沙箱失败: %s", exc)


def _try_handoff_compile_sandbox_for_smoke(manager, sandbox) -> str | None:
    """S1-4：L2 编译沙箱 → 冒烟延活转交（设计 §2.2/§2.3）。仅在编译成功后调用。

    冒烟开启且 try_extend_lifetime(冒烟预算+缓冲) 成功 → 返回 sandbox_id（对象留在
    manager._instances 进程内 registry，state 只存 sid 字符串——沙箱对象不可序列化进
    PG checkpoint）；开关关/续期失败/异常 → None（调用方照旧 finally kill，转交不成立，
    verify_runtime 走回退自建）。"""
    try:
        from swarm.brain.nodes.runtime_smoke import (
            RUN_TIMEOUT_BUFFER_SEC,
            resolve_prepare_timeout_sec,
            resolve_smoke_timeout_sec,
        )
        from swarm.brain.nodes.verify import _runtime_smoke_enabled

        if not _runtime_smoke_enabled():
            return None
        # F1：与 verify_runtime 的预算公式同口径。转交时冒烟推导尚未发生（prepare_cmd 未知），
        # 保守恒加 prepare 预算——多续的寿命由 verify_runtime finally 必杀兜底，无泄漏代价；
        # 少续则转交沙箱在增量 package（prepare）中途到期，白白废掉快路径。
        budget = (resolve_smoke_timeout_sec() + RUN_TIMEOUT_BUFFER_SEC + 120
                  + resolve_prepare_timeout_sec())
        if manager.try_extend_lifetime(sandbox, budget):
            logger.info("[VERIFY_L2] 编译沙箱 %s 延活转交冒烟(+%ds)", sandbox.sandbox_id, budget)
            return sandbox.sandbox_id
        logger.info("[VERIFY_L2] 冒烟转交续期失败 → 照旧销毁编译沙箱（verify_runtime 自建兜底）")
        return None
    except Exception as exc:  # noqa: BLE001 — 转交是优化路径，任何异常都退回"照旧 kill"
        logger.debug("[VERIFY_L2] 冒烟转交尝试异常，照旧销毁: %s", exc)
        return None


def _run_reactor_build_in_sandbox(
    project_path: str,
    project_id: str,
    build_cmd: str,
    *,
    timeout: int = 600,
) -> tuple[bool, bool, str, str | None]:
    """在【项目沙箱】(按检测栈版本烤的工具链，见 image_builder._toolchain_install)跑全 reactor 集成
    编译——治本 round21 的 L2 空气闸：brain host 无需装任何栈/版本，Java8/17/21·Go·Rust·Node 由沙箱
    镜像各自正确。

    契约：调用前 project_path 工作树【已 apply merged_diff】(run_integration_review 本地 apply)。这里把
    该已应用工作树 sync 进沙箱后【直接跑 build_cmd】(不再沙箱内 git apply → 规避双重应用/脏基线)。
    返回 (ran, ok, output, smoke_handoff_sid)：ran=False = 沙箱不可用/异常 → 交调用方退回本机或
    fail-loud。smoke_handoff_sid（S1-4）=编译成功且冒烟延活转交成立时的沙箱 id（该沙箱【未被 kill】，
    处置责任移交调用方→verify_runtime）；其余一切路径 None 且沙箱照旧 finally kill。"""
    if not _sandbox_available():
        return False, False, "", None
    from pathlib import Path

    from swarm.worker.sandbox import get_sandbox_manager

    cfg = get_config().sandbox
    workdir = cfg.sandbox_remote_workdir
    manager = get_sandbox_manager()
    run_command = getattr(manager, "run_command", None)
    if run_command is None:
        return False, False, "", None
    sandbox = None
    handed_off = False
    try:
        sandbox = manager.create(project_id=project_id or None, source="verify_l2_compile")
        manager.sync_project_to_sandbox(sandbox, Path(project_path), workdir)
        # 包 echo __RC__$? 取退出码，robust 不依赖 result 对象的 exit_code 字段形态。
        result = run_command(
            sandbox, f"cd {workdir} && ({build_cmd}); echo __RC__$?", timeout=timeout
        )
        out = (getattr(result, "stdout", "") or "") + (getattr(result, "stderr", "") or "")
        ok = "__RC__0" in out
        # R34-7：探针沙箱缺构建工具（round34 实证 verify_l2_compile 模板无 mvn，RC=127
        # "command not found"）→ 这是 infra 非代码失败。ran=False 走"沙箱不可用"退路
        # （本机兜底/如实 fail-loud），绝不把环境缺陷记成集成编译失败误导归因/修复循环。
        # hunter LOW：收紧判据——只认【构建驱动本身】的 command not found 串（bash/dash
        # 两种措辞），不再用宽 __RC__127（构建插件 shell 出缺失二进制也 127，宽判会把真
        # 构建失败误标 infra 掩盖）。驱动缺失=infra，插件内 127=真失败，须分开。
        _tool = (build_cmd.split() or [""])[0]
        if not ok and (f"{_tool}: command not found" in out or f"{_tool}: not found" in out):
            logger.error(
                "[VERIFY_L2] 探针沙箱缺构建工具 %r（模板装配问题，非代码失败）——"
                "按沙箱不可用处理，请核查 verify_l2_compile 模板", _tool)
            return False, False, "", None
        logger.info("[VERIFY_L2] 沙箱集成编译: %s (cmd=%s)", "通过" if ok else "未通过", build_cmd)
        smoke_sid: str | None = None
        if ok:
            # S1-4 延活转交：仅编译成功时尝试；成立则 finally 不 kill（编译产物留给冒烟复用，
            # 省一次 create+sync+全量重编译）。泄漏兜底=远端 900s 自动到期+启动清扫，
            # 但 verify_runtime 的 finally kill 是第一责任人。
            smoke_sid = _try_handoff_compile_sandbox_for_smoke(manager, sandbox)
            handed_off = smoke_sid is not None
        return True, ok, out[-3000:], smoke_sid
    except Exception as exc:  # noqa: BLE001
        logger.warning("[VERIFY_L2] 沙箱集成编译异常(退回本机/fail-loud): %s", exc)
        return False, False, "", None
    finally:
        if sandbox is not None and not handed_off:
            try:
                manager.kill(sandbox.sandbox_id)
            except Exception as _exc:  # noqa: BLE001
                logger.debug("[VERIFY_L2] 销毁编译沙箱失败: %s", _exc)


def _try_l2_sandbox_verify(
    project_id: str,
    merged_diff: str,
    test_cmd: str,
    *,
    timeout: int = 180,
) -> bool | None:
    """Run L2 in sandbox. Returns None if sandbox unavailable **or** infra 失败
    (命令没跑成，见 _run_l2_in_sandbox D31)——调用方据 None 降级本地/LLM，不判测试失败。"""
    if not _sandbox_available():
        return None
    project_path = _get_project_path(project_id)
    if not project_path:
        return None
    logger.info("[VERIFY_L2] 沙箱 L2 验证: cmd=%s", test_cmd)
    return _run_l2_in_sandbox(
        project_path,
        merged_diff,
        test_cmd,
        project_id=project_id,
        timeout=timeout,
    )


def _try_l2_local_verify(
    project_id: str,
    merged_diff: str,
    test_cmd: str,
    *,
    timeout: int = 180,
    base_ref: str | None = None,
) -> bool | None:
    """Run L2 locally via git apply + subprocess. Returns None if no project path."""
    project_path = _get_project_path(project_id)
    if not project_path:
        return None
    logger.info("[VERIFY_L2] 本地 L2 验证: cmd=%s", test_cmd)
    return _run_l2_local(project_path, merged_diff, test_cmd, timeout=timeout, base_ref=base_ref)


async def _verify_l2_via_llm(
    task_description: str,
    merged_diff: str,
    acceptance_criteria: list[str],
    subtask_results: dict,
) -> bool:
    try:
        llm = _get_brain_llm()
        prompt_user = VERIFY_L2_USER.format(
            task_description=task_description,
            merged_diff=merged_diff[:4000],
            acceptance_criteria=json.dumps(acceptance_criteria, ensure_ascii=False),
        )
        response = await llm.ainvoke([
            {"role": "system", "content": VERIFY_L2_SYSTEM},
            {"role": "user", "content": prompt_user},
        ])
        result = _parse_json_from_llm(response.content)
        return bool(result.get("l2_passed", result.get("passed", False)))
    except json.JSONDecodeError as e:
        logger.warning(f"[VERIFY_L2] LLM 输出 JSON 解析失败，回退到 L1 检查: {e}")
        # N-05 修复：all([]) 恒 True 会把"无可信 worker 产出"误判为通过 → 假 DONE。
        # 必须有至少一个 WorkerOutput 佐证，且全部 l1_passed，才算回退通过；空集合判失败。
        l1_outs = [out for out in subtask_results.values() if isinstance(out, WorkerOutput)]
        if not l1_outs:
            logger.warning("[VERIFY_L2] 回退检查无任何 WorkerOutput 佐证 → 判未通过（防 all([])→True 假 DONE）")
            return False
        return all(out.l1_passed for out in l1_outs)
    except Exception as e:
        logger.warning(f"[VERIFY_L2] LLM 验证异常，默认未通过: {e}")
        return False


# S2-6：deliver 人工闸 payload 的逐条断言 verdict 限量（体积节制——merged_diff[:2000] 同款
# 纪律：payload 走 SSE/checkpoint，绝不塞全量 details；超出部分以计数如实呈现）。
_DELIVER_ASSERT_ROWS_MAX = 20


def _deliver_review_payload(state: BrainState) -> dict:
    """S2-6：deliver 人工闸审核 payload 补全（纯函数，全部从 state 读、缺键容错）。

    S2-1 取证：payload 此前只有 merged_diff[:2000]+l2_passed——runtime/migration/acceptance
    结论与需求覆盖矩阵根本不进人工审核视野，人工审核是盲的。本函数加法补齐：
      - runtime_smoke：三态结论 + skipped/message/classification（state 键 S1-4/S1-6）
      - migration_verify：三态结论 + kind（S1-5）
      - acceptance：三态结论 + 逐条断言 verdict 摘要（限量 _DELIVER_ASSERT_ROWS_MAX 条 +
        总数/省略数）+ manual 清单（auth≠none 不自动执行的"N 条需人工验证"，设计 §5.3）
      - coverage：build_coverage_matrix 现算（矩阵是派生数据不进 state，防两份事实漂移）
      - degraded_reasons：降级留痕全量（reducer 已去重，体积可控）
    旧 checkpoint 无新键 → 各段返回 None/空缺省，绝不抛（deliver 是 interrupt 锚点，
    payload 组装失败=人工闸打不开）。消费面（runner._extract_interrupt_info → SSE 事件 /
    get_pending_interrupt API）对 payload dict 整体透传无键白名单，加法安全。
    """
    rt_details = state.get("runtime_smoke_details") or {}
    if not isinstance(rt_details, dict):
        rt_details = {}
    mig_details = state.get("migration_verify_details") or {}
    if not isinstance(mig_details, dict):
        mig_details = {}
    acc_details = state.get("acceptance_details") or {}
    if not isinstance(acc_details, dict):
        acc_details = {}

    rows = [r for r in (acc_details.get("assertions") or []) if isinstance(r, dict)]
    row_summaries: list[dict] = []
    for r in rows[:_DELIVER_ASSERT_ROWS_MAX]:
        req = r.get("request") if isinstance(r.get("request"), dict) else {}
        row_summaries.append({
            "id": r.get("id"),
            "req_id": r.get("req_id"),
            "verdict": r.get("verdict"),
            "method": req.get("method"),
            "path": req.get("path"),
            "http_code": r.get("http_code"),
            "reason": str(r.get("reason") or "")[:160],
        })
    manual_rows = [
        {"id": r.get("id"), "req_id": r.get("req_id"), "kind": r.get("kind")}
        for r in rows if r.get("verdict") == "skipped_manual"
    ][:_DELIVER_ASSERT_ROWS_MAX]

    try:
        from swarm.brain.plan_validator import build_coverage_matrix
        matrix = build_coverage_matrix(
            state.get("plan"), state.get("requirement_items"),
            state.get("baseline_covered"))
        coverage = {
            "total": matrix["total_items"],
            "covered": matrix["covered_items"],
            "uncovered": [
                {"id": u.get("id"), "text": str(u.get("text") or "")[:120]}
                for u in matrix["uncovered"][:_DELIVER_ASSERT_ROWS_MAX]
            ],
            "uncovered_count": len(matrix["uncovered"]),
            # R31-1 T1：存量申报对人工闸可见（申报≠实现，验收断言兜底；人工要能看到
            # "哪些条目是 PLAN 声称基线已有"来行使否决）
            "baseline_covered": [
                {"id": b.get("id"), "reason": str(b.get("reason") or "")[:120]}
                for b in matrix["baseline_covered"][:_DELIVER_ASSERT_ROWS_MAX]
            ],
            "baseline_covered_count": len(matrix["baseline_covered"]),
        }
    except Exception as exc:  # noqa: BLE001 — 矩阵现算失败绝不挡人工闸，如实留痕
        logger.warning("[DELIVER] 覆盖矩阵现算失败(payload 降级为空): %s", exc)
        coverage = {"total": 0, "covered": 0, "uncovered": [], "uncovered_count": 0,
                    "error": str(exc)[:200]}

    return {
        "runtime_smoke": {
            "passed": state.get("runtime_smoke_passed", None),
            "skipped": state.get("runtime_smoke_skipped", None),
            "message": str(state.get("runtime_smoke_message") or "")[:400],
            "classification": rt_details.get("classification"),
        },
        "migration_verify": {
            "passed": state.get("migration_verify_passed", None),
            "kind": mig_details.get("kind"),
        },
        "acceptance": {
            "passed": state.get("acceptance_passed", None),
            "reason": acc_details.get("reason"),
            "total": acc_details.get("total"),
            "manual_count": acc_details.get("manual_count"),
            "failed_count": acc_details.get("failed_count"),
            "assertions": row_summaries,
            "assertions_total": len(rows),
            "assertions_omitted": max(0, len(rows) - _DELIVER_ASSERT_ROWS_MAX),
            "manual": manual_rows,
        },
        "coverage": coverage,
        "degraded_reasons": list(state.get("degraded_reasons") or []),
    }


def deliver(state: BrainState) -> dict:
    """DELIVER 节点 — 交付结果，等待人工决策

    使用 langgraph.types.interrupt 实现挂起等待人工决策。
    输入: merged_diff, l2_passed
    输出: human_decision

    在 auto_accept 模式下（API 调用），跳过 interrupt 直接接受。
    """
    logger.info("[DELIVER] 等待人工决策")

    # API 模式下自动接受
    auto_accept = state.get("auto_accept", False) or os.environ.get("SWARM_AUTO_ACCEPT", "").lower() in ("1", "true", "yes")

    if auto_accept:
        # P1 闸门（对齐 CONFIRM 的 P0-3）：auto_accept 只对【真正成功】的产出放行。
        # 失败/升级/未验证通过的产出绝不能被当成功 ACCEPT，否则 after_deliver 会路由到
        # LEARN_SUCCESS，把失败任务学成成功模式污染知识库（task 37460a5b: escalate 后
        # 仍 LEARN_SUCCESS id=393）。放行判据收敛在 brain.gates 单一事实源。
        from swarm.brain.gates import can_auto_accept_delivery

        allow, reason = can_auto_accept_delivery(state)
        if not allow:
            logger.warning(
                "[DELIVER] auto_accept 拒绝放行未成功产出（fail-fast，走 LEARN_FAILURE）：%s",
                reason,
            )
            return {
                "human_decision": HumanDecision.REJECT,
                "deliver_auto_reject_reason": reason,
            }
        logger.info("[DELIVER] 自动接受 (auto_accept 模式，产出已验证通过)")
        return {"human_decision": HumanDecision.ACCEPT}

    # interrupt 暂停图执行，等待外部输入
    # S2-6：payload 加法补齐 runtime/migration/acceptance/coverage/degraded 审核视野
    # （_deliver_review_payload，旧键一个不动——消费面兼容）。
    decision = interrupt(
        {
            "type": "deliver",
            "task_id": state.get("task_id"),
            "task_description": state.get("task_description"),
            "merged_diff": state.get("merged_diff", "")[:2000],
            "l2_passed": state.get("l2_passed", False),
            **_deliver_review_payload(state),
            "message": "任务执行完成，请审核结果并决定: accept(接受) / revise(修订) / reject(拒绝)",
        }
    )

    # 解析决策（与 confirm_plan 对称 fail-closed）：畸形/未知 resume payload 不再静默默认 ACCEPT
    # （原 bug：把不确定的人工意图当"接受交付"放行），非法决策字符串也不抛异常打崩整图。
    _raw = decision.get("decision") if isinstance(decision, dict) else decision
    try:
        human_decision = _raw if isinstance(_raw, HumanDecision) else HumanDecision(_raw)
    except (ValueError, TypeError):
        logger.warning("[DELIVER] 无法解析人工决策 payload=%r → fail-closed 按 REJECT 处理", decision)
        human_decision = HumanDecision.REJECT

    # 如果有修订反馈
    revision_feedback = ""
    if isinstance(decision, dict) and "feedback" in decision:
        revision_feedback = decision["feedback"]
    elif human_decision == HumanDecision.REVISE:
        revision_feedback = "请修复问题"

    logger.info(f"[DELIVER] 人工决策: {human_decision.value}")
    return {
        "human_decision": human_decision,
        "revision_feedback": revision_feedback,
    }


async def revision(state: BrainState) -> dict:
    """REVISION 节点 — 根据人工修订反馈生成修订子任务

    输入: revision_feedback, merged_diff, task_description, plan
    输出: plan (更新), dispatch_remaining, subtask_results (清空失败部分)
    """
    revision_feedback = state.get("revision_feedback", "")
    merged_diff = state.get("merged_diff", "")
    task_description = state.get("task_description", "")
    plan_obj = state.get("plan")

    logger.info(f"[REVISION] 处理修订反馈: {revision_feedback[:100]}...")

    # ── LLM 修订分析 ──
    try:
        llm = _get_brain_llm()
        prompt_user = REVISION_USER.format(
            revision_feedback=revision_feedback,
            task_description=task_description,
            merged_diff=merged_diff[:4000],
        )
        response = await llm.ainvoke([
            {"role": "system", "content": REVISION_SYSTEM},
            {"role": "user", "content": prompt_user},
        ])
        result = _parse_json_from_llm(response.content)
        # N-02 修复：REVISION_SYSTEM 产出 {"revision_subtasks":[{id,description,scope,...}]}（嵌数组），
        # 原从顶层 result.get("id") 读永远落空 → 人工修订退化成空 scope 桩任务空跑。
        # 优先从 revision_subtasks[0] 取；兼容旧的顶层平铺格式。
        _subs = result.get("revision_subtasks")
        rsrc = _subs[0] if isinstance(_subs, list) and _subs and isinstance(_subs[0], dict) else result
        # 尝试从 LLM 结果中提取修订子任务
        revision_subtask = SubTask(
            id=rsrc.get("id", f"rev-{len(plan_obj.subtasks) + 1 if plan_obj else 1}"),
            description=rsrc.get("description", f"修订: {revision_feedback[:100]}"),
            difficulty=SubTaskDifficulty(rsrc.get("difficulty", "medium")),
            modality=SubTaskModality(rsrc.get("modality", "text")),
            scope=FileScope(**rsrc.get("scope", {"writable": [], "readable": []})),
            contract=rsrc.get("contract", {"input": "修订反馈", "output": "修订后代码"}),
            acceptance_criteria=rsrc.get("acceptance_criteria", ["修订内容正确", "回归测试通过"]),
            depends_on=rsrc.get("depends_on", []),
        )
    except json.JSONDecodeError as e:
        logger.warning(f"[REVISION] LLM 输出 JSON 解析失败，使用默认修订子任务: {e}")
        revision_subtask = SubTask(
            id=f"rev-{len(plan_obj.subtasks) + 1 if plan_obj else 1}",
            description=f"修订: {revision_feedback[:100]}",
            scope=FileScope(writable=[], readable=[]),
            contract={"input": "修订反馈", "output": "修订后代码"},
            acceptance_criteria=["修订内容正确", "回归测试通过"],
            depends_on=[],
        )
    except Exception as e:
        logger.warning(f"[REVISION] 分析异常，使用默认修订子任务: {e}")
        revision_subtask = SubTask(
            id=f"rev-{len(plan_obj.subtasks) + 1 if plan_obj else 1}",
            description=f"修订: {revision_feedback[:100]}",
            scope=FileScope(writable=[], readable=[]),
            contract={"input": "修订反馈", "output": "修订后代码"},
            acceptance_criteria=["修订内容正确", "回归测试通过"],
            depends_on=[],
        )

    if plan_obj:
        new_subtasks = list(plan_obj.subtasks) + [revision_subtask]
        new_parallel_groups = list(plan_obj.parallel_groups) + [[revision_subtask.id]]
        updated_plan = TaskPlan(
            subtasks=new_subtasks, parallel_groups=new_parallel_groups,
            shared_contract=getattr(plan_obj, "shared_contract", {}) or {},  # B18：保留契约
        )
    else:
        updated_plan = TaskPlan(
            subtasks=[revision_subtask],
            parallel_groups=[[revision_subtask.id]],
        )

    # TD2606-B18：修订计划过去直接 dispatch，绕过 plan 路径的 scope 归一/冲突消解 → 修订子任务
    # 的写权可能与保留的兄弟成果冲突。补做冲突消解（与 plan 路径同源）。
    # ⚠️ resolve_plan_conflicts 【原地变更】plan 并返回计数 dict（内部 step3 已含 normalize_plan_scopes）；
    # 绝不能把返回值赋回 updated_plan（否则 plan 被替换成 dict，state["plan"] 损坏）。
    try:
        from swarm.brain.contract_utils import resolve_plan_conflicts
        # ★复核 H-2★：revision 路径也须传 project_path+钉扎 base，否则 aggregate-vs-新建撞车判定
        # 因 project_path=None 短路成 False → base-pin 要防的 pom 多写者缺陷在 revision 期重开。
        resolve_plan_conflicts(updated_plan,  # 原地变更；返回值(计数 dict)丢弃
                               project_path=_get_project_path(state.get("project_id") or ""),
                               base_ref=state.get("base_commit"))
    except Exception as exc:  # noqa: BLE001
        logger.warning("[REVISION] 计划冲突消解跳过(非致命): %s", exc)

    # 保留已完成子任务的产出 —— 修订只新增一个 rev-* 子任务，不应丢弃此前所有
    # Worker 成果（否则 merge 阶段会丢失未被修订的文件 diff）。仅派发新子任务。
    preserved_results = dict(state.get("subtask_results", {}))
    return {
        "plan": updated_plan,
        "dispatch_remaining": [revision_subtask.id],
        "subtask_results": preserved_results,
        "failed_subtask_ids": [],
        "subtask_retry_counts": {},  # 修订是新一轮，重置重试计数
        # 批4c：修订=重新开始，清历史 escalate 粘滞标记——否则 gates.py:112 对修订成功的
        # 交付永拒 auto_accept、after_merge:285 残留条件把干净合并再送人工
        # （merge_conflicts 粘滞同族，专项取证 CONFIRMED；escalate 分支会按需重新置 True）。
        "failure_escalated": False,
        # S2 复核 F3：REVISE=用户对交付行为不满、预期已变——冻结的验收断言会对抗用户修订
        # （verify_runtime 的幂等复用对已存在 assertions 直接跳过重生成，"reused_existing"）。
        # 清空三键让下一轮 verify_runtime 按修订后的 design/merged_diff 重新生成断言。
        # requirement_items 不动（需求源文本未变，条目 ID 内容 hash 稳定；把修订反馈并入
        # 抽取语料是后续项）。replan（handle_failure）路径【不清】——代码级重做不改需求，
        # 断言挂 requirement item 级，复用省 LLM（幂等复用逻辑只对"本轮已生成"成立）。
        "acceptance_assertions": [],
        "acceptance_passed": None,
        "acceptance_details": {},
    }


_project_delivery_locks: dict[str, "object"] = {}


async def _deliver_merged_diff_serialized(
    proj_path: str, merged_diff: str, base_commit: str | None,
    out_files: list[str], task_id: str | None,
) -> dict:
    """P1d 复核 Finding 1：用 per-project asyncio.Lock 在【事件循环层】序列化同进程交付——
    等待中的同项目交付让出事件循环，不占 to_thread 线程池槽（否则 N 个同项目交付各占一个
    blocked 在 fcntl.flock 的池槽 → 池耗尽 → pull-back/沙箱上传等其它 to_thread 饿死，
    比拆 4 段前更糟）。锁内单次 to_thread 拉起同步交付；内层 _ProjectGitFlock 仍作【跨进程】
    兜底（多 leader 降级场景）。get/set 间无 await → 同一事件循环步内原子，不会各持不同锁。

    ★B6 复核 #1/L-4★：字典键用 canon_path（与 _ProjectGitFlock 同一函数），否则同项目不同拼法的
    proj_path 落不同 asyncio 锁、连 resolve() 异常 fallback 都分裂，进程内串行化失效。"""
    import asyncio as _a
    from swarm.git_base import canon_path
    _key = canon_path(proj_path)
    lock = _project_delivery_locks.get(_key)
    if lock is None:
        lock = _a.Lock()
        _project_delivery_locks[_key] = lock
    async with lock:
        return await _a.to_thread(
            _deliver_merged_diff_locked, proj_path, merged_diff, base_commit, out_files, task_id)


def _deliver_merged_diff_locked(
    proj_path: str, merged_diff: str, base_commit: str | None,
    out_files: list[str], task_id: str | None,
) -> dict:
    """3rd-P1d 治本：交付 git 写临界区（reset→resilient apply→清单对账→commit）在
    per-project flock 内【原子】完成，串行化同项目跨模块并发任务的真仓写。

    根因：plan 后 ModuleLock 从 (project,"default") 升级到 (project,module_key) 并释放 default →
    同项目不同 module 的两任务可同时抵达 learn_success。原实现把 reset/apply/manifest/commit 拆成
    4 段独立 to_thread，段间事件循环可切到另一任务的交付 → git index.lock 互踩 / 交错 commit /
    交付损坏。整段收进 _ProjectGitFlock（跨进程 fcntl，按 project_path 哈希）后，同项目真仓写严格
    串行，不同项目仍并行。同步执行（由调用方单次 to_thread 拉起，flock 在 worker 线程阻塞、不堵事件
    循环）。返回 {ap, wm, commit, out_files}——日志/KB 触发在锁外由调用方按结果处理。"""
    from swarm.brain.integration_review import _reset_worktree_to_head
    from swarm.project.diff_apply import apply_git_diff_resilient, commit_task_output
    from swarm.worker.executor import _ProjectGitFlock

    result: dict = {"ap": {}, "wm": {}, "commit": {}, "out_files": list(out_files)}
    with _ProjectGitFlock(proj_path):
        _reset_worktree_to_head(proj_path, merged_diff, base_commit)
        result["ap"] = apply_git_diff_resilient(proj_path, merged_diff)
        try:
            from swarm.worker.workspace_manifest import reconcile_workspace_manifests
            _wm = reconcile_workspace_manifests(proj_path)
            result["wm"] = _wm
            for _mf in (_wm.get("modified_manifests") or []):
                if _mf not in result["out_files"]:
                    result["out_files"].append(_mf)
        except Exception as _wmexc:  # noqa: BLE001
            result["wm_error"] = str(_wmexc)
        result["commit"] = commit_task_output(proj_path, result["out_files"], task_id=task_id)
    return result


async def learn_success(state: BrainState) -> dict:
    """LEARN_SUCCESS 节点 — 从成功任务中学习并写入 L6/L2"""
    from swarm.brain.learn_store import merge_persist_meta, persist_learn_success

    task_description = state.get("task_description", "")
    plan_obj = state.get("plan")
    merged_diff = state.get("merged_diff", "")
    complexity = effective_complexity(state)  # 修复 12.3：澄清后定级优先
    _degraded: list[str] = []  # 复核 M-3：交付降级信号（如 base 不可达），并入 degraded_reasons 可观测

    # ── 第二批根因(选项A)：产出本地 git commit（单一收口点，覆盖 auto+人工 accept）──
    # accept 后必经 learn_success。worker pull-back 把产出写进工作区但【不 commit】，
    # 后续 git checkout / VERIFY_L2 reset / 下个任务会把未提交产出冲掉 → 事实库滞后丢失。
    # 这里 commit（仅本地，不 push）让产出稳定落盘，且触发已有 git 增量索引链路。
    try:
        if merged_diff.strip():
            proj_path = _get_project_path(state.get("project_id") or "")
            if proj_path:
                # apply/commit 已移入 _deliver_merged_diff_locked（P1d 原子交付）；此处仅需拆文件列表。
                from swarm.project.diff_apply import files_from_unified_diff
                out_files = files_from_unified_diff(merged_diff)
                import asyncio as _asyncio
                # ★治本 round21 Blocker B（全流程推演·post-MERGE 从未触达路径的确定性缺陷）★：
                # 原逻辑"仅补【磁盘缺失】文件"假设"文件还在=worker pull-back 的改好内容仍在"，但
                # VERIFY_L2 的 `_reset_worktree_to_head`(integration_review:197 finally) 编译后已把
                # 【MODIFY 型文件】checkout 回 HEAD(文件仍在、内容=HEAD 原版)→只补缺失会漏掉它们→
                # 按 HEAD 原样 commit → worker 的修改【静默丢弃】。故不再看"是否缺失"，而是【先把
                # merged_diff 涉及文件统一 reset 到 HEAD 干净基线，再 resilient apply】——HEAD-relative
                # 补丁对干净 HEAD 必 apply，new+modify 全部正确落盘，且解决 task 5dc6e634 的"基线已变
                # 冲突"(reset 已消除 pull-back 脏内容，不再冲突)。
                # ★3rd-P1b 治本（与 base-pin 耦合）★：base-pin 后 reset 复位到【钉扎 base】，若运行期
                # 用户/兄弟任务已把 merged_diff 涉及的同名文件 commit 过，reset 会确定性覆盖其改动。
                # 覆盖不阻断交付（任务变更仍产出），但绝不能【静默】——loud 告警 + audit 记录受害文件，
                # 让运维可感知并人工对账（完整 3-way 自动重放留后续增量）。
                _base_commit = state.get("base_commit")
                if _base_commit:
                    try:
                        from swarm.git_base import (
                            base_ref_exists,
                            files_changed_since_base,
                            uncommitted_changed_files,
                            worktree_diverged_from_base,
                        )
                        # ① 已提交偏移：HEAD≠base 且交付文件在 base..HEAD 被中途 commit → reset 覆盖其改动。
                        _diverged, _head = worktree_diverged_from_base(proj_path, _base_commit)
                        if _diverged:
                            _victims = files_changed_since_base(proj_path, _base_commit, out_files)
                            if _victims:
                                logger.warning(
                                    "[LEARN_SUCCESS] ⚠️交付基线偏移：HEAD(%s)≠钉扎 base(%s)，且以下交付文件"
                                    "在此期间被中途 commit → reset 到 base 将覆盖其改动，请人工对账: %s",
                                    (_head or "?")[:12], _base_commit[:12], _victims[:20],
                                )
                                try:
                                    audit(
                                        "delivery_baseline_diverged",
                                        orchestrator="Brain",
                                        task_id=state.get("task_id"),
                                        base_commit=_base_commit[:12],
                                        head=(_head or "")[:12],
                                        clobbered_files=_victims[:50],
                                    )
                                except Exception:  # noqa: BLE001
                                    pass
                        # ② ★B6 #3★ 未提交脏改：HEAD 未动也可能有用户未 commit 的编辑，reset 会静默抹掉。
                        # 先前只比 SHA 漏了此类；此处补探 git status --porcelain 并 loud 告警(非静默)。
                        _dirty = uncommitted_changed_files(proj_path, out_files)
                        if _dirty:
                            logger.warning(
                                "[LEARN_SUCCESS] ⚠️交付文件有【未提交】本地改动，reset 到 base 将丢弃它们，"
                                "请人工对账/先 commit: %s", _dirty[:20],
                            )
                            try:
                                audit("delivery_uncommitted_overwrite", orchestrator="Brain",
                                      task_id=state.get("task_id"), dirty_files=_dirty[:50])
                            except Exception:  # noqa: BLE001
                                pass
                        # ③ ★B6 #4★ 钉扎 base 不可达(GC/历史重写)：merged_diff 相对旧 base 生成,apply 必失败,
                        # 交付基线已损。loud 告警 + audit,让运维知晓本次交付基线可疑(非静默假成功)。
                        if _base_commit != "HEAD" and not base_ref_exists(proj_path, _base_commit):
                            logger.warning(
                                "[LEARN_SUCCESS] ⚠️钉扎 base %s 不可达(GC/历史重写)，merged_diff 相对旧 base "
                                "生成 → 交付 apply 可能失败/不完整，请核验交付物", _base_commit[:12],
                            )
                            _degraded.append("delivery_base_unreachable")  # M-3：并入终态可观测降级
                            try:
                                audit("delivery_base_unreachable", orchestrator="Brain",
                                      task_id=state.get("task_id"), base_commit=_base_commit[:12])
                            except Exception:  # noqa: BLE001
                                pass
                    except Exception as _dvexc:  # noqa: BLE001
                        logger.debug("[LEARN_SUCCESS] 基线偏移检测跳过(非致命): %s", _dvexc)

                # ★3rd-P1d 治本★：reset→resilient apply→清单对账→commit 收进单次 to_thread 内的
                # per-project flock，原子完成——杜绝同项目跨模块并发任务在段间交错真仓写(index.lock
                # 互踩/交错 commit/交付损坏)。resilient 分文件落盘、清单对账口径与原逐段实现逐字节一致。
                _deliv = await _deliver_merged_diff_serialized(
                    proj_path, merged_diff, _base_commit, out_files, state.get("task_id"))
                _ap = _deliv["ap"]
                out_files = _deliv["out_files"]
                if _deliv.get("wm_error"):
                    # 复核 Finding 2：清单对账整体异常（如 /tmp 满 OSError）不再静默 → loud 可观测。
                    logger.warning("[LEARN_SUCCESS] 清单对账异常(非致命,聚合清单可能不一致): %s",
                                   _deliv["wm_error"])
                if not _ap.get("ok"):
                    logger.warning("[LEARN_SUCCESS] commit 前 reset+重放 merged_diff 全失败(非致命): %s",
                                   _ap.get("failed") or _ap.get("stderr", ""))
                    # F5：交付 apply 全失败 = 产物没真正落到本地仓 → 绝不能学成成功模式（下方并入
                    # state.degraded_reasons，should_write_success 据此跳过 L6 写入，防毒化知识库）。
                    _degraded.append("delivery_apply_failed")
                elif _ap.get("failed"):
                    logger.warning("[LEARN_SUCCESS] 分文件落盘：好文件已保留 %d，剔除坏段 %d(交 owner 重修)",
                                   len(_ap.get("applied") or []), len(_ap.get("failed") or []))
                    # F5：部分文件未落（坏段被剔）= 交付不完整 → 同样降级，不作可复用成功模式。
                    _degraded.append("delivery_apply_incomplete")
                if (_deliv.get("wm") or {}).get("modified_manifests"):
                    logger.info("[LEARN_SUCCESS] 交付前对账聚合清单成员并纳入提交: %s",
                                _deliv["wm"].get("added"))
                _c = _deliv["commit"]
                if _c.get("committed"):
                    logger.info("[LEARN_SUCCESS] 产出已本地 commit: %s (%d 文件)",
                                _c.get("commit_hash"), len(out_files))
                elif not _c.get("ok"):
                    logger.warning("[LEARN_SUCCESS] 产出 commit 跳过(非致命): %s", _c.get("reason"))
                    # F5：真 commit 错误(ok=False，区别于 committed=False 的 no-op/无改动)= 产物未固化
                    # 到本地仓历史 → 降级，不学成成功模式（no-op/nothing-to-commit 不算，ok=True 不入此支）。
                    _degraded.append("delivery_commit_failed")
                # ★对抗复核 3rd#1 + Finding 2 治本★：KB 增量索引在此触发——磁盘=已 apply 的最终产出
                # （非 L2 回滚后 HEAD 旧内容），覆盖 auto+manual accept 两条路径。触发条件用 `ok`（含
                # "无改动可提交"的合法 no-op）而非 `committed`——否则 commit 报 no-op/nothing-to-commit
                # 时会静默漏掉整任务 KB 更新（Finding 2 回归）。仅【真 commit 错误(ok=False)】才跳过。
                if _c.get("ok"):
                    try:
                        from swarm.knowledge.hooks import schedule_incremental_update
                        schedule_incremental_update(
                            state.get("project_id") or "", proj_path, merged_diff,
                            task_id=state.get("task_id"),
                        )
                    except Exception as _kbexc:  # noqa: BLE001
                        logger.warning("[LEARN_SUCCESS] KB 增量索引触发失败(知识库本次未更新): %s", _kbexc)
                else:
                    logger.warning("[LEARN_SUCCESS] commit 失败 → KB 增量索引跳过（知识库本次未更新）: %s",
                                   _c.get("reason"))
    except Exception as exc:  # noqa: BLE001
        logger.warning("[LEARN_SUCCESS] 产出 commit 异常(非致命): %s", exc)

    logger.info("[LEARN_SUCCESS] 提炼成功模式")

    parsed: dict = {}
    if complexity == Complexity.SIMPLE:
        parsed = {
            "pattern_name": "trivial-edit",
            "pattern_description": f"简单任务成功: {task_description[:120]}",
            "complexity": complexity.value,
        }
        learn_summary = json.dumps(parsed, ensure_ascii=False)
    else:
        # ── LLM 成功模式提炼 ──
        try:
            llm = _get_brain_llm()
            # D50：瘦身 plan 注入（剥子任务 contract/context_snippets），同 validate_plan 口径。
            from swarm.brain.plan_validator import slim_plan_json_or_empty
            plan_json = slim_plan_json_or_empty(plan_obj)
            prompt_user = LEARN_SUCCESS_USER.format(
                task_description=task_description,
                plan_json=plan_json,
                merged_diff=merged_diff[:4000],
                complexity=_complexity_str(complexity),
            )
            response = await llm.ainvoke([
                {"role": "system", "content": LEARN_SUCCESS_SYSTEM},
                {"role": "user", "content": prompt_user},
            ])
            parsed = _parse_json_from_llm(response.content)
            learn_summary = json.dumps(parsed, ensure_ascii=False)
        except json.JSONDecodeError as e:
            logger.warning(f"[LEARN_SUCCESS] LLM 输出 JSON 解析失败，使用原始输出: {e}")
            parsed = {
                "pattern_name": f"成功模式-{_complexity_str(complexity)}",
                "pattern_description": f"任务 '{task_description[:50]}' 的成功执行模式",
            }
            learn_summary = json.dumps(parsed, ensure_ascii=False)
        except Exception as e:
            parsed = {
                "pattern_name": f"成功模式-{_complexity_str(complexity)}",
                "pattern_description": f"任务 '{task_description[:50]}' 的成功执行模式",
                "error": str(e),
            }
            learn_summary = json.dumps(parsed, ensure_ascii=False)

    # F5 治本：本节点内交付阶段探到的降级（base 不可达 / apply 全失败 / apply 不完整 / commit
    # 失败）必须在 persist 之前并入 state.degraded_reasons——否则 should_write_success 只看 state
    # 里【进本节点前】的旧 degraded，本轮交付真出问题却仍被学成 L6 成功模式（记忆毒化）。原 _degraded
    # 只在 2359 并入返回值(终态可观测)，晚于此处 persist，对成功判据是死信号。就地并入供守卫读取。
    if _degraded:
        state["degraded_reasons"] = list(state.get("degraded_reasons") or []) + _degraded
    persist_meta = await persist_learn_success(state, parsed)
    learn_summary = merge_persist_meta(learn_summary, persist_meta)

    mr_url = ""
    if os.environ.get("SWARM_GITLAB_MR_ON_ACCEPT", "false").lower() in ("1", "true", "yes"):
        from swarm.brain.l3_gitlab import create_merge_request, gitlab_configured

        if gitlab_configured() and merged_diff.strip():
            task_id = state.get("task_id", "")
            title = f"swarm: {task_description[:80]}"
            body = (
                f"Swarm 任务 `{task_id}` 自动 MR\n\n"
                f"复杂度: {_complexity_str(complexity)}\n\n"
                f"L2: {state.get('l2_passed')}\n"
                f"L3: {state.get('l3_message') or state.get('l3_passed')}\n"
            )
            source_branch = state.get("l3_branch") or f"swarm/task-{task_id[:12]}"
            mr_url, mr_err = create_merge_request(
                title=title,
                description=body,
                source_branch=source_branch,
                task_id=task_id,
            )
            if mr_url:
                logger.info("[LEARN_SUCCESS] MR 已创建: %s", mr_url)
                learn_summary = merge_persist_meta(
                    learn_summary, {"mr_url": mr_url}
                )
            elif mr_err:
                logger.warning("[LEARN_SUCCESS] MR 创建失败: %s", mr_err)

    # 批5：event_bus.publish_kb_event 已删——Redis stream swarm:kb_events 全仓无
    # xread 消费者，纯写黑洞（知识增量真正的驱动是 PG kb_update_events 队列）。

    logger.info("[LEARN_SUCCESS] 学习完成 (persisted=%s)", persist_meta.get("persisted"))
    _out: dict = {
        "learned": True,
        "learn_summary": learn_summary,
    }
    if _degraded:
        _out["degraded_reasons"] = _degraded  # reducer append+dedup，终态可观测（M-3）
    return _out


async def learn_failure(state: BrainState) -> dict:
    """LEARN_FAILURE 节点 — 从失败任务中学习并写入 L5/L2"""
    from swarm.brain.learn_store import merge_persist_meta, persist_learn_failure

    task_description = state.get("task_description", "")
    plan_obj = state.get("plan")
    revision_feedback = state.get("revision_feedback", "")
    failed_ids = state.get("failed_subtask_ids", [])

    logger.info("[LEARN_FAILURE] 提炼错误模式")

    parsed: dict = {}
    # ── LLM 错误模式提炼 ──
    try:
        llm = _get_brain_llm()
        # D50：瘦身 plan 注入（剥子任务 contract/context_snippets），同 validate_plan 口径。
        from swarm.brain.plan_validator import slim_plan_json_or_empty
        plan_json = slim_plan_json_or_empty(plan_obj)
        prompt_user = LEARN_FAILURE_USER.format(
            task_description=task_description,
            plan_json=plan_json,
            revision_feedback=revision_feedback,
            failed_subtask_ids=failed_ids,
        )
        response = await llm.ainvoke([
            {"role": "system", "content": LEARN_FAILURE_SYSTEM},
            {"role": "user", "content": prompt_user},
        ])
        parsed = _parse_json_from_llm(response.content)
        learn_summary = json.dumps(parsed, ensure_ascii=False)
    except json.JSONDecodeError as e:
        logger.warning(f"[LEARN_FAILURE] LLM 输出 JSON 解析失败，使用默认: {e}")
        parsed = {
            "mistake_name": "错误模式",
            "mistake_description": f"任务 '{task_description[:50]}' 的失败模式",
            "root_cause": revision_feedback[:100] if revision_feedback else "未知",
        }
        learn_summary = json.dumps(parsed, ensure_ascii=False)
    except Exception as e:
        parsed = {
            "mistake_name": "错误模式",
            "mistake_description": f"任务 '{task_description[:50]}' 的失败模式",
            "root_cause": str(e),
        }
        learn_summary = json.dumps(parsed, ensure_ascii=False)

    persist_meta = await persist_learn_failure(state, parsed)
    learn_summary = merge_persist_meta(learn_summary, persist_meta)

    logger.info("[LEARN_FAILURE] 学习完成 (persisted=%s)", persist_meta.get("persisted"))
    return {
        "learned": True,
        "learn_summary": learn_summary,
    }
