"""Brain 节点函数 — LangGraph 状态机的所有节点实现

每个节点是一个函数: (BrainState) -> dict
返回的 dict 会被 merge 回 BrainState。

真实 LLM 调用 + mock fallback：每个节点优先调用 Brain LLM，
失败时回退到原有 mock 逻辑。
"""

from __future__ import annotations

import asyncio
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
    HANDLE_FAILURE_SYSTEM,
    HANDLE_FAILURE_USER,
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
    _parse_json_from_llm,
    _planning_triage,
    _worker_profile_prompt,
    parse_and_validate,
)
from swarm.brain.llm_schemas import (  # noqa: E402
    ComplexityAssessmentResponse,
    FailureStrategyResponse,
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
    """获取 Brain LLM 实例"""
    router = ModelRouter()
    return router.get_brain_llm()






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
        recent_summaries = await load_recent_task_summaries(project_id)
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


async def _plan_ultra_batched(
    llm, state, task_description, complexity, routing_table,
    knowledge_context, knowledge_prompt, recent_tasks_prompt, sliding_ctx, file_plan,
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
        dedupe_file_plan,
        group_into_module_batches,
        merge_subtask_batches,
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
    _PLAN_BATCH_TIMEOUT = 300.0  # 秒/批（正常 ≤171s，留 ~1.7x 余量，失控时 5min 截断降级）
    batch_results: list[list[dict]] = []
    failed_batches = 0
    # 各模块批【独立分解】(生成时无跨批依赖，跨批串行依赖在 merge 阶段才加)→ 并发执行。
    # 实测本地 40B 后端(2×5090 连续批处理)8 并发仍近线性(1.46×)，保守取 4 并发，留足余量。
    # 串行 11 批 ~20min → 并发4 ~5min。gather 保序：结果按 module_batches 顺序收集，merge 全局
    # 编号与串行版一致。逐批超时/异常降级与原行为逐字节一致(返回标记，主循环统一计 failed_batches)。
    _plan_sem = _asyncio.Semaphore(4)

    async def _decompose_batch(i: int, mod_name: str, batch: list) -> tuple:
        batch_fp_text = "\n".join(
            f"  - {fp.get('path')} [{fp.get('action', 'create')}] {fp.get('responsibility', '')}"
            for fp in batch
        )
        # P3：判断该模块是否为新模块（文件路径顶层目录不在现有目录里）
        top_dirs = {(fp.get("path") or "").replace("\\", "/").split("/")[0]
                    for fp in batch if fp.get("path")}
        new_module_dirs = [d for d in top_dirs if d and d not in existing_dirs]
        scaffold_hint = ""
        if new_module_dirs:
            scaffold_hint = (
                f"\n\n【重要-P3 新模块脚手架】本模块涉及新建模块目录 {new_module_dirs}，"
                f"项目中尚不存在。请在本批【第一个子任务】先创建该模块的基础设施"
                f"（如 Maven 模块的 pom.xml 并注册到父 pom 的 <modules>、基础目录结构），"
                f"该模块其他子任务 depends_on 这个脚手架子任务。")
        prompt_user = PLAN_BATCH_USER.format(
            task_description=task_description[:2000],
            batch_idx=i, total_batches=total,
            batch_file_plan=f"模块 '{mod_name}'：\n{batch_fp_text}{scaffold_hint}",
            project_structure=proj_struct,
            tech_design_extra=tech_design_extra,
        )
        async with _plan_sem:
            _t0 = _time.monotonic()
            try:
                response = await _asyncio.wait_for(
                    llm.ainvoke([
                        {"role": "system", "content": PLAN_BATCH_SYSTEM},
                        {"role": "user", "content": prompt_user},
                    ]),
                    timeout=_PLAN_BATCH_TIMEOUT,
                )
                _dt = _time.monotonic() - _t0
                result = _parse_json_from_llm(response.content)
                subs = result.get("subtasks", []) if isinstance(result, dict) else []
                for _st in subs:
                    if isinstance(_st, dict):
                        for _opt in ("harness", "contract"):
                            if _opt in _st and _st[_opt] is None:
                                _st.pop(_opt)
                return ("ok", i, mod_name, subs, _dt, len(batch))
            except _asyncio.TimeoutError:
                return ("timeout", i, mod_name, None, None, len(batch))
            except Exception as exc:  # noqa: BLE001
                return ("error", i, mod_name, exc, None, len(batch))

    # gather 按输入顺序返回 → 保持 module_batches(模块依赖序)的批次顺序
    _outcomes = await _asyncio.gather(*[
        _decompose_batch(i, mod_name, batch)
        for i, (mod_name, batch) in enumerate(module_batches, start=1)
    ])
    for kind, i, mod_name, payload, _dt, _nfiles in _outcomes:
        if kind == "ok" and payload:
            batch_results.append(payload)
            logger.info("%s 模块'%s' 拆出 %d 个子任务",
                        batch_progress_line(i, total, _nfiles, _dt), mod_name, len(payload))
        elif kind == "ok":
            failed_batches += 1
            logger.warning("%s 模块'%s' 未拆出子任务（降级跳过）",
                           batch_progress_line(i, total, _nfiles, _dt), mod_name)
        elif kind == "timeout":
            failed_batches += 1
            logger.warning(
                "%s 模块'%s' LLM 调用超时 >%.0fs（降级跳过，防 PLAN 无限挂 — FINDING-10）",
                batch_progress_line(i, total, _nfiles), mod_name, _PLAN_BATCH_TIMEOUT)
        else:
            failed_batches += 1
            logger.warning("%s 模块'%s' 拆解异常（降级跳过）: %s",
                           batch_progress_line(i, total, _nfiles), mod_name, payload)

    merged = merge_subtask_batches(batch_results)
    logger.info(
        "[PLAN-BATCH] 按模块分批完成：%d/%d 模块成功，合并出 %d 个子任务（失败 %d）",
        total - failed_batches, total, len(merged), failed_batches,
    )
    if not merged:
        return TaskPlan(subtasks=[SubTask(
            id="st-1", description=task_description,
            difficulty=SubTaskDifficulty.MEDIUM, modality=SubTaskModality.TEXT,
            scope=FileScope(writable=[], readable=[]), contract={},
        )])
    # N-03 兼容：万一 LLM 仍吐旧键 acceptance（SubTask 字段是 acceptance_criteria，
    # extra=ignore 会静默丢弃致验收恒空），重映射后再构造。
    for st in merged:
        if isinstance(st, dict) and "acceptance" in st and "acceptance_criteria" not in st:
            st["acceptance_criteria"] = st.pop("acceptance")
    return TaskPlan(subtasks=[SubTask(**st) for st in merged])


async def plan(state: BrainState) -> dict:
    """PLAN 节点 — 将任务拆解为子任务 DAG

    输入: task_description, complexity, knowledge_context
    输出: plan
    """
    task_description = state.get("task_description", "")
    # 优先用澄清后定级(assess)，回退 analyze 初判
    complexity = state.get("assessed_complexity") or state.get("complexity", Complexity.MEDIUM)
    # checkpoint resume 后枚举会反序列化成字符串("ultra")——这里统一归一为 Complexity 枚举，
    # 否则下游 complexity.value / == Complexity.X 触发 AttributeError（task 8537fa5e 真因，
    # 与 ASSESS 308cd191 同类：interrupt→resume 路径上状态枚举退化为 str）。
    if not isinstance(complexity, Complexity):
        try:
            complexity = Complexity(str(complexity).lower())
        except ValueError:
            complexity = Complexity.MEDIUM
    knowledge_context = state.get("knowledge_context", {})

    # I3 防 premature victory：检测 replan 重入——若 state 已有 subtask_results（说明这是
    # handle_failure(replan) / confirm(revise) 触发的重新规划，非首次），则旧的完成态事实表
    # 不可信（新 plan 可能复用旧子任务 id 但语义已变，旧"成功"结果会让新子任务被误判已完成
    # 而跳过执行 = premature victory）。replan 语义 = 一切重来，确定性清空完成态 + 派发队列，
    # 让新 plan 的所有子任务都重新派发。完成态只由 dispatch 基于真实 WorkerOutput 重新写。
    _replan_reset: dict = {}
    if state.get("subtask_results"):
        logger.info(
            "[PLAN] 检测到 replan 重入（已有 %d 个旧完成态）→ 清空完成态事实表，"
            "防 premature victory（新 plan 子任务全部重新派发）",
            len(state.get("subtask_results") or {}),
        )
        _replan_reset = {
            "subtask_results": {},
            "dispatch_remaining": [],
            "failed_subtask_ids": [],
        }

    logger.info(f"[PLAN] 拆解任务 (复杂度={complexity.value})")

    if complexity == Complexity.SIMPLE:
        affected_files = state.get("affected_files") or []
        _proj_path = _get_project_path(state.get("project_id") or "")
        task_plan = _build_simple_plan(task_description, affected_files, project_path=_proj_path)
        logger.info(
            "[PLAN] SIMPLE 快速路径 — 1 个 trivial 子任务 (scope=%d 文件)",
            len(affected_files),
        )
        from swarm.brain.contract_utils import enrich_plan_with_shared_contract

        task_plan = enrich_plan_with_shared_contract(task_plan)
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
            **_replan_reset,
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
    sliding_ctx = sliding_context_prompt(state)

    # P0-2：replan 重入时把上轮失败原因拼进上下文，引导 LLM 避开同样的坏计划
    # （见 task 0f93f1fc：replan 后 LLM 看不到"依赖悬空/scope 冲突"原因 → 原样重生成）。
    _replan_feedback = (state.get("replan_feedback") or "").strip()
    if _replan_feedback:
        sliding_ctx = (
            f"⚠️ 上一轮规划执行失败，本次为重新规划（第 {state.get('replan_count', 1)} 次）。\n"
            f"上轮失败根因（务必规避，不要重复同样的拆分/依赖/scope 错误）：\n"
            f"{_replan_feedback}\n\n"
            + (sliding_ctx or "")
        )
        logger.info("[PLAN] replan 重入 — 已注入上轮失败原因供 LLM 规避")

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
            task_plan = await _plan_ultra_batched(
                llm, state, task_description, complexity, routing_table,
                knowledge_context, knowledge_prompt, recent_tasks_prompt,
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
                from swarm.brain.plan_batch import dedupe_subtasks
                result["subtasks"] = dedupe_subtasks(result.get("subtasks", []) or [])
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

    from swarm.brain.contract_utils import enrich_plan_with_shared_contract

    # T1：把 contract_design 节点产出的全局共享契约(state.shared_contract_draft)注入 plan，
    # 再 enrich 进每个子任务的 contract → worker 执行时看到统一契约，避免各写各的接口对不上。
    _contract = state.get("shared_contract_draft") or {}
    if _contract and not (task_plan.shared_contract or {}):
        task_plan.shared_contract = _contract
    elif _contract and isinstance(task_plan.shared_contract, dict):
        # PLAN LLM 自带了 shared_contract（无 dependencies）会盖掉 contract_design 的草案。
        # dependencies 是编译期硬契约（Rule5 据此把模块依赖并集落进 pom owner 验收），
        # 绝不能被丢——草案有、plan 自身没有时补进去（其余键以 plan 自身为准，不动）。
        if _contract.get("dependencies") and not task_plan.shared_contract.get("dependencies"):
            task_plan.shared_contract["dependencies"] = _contract["dependencies"]
    task_plan = enrich_plan_with_shared_contract(task_plan)

    # T3：同文件写权唯一——消除"同一文件被多个子任务并发写"的冲突（写权保留首个，
    # 其余降级为 readable）+ 被依赖产物自动入域。防多 worker 同时编辑同一文件互相覆盖。
    from swarm.brain.contract_utils import normalize_plan_scopes
    if normalize_plan_scopes(task_plan):
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
        ),
        # TD2606-A5：规划 LLM 失败时上面产出的是空 scope「无验证」兜底假计划。打专用标记，
        # 让 can_auto_accept_plan fail-fast 拦下，绝不让它静默 dispatch → 空 diff → 假 DONE。
        # （_plan_degraded 仅在两条 except 失败分支被赋值，故等价于"规划生成失败"。）
        "plan_generation_failed": _plan_degraded is not None,
        **_replan_reset,
        **plan_touch,
    }


async def validate_plan(state: BrainState) -> dict:
    """VALIDATE_PLAN 节点 — PlanValidator 硬校验 + 可选 LLM 补充

    输入: plan, task_description, affected_files
    输出: plan_valid, plan_validation_issues
    """
    from swarm.brain.plan_validator import validate_plan_structure

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
        }

    if effective_complexity(state) == Complexity.SIMPLE:  # 修复 12.3：澄清后定级优先
        logger.info("[VALIDATE_PLAN] SIMPLE 快速路径 — 结构验证通过")
        return {
            "plan_valid": True,
            "plan_retry_count": retry_count,
            "plan_validation_issues": [],
        }

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
        llm = _get_brain_llm()
        plan_json = plan_obj.model_dump_json(indent=2)
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
    return {
        "plan_valid": plan_valid,
        "plan_retry_count": retry_count,
        "plan_validation_issues": [] if plan_valid else (llm_issues or ["LLM 计划验证未通过"]),
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
    _plan_valid = state.get("plan_valid", True)
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
            else:
                _vf = "plan_invalid"
            _patch = {
                "human_decision": HumanDecision.REJECT,
                "confirm_reason": _reason,
                "verification_failure": _vf,
            }
            # tech_design 残缺 / 规划生成失败 → 升级人工(escalate)，与"计划非法"一样 fail-fast
            # 但归因区分，绝不静默成功。
            if _vf in ("tech_design_incomplete", "plan_generation_failed"):
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
            "degraded_reasons": state.get("degraded_reasons") or [],
            "message": _msg,
        }
    )

    # decision 可能是字符串 "accept"/"reject" 或 HumanDecision
    if isinstance(decision, str):
        human_decision = HumanDecision(decision)
    elif isinstance(decision, dict) and "decision" in decision:
        human_decision = HumanDecision(decision["decision"])
    else:
        human_decision = HumanDecision.ACCEPT  # 默认接受

    logger.info(f"[CONFIRM] 人工决策: {human_decision.value}")
    return {"human_decision": human_decision}






async def _run_security_audit(
    subtask: SubTask,
    project_path: str | None,
    *,
    project_id: str = "",
    task_id: str = "",
) -> WorkerOutput:
    """AUDIT 意图执行分支：跑安全扫描，产结构化报告(不产 diff)。

    阻断/仅报告双模式由 WorkerConfig.security_block_severity 控制：
    - critical/high：发现该级别漏洞 → should_block → l1_passed=False(阻断交付)
    - none：仅报告，永不阻断(l1_passed=True)
    """
    import asyncio as _asyncio

    from swarm.config.settings import get_config

    lang = getattr(getattr(subtask, "harness", None), "language", "") or ""
    block_severity = get_config().worker.security_block_severity

    audit(
        "security_audit_start",
        orchestrator="Brain",
        executor="Worker",
        task_id=task_id,
        subtask_id=subtask.id,
        language=lang,
        block_severity=block_severity,
    )

    # N-01 fail-closed 判据：仅用于【扫描器崩溃】路径——我们【有】东西可扫但扫挂了，
    # 在阻断模式(block_severity != "none")下"扫不了"绝不能与"真·零漏洞"混同放行。
    # report-only(none)模式是运维明示"永不阻断"，此时保持不阻断(可观测性不误杀)。
    # 注意：无 project_path 是【编排未提供可扫对象】(非攻击面/非扫描失败)，按既有契约安全跳过。
    _audit_fail_closed = block_severity != "none"

    if not project_path:
        logger.warning("[AUDIT] 子任务 %s 无项目路径，安全审计跳过", subtask.id)
        return WorkerOutput(
            subtask_id=subtask.id,
            diff="",
            summary="安全审计跳过：无项目路径",
            confidence=Confidence.LOW,
            l1_passed=True,  # 无路径=无可扫对象，安全跳过不误杀（既有契约）
            l1_details={"mode": "audit", "skipped": "no_project_path"},
            audit_findings=[],
        )

    def _scan() -> tuple[list, bool]:
        from swarm.worker.security_scan import run_security_scan

        scope_files = list(
            getattr(subtask.scope, "writable", []) or []
        ) + list(getattr(subtask.scope, "readable", []) or [])
        return run_security_scan(
            project_path,
            lang,
            files=scope_files or None,
            block_severity=block_severity,
        )

    try:
        findings, should_block = await _asyncio.get_running_loop().run_in_executor(None, _scan)
    except Exception as exc:  # noqa: BLE001
        logger.error("[AUDIT] 安全扫描失败: %s (fail_closed=%s)", exc, _audit_fail_closed)
        return WorkerOutput(
            subtask_id=subtask.id,
            diff="",
            summary=f"安全审计执行失败: {exc}",
            confidence=Confidence.LOW,
            # N-01：阻断模式下扫描器崩溃→fail-closed(不可与"真零漏洞"混同)；none 模式不阻断
            l1_passed=not _audit_fail_closed,
            l1_details={
                "mode": "audit",
                "error": str(exc),
                "fail_closed": _audit_fail_closed,
                "block_severity": block_severity,
            },
            audit_findings=[],
        )

    by_sev: dict[str, int] = {}
    for f in findings:
        sev = f.severity.value if hasattr(f.severity, "value") else str(f.severity)
        by_sev[sev] = by_sev.get(sev, 0) + 1
    summary = (
        f"安全审计完成：{len(findings)} 项发现 "
        f"({', '.join(f'{k}={v}' for k, v in sorted(by_sev.items())) or '无'})"
        f"；block_severity={block_severity} → {'阻断交付' if should_block else '通过'}"
    )
    audit(
        "security_audit_done",
        orchestrator="Brain",
        executor="Worker",
        task_id=task_id,
        subtask_id=subtask.id,
        findings=len(findings),
        should_block=should_block,
        by_severity=by_sev,
    )
    logger.info("[AUDIT] %s | %s", subtask.id, summary)
    return WorkerOutput(
        subtask_id=subtask.id,
        diff="",  # 审计不产 diff
        summary=summary,
        confidence=Confidence.HIGH,
        l1_passed=not should_block,  # 阻断模式下有高危发现即 L1 不通过
        l1_details={
            "mode": "audit",
            "l1_decision_source": "deterministic",
            "findings_total": len(findings),
            "by_severity": by_sev,
            "block_severity": block_severity,
            "should_block": should_block,
        },
        audit_findings=findings,
    )


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




def _l1_details_of(subtask_results: dict, fid: str) -> dict:
    """取子任务的 L1 详情(含 build_output/编译标志),WorkerOutput / dict 两种形态兼容。"""
    out = subtask_results.get(fid)
    if isinstance(out, WorkerOutput):
        return out.l1_details or {}
    if isinstance(out, dict):
        return out.get("l1_details", {}) or {}
    return {}


def _widen_scope_for_compile_repair(plan_obj, fid: str, details: dict) -> list[str]:
    """治本(RUN16 st-20 死循环)：子任务编译失败、但【根因在其 scope 之外】(模块 pom 缺依赖 /
    上游文件签名不符)→ 该子任务 scope 改不到那些文件 → 重试永远编不过 → 死循环。

    重试前把根因文件纳入该子任务 writable scope,让重试能真正修：
      1. 模块 pom.xml(从子任务文件推断 <module>/pom.xml)——治"缺依赖/包不存在"(报错只点症状文件、
         不点 pom,故无条件补模块 pom)。
      2. 编译错误输出里【点名的项目文件】(.java/.xml,去 /workspace/ 前缀)——治"上游接口缺方法/缺类"。
    仅在确实是编译失败时加宽,返回新增文件列表(空=未加宽)。pom 多写者由 normalize 串行化,安全。
    """
    if not plan_obj or not getattr(plan_obj, "subtasks", None) or not details:
        return []
    build_ok = details.get("l1_2_1_build_ok", details.get("l1_2_compile_ok"))
    build_out = str(details.get("build_output") or "")
    is_compile_fail = (build_ok is False) or ("COMPILATION" in build_out) or ("cannot find symbol" in build_out)
    if not is_compile_fail:
        return []
    st = next((s for s in plan_obj.subtasks if getattr(s, "id", None) == fid), None)
    scope = getattr(st, "scope", None) if st else None
    if not scope:
        return []
    import re as _re
    cur = set(getattr(scope, "writable", []) or []) | set(getattr(scope, "create_files", []) or [])
    add: set[str] = set()
    # 1) 模块 pom：从已 scope 文件的 "<module>/src/" 推断 <module>/pom.xml
    for f in cur:
        m = _re.match(r"(.+?)/src/", f.replace("\\", "/"))
        if m:
            add.add(f"{m.group(1)}/pom.xml")
    # 2) 编译报错点名的项目文件(绝对沙箱路径去 /workspace/ 前缀)
    for m in _re.finditer(r"/workspace/([\w./\-]+\.(?:java|xml))", build_out):
        add.add(m.group(1))
    new = sorted(f for f in add if f not in cur)
    if new and st is not None:
        scope.writable = list(getattr(scope, "writable", []) or []) + new
    return new


# ── P0-B/P1-D：scope 不可满足的编译失败（缺依赖/缺符号）识别 + 定向恢复（task f9e38dae）──
# 现场：st-24 用 RedisTemplate 但 ruoyi-alarm/pom.xml 没声明依赖、pom 又不在 st-24 scope →
# 原地重试 N 次必败（数学上不可满足）→ 耗尽配额 → 落全量 replan 清空 23 个完成态。治本：识别
# 这类"缺符号/缺依赖"失败，给失败子任务补其【模块 pom】写权 + 重置徒劳的重试计数，只重派失败
# 子任务（保留成功兄弟），让 worker 拿到编译错误 + pom 写权后真正补依赖，而非推倒重来。
# 仅保留【缺依赖/缺符号】的特异信号，杜绝 "does not exist"/"无法访问" 这类宽串误伤
# （会命中 "User does not exist"/"table does not exist"/Java 模块可见性 "cannot access" 等
# 非依赖失败 → 误授 pom 写权、空烧定向恢复配额）。各语言 javac/go/rustc/py/node 的缺包特征：
_MISSING_DEP_PATTERNS = (
    "cannot find symbol",      # javac (en)
    "找不到符号",               # javac (zh)
    "程序包",                   # javac (zh): "程序包 xxx 不存在"
    "package does not exist",  # javac (en): "package xxx does not exist"
    "cannot find package",     # go
    "unresolved import",       # rust / python 工具链
    "no module named",         # python ImportError
    "module not found",        # node
)


def _is_missing_dependency_failure(subtask_results: dict, failed_ids: list) -> bool:
    """失败详情里是否命中"缺符号/缺依赖"编译特征（确定性、零 LLM）。"""
    for fid in failed_ids:
        out = subtask_results.get(fid)
        if isinstance(out, WorkerOutput):
            det = out.l1_details or {}
        elif isinstance(out, dict):
            det = out.get("l1_details", {}) or {}
        else:
            det = {}
        try:
            blob = json.dumps(det, ensure_ascii=False).lower()
        except (TypeError, ValueError):
            blob = str(det).lower()
        if any(p in blob for p in _MISSING_DEP_PATTERNS):
            return True
    return False


# 治本 C：流式 stall（模型服务并发拥塞，_DualTimeoutChatOpenAI 抛 TransientInfraError 的特征词）。
_STREAM_STALL_MARKERS = ("stream stall", "解码中途", "首 token(prefill)", "stream stall timeout")


def _has_stream_stall(subtask_results: dict, ids: list) -> bool:
    """失败详情里是否有【流式 stall】特征——据此给更长退避，让模型服务并发拥塞散去再重试。"""
    for fid in ids or []:
        out = (subtask_results or {}).get(fid)
        if isinstance(out, WorkerOutput):
            det, extra = (out.l1_details or {}), (out.summary or "")
        elif isinstance(out, dict):
            det, extra = (out.get("l1_details", {}) or {}), (out.get("summary", "") or "")
        else:
            det, extra = {}, ""
        try:
            blob = json.dumps(det, ensure_ascii=False) + extra
        except (TypeError, ValueError):
            blob = str(det) + extra
        if any(m in blob for m in _STREAM_STALL_MARKERS):
            return True
    return False


# 顶层不是【模块目录】的常见前缀——取模块名时跳过，避免把 src/test 误当模块（MEDIUM-1）。
_NON_MODULE_TOP = ("src", "test", "target", "build", "dist", "out", "node_modules")


def _module_of(files: list) -> str | None:
    """从文件路径列表取顶层【模块目录】（首个含 '/' 且首段不是 src/test 等的路径）。"""
    for f in files or []:
        if "/" in f:
            top = f.split("/", 1)[0]
            if top and top not in _NON_MODULE_TOP:
                return top
    return None


def _reaches(by_id: dict, start: str, target: str) -> bool:
    """start 是否经 depends_on 链（传递）到达 target——用于加边前防环（HIGH-4）。"""
    seen, stack = set(), [start]
    while stack:
        cur = stack.pop()
        if cur == target:
            return True
        if cur in seen:
            continue
        seen.add(cur)
        st = by_id.get(cur)
        if st is not None:
            stack.extend(getattr(st, "depends_on", []) or [])
    return False


def _add_dep_safe(by_id: dict, dependent: str, dep: str) -> bool:
    """给 dependent 加 depends_on=dep，带传递防环（dep 已传递依赖 dependent 则不加）。"""
    if dependent == dep:
        return False
    cur = by_id.get(dependent)
    if cur is None:
        return False
    existing = list(getattr(cur, "depends_on", []) or [])
    if dep in existing:
        return False
    if _reaches(by_id, dep, dependent):  # dep 已能到达 dependent → 加边会成环
        return False
    cur.depends_on = existing + [dep]
    return True


# ── 治本 A2：缺依赖确定性补全（据项目自身 pom 自证坐标，不靠小模型、不臆造） ──
# 定向恢复给了失败子任务模块 pom 写权，但小模型仍常不会把缺的依赖加进去（实测 RuoYi st-31：
# 用 org.quartz 但 ruoyi-alarm/pom.xml 没声明 → 2 次定向恢复耗尽 → 落全量 replan 砸掉 30 个成功）。
# 这里在【授权后立即】确定性补：从编译错误取缺失包 → 在项目【其它 pom】里找声明了它的 <dependency>
# 块（项目自己用过=权威坐标）→ 注入失败模块 pom。项目从没用过该包 → 查无、不动（不臆造坐标）。
_MAVEN_GENERIC_SEG = {"org", "com", "net", "io", "cn", "www", "java", "javax",
                      "jakarta", "apache", "springframework", "google"}
_MISSING_PKG_BRAIN_RE = re.compile(
    r"(?:程序包|package)\s+([\w.]+)\s+(?:不存在|does not exist)", re.I)
_DEP_BLOCK_RE = re.compile(r"<dependency>([\s\S]*?)</dependency>", re.I)
_ARTIFACT_RE = re.compile(r"<artifactId>\s*([^<\s]+)\s*</artifactId>", re.I)
_GROUP_RE = re.compile(r"<groupId>\s*([^<\s]+)\s*</groupId>", re.I)


def _pkg_match_tokens(pkg: str) -> list[str]:
    """从缺失包名提取可匹配 Maven artifactId/groupId 的辨识 token（去通用段、去数字后缀变体）。
    org.quartz→['quartz']；okhttp3.x→['okhttp3','okhttp']；com.fasterxml.jackson.databind→['fasterxml','jackson','databind']。"""
    toks: list[str] = []
    for s in [s for s in pkg.split(".") if s]:
        if s in _MAVEN_GENERIC_SEG or len(s) <= 2:
            continue
        if s not in toks:
            toks.append(s)
        st = s.rstrip("0123456789")
        if st and st != s and st not in toks:
            toks.append(st)
    return toks


def _extract_missing_pkgs(blob: str) -> list[str]:
    """从编译错误文本解析缺失包名（确定性）。"""
    seen: set = set()
    out: list[str] = []
    for m in _MISSING_PKG_BRAIN_RE.finditer(blob or ""):
        p = m.group(1)
        # 不强求含 "."：okhttp3 这类单段包名也是合法缺失包（实测 st-17）。正则上下文
        # （程序包 X 不存在）已足够特定，X 必是包名。
        if p and len(p) >= 3 and p not in seen:
            seen.add(p)
            out.append(p)
    return out


def _iter_project_poms(project_path: str, limit: int = 80) -> list:
    skip = {"target", "node_modules", ".git", "build", "dist", ".gradle", ".idea"}
    out: list = []
    try:
        for p in Path(project_path).rglob("pom.xml"):
            if any(part in skip for part in p.relative_to(project_path).parts):
                continue
            out.append(p)
            if len(out) >= limit:
                break
    except OSError:
        pass
    return out


def _find_maven_dep_for_pkg(project_path: str, pkg: str, exclude_pom_rel: str) -> str | None:
    """在项目【其它 pom】找声明了能提供该缺失包的 <dependency> 块（项目自证坐标，不臆造）。
    辨识 token 命中 artifactId/groupId；多命中取 artifactId 最短（最贴近）。返回 <dependency> 块文本或 None。"""
    toks = _pkg_match_tokens(pkg)
    if not toks:
        return None
    try:
        excl = (Path(project_path) / exclude_pom_rel).resolve() if exclude_pom_rel else None
    except OSError:
        excl = None
    cands: list[tuple[int, str]] = []
    for pom in _iter_project_poms(project_path):
        try:
            if excl and pom.resolve() == excl:
                continue
            text = pom.read_text("utf-8", errors="ignore")
        except OSError:
            continue
        for m in _DEP_BLOCK_RE.finditer(text):
            block = m.group(0)
            aid = _ARTIFACT_RE.search(block)
            gid = _GROUP_RE.search(block)
            hay = f"{gid.group(1) if gid else ''} {aid.group(1) if aid else ''}".lower()
            if aid and any(t.lower() in hay for t in toks):
                cands.append((len(aid.group(1)), block.strip()))
    if not cands:
        return None
    cands.sort(key=lambda x: x[0])
    return cands[0][1]


def _inject_dep_into_pom(pom_path: Path, dep_block: str) -> bool:
    """把 <dependency> 块注入 pom 最后一个 <dependencies>（模块项目级，通常在 dependencyManagement 之后）。
    已声明同 artifactId 则跳过。无 <dependencies> 段则保守不动（不新建段，免破坏结构）。返回是否改动。"""
    try:
        text = pom_path.read_text("utf-8", errors="ignore")
    except OSError:
        return False
    aid_m = _ARTIFACT_RE.search(dep_block)
    if aid_m and re.search(r"<artifactId>\s*" + re.escape(aid_m.group(1)) + r"\s*</artifactId>", text):
        return False
    idx = text.rfind("</dependencies>")
    if idx == -1:
        return False
    inject = "        " + dep_block.strip() + "\n    "
    try:
        pom_path.write_text(text[:idx] + inject + text[idx:], encoding="utf-8")
        return True
    except OSError:
        return False


def _proj_path_from_state(state) -> str | None:
    pid = state.get("project_id") if isinstance(state, dict) else None
    if not pid:
        return None
    try:
        from swarm.project import store as _store
        proj = _store.get_project(pid)
        return proj.get("path") if proj else None
    except Exception:  # noqa: BLE001
        return None


def _inject_missing_maven_deps(project_path: str | None, granted: dict, subtask_results: dict) -> dict:
    """治本 A2：授权后据项目自身 pom 把缺失包对应的 <dependency> 直接补进失败模块 pom。
    返回 {sid: [已补 artifactId]}。让重派的 worker 直接编过，不再耗尽定向恢复配额→不触发全量 replan。"""
    if not project_path:
        return {}
    injected: dict = {}
    for sid, mod_pom in (granted or {}).items():
        out = (subtask_results or {}).get(sid)
        if isinstance(out, WorkerOutput):
            det = out.l1_details or {}
        elif isinstance(out, dict):
            det = out.get("l1_details", {}) or {}
        else:
            det = {}
        blob = det.get("build_output") if isinstance(det.get("build_output"), str) else ""
        if not blob:
            try:
                blob = json.dumps(det, ensure_ascii=False)
            except (TypeError, ValueError):
                blob = str(det)
        added: list = []
        for pkg in _extract_missing_pkgs(blob):
            dep = _find_maven_dep_for_pkg(project_path, pkg, mod_pom)
            if not dep:
                continue
            if _inject_dep_into_pom(Path(project_path) / mod_pom, dep):
                a = _ARTIFACT_RE.search(dep)
                added.append(a.group(1) if a else pkg)
        if added:
            injected[sid] = added
    return injected


def _grant_module_pom_writable(plan_obj, failed_ids: list) -> dict:
    """给失败子任务补其模块 <module>/pom.xml 写权，返回 {sid: mod_pom} 已授权映射。

    让重试能真正改 pom 补依赖（原本 pom 不在 scope，重试再多也修不了）。同时让失败子任务
    depends_on【该 pom 的既有 owner】（HIGH-2）：owner 可能是已 DONE 的脚手架子任务，二者都写
    同一 pom，必须靠拓扑序让 owner 的 pom-create 在前、coder 的 pom-modify 在后，MERGE 才不冲突。
    """
    granted: dict = {}
    if plan_obj is None or not hasattr(plan_obj, "subtasks"):
        return granted
    subs = list(plan_obj.subtasks)
    by_id = {st.id: st for st in subs}
    for st in subs:
        if st.id not in failed_ids:
            continue
        sc = getattr(st, "scope", None)
        if sc is None:
            continue
        files = list(getattr(sc, "create_files", []) or []) + list(getattr(sc, "writable", []) or [])
        mod = _module_of(files)
        if not mod:
            continue
        mod_pom = f"{mod}/pom.xml"
        w = list(getattr(sc, "writable", []) or [])
        cf = list(getattr(sc, "create_files", []) or [])
        if mod_pom not in w and mod_pom not in cf:
            w.append(mod_pom)
            sc.writable = w
        granted[st.id] = mod_pom
        # 串到该 pom 的既有 owner 后面（owner = create/writable 含 mod_pom 的另一子任务）。
        owner = next(
            (
                o for o in subs
                if o.id != st.id and mod_pom in (
                    list(getattr(getattr(o, "scope", None), "create_files", []) or [])
                    + list(getattr(getattr(o, "scope", None), "writable", []) or [])
                )
            ),
            None,
        )
        if owner is not None:
            _add_dep_safe(by_id, st.id, owner.id)
    return granted


def _serialize_pom_writers(plan_obj, pom_by_id: dict) -> None:
    """同一模块 pom 的多个失败写者按 id 序串成依赖链，杜绝并发写同一 pom 争抢。

    传递防环（HIGH-4）：经 _add_dep_safe 检查传递可达性，不止看直接边。
    """
    if plan_obj is None or not hasattr(plan_obj, "subtasks"):
        return
    by_id = {st.id: st for st in plan_obj.subtasks}
    groups: dict = {}
    for sid, pom in pom_by_id.items():
        groups.setdefault(pom, []).append(sid)
    for _pom, members in groups.items():
        members = sorted(members)
        for i in range(1, len(members)):
            _add_dep_safe(by_id, members[i], members[i - 1])


async def handle_failure(state: BrainState) -> dict:
    """HANDLE_FAILURE 节点 — 处理子任务失败

    输入: failed_subtask_ids, subtask_results, plan, merge_conflicts
    输出: 按 strategy 分支更新状态（retry / retry_alternate / replan / escalate）
    """
    failed_ids = list(state.get("failed_subtask_ids", []))
    subtask_results = dict(state.get("subtask_results", {}))
    plan_obj = state.get("plan")
    strategy = "retry"

    logger.info(f"[HANDLE_FAILURE] 处理 {len(failed_ids)} 个失败子任务")

    if state.get("verification_failure") == "l2":
        # H2 修复：L2 失败 replan 也要走 replan_count 计数/上限，否则绕过熔断可无限重规划
        # （原直接 return replan 不自增计数，仅靠 recursion_limit=50 兜底，违背承诺）。
        _l2_replan = state.get("replan_count", 0) + 1
        _l2_max = get_config().model.max_retries
        if _l2_replan > _l2_max:
            logger.warning(
                "[HANDLE_FAILURE] L2 集成验证失败且 replan 已达上限(%d 次) → 升级人工审核",
                _l2_max,
            )
            return {
                "failure_strategy": "escalate",
                "failed_subtask_ids": failed_ids,
                "failure_escalated": True,
                "verification_failure": None,
                "l2_passed": False,
                "replan_count": _l2_replan,
            }
        logger.info("[HANDLE_FAILURE] L2 集成验证失败 — 触发 replan (第 %d/%d 次)",
                    _l2_replan, _l2_max)
        return {
            "failure_strategy": "replan",
            "failed_subtask_ids": [],
            "verification_failure": None,
            "l2_passed": False,
            "replan_count": _l2_replan,
        }

    if state.get("verification_failure") == "l3":
        logger.info("[HANDLE_FAILURE] L3 预发/CI 验证失败 — 升级人工审核")
        return {
            "failure_strategy": "escalate",
            "failed_subtask_ids": [],
            "verification_failure": None,
            "l3_passed": False,
        }

    if state.get("verification_failure") == "contract":
        # audit A-P1-03：契约偏离重试必须计数+设上限，否则与能力分支不同——
        # 可无限 retry→contract→retry 至 recursion_limit。复用 subtask_retry_counts
        # 与 max_retries 上限（与 capability/SIMPLE 路径一致），超限升级人工。
        failed = list(state.get("failed_subtask_ids", [])) or list(
            (state.get("subtask_results") or {}).keys()
        )
        failed = failed[:3]
        _max_retries = get_config().model.max_retries  # 默认 2
        _retry_counts = dict(state.get("subtask_retry_counts", {}))
        _next_counts = {fid: _retry_counts.get(fid, 0) + 1 for fid in failed}
        _deepest = max(_next_counts.values(), default=0)
        if _deepest > _max_retries + 1:
            logger.warning(
                "[HANDLE_FAILURE] 契约偏离重试达上限(%d+alternate)，升级人工: %s",
                _max_retries, failed,
            )
            return {
                "failure_escalated": True,
                "failure_strategy": "escalate",
                "failed_subtask_ids": failed,
                "verification_failure": None,
                "subtask_retry_counts": {**_retry_counts, **_next_counts},
            }
        logger.info("[HANDLE_FAILURE] 契约偏离 — 重试相关子任务(第 %d 次)", _deepest)
        return {
            "failure_strategy": "retry",
            "failed_subtask_ids": failed,
            "verification_failure": None,
            "subtask_retry_counts": {**_retry_counts, **_next_counts},
        }

    if effective_complexity(state) == Complexity.SIMPLE:  # 修复 12.3：澄清后定级优先
        # 确定性重试上限（与复杂路径一致，防止 SIMPLE 任务无限重试死循环）。
        # 历史 bug：SIMPLE 分支原先无条件 retry，遇到"L1 通过但 diff 收集为空"
        # (如重试时本地文件已被上一轮改过→difflib 基线已含变更→diff=空→被判失败)
        # 会无限循环。这里引入与复杂路径相同的 subtask_retry_counts 硬上限。
        max_retries = get_config().model.max_retries  # 默认 2
        retry_counts = dict(state.get("subtask_retry_counts", {}))
        next_counts = {fid: retry_counts.get(fid, 0) + 1 for fid in failed_ids}
        deepest = max(next_counts.values(), default=0)
        if deepest > max_retries + 1:
            logger.warning(
                "[HANDLE_FAILURE] SIMPLE 子任务重试达上限(%d+alternate)，升级人工: %s",
                max_retries, failed_ids,
            )
            return {
                "failure_escalated": True,
                "failure_strategy": "escalate",
                "l2_passed": False,
                "failed_subtask_ids": failed_ids,
                "subtask_retry_counts": {**retry_counts, **next_counts},
            }
        dispatch_remaining = list(state.get("dispatch_remaining", []))
        for fid in failed_ids:
            subtask_results.pop(fid, None)
            if fid not in dispatch_remaining:
                dispatch_remaining.append(fid)
        forced_alternate = deepest > max_retries
        logger.info(
            "[HANDLE_FAILURE] SIMPLE 快速路径 — 重试失败子任务(第 %d 次%s)",
            deepest, "，换备选模型" if forced_alternate else "",
        )
        return {
            "subtask_results": subtask_results,
            "dispatch_remaining": dispatch_remaining,
            "failed_subtask_ids": [],
            "failure_strategy": "retry_alternate" if forced_alternate else "retry",
            "use_alternate_model": forced_alternate,
            "subtask_retry_counts": {**retry_counts, **next_counts},
        }

    # ── LLM 故障分析 ──
    # audit #17：strategy 必须在 try 前有确定默认值——否则 _get_brain_llm() 抛异常时
    # except 分支用到 strategy 会 NameError。默认 "retry" 表示确定性回退（非 LLM 建议）。
    strategy = "retry"
    try:
        llm = _get_brain_llm()
        failure_details_dict: dict[str, dict] = {}
        for fid in failed_ids:
            out = subtask_results.get(fid)
            if isinstance(out, WorkerOutput):
                failure_details_dict[fid] = out.l1_details
            elif isinstance(out, dict):
                failure_details_dict[fid] = out.get("l1_details", {})
            else:
                failure_details_dict[fid] = {}
        failure_details = json.dumps(failure_details_dict, ensure_ascii=False)
        plan_json = plan_obj.model_dump_json(indent=2) if plan_obj and hasattr(plan_obj, "model_dump_json") else "{}"
        prompt_user = HANDLE_FAILURE_USER.format(
            failed_subtask_ids=failed_ids,
            failure_details=failure_details,
            plan_json=plan_json,
        )
        response = await llm.ainvoke([
            {"role": "system", "content": HANDLE_FAILURE_SYSTEM},
            {"role": "user", "content": prompt_user},
        ])
        result = _parse_json_from_llm(response.content)
        # Wave 1/TD2606-B1：策略走类型边界。未知策略 → ValidationError → 下方 except 确定性回退 retry
        # （不让 LLM 吐的未知字符串静默穿过策略阶梯）。result 保留供下游读取 adjusted_subtasks 等。
        _fs = FailureStrategyResponse.model_validate(result)
        strategy = _fs.strategy
        logger.info(f"[HANDLE_FAILURE] LLM 策略: {strategy} — {_fs.reasoning}")
    except json.JSONDecodeError as e:
        logger.warning(f"[HANDLE_FAILURE] LLM 输出解析失败 → 确定性回退 retry（非 LLM 建议）: {e}")
        strategy = "retry"
    except Exception as e:
        logger.warning(f"[HANDLE_FAILURE] LLM 分析异常 → 确定性回退 retry（非 LLM 建议）: {e}")
        strategy = "retry"

    # ── P0-B/P1-D：缺符号/缺依赖编译失败 → 定向恢复（先于一切 strategy 分支拦截）──
    # 这类失败是【scope 不可满足】（pom 不在子任务写权内，原地重试 100 次也修不了）。无论 LLM
    # 选 retry 还是 replan，都先走定向恢复：补模块 pom 写权 + 重置徒劳的重试计数 + 只重派失败
    # 子任务（保留成功兄弟、不进 PLAN、不清完成态全表）。targeted_recovery_count 熔断防死循环。
    if _is_missing_dependency_failure(subtask_results, failed_ids) and failed_ids:
        _tr = state.get("targeted_recovery_count", 0) + 1
        _tr_max = get_config().model.max_retries  # 复用 max_retries（默认 2）
        if _tr > _tr_max:
            # 熔断：达上限仍缺依赖 → 不再 mutate plan，落常规 strategy 兜底（HIGH-3：先判上限再改 plan）。
            logger.warning(
                "[HANDLE_FAILURE] 定向恢复已达上限(%d 次)仍缺依赖 → 落常规 %s 兜底",
                _tr_max, strategy,
            )
        else:
            # 仅在配额内才 mutate plan（补 pom 写权 + 串 owner 依赖），杜绝兜底路径留下孤儿 scope 改动。
            granted = _grant_module_pom_writable(plan_obj, failed_ids)
            if granted:
                # 治本 A2：授权后【确定性】据项目自身 pom 把缺失依赖补进失败模块 pom，
                # 不再指望小模型自己加（实测它加不上 → 耗尽配额 → 全量 replan 砸成功子任务）。
                _dep_injected = _inject_missing_maven_deps(
                    _proj_path_from_state(state), granted, subtask_results)
                if _dep_injected:
                    logger.info(
                        "[HANDLE_FAILURE] 确定性补依赖（治本 A2，据项目自身 pom 自证坐标，"
                        "重派 worker 直接编过、不再耗配额）：%s", _dep_injected,
                    )
                _serialize_pom_writers(plan_obj, granted)
                dispatch_remaining = list(state.get("dispatch_remaining", []))
                for fid in failed_ids:
                    subtask_results.pop(fid, None)
                    if fid not in dispatch_remaining:
                        dispatch_remaining.append(fid)
                # 之前的重试因 scope 不可满足而徒劳，不计入配额——重置失败子任务重试计数。
                _rc = dict(state.get("subtask_retry_counts", {}))
                for fid in failed_ids:
                    _rc[fid] = 0
                _kept = [sid for sid in subtask_results if sid not in failed_ids]
                logger.info(
                    "[HANDLE_FAILURE] 定向恢复（第 %d/%d 次）：缺符号/缺依赖编译失败 → 给失败子任务 "
                    "补模块 pom 写权 %s + 重置重试计数，仅重派失败子任务 %s（保留 %d 个完成态），"
                    "换备选模型，不进 PLAN、不清完成态全表",
                    _tr, _tr_max, granted, failed_ids, len(_kept),
                )
                return {
                    "plan": plan_obj,
                    "subtask_results": subtask_results,
                    "dispatch_remaining": dispatch_remaining,
                    "failed_subtask_ids": [],
                    "failure_strategy": "retry_alternate",
                    "use_alternate_model": True,
                    "subtask_retry_counts": _rc,
                    "targeted_recovery_count": _tr,
                    "targeted_recovery": True,
                }
            # granted 为空（推不出模块 pom）→ 不 mutate、不自增计数，落常规 strategy（其自带
            # replan_count 熔断会兜底升级），不会在此空转（MEDIUM-2）。
            logger.info(
                "[HANDLE_FAILURE] 缺依赖失败但推不出可补的模块 pom（失败子任务无模块路径）→ 落常规 %s",
                strategy,
            )

    if strategy == "replan":
        # ── 修复 B：replan 守卫 —— 保护已成功的兄弟子任务，避免一个子任务失败就全量推倒重来 ──
        # 背景(task dab669bb)：medium 任务拆成 st-1(实现)+st-2(测试)，st-1 成功 DONE、
        # st-2 因写错 JUnit L1 失败 → LLM 选 replan → 清空【含成功的 st-1】全部重新规划 ~10min →
        # 循环。replan 只该用于【计划本身有结构性问题】(拆分错/依赖悬空)，单个子任务的
        # L1 质量失败应只【重做失败子任务】，保留成功成果。
        # 守卫条件：本批失败是子任务级 L1 失败 + 存在已成功(L1 通过)的兄弟子任务 +
        #          失败子任务未达重试上限 → 降级为 retry（只重派失败的，不动成功的）。
        def _is_l1_passed(out) -> bool:
            if isinstance(out, WorkerOutput):
                return bool(out.l1_passed)
            if isinstance(out, dict):
                return bool(out.get("l1_passed"))
            return False

        succeeded_siblings = [
            sid for sid, out in subtask_results.items()
            if sid not in failed_ids and _is_l1_passed(out)
        ]
        _retry_counts = dict(state.get("subtask_retry_counts", {}))
        _next_counts = {fid: _retry_counts.get(fid, 0) + 1 for fid in failed_ids}
        _deepest = max(_next_counts.values(), default=0)
        _max_retries = get_config().model.max_retries  # 默认 2
        # 仅在【有成功兄弟】且【失败子任务还没烧光重试配额】时拦截 replan，降级为 retry。
        # 没有成功兄弟（整批都失败）或已达上限，仍走原 replan 逻辑（可能真是计划问题）。
        if succeeded_siblings and failed_ids and _deepest <= _max_retries + 1:
            dispatch_remaining = list(state.get("dispatch_remaining", []))
            for fid in failed_ids:
                subtask_results.pop(fid, None)
                if fid not in dispatch_remaining:
                    dispatch_remaining.append(fid)
            forced_alternate = _deepest > _max_retries
            logger.info(
                "[HANDLE_FAILURE] replan 守卫生效 — 保留 %d 个成功子任务 %s，"
                "仅重做失败子任务 %s（第 %d 次%s），不全量重规划",
                len(succeeded_siblings), succeeded_siblings, failed_ids, _deepest,
                "，换备选模型" if forced_alternate else "",
            )
            return {
                "subtask_results": subtask_results,
                "dispatch_remaining": dispatch_remaining,
                "failed_subtask_ids": [],
                "failure_strategy": "retry_alternate" if forced_alternate else "retry",
                "use_alternate_model": forced_alternate,
                "subtask_retry_counts": {**_retry_counts, **_next_counts},
            }

        for fid in failed_ids:
            subtask_results.pop(fid, None)
        # P0-2 熔断：replan 不能无限重入。每次 replan 计数，超过上限直接升级人工，
        # 而非继续 PLAN→ELABORATE→（可能同样的坏计划）→再失败，最终撞穿 recursion_limit
        # （见 task 0f93f1fc：replan 后又拆出同样的悬空依赖）。
        replan_count = state.get("replan_count", 0) + 1
        max_replan = get_config().model.max_retries  # 复用 max_retries（默认 2）
        if replan_count > max_replan:
            logger.warning(
                "[HANDLE_FAILURE] replan 已达上限(%d 次)仍失败 → 升级人工审核（避免无限重规划）",
                max_replan,
            )
            return {
                "subtask_results": subtask_results,
                "failed_subtask_ids": failed_ids,
                "failure_escalated": True,
                "failure_strategy": "escalate",
                "l2_passed": False,
                "replan_count": replan_count,
            }
        # P0-2 携带失败原因：把本轮失败详情注入 state，供 PLAN 重新规划时参考，
        # 避免 LLM 看不到失败原因而原样重生成同一个坏计划。
        replan_feedback = (result.get("reasoning") or "").strip()
        logger.info(
            "[HANDLE_FAILURE] 策略=replan（第 %d/%d 次）— 清除失败结果，触发重新规划%s",
            replan_count, max_replan,
            "（已携带失败原因供 PLAN 参考）" if replan_feedback else "",
        )
        return {
            "subtask_results": subtask_results,
            "failed_subtask_ids": [],
            "plan_valid": False,
            "failure_strategy": "replan",
            "replan_count": replan_count,
            "replan_feedback": replan_feedback,
        }

    if strategy == "escalate":
        logger.info("[HANDLE_FAILURE] 策略=escalate — 上报人工审核")
        return {
            "failure_escalated": True,
            "failure_strategy": "escalate",
            "l2_passed": False,
            "failed_subtask_ids": failed_ids,
        }

    # ── P2：瞬时(transient)失败优先走退避重试，与 capability 配额隔离 ──
    # 背景(task 37460a5b)：Connection error/Internal Server Error 等基础设施抖动，过去与
    # 拒答/空 diff 等能力问题混在同一条 retry 阶梯，0.8s 内连撞两次烧光配额直接 escalate。
    # 现在：本批若【全部】是 transient 失败 → 走带指数退避的轻量重试(独立计数器，上限 3)，
    # 不消耗 capability 的 subtask_retry_counts。一旦混入 capability 失败，则交给下方阶梯
    # (capability 才是该换模型/升级的真问题，不能被 transient 掩盖)。
    from swarm.models.errors import TRANSIENT, classify_failure, backoff_seconds

    def _failure_class_of(fid: str) -> str | None:
        out = subtask_results.get(fid)
        details: dict = {}
        summary = ""
        if isinstance(out, WorkerOutput):
            details = out.l1_details or {}
            summary = out.summary or ""
        elif isinstance(out, dict):
            details = out.get("l1_details", {}) or {}
            summary = out.get("summary", "") or ""
        fc = details.get("failure_class")
        if fc:
            return fc
        # 兜底：summary/error 文本再分类一次（worker 未显式标注时）
        return classify_failure(summary or details.get("error"))

    failure_classes = {fid: _failure_class_of(fid) for fid in failed_ids}
    transient_ids = [fid for fid, fc in failure_classes.items() if fc == TRANSIENT]
    MAX_TRANSIENT_RETRY = 3

    # 仅当本批失败【全部】为 transient 时才走退避快路（混入 capability 则不抢占阶梯）。
    if transient_ids and len(transient_ids) == len(failed_ids):
        transient_counts = dict(state.get("subtask_transient_counts", {}))
        next_tcounts = {fid: transient_counts.get(fid, 0) + 1 for fid in transient_ids}
        deepest_t = max(next_tcounts.values(), default=0)
        if deepest_t <= MAX_TRANSIENT_RETRY:
            # 治本 C：流式 stall（模型服务并发拥塞）立即重试会撞同一拥塞 → 给【更长退避】让拥塞散去
            # （8/16/32s）；普通 transient（连接抖动/5xx）恢复快，沿用短退避（2/4/8s）。两者都【不换模型】
            # （use_alternate_model=False）——是基建瞬时不是模型弱。
            _stall = _has_stream_stall(subtask_results, transient_ids)
            delay = backoff_seconds(deepest_t, base=8.0, cap=60.0) if _stall else backoff_seconds(deepest_t)
            logger.info(
                "[HANDLE_FAILURE] 策略=retry(transient%s 退避，第 %d/%d 次，sleep %.1fs，不换模型/不计 capability 配额): %s",
                "·流式stall" if _stall else "", deepest_t, MAX_TRANSIENT_RETRY, delay, transient_ids,
            )
            await asyncio.sleep(delay)
            dispatch_remaining = list(state.get("dispatch_remaining", []))
            for fid in transient_ids:
                subtask_results.pop(fid, None)
                if fid not in dispatch_remaining:
                    dispatch_remaining.append(fid)
            return {
                "dispatch_remaining": dispatch_remaining,
                "failed_subtask_ids": [],
                "subtask_results": subtask_results,
                "failure_strategy": "retry",
                "use_alternate_model": False,
                "subtask_transient_counts": {**transient_counts, **next_tcounts},
            }
        # transient 退避也用尽 → 落入下方 capability 阶梯（基础设施持续不可用，升级人工）
        logger.warning(
            "[HANDLE_FAILURE] transient 退避重试已达上限(%d 次)仍失败，转入 capability 阶梯: %s",
            MAX_TRANSIENT_RETRY, transient_ids,
        )

    # retry / retry_alternate — 确定性递进升级（覆盖 LLM 决策，防止无限重试）
    #
    # 设计文档要求"重试最多 2 次 → 换模型 → 上报人工"，但原实现完全依赖 LLM 单次
    # 决策，LLM 可能持续输出 retry 导致死循环。这里引入每子任务的确定性重试计数器，
    # 强制执行升级阶梯：
    #   retry_count < max_retries        → retry        (普通重试)
    #   retry_count == max_retries       → retry_alternate (换备选模型)
    #   retry_count > max_retries        → escalate     (上报人工)
    # LLM 仍可主动选择 replan/escalate（上面已处理），但 retry 类不会突破硬上限。
    max_retries = get_config().model.max_retries  # 默认 2
    retry_counts = dict(state.get("subtask_retry_counts", {}))

    # 计算本批失败子任务里"最深"的重试次数，决定整批升级档位
    next_counts = {fid: retry_counts.get(fid, 0) + 1 for fid in failed_ids}
    deepest = max(next_counts.values(), default=0)

    # FINDING-12：拒答/步数耗尽(refusal_hard_fail)的子任务，重试强制走【最强模型】(40B 256k)，
    # 而非更弱 fallback——步数耗尽是小模型 agent 循环不收敛，换更弱只会更糟。
    force_strong = dict(state.get("subtask_force_strong", {}))
    for _fid in failed_ids:
        _res = subtask_results.get(_fid)
        _src = (getattr(_res, "l1_details", {}) or {}).get("l1_decision_source") if _res else None
        if _src == "refusal_hard_fail":
            force_strong[_fid] = True

    if deepest > max_retries + 1:
        # 重试耗尽。【部分交付】：已有完成子任务 + 开启 partial → 放弃 failed(+传递依赖者)，
        # 继续交付其余，终态 PARTIAL(非 DONE，诚实未完成)。否则(0 完成 / 关闭 partial) →
        # 维持 escalate(整任务失败)，避免无产出却假成功。
        _abandoned_so_far = set(state.get("abandoned_subtask_ids") or [])
        _done = [tid for tid in subtask_results
                 if tid not in failed_ids and tid not in _abandoned_so_far]
        _allow_partial = getattr(get_config().worker, "allow_partial_delivery", True)
        if _allow_partial and _done and plan_obj is not None:
            abandoned = _abandoned_so_far | set(failed_ids)
            # 传递放弃：依赖被放弃者的子任务也放弃(缺依赖跑不了)，避免它们永留 remaining 死循环
            _changed = True
            while _changed:
                _changed = False
                for _st in plan_obj.subtasks:
                    if _st.id not in abandoned and any(
                        d in abandoned for d in (getattr(_st, "depends_on", []) or [])
                    ):
                        abandoned.add(_st.id)
                        _changed = True
            _remaining = [t for t in (state.get("dispatch_remaining") or []) if t not in abandoned]
            logger.warning(
                "[HANDLE_FAILURE] 部分交付：放弃 %s(+依赖者，共 %d)，继续交付其余 %d 个，终态将 PARTIAL",
                failed_ids, len(abandoned), len(_remaining),
            )
            return {
                "failure_strategy": "abandon",
                "abandoned_subtask_ids": sorted(abandoned),
                "failed_subtask_ids": [],
                "dispatch_remaining": _remaining,
                "subtask_force_strong": force_strong,
                "subtask_retry_counts": {**retry_counts, **next_counts},
            }
        # 已用尽 retry + alternate 且无可交付/关闭 partial → 升级人工(整任务失败)
        logger.warning(
            "[HANDLE_FAILURE] 子任务重试已达上限（retry %d + alternate 1），升级人工审核: %s",
            max_retries, failed_ids,
        )
        return {
            "failure_escalated": True,
            "failure_strategy": "escalate",
            "l2_passed": False,
            "failed_subtask_ids": failed_ids,
            "subtask_retry_counts": {**retry_counts, **next_counts},
        }

    # 将失败子任务重新加入 dispatch_remaining
    dispatch_remaining = list(state.get("dispatch_remaining", []))
    for fid in failed_ids:
        subtask_results.pop(fid, None)
        if fid not in dispatch_remaining:
            dispatch_remaining.append(fid)

    # 确定性档位：超过 max_retries 次普通重试后切换备选模型
    forced_alternate = deepest > max_retries
    effective_strategy = "retry_alternate" if forced_alternate else "retry"
    # 若 LLM 主动要求 retry_alternate 且尚未到 alternate 档，也尊重它（提前换模型）
    if strategy == "retry_alternate":
        effective_strategy = "retry_alternate"

    # ── 治本：编译失败根因在 scope 外(缺 pom 依赖/上游文件)→ 加宽 scope 让重试能真正修 ──
    _scope_widened = False
    if plan_obj is not None:
        for fid in failed_ids:
            new_files = _widen_scope_for_compile_repair(plan_obj, fid, _l1_details_of(subtask_results, fid))
            if new_files:
                _scope_widened = True
                logger.info(
                    "[HANDLE_FAILURE] 编译修复加宽 scope：子任务 %s 纳入 %s（治根因在 scope 外的编译失败，使重试可改 pom/上游）",
                    fid, new_files,
                )

    out: dict = {
        "dispatch_remaining": dispatch_remaining,
        "failed_subtask_ids": [],
        "subtask_results": subtask_results,
        "failure_strategy": effective_strategy,
        "subtask_retry_counts": {**retry_counts, **next_counts},
        "subtask_force_strong": force_strong,  # FINDING-12：拒答子任务重试走最强模型
    }
    if _scope_widened:
        out["plan"] = plan_obj  # 回写加宽后的 scope，dispatch 重试用
    if effective_strategy == "retry_alternate":
        out["use_alternate_model"] = True
        logger.info(
            "[HANDLE_FAILURE] 策略=retry_alternate（第 %d 次，换备选模型）: %s",
            deepest, failed_ids,
        )
    else:
        out["use_alternate_model"] = False
        logger.info(
            "[HANDLE_FAILURE] 策略=retry（第 %d/%d 次）: %s",
            deepest, max_retries, failed_ids,
        )
    return out


def _make_base_reader(state: BrainState):
    """从项目工作区读取 base 文件内容，供 3-way merge 使用。"""
    project_id = state.get("project_id") or ""
    project_path = _get_project_path(project_id)

    def read(file_path: str) -> str | None:
        if not project_path:
            return None
        rel = file_path.lstrip("/")
        if rel.startswith("a/") or rel.startswith("b/"):
            rel = rel[2:]
        full = Path(project_path) / rel
        try:
            if full.is_file():
                return full.read_text(encoding="utf-8", errors="replace")
        except OSError as exc:
            logger.debug("[MERGE] read base %s: %s", full, exc)
        return None

    return read


def merge(state: BrainState) -> dict:
    """MERGE 节点 — 合并所有子任务的 diff

    输入: subtask_results
    输出: merged_diff, merge_conflicts (如有硬冲突), rebase_subtask_ids (如有 rebase)
    """
    from swarm.brain.merge_engine import merge_diffs

    subtask_results: dict = state.get("subtask_results", {})

    logger.info(f"[MERGE] 合并 {len(subtask_results)} 个子任务的 diff")

    subtask_diffs: list[tuple[str, str]] = []
    for subtask_id, output in subtask_results.items():
        if isinstance(output, WorkerOutput):
            subtask_diffs.append((subtask_id, output.diff or ""))
        elif isinstance(output, dict):
            subtask_diffs.append((subtask_id, output.get("diff", "") or ""))

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

    logger.info(
        "[MERGE] 合并完成, 总长度=%d, 冲突=%d, 自动消解=%d, rebase=%d, success=%s",
        len(result.merged_diff),
        len(result.conflicts),
        len(result.auto_resolved_files),
        len(result.rebase_subtask_ids),
        result.success,
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
            # rebase 已达上限仍冲突 → 升级人工，不再无限重生成
            logger.warning(
                "[MERGE] 子任务 rebase 达上限(%d)，升级人工: %s", max_rebase, over_limit,
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
        logger.warning("[VERIFY_L2] 获取项目路径失败: %s", exc)
    return None


def _sandbox_available() -> bool:
    cfg = get_config().sandbox
    return bool(cfg.use_for_worker and cfg.api_url)


def _run_l2_local(project_path: str, merged_diff: str, test_cmd: str, *, timeout: int = 180) -> bool:
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
        # 照抄 integration_review 的回滚：checkout 已跟踪文件 + clean 未跟踪文件。
        try:
            subprocess.run(["git", "checkout", "--", "."], cwd=project_path,
                           capture_output=True, timeout=60)
            subprocess.run(["git", "clean", "-fd"], cwd=project_path,
                           capture_output=True, timeout=60)
        except Exception as exc:  # noqa: BLE001
            logger.warning("[VERIFY_L2] 工作树回滚失败(非致命): %s", exc)


def _run_l2_in_sandbox(
    project_path: str,
    merged_diff: str,
    test_cmd: str,
    *,
    project_id: str = "",
    timeout: int = 180,
) -> bool:
    import base64
    from pathlib import Path

    from swarm.worker.sandbox import get_sandbox_manager

    cfg = get_config().sandbox
    workdir = cfg.sandbox_remote_workdir
    manager = get_sandbox_manager()
    sandbox = None

    try:
        sandbox = manager.create(
            project_id=project_id or None,
            source="verify_l2",
        )
        manager.sync_project_to_sandbox(sandbox, Path(project_path), workdir)

        patch_b64 = base64.b64encode(merged_diff.encode()).decode()
        apply_code = f"""
import base64, subprocess, tempfile, os
patch = base64.b64decode({patch_b64!r}).decode()
with tempfile.NamedTemporaryFile(mode='w', suffix='.patch', delete=False) as tf:
    tf.write(patch)
    patch_path = tf.name
try:
    r = subprocess.run(['git', 'apply', patch_path], cwd={workdir!r}, capture_output=True, text=True, timeout=60)
    print('APPLY_RC', r.returncode)
    if r.stderr:
        print('APPLY_ERR', r.stderr)
finally:
    os.unlink(patch_path)
"""
        apply_result = manager.run_code(sandbox, apply_code, timeout=90)
        if apply_result.error or "APPLY_RC 0" not in (apply_result.stdout or ""):
            logger.warning(
                "[VERIFY_L2] 沙箱 git apply 失败: %s",
                apply_result.error or apply_result.stdout or apply_result.stderr,
            )
            return False

        test_code = f"""
import subprocess
r = subprocess.run({test_cmd!r}, cwd={workdir!r}, shell=True, capture_output=True, text=True, timeout={timeout})
print('TEST_RC', r.returncode)
if r.stdout:
    print(r.stdout[-4000:])
if r.stderr:
    print(r.stderr[-2000:], end='')
"""
        test_result = manager.run_code(sandbox, test_code, timeout=timeout + 30)
        if test_result.error:
            logger.warning("[VERIFY_L2] 沙箱测试执行失败: %s", test_result.error)
            return False
        return "TEST_RC 0" in (test_result.stdout or "")
    finally:
        if sandbox is not None:
            try:
                manager.kill(sandbox.sandbox_id)
            except Exception as exc:
                logger.debug("[VERIFY_L2] 销毁沙箱失败: %s", exc)


def _try_l2_sandbox_verify(
    project_id: str,
    merged_diff: str,
    test_cmd: str,
    *,
    timeout: int = 180,
) -> bool | None:
    """Run L2 in sandbox. Returns None if sandbox unavailable."""
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
) -> bool | None:
    """Run L2 locally via git apply + subprocess. Returns None if no project path."""
    project_path = _get_project_path(project_id)
    if not project_path:
        return None
    logger.info("[VERIFY_L2] 本地 L2 验证: cmd=%s", test_cmd)
    return _run_l2_local(project_path, merged_diff, test_cmd, timeout=timeout)


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
    decision = interrupt(
        {
            "type": "deliver",
            "task_id": state.get("task_id"),
            "task_description": state.get("task_description"),
            "merged_diff": state.get("merged_diff", "")[:2000],
            "l2_passed": state.get("l2_passed", False),
            "message": "任务执行完成，请审核结果并决定: accept(接受) / revise(修订) / reject(拒绝)",
        }
    )

    # 解析决策
    if isinstance(decision, str):
        human_decision = HumanDecision(decision)
    elif isinstance(decision, dict) and "decision" in decision:
        human_decision = HumanDecision(decision["decision"])
    else:
        human_decision = HumanDecision.ACCEPT

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
        resolve_plan_conflicts(updated_plan)  # 原地变更；返回值(计数 dict)丢弃
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
    }


async def learn_success(state: BrainState) -> dict:
    """LEARN_SUCCESS 节点 — 从成功任务中学习并写入 L6/L2"""
    from swarm.brain.learn_store import merge_persist_meta, persist_learn_success

    task_description = state.get("task_description", "")
    plan_obj = state.get("plan")
    merged_diff = state.get("merged_diff", "")
    complexity = effective_complexity(state)  # 修复 12.3：澄清后定级优先

    # ── 第二批根因(选项A)：产出本地 git commit（单一收口点，覆盖 auto+人工 accept）──
    # accept 后必经 learn_success。worker pull-back 把产出写进工作区但【不 commit】，
    # 后续 git checkout / VERIFY_L2 reset / 下个任务会把未提交产出冲掉 → 事实库滞后丢失。
    # 这里 commit（仅本地，不 push）让产出稳定落盘，且触发已有 git 增量索引链路。
    try:
        if merged_diff.strip():
            proj_path = _get_project_path(state.get("project_id") or "")
            if proj_path:
                from swarm.project.diff_apply import (
                    apply_git_diff,
                    commit_task_output,
                    files_from_unified_diff,
                )
                out_files = files_from_unified_diff(merged_diff)
                import asyncio as _asyncio
                import os as _os2
                # 仅当产出文件【在工作区缺失】时才重新 apply（VERIFY_L2 reset 删了新建文件的场景）。
                # 若文件已在工作区（worker pull-back 已写入改好的内容），跳过 apply——
                # 否则对 modify 文件会因"补丁基线已变"冲突报错（task 5dc6e634）。commit 直接收录工作区现状。
                missing = [f for f in out_files
                           if not _os2.path.isfile(_os2.path.join(proj_path, f))]
                if missing:
                    _ap = await _asyncio.to_thread(
                        lambda: apply_git_diff(proj_path, merged_diff, check_only=False))
                    if not _ap.get("ok"):
                        logger.warning("[LEARN_SUCCESS] commit 前重新 apply(补缺失文件)失败(非致命): %s",
                                       _ap.get("stderr", "")[:160])
                _c = await _asyncio.to_thread(
                    commit_task_output, proj_path, out_files, task_id=state.get("task_id"))
                if _c.get("committed"):
                    logger.info("[LEARN_SUCCESS] 产出已本地 commit: %s (%d 文件)",
                                _c.get("commit_hash"), len(out_files))
                elif not _c.get("ok"):
                    logger.warning("[LEARN_SUCCESS] 产出 commit 跳过(非致命): %s", _c.get("reason"))
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
            plan_json = plan_obj.model_dump_json(indent=2) if plan_obj and hasattr(plan_obj, "model_dump_json") else "{}"
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

    from swarm.knowledge.event_bus import publish_kb_event

    publish_kb_event(
        "learn_success",
        {
            "project_id": state.get("project_id"),
            "task_id": state.get("task_id"),
            "mr_url": mr_url,
        },
    )

    logger.info("[LEARN_SUCCESS] 学习完成 (persisted=%s)", persist_meta.get("persisted"))
    return {
        "learned": True,
        "learn_summary": learn_summary,
    }


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
        plan_json = plan_obj.model_dump_json(indent=2) if plan_obj and hasattr(plan_obj, "model_dump_json") else "{}"
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
