"""Worker Agent 创建 — 基于 LangGraph create_react_agent 的 ReAct Agent

每个 Worker 接收一个 SubTask + FileScope，使用授权的 Tool 集执行。
"""

from __future__ import annotations

from langchain_core.tools import BaseTool
from langgraph.prebuilt import create_react_agent

from swarm.models.router import ModelRouter
from swarm.tools.build_tools import run_command, run_compile, run_tests
from swarm.tools.file_tools import patch_file, read_file, search_in_file, write_file
from swarm.tools.git_tools import git_blame, git_checkout, git_diff, git_log
from swarm.tools.knowledge_tools import query_knowledge_base
from swarm.tools.scope_guard import set_scope
from swarm.types import FileScope, KnowledgeContext, SubTask
from swarm.worker.prompts import build_worker_prompt


def _make_pre_model_hook(max_input_tokens: int):
    """生成 create_react_agent 的 pre_model_hook：每次调 LLM 前裁剪历史 messages，
    防止 ReAct 多轮工具调用的历史(read_file 结果/reasoning/tool 输出)无限累积撞穿
    模型上下文窗口(实测 Qwen3.5-122B 65536 窗口被累积到 57345 输入 → 400 报错 → 子任务死循环)。

    用 LangChain trim_messages 保留最近的 message(strategy="last")，按 token 预算裁剪，
    但始终保留 system prompt(include_system=True)。裁剪结果写入 llm_input_messages —— 只影响
    传给 LLM 的输入，不改持久 state(工具结果仍在 state 里供 diff 采集)。
    """
    from langchain_core.messages.utils import count_tokens_approximately, trim_messages

    def _hook(state: dict) -> dict:
        msgs = state.get("messages", [])
        if not msgs:
            return {}
        try:
            trimmed = trim_messages(
                msgs,
                max_tokens=max_input_tokens,
                token_counter=count_tokens_approximately,
                strategy="last",          # 保留最近的(当前任务上下文最相关)
                include_system=True,      # 始终保留 system prompt
                start_on="human",         # 裁剪后首条非 system 必须是 human(满足 API 要求)
                allow_partial=False,
            )
            return {"llm_input_messages": trimmed}
        except Exception:  # noqa: BLE001 — 裁剪失败不应让子任务崩，退回原 messages
            return {}

    return _hook


def _get_worker_tools(scope: FileScope | None = None,
                      intent: str = "") -> list[BaseTool]:
    """获取 Worker 可用的 Tool 列表——C10（阶段4，登记册 §四）按 scope/intent 确定性裁剪。

    12 个全集对小模型是复读死循环土壤（工具越多选择面越糊）。裁剪规则（通用多栈，
    不看语言）：
      · 只读 scope（无 writable/create/delete 且非 allow_any）→ 去 write_file/patch_file
        （审计/纯分析任务给写工具=诱导越权+噪声）；
      · git_log/git_blame 只给 debug/audit 意图（历史考古工具；普通编码子任务用不上，
        且沙箱常无 .git——round20#13）；
    不传参=旧全集（legacy 调用方零回归）。典型编码子任务 12→10 个。
    """
    tools: list[BaseTool] = [
        # 文件操作
        read_file,
        write_file,
        patch_file,
        search_in_file,
        # Git 操作
        git_checkout,
        git_diff,
        git_log,
        git_blame,
        # 构建 & 测试
        run_command,
        run_compile,
        run_tests,
        # 知识检索
        query_knowledge_base,
    ]
    if scope is None and not intent:
        return tools
    _intent = str(intent or "").strip().lower()
    if _intent not in ("debug", "audit"):
        tools = [t for t in tools if t not in (git_log, git_blame)]
    if scope is not None and not getattr(scope, "allow_any", False):
        _writes = (list(getattr(scope, "writable", []) or [])
                   + list(getattr(scope, "create_files", []) or [])
                   + list(getattr(scope, "delete_files", []) or []))
        if not _writes:
            tools = [t for t in tools if t not in (write_file, patch_file)]
    return tools


def create_worker_agent(
    subtask: SubTask,
    scope: FileScope | None = None,
    model_name: str | None = None,
    model_strategy: str = "cost_optimized",
    knowledge: KnowledgeContext | None = None,
    project_id: str | None = None,
    user_profile_prompt: str = "",
    shared_contract: dict | None = None,
    project_stack: dict | None = None,
) -> dict:
    """创建一个 Worker ReAct Agent

    Args:
        subtask: 要执行的子任务
        scope: 文件访问权限范围，默认使用 subtask.scope
        model_name: 指定模型名称，优先级高于 model_strategy
        model_strategy: 模型选择策略（cost_optimized/privacy/quality）

    Returns:
        dict 包含:
            - agent: LangGraph CompiledGraph（可 invoke）
            - scope: FileScope 实例
            - tools: Tool 列表
            - system_prompt: 生成的系统提示词
    """
    effective_scope = scope or subtask.scope

    # 设置 Scope（Worker 生命周期内保持）
    set_scope(effective_scope)

    # 获取 Worker LLM
    router = ModelRouter()
    if model_name:
        # 治本：主力并行轮转 override 模型【必须】带该难度 fallback 链。
        # 此前误用 get_model_by_name（裸模型、无 fallback）→ override 模型不可用时
        # （如端点中途下线返回 400 Model not found）无链可切，worker 直接抛异常死，
        # 连累一簇子任务零进展 → 看守判死循环取消整任务（实测 E2E 996 第三轮）。
        # 改用 get_llm_by_name（专为轮转 override 设计，复用该难度 fallback 链、排除自身）。
        _diff = subtask.difficulty.value if hasattr(subtask.difficulty, "value") else str(subtask.difficulty)
        llm = router.get_llm_by_name(model_name, difficulty=_diff)
    else:
        llm = router.get_worker_llm(strategy=model_strategy)

    # 获取 Tool 集（基础工具按 scope/intent 裁剪 + 经验拔插层按上下文挂的离散经验工具
    # experience__<id>）。经验工具 advisory·可选：小模型自己决定调哪个（或不调）。
    # fail-open：任何异常都退回纯基础工具，绝不因经验层拖垮 worker 创建。
    tools = _get_worker_tools(
        effective_scope, str(getattr(subtask, "intent", "") or ""))
    try:
        from swarm.experience.service import build_worker_experience_tools
        _exp_tools = build_worker_experience_tools(subtask, project_stack)
        if _exp_tools:
            tools = tools + _exp_tools
    except Exception:  # noqa: BLE001 — 经验工具绝不拖垮 worker
        import logging
        logging.getLogger(__name__).warning(
            "[skills] 挂载 worker 经验工具失败，退回纯基础工具", exc_info=True
        )

    # 构建系统提示词（注入 Brain 检索到的错题/成功范例）
    system_prompt = build_worker_prompt(
        subtask=subtask,
        scope=effective_scope,
        knowledge=knowledge,
        user_profile_prompt=user_profile_prompt,
        shared_contract=shared_contract,
        project_stack=project_stack,
    )

    # 创建 ReAct Agent
    # pre_model_hook：每次调 LLM 前裁剪历史 messages，防 ReAct 多轮累积撞穿上下文窗口。
    # 预算 = worker 上下文预算(min(模型窗口×0.75,150k))再留余量给输出+system，取 0.7。
    # 复用 planning_nodes._context_budget()(已按真实模型窗口算)，保持单一事实源。
    try:
        from swarm.brain.planning_nodes import _context_budget
        _budget = int(_context_budget() * 0.7)
    except Exception:  # noqa: BLE001
        _budget = 40000
    agent = create_react_agent(
        model=llm,
        tools=tools,
        prompt=system_prompt,
        pre_model_hook=_make_pre_model_hook(_budget),
        # round29 遗漏项#1 治本（d37a52a3 st-26 实证）：worker agent 在 brain 图 dispatch 节点内
        # ainvoke，LangGraph 会经 config 传播把【父图的 PG checkpointer】自动继承给子图 →
        # worker 每步 messages（AIMessage）都被序列化入库（DB 实证 checkpoint_ns='dispatch:…|N'）
        # → 模型返回带不可 msgpack 序列化负载时 MsgpackEncodeError 炸掉整轮执行 + 海量无用
        # checkpoint 写入。worker agent 是无状态一次性执行体（失败恢复靠 brain 重派整个子任务，
        # 从不 resume agent 内部状态），checkpointer=False 显式阻断继承（官方语义）。
        checkpointer=False,
    )

    return {
        "agent": agent,
        "scope": effective_scope,
        "tools": tools,
        "system_prompt": system_prompt,
    }
