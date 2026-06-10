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


def _get_worker_tools() -> list[BaseTool]:
    """获取 Worker 可用的所有 Tool 列表"""
    return [
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


def create_worker_agent(
    subtask: SubTask,
    scope: FileScope | None = None,
    model_name: str | None = None,
    model_strategy: str = "cost_optimized",
    knowledge: KnowledgeContext | None = None,
    project_id: str | None = None,
    user_profile_prompt: str = "",
    shared_contract: dict | None = None,
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
        llm = router.get_model_by_name(model_name, temperature=0.2)
    else:
        llm = router.get_worker_llm(strategy=model_strategy)

    # 获取 Tool 集
    tools = _get_worker_tools()

    # 构建系统提示词（注入 Brain 检索到的错题/成功范例）
    system_prompt = build_worker_prompt(
        subtask=subtask,
        scope=effective_scope,
        knowledge=knowledge,
        user_profile_prompt=user_profile_prompt,
        shared_contract=shared_contract,
    )

    # 创建 ReAct Agent
    agent = create_react_agent(
        model=llm,
        tools=tools,
        prompt=system_prompt,
    )

    return {
        "agent": agent,
        "scope": effective_scope,
        "tools": tools,
        "system_prompt": system_prompt,
    }
