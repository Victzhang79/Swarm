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
from swarm.brain.state import BrainState
from swarm.config.settings import get_config
from swarm.memory.sliding_window import PRIORITY_WORKER
from swarm.models.router import ModelRouter
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
    TaskPlan,
    WorkerOutput,
)

logger = logging.getLogger(__name__)

_TRIVIAL_HINTS = ("docstring", "注释", "comment", "一行", "typo", "拼写", "添加一行")

# 文件名匹配：要求左边界是非文件名字符（含中文/空格/标点），杜绝中文粘连。
# 旧正则 [\w./-]+ 会把中文也算进 \w，导致 "输出readme.md" → "输出readme.md"。
_FILE_EXT = (
    r"py|js|jsx|ts|tsx|java|go|rs|rb|php|c|cc|cpp|h|hpp|cs|kt|swift|scala|"
    r"sh|md|rst|txt|toml|yaml|yml|json|ini|cfg|env|xml|html|css"
)
# (?<![\w/.\-]) 前面不能是文件名字符（ASCII），中文不在此类→自然成为边界
# 末尾用 (?![A-Za-z0-9_./\-]) 而非 \b：中文是 \w，\b 在 ".md出" 处不成立会漏匹配。
_FILE_PAT = re.compile(
    rf"(?<![A-Za-z0-9_/.\-])([A-Za-z0-9_][A-Za-z0-9_./\-]*\.(?:{_FILE_EXT}))(?![A-Za-z0-9_./\-])"
)

# 操作意图关键词
_CREATE_HINTS = ("新建", "新增", "创建", "添加文件", "生成", "输出", "create", "add file", "new file", "生成一个", "写一个", "写个", "实现一个", "做一个", "开发")
_DELETE_HINTS = ("删除", "移除", "去掉", "删掉", "delete", "remove")


def _guess_target_files(task_description: str) -> list[str]:
    """从需求中抠出 ASCII 文件名（中文不会粘连）。"""
    return list(dict.fromkeys(m.group(1) for m in _FILE_PAT.finditer(task_description)))


def _classify_file_ops(task_description: str) -> dict[str, list[str]]:
    """把需求里点名的文件按操作意图分类: modify / create / delete。

    启发式：在文件名附近（同一子句）出现删除/新建关键词则归类，否则默认 modify。
    用中文/标点/英文动词切分子句，逐句判断该句里的文件归哪类。
    """
    create: list[str] = []
    delete: list[str] = []
    modify: list[str] = []
    # 按常见分隔符切子句，让"删除 a.py，新增 b.py"能分别归类。
    # 注意：不能用 '.' 当分隔符（会切断 readme.md）；中文句号'。'可以。
    clauses = re.split(r"[，,；;。\n、]| and | then |然后|以及|并且|并|再|同时", task_description)
    for clause in clauses:
        files = _guess_target_files(clause)
        if not files:
            continue
        low = clause.lower()
        if any(h in clause or h in low for h in _DELETE_HINTS):
            delete.extend(files)
        elif any(h in clause or h in low for h in _CREATE_HINTS):
            create.extend(files)
        else:
            modify.extend(files)
    # 去重 + 互斥优先级：delete > create > modify（同名只归最强意图）
    delete = list(dict.fromkeys(delete))
    create = list(dict.fromkeys(f for f in create if f not in delete))
    modify = list(dict.fromkeys(f for f in modify if f not in delete and f not in create))
    return {"modify": modify, "create": create, "delete": delete}


def _format_project_structure(knowledge_context: dict | None) -> str:
    """从知识上下文(codegraph struct 层)提炼真实文件/符号清单，供 LLM 拆分参考。

    让大模型基于真实存在的文件分配 scope，而非凭需求文字臆造文件名。
    """
    if not knowledge_context:
        return "（无项目结构索引——可能是新项目或预处理未完成，请根据需求合理新建文件）"
    struct = knowledge_context.get("struct") or []
    if not struct:
        return "（结构索引为空，请根据需求合理命名新建文件）"
    by_file: dict[str, list[str]] = {}
    for item in struct:
        fp = item.get("file_path") or item.get("file") or ""
        name = item.get("symbol_name") or item.get("name") or ""
        if not fp:
            continue
        by_file.setdefault(fp, [])
        if name and len(by_file[fp]) < 8:
            by_file[fp].append(name)
    if not by_file:
        return "（结构索引为空，请根据需求合理命名新建文件）"
    lines = []
    for fp in sorted(by_file)[:25]:  # 限制文件数，避免 prompt 膨胀
        syms = ", ".join(by_file[fp])
        lines.append(f"- {fp}" + (f"  (符号: {syms})" if syms else ""))
    extra = "" if len(by_file) <= 25 else f"\n…… 等共 {len(by_file)} 个相关文件"
    return "\n".join(lines) + extra


def _infer_harness(task_description: str, scope, project_path: str = "") -> "TaskHarness":
    """根据任务描述/scope 文件/项目结构推断一个合理的验证 harness。

    用于 SIMPLE 快速路径，以及 LLM plan 未给出 harness 时的兜底。按语言给出
    构建/测试/验收命令 + 需放行的命令白名单，让 Worker 能真正跑验证而非口头自报。
    """
    # 收集 scope 内所有文件后缀判断语言
    files: list[str] = []
    for attr in ("writable", "create_files", "readable"):
        files.extend(getattr(scope, attr, []) or [])
    exts = {f.rsplit(".", 1)[-1].lower() for f in files if "." in f}
    text = (task_description or "").lower()

    def has(*kw: str) -> bool:
        return any(k in text for k in kw)

    # 语言判定（scope 后缀优先，其次描述关键词）
    if "py" in exts or has("python", "pytest", "django", "flask", "fastapi", "pygame"):
        return TaskHarness(
            language="python",
            build_command="python -m py_compile .",
            test_command="python -m pytest -q",
            extra_whitelist=[
                "python", "python3", "python -m", "python -c",
                "pytest", "ruff", "mypy", "pip install", "ls", "cat",
            ],
        )
    if exts & {"js", "jsx", "ts", "tsx"} or has("node", "npm", "react", "typescript", "vue"):
        return TaskHarness(
            language="node",
            build_command="npm run build",
            test_command="npm test",
            extra_whitelist=["node", "npm", "npx", "tsc", "eslint", "ls", "cat"],
        )
    if "go" in exts or has("golang", " go "):
        return TaskHarness(
            language="go",
            build_command="go build ./...",
            test_command="go test ./...",
            extra_whitelist=["go ", "gofmt", "ls", "cat"],
        )
    if "rs" in exts or has("rust", "cargo"):
        return TaskHarness(
            language="rust",
            build_command="cargo build",
            test_command="cargo test",
            extra_whitelist=["cargo", "rustc", "ls", "cat"],
        )
    if exts & {"java", "kt"} or has("maven", "gradle", "java", "spring"):
        return TaskHarness(
            language="java",
            build_command="mvn compile",
            test_command="mvn test",
            extra_whitelist=["mvn", "gradle", "javac", "ls", "cat"],
        )
    # 兜底：通用，至少放行基本探查命令
    return TaskHarness(language="", extra_whitelist=["ls", "cat", "python -c", "python -m py_compile"])


def _heuristic_complexity(task_description: str) -> Complexity | None:
    t = task_description.lower()
    if any(h in t for h in _TRIVIAL_HINTS) and len(task_description) < 280:
        return Complexity.SIMPLE
    return None


def _build_simple_plan(task_description: str, affected_files: list[str] | None = None) -> TaskPlan:
    # Scope 解析优先级（确保 scope 既非空又精准）：
    # 1) 任务描述中【显式点名】的文件（如 "只修改 README.md"）—— 最强信号，
    #    用户意图明确时，writable 应严格限定为这些文件，避免改到无关文件。
    # 2) analyze 节点经知识库检索得到的 affected_files —— 作为上下文/回退。
    ops = _classify_file_ops(task_description)
    explicit = ops["modify"] + ops["create"] + ops["delete"]
    retrieved = [f for f in (affected_files or []) if f]

    if explicit:
        # 用户点名了文件：按操作意图分别填 modify/create/delete；
        # readable 额外纳入检索文件作上下文
        modify_files = list(dict.fromkeys(ops["modify"]))
        create_files = list(dict.fromkeys(ops["create"]))
        delete_files = list(dict.fromkeys(ops["delete"]))
        readable = list(dict.fromkeys(explicit + retrieved))
    elif retrieved:
        modify_files = list(dict.fromkeys(retrieved))
        create_files = []
        delete_files = []
        readable = modify_files
    else:
        modify_files = []
        create_files = []
        delete_files = []
        readable = []
    # 无任何文件线索（如"写个推箱子游戏"这类从零/开放式需求）→ 放行任意路径，
    # 否则 scope_guard 会拒绝所有写操作导致 worker 寸步难行。
    allow_any = not (modify_files or create_files or delete_files or readable)
    scope = FileScope(
        readable=readable,
        writable=modify_files,
        create_files=create_files,
        delete_files=delete_files,
        allow_any=allow_any,
    )
    return TaskPlan(
        subtasks=[
            SubTask(
                id="st-1",
                description=task_description,
                difficulty=SubTaskDifficulty.TRIVIAL,
                modality=SubTaskModality.TEXT,
                scope=scope,
                contract={"input": "当前代码", "output": "按要求修改后的代码"},
                acceptance_criteria=["变更符合任务描述", "语法检查通过"],
                depends_on=[],
                harness=_infer_harness(task_description, scope),
            )
        ],
        parallel_groups=[["st-1"]],
    )


# ══════════════════════════════════════════════
# 辅助工具
# ══════════════════════════════════════════════

def _get_brain_llm():
    """获取 Brain LLM 实例"""
    router = ModelRouter()
    return router.get_brain_llm()


def _complexity_str(complexity: Complexity | str | None) -> str:
    if complexity is None:
        return Complexity.MEDIUM.value
    if hasattr(complexity, "value"):
        return complexity.value
    return str(complexity)


def _parse_json_from_llm(text: str | list) -> dict:
    """从 LLM 输出中解析 JSON（支持 markdown 代码块）

    Args:
        text: LLM response.content，可能是 str 或 list (多模态消息)
    """
    # 处理多模态 content（list 类型）
    if isinstance(text, list):
        # 提取文本部分
        parts = [item for item in text if isinstance(item, str)]
        if not parts:
            parts = [item.get("text", "") for item in text if isinstance(item, dict) and "text" in item]
        text = "\n".join(parts)
    # 去除 markdown 代码块包裹
    text = text.strip()
    if text.startswith("```"):
        # 找到第一个换行后、最后一个 ``` 之前
        first_nl = text.index("\n")
        last_fence = text.rfind("```")
        text = text[first_nl + 1 : last_fence].strip()
    return json.loads(text)


# ══════════════════════════════════════════════
# 节点函数
# ══════════════════════════════════════════════


def _brain_profile_prompt(state: BrainState) -> str:
    return state.get("user_profile_prompt_brain") or "（未加载用户画像）"


def _worker_profile_prompt(state: BrainState) -> str:
    return state.get("user_profile_prompt_worker") or "（未加载用户画像）"


async def analyze(state: BrainState) -> dict:
    """ANALYZE 节点 — 分析任务复杂度 & 检索知识上下文

    输入: task_description, project_id
    输出: complexity, knowledge_context
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

    heuristic = _heuristic_complexity(task_description)
    if heuristic is not None:
        logger.info("[ANALYZE] 启发式判定复杂度: %s", heuristic.value)
        affected_files = list(knowledge_context.get("affected_files") or [])
        if not affected_files:
            affected_files = [
                r.get("file_path", "")
                for r in knowledge_context.get("struct", [])
                if r.get("file_path")
            ]
        analyze_touch = touch_context(
            work_state,
            "analyze",
            f"复杂度={heuristic.value}（启发式判定）",
        )
        return {
            "complexity": heuristic,
            "knowledge_context": knowledge_context,
            "affected_files": affected_files,
            "recent_task_summaries": recent_summaries or [],
            **context_patch,
            **analyze_touch,
        }

    # ── LLM 复杂度分类 ──
    knowledge_prompt = format_brain_knowledge_prompt(
        knowledge_context, task_description
    )
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
        complexity = Complexity(result["complexity"])
    except json.JSONDecodeError as e:
        logger.warning(f"[ANALYZE] LLM 输出 JSON 解析失败: {e}")
        result = {
            "complexity": "medium",
            "reasoning": f"Mock: JSON 解析失败回退默认中等复杂度 — {e}",
            "key_risks": [],
            "suggested_subtask_count": 2,
        }
        complexity = Complexity(result["complexity"])
    except Exception as e:
        logger.warning(f"[ANALYZE] LLM 调用失败，回退到 medium: {e}")
        complexity = Complexity.MEDIUM
        result = {
            "complexity": "medium",
            "reasoning": f"LLM 调用失败，回退: {e}",
            "key_risks": [],
            "suggested_subtask_count": 2,
        }

    logger.info(f"[ANALYZE] 复杂度判定: {complexity.value}")
    affected_files = list(knowledge_context.get("affected_files") or [])
    if not affected_files:
        affected_files = [
            r.get("file_path", "")
            for r in knowledge_context.get("struct", [])
            if r.get("file_path")
        ]
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
        **context_patch,
        **analyze_touch,
    }


async def plan(state: BrainState) -> dict:
    """PLAN 节点 — 将任务拆解为子任务 DAG

    输入: task_description, complexity, knowledge_context
    输出: plan
    """
    task_description = state.get("task_description", "")
    complexity = state.get("complexity", Complexity.MEDIUM)
    knowledge_context = state.get("knowledge_context", {})

    logger.info(f"[PLAN] 拆解任务 (复杂度={complexity.value})")

    if complexity == Complexity.SIMPLE:
        affected_files = state.get("affected_files") or []
        task_plan = _build_simple_plan(task_description, affected_files)
        logger.info(
            "[PLAN] SIMPLE 快速路径 — 1 个 trivial 子任务 (scope=%d 文件)",
            len(affected_files),
        )
        from swarm.brain.contract_utils import enrich_plan_with_shared_contract

        task_plan = enrich_plan_with_shared_contract(task_plan)
        plan_touch = touch_context(
            state,
            "plan",
            f"生成 {len(task_plan.subtasks)} 个子任务（SIMPLE 快速路径）",
        )
        return {
            "plan": task_plan,
            "shared_contract": task_plan.shared_contract or {},
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
    sliding_ctx = sliding_context_prompt(state)

    # ── LLM 任务拆解 ──
    try:
        llm = _get_brain_llm()
        router = ModelRouter()
        routing_table = router.get_routing_table()
        prompt_user = PLAN_USER.format(
            task_description=task_description,
            complexity=complexity.value,
            routing_table=json.dumps(routing_table, ensure_ascii=False, indent=2),
            project_structure=_format_project_structure(knowledge_context),
            knowledge_context=knowledge_prompt,
            user_profile=_brain_profile_prompt(state),
            recent_tasks=recent_tasks_prompt,
            sliding_context=sliding_ctx,
        )
        response = await llm.ainvoke([
            {"role": "system", "content": PLAN_SYSTEM},
            {"role": "user", "content": prompt_user},
        ])
        result = _parse_json_from_llm(response.content)
        task_plan = TaskPlan(**result)
    except json.JSONDecodeError as e:
        logger.warning(f"[PLAN] LLM 输出 JSON 解析失败，使用简单单子任务 plan: {e}")
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
    from swarm.brain.contract_utils import enrich_plan_with_shared_contract

    task_plan = enrich_plan_with_shared_contract(task_plan)

    # harness 兜底：LLM 未给出 harness 的子任务，按语言推断一个，确保 Worker 有
    # 项目特定的构建/测试命令 + 命令白名单可用（否则又退化成"口头自报通过"）。
    for st in task_plan.subtasks:
        h = getattr(st, "harness", None)
        if h is None or not (h.build_command or h.test_command or h.verify_commands or h.extra_whitelist):
            st.harness = _infer_harness(st.description or task_description, st.scope)
    plan_touch = touch_context(
        state,
        "plan",
        f"生成 {len(task_plan.subtasks)} 个子任务",
    )
    return {
        "plan": task_plan,
        "shared_contract": task_plan.shared_contract or {},
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

    if state.get("complexity") == Complexity.SIMPLE:
        logger.info("[VALIDATE_PLAN] SIMPLE 快速路径 — 结构验证通过")
        return {
            "plan_valid": True,
            "plan_retry_count": retry_count,
            "plan_validation_issues": [],
        }

    # ── LLM 计划验证（结构已通过后的补充）──
    llm_valid = True
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
        llm_valid = bool(result.get("valid", False))
    except json.JSONDecodeError as e:
        logger.warning(f"[VALIDATE_PLAN] LLM JSON 解析失败，结构已通过则放行: {e}")
        llm_valid = True
    except Exception as e:
        logger.warning(f"[VALIDATE_PLAN] LLM 验证异常，结构已通过则放行: {e}")
        llm_valid = True

    plan_valid = llm_valid
    logger.info(f"[VALIDATE_PLAN] 结果: {'通过' if plan_valid else '未通过'}")
    return {
        "plan_valid": plan_valid,
        "plan_retry_count": retry_count,
        "plan_validation_issues": [] if plan_valid else ["LLM 计划验证未通过"],
    }


def confirm_plan(state: BrainState) -> dict:
    """CONFIRM 节点 — ultra 复杂度任务的人工确认点

    使用 langgraph.types.interrupt 实现挂起等待人工输入。
    输入: plan, task_description, complexity
    输出: human_decision

    在 auto_accept 模式下（API 调用），跳过 interrupt 直接接受。
    """
    logger.info("[CONFIRM] 等待人工确认 (ultra 复杂度)")

    # API 模式下自动接受，避免 ainvoke 挂起
    auto_accept = state.get("auto_accept", False) or os.environ.get("SWARM_AUTO_ACCEPT", "").lower() in ("1", "true", "yes")

    if auto_accept:
        logger.info("[CONFIRM] 自动接受 (auto_accept 模式)")
        return {"human_decision": HumanDecision.ACCEPT}

    # interrupt 会暂停图执行，等待外部输入
    # 外部调用方通过 Command(resume=...) 提供人类决策
    plan_obj = state.get("plan")
    decision = interrupt(
        {
            "type": "confirm_plan",
            "task_id": state.get("task_id"),
            "task_description": state.get("task_description"),
            "complexity": state.get("complexity", Complexity.ULTRA).value,
            "plan": plan_obj.model_dump() if plan_obj is not None and hasattr(plan_obj, "model_dump") else {},
            "message": "此任务为架构级变更（ultra），请审核执行计划并决定是否继续。",
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


def _diff_has_changes(diff: str) -> bool:
    return any(
        line.startswith("+") and not line.startswith("+++")
        for line in (diff or "").splitlines()
    )


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

    completed_ids = set(subtask_results.keys())
    config = get_config()
    max_concurrent = config.worker.max_concurrent

    to_dispatch = plan_obj.get_dispatch_batch(
        completed_ids, dispatch_remaining, max_concurrent
    )

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

    async def _run_one(subtask: SubTask) -> tuple[SubTask, WorkerOutput | Exception]:
        try:
            output = await _dispatch_to_worker(
                subtask,
                knowledge_context,
                project_id=project_id,
                task_id=task_id,
                use_alternate=use_alternate,
                user_profile_prompt=_worker_profile_prompt(state),
                shared_contract=shared_contract,
            )
            return subtask, output
        except Exception as e:
            return subtask, e

    outcomes = await asyncio.gather(*[_run_one(st) for st in to_dispatch])

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

    result: dict = {
        "subtask_results": subtask_results,
        "dispatch_remaining": dispatch_remaining,
        **worker_ctx,
    }
    if failed_ids:
        result["failed_subtask_ids"] = failed_ids
    return result


async def _dispatch_to_worker(
    subtask: SubTask,
    knowledge_context: KnowledgeContext,
    project_id: str = "",
    task_id: str = "",
    *,
    use_alternate: bool = False,
    user_profile_prompt: str = "",
    shared_contract: dict | None = None,
) -> WorkerOutput:
    """将子任务派发给 Worker 执行 — 真实调用 WorkerExecutor"""
    from swarm.knowledge.service import compact_knowledge_context, set_worker_context

    router = ModelRouter()
    difficulty = subtask.difficulty.value if hasattr(subtask.difficulty, "value") else str(subtask.difficulty)
    modality = subtask.modality.value if hasattr(subtask.modality, "value") else str(subtask.modality)
    if use_alternate:
        _primary, fallback_name = router._resolve_route(difficulty, modality)
        model_name = fallback_name or _primary
        worker_llm = router._get_provider_for_model(model_name).get_chat_model(
            model_name,
            temperature=router.config.worker_temperature,
        )
        logger.info(f"[DISPATCH] 子任务 {subtask.id} 使用备选模型: {model_name}")
    else:
        worker_llm = router.get_llm_for_subtask(
            difficulty=difficulty,
            modality=modality,
        )
        model_name = getattr(worker_llm, 'model_name', None) or getattr(worker_llm, 'model', 'routed')
        logger.info(f"[DISPATCH] 子任务 {subtask.id} 使用模型: {model_name}")

    set_worker_context(project_id or None)
    worker_knowledge = compact_knowledge_context(
        knowledge_context,
        limits={"mistakes": 3, "successes": 3, "struct": 8, "semantic": 3, "norms": 5, "behavior": 3},
    )

    project_path = None
    if project_id:
        try:
            from swarm.project import store
            proj = store.get_project(project_id)
            if proj and proj.get("path"):
                project_path = proj["path"]
        except Exception as exc:
            logger.warning("[DISPATCH] 获取项目路径失败: %s", exc)

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
        from swarm.worker.executor import WorkerExecutor
        executor = WorkerExecutor(
            subtask=subtask,
            model_name=model_name if isinstance(model_name, str) else None,
            knowledge=worker_knowledge,
            project_id=project_id or None,
            project_path=project_path,
            task_id=task_id or None,
            user_profile_prompt=user_profile_prompt,
            shared_contract=shared_contract or {},
        )
        t0 = time.monotonic()
        output = await executor.run()
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
        logger.info("[HANDLE_FAILURE] L2 集成验证失败 — 触发 replan")
        return {
            "failure_strategy": "replan",
            "failed_subtask_ids": [],
            "verification_failure": None,
            "l2_passed": False,
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
        logger.info("[HANDLE_FAILURE] 契约偏离 — 重试相关子任务")
        failed = list(state.get("failed_subtask_ids", [])) or list(
            (state.get("subtask_results") or {}).keys()
        )
        return {
            "failure_strategy": "retry",
            "failed_subtask_ids": failed[:3],
            "verification_failure": None,
        }

    if state.get("complexity") == Complexity.SIMPLE:
        dispatch_remaining = list(state.get("dispatch_remaining", []))
        for fid in failed_ids:
            subtask_results.pop(fid, None)
            if fid not in dispatch_remaining:
                dispatch_remaining.append(fid)
        logger.info("[HANDLE_FAILURE] SIMPLE 快速路径 — 重试失败子任务")
        return {
            "subtask_results": subtask_results,
            "dispatch_remaining": dispatch_remaining,
            "failed_subtask_ids": [],
            "failure_strategy": "retry",
        }

    # ── LLM 故障分析 ──
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
        strategy = result.get("strategy", "retry")
        logger.info(f"[HANDLE_FAILURE] LLM 策略: {strategy} — {result.get('reasoning', '')}")
    except json.JSONDecodeError as e:
        logger.warning(f"[HANDLE_FAILURE] LLM 输出 JSON 解析失败，回退到简单重试: {e}")
    except Exception as e:
        logger.warning(f"[HANDLE_FAILURE] 分析异常，回退到简单重试: {e}")

    if strategy == "replan":
        for fid in failed_ids:
            subtask_results.pop(fid, None)
        logger.info("[HANDLE_FAILURE] 策略=replan — 清除失败结果，触发重新规划")
        return {
            "subtask_results": subtask_results,
            "failed_subtask_ids": [],
            "plan_valid": False,
            "failure_strategy": "replan",
        }

    if strategy == "escalate":
        logger.info("[HANDLE_FAILURE] 策略=escalate — 上报人工审核")
        return {
            "failure_escalated": True,
            "failure_strategy": "escalate",
            "l2_passed": False,
            "failed_subtask_ids": failed_ids,
        }

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

    if deepest > max_retries + 1:
        # 已用尽 retry(max_retries 次) + retry_alternate(1 次) 仍失败 → 升级人工
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

    out: dict = {
        "dispatch_remaining": dispatch_remaining,
        "failed_subtask_ids": [],
        "subtask_results": subtask_results,
        "failure_strategy": effective_strategy,
        "subtask_retry_counts": {**retry_counts, **next_counts},
    }
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

    result = merge_diffs(subtask_diffs, base_reader=_make_base_reader(state))

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
    if result.rebase_subtask_ids:
        out["rebase_subtask_ids"] = result.rebase_subtask_ids
        dispatch_remaining = list(state.get("dispatch_remaining", []))
        remaining_results = dict(subtask_results)
        for sid in result.rebase_subtask_ids:
            remaining_results.pop(sid, None)
            if sid not in dispatch_remaining:
                dispatch_remaining.append(sid)
        out["subtask_results"] = remaining_results
        out["dispatch_remaining"] = dispatch_remaining

    return out


_L2_CMD_RE = re.compile(
    r"\b((?:pytest|python\s+-m\s+pytest|npm\s+test|mvn\s+test|make\s+test)(?:\s+[^\n;|]+)?)",
    re.IGNORECASE,
)


def _l2_test_command_from_criteria(criteria: list[str]) -> str:
    for item in criteria:
        match = _L2_CMD_RE.search(item)
        if match:
            return match.group(1).strip()
        stripped = item.strip()
        if stripped.startswith(("pytest", "python -m pytest", "npm test", "mvn test", "make test")):
            return stripped
    return "pytest -q"


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
        return all(
            out.l1_passed
            for out in subtask_results.values()
            if isinstance(out, WorkerOutput)
        )
    except Exception as e:
        logger.warning(f"[VERIFY_L2] LLM 验证异常，默认未通过: {e}")
        return False


async def verify_l2(state: BrainState) -> dict:
    """VERIFY_L2 节点 — L2 集成测试验证

    输入: merged_diff, plan, task_description
    输出: l2_passed
    """
    merged_diff = state.get("merged_diff", "")
    plan_obj = state.get("plan")
    task_description = state.get("task_description", "")
    project_id = state.get("project_id", "")
    subtask_results = state.get("subtask_results", {})

    logger.info("[VERIFY_L2] 执行集成验证")

    complexity = state.get("complexity", Complexity.MEDIUM)
    if complexity == Complexity.SIMPLE:
        merged = (merged_diff or "").strip()
        l2_passed = _diff_has_changes(merged)
        if subtask_results:
            l2_passed = l2_passed and all(
                (isinstance(o, WorkerOutput) and o.l1_passed)
                or (isinstance(o, dict) and o.get("l1_passed", True))
                for o in subtask_results.values()
            )
        logger.info("[VERIFY_L2] SIMPLE 快速路径 — diff+L1 检查: %s", "通过" if l2_passed else "未通过")
        if not l2_passed:
            return _l2_failure_state(subtask_results)
        return {"l2_passed": l2_passed}

    acceptance_criteria: list[str] = []
    if plan_obj:
        for t in plan_obj.subtasks:
            acceptance_criteria.extend(t.acceptance_criteria)

    test_cmd = _l2_test_command_from_criteria(acceptance_criteria)
    shared_contract = state.get("shared_contract") or (
        plan_obj.shared_contract if plan_obj else {}
    )

    # L2 确定性集成审查（编译 + 契约）
    project_path = _get_project_path(project_id)
    if project_path and (merged_diff or "").strip():
        from swarm.brain.integration_review import run_integration_review

        ir_ok, ir_issues, ir_details = run_integration_review(
            project_path,
            merged_diff,
            shared_contract or None,
            timeout=300,
        )
        logger.info("[VERIFY_L2] integration_review: %s issues=%s", ir_ok, ir_issues[:3])
        if not ir_ok:
            if any("契约" in i for i in ir_issues):
                return {
                    "l2_passed": False,
                    "verification_failure": "contract",
                    "failure_strategy": "retry",
                    "failed_subtask_ids": list(subtask_results.keys()),
                    "l2_details": {"integration_review": ir_details, "issues": ir_issues},
                }
            return _l2_failure_state(subtask_results)

    if (merged_diff or "").strip():
        sandbox_result = _try_l2_sandbox_verify(
            project_id, merged_diff, test_cmd, timeout=180
        )
        if sandbox_result is not None:
            logger.info("[VERIFY_L2] 沙箱结果: %s", "通过" if sandbox_result else "未通过")
            if not sandbox_result:
                return _l2_failure_state(subtask_results)
            return {"l2_passed": sandbox_result}

        local_result = _try_l2_local_verify(
            project_id, merged_diff, test_cmd, timeout=180
        )
        if local_result is not None:
            logger.info("[VERIFY_L2] 本地结果: %s", "通过" if local_result else "未通过")
            if not local_result:
                return _l2_failure_state(subtask_results)
            return {"l2_passed": local_result}

    l2_passed = await _verify_l2_via_llm(
        task_description,
        merged_diff,
        acceptance_criteria,
        subtask_results,
    )

    logger.info(f"[VERIFY_L2] 结果: {'通过' if l2_passed else '未通过'}")
    if not l2_passed:
        return _l2_failure_state(subtask_results)
    return {"l2_passed": l2_passed}


def _l2_failure_state(subtask_results: dict) -> dict:
    failed_ids = list(subtask_results.keys()) if subtask_results else []
    return {
        "l2_passed": False,
        "verification_failure": "l2",
        "failure_strategy": "replan",
        "failed_subtask_ids": failed_ids,
    }


def _l3_failure_state() -> dict:
    return {
        "l3_passed": False,
        "l3_skipped": False,
        "verification_failure": "l3",
        "failure_strategy": "escalate",
    }


async def verify_l3(state: BrainState) -> dict:
    """VERIFY_L3 节点 — L3 预发/扩展验证（COMPLEX/ULTRA）

    输入: merged_diff, complexity, task_description
    输出: l3_passed, l3_skipped, l3_message
    """
    complexity = state.get("complexity", Complexity.MEDIUM)
    merged_diff = (state.get("merged_diff") or "").strip()
    task_description = state.get("task_description", "")

    if complexity in (Complexity.SIMPLE, Complexity.MEDIUM):
        logger.info("[VERIFY_L3] SIMPLE/MEDIUM — 跳过 L3")
        return {
            "l3_passed": None,
            "l3_skipped": True,
            "l3_message": "L3 skipped for simple/medium complexity",
        }

    if not merged_diff:
        logger.info("[VERIFY_L3] 无 merged_diff — 跳过 L3")
        return {
            "l3_passed": None,
            "l3_skipped": True,
            "l3_message": "No merged diff for L3",
        }

    task_id = state.get("task_id", "")
    project_id = state.get("project_id", "")
    from swarm.brain.l3_gitlab import (
        gitlab_configured,
        l3_push_enabled,
        push_merged_diff_branch,
        trigger_and_poll_pipeline,
    )

    if gitlab_configured():
        try:
            ref = os.environ.get("SWARM_GITLAB_REF", "main")
            if l3_push_enabled():
                project_path = _get_project_path(project_id)
                if project_path:
                    branch, push_err = push_merged_diff_branch(
                        project_path, merged_diff, task_id or "unknown", base_ref=ref
                    )
                    if branch:
                        ref = branch
                        logger.info("[VERIFY_L3] 已推送 L3 分支: %s", branch)
                    elif push_err:
                        logger.warning("[VERIFY_L3] L3 push 失败，回退默认 ref: %s", push_err)

            l3_passed, l3_message = trigger_and_poll_pipeline(
                task_id=task_id or "unknown", ref=ref
            )
            logger.info("[VERIFY_L3] GitLab: %s — %s", "通过" if l3_passed else "未通过", l3_message)
            if not l3_passed:
                return {**_l3_failure_state(), "l3_message": l3_message}
            return {
                "l3_passed": l3_passed,
                "l3_skipped": False,
                "l3_message": l3_message,
            }
        except Exception as exc:
            logger.warning("[VERIFY_L3] GitLab pipeline 失败，回退 staging/LLM: %s", exc)

    staging_url = os.environ.get("SWARM_STAGING_URL", "").strip()
    if not staging_url:
        logger.info("[VERIFY_L3] 未配置 SWARM_STAGING_URL — 跳过 L3")
        return {
            "l3_passed": None,
            "l3_skipped": True,
            "l3_message": "No staging URL configured",
        }

    logger.info("[VERIFY_L3] 执行扩展验证: %s", staging_url)
    try:
        llm = _get_brain_llm()
        prompt_user = VERIFY_L3_USER.format(
            task_description=task_description,
            merged_diff=merged_diff[:4000],
            staging_url=staging_url,
        )
        response = await llm.ainvoke([
            {"role": "system", "content": VERIFY_L3_SYSTEM},
            {"role": "user", "content": prompt_user},
        ])
        result = _parse_json_from_llm(response.content)
        l3_passed = bool(result.get("l3_passed", False))
        l3_message = str(result.get("message", "L3 LLM validation"))
    except json.JSONDecodeError as e:
        logger.warning("[VERIFY_L3] LLM JSON 解析失败，回退 HTTP 探测: %s", e)
        l3_passed, l3_message = _l3_staging_http_check(staging_url)
    except Exception as e:
        logger.warning("[VERIFY_L3] LLM 验证异常，回退 HTTP 探测: %s", e)
        l3_passed, l3_message = _l3_staging_http_check(staging_url)

    logger.info("[VERIFY_L3] 结果: %s — %s", "通过" if l3_passed else "未通过", l3_message)
    if not l3_passed:
        return {**_l3_failure_state(), "l3_message": l3_message}
    return {
        "l3_passed": l3_passed,
        "l3_skipped": False,
        "l3_message": l3_message,
    }


def _l3_staging_http_check(staging_url: str) -> tuple[bool, str]:
    """对预发 URL 做轻量 HTTP 可达性检查。"""
    import urllib.error
    import urllib.request

    try:
        req = urllib.request.Request(staging_url, method="HEAD")
        with urllib.request.urlopen(req, timeout=10) as resp:
            passed = resp.status < 400
            return passed, f"Staging HEAD {staging_url} status={resp.status}"
    except urllib.error.HTTPError as exc:
        return exc.code < 400, f"Staging HEAD {staging_url} status={exc.code}"
    except Exception as exc:
        return False, f"Staging check failed: {exc}"


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
        logger.info("[DELIVER] 自动接受 (auto_accept 模式)")
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
        # 尝试从 LLM 结果中提取修订子任务
        revision_subtask = SubTask(
            id=result.get("id", f"rev-{len(plan_obj.subtasks) + 1 if plan_obj else 1}"),
            description=result.get("description", f"修订: {revision_feedback[:100]}"),
            difficulty=SubTaskDifficulty(result.get("difficulty", "medium")),
            modality=SubTaskModality(result.get("modality", "text")),
            scope=FileScope(**result.get("scope", {"writable": [], "readable": []})),
            contract=result.get("contract", {"input": "修订反馈", "output": "修订后代码"}),
            acceptance_criteria=result.get("acceptance_criteria", ["修订内容正确", "回归测试通过"]),
            depends_on=result.get("depends_on", []),
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
        updated_plan = TaskPlan(subtasks=new_subtasks, parallel_groups=new_parallel_groups)
    else:
        updated_plan = TaskPlan(
            subtasks=[revision_subtask],
            parallel_groups=[[revision_subtask.id]],
        )

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
    complexity = state.get("complexity", Complexity.MEDIUM)

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
