"""Swarm Brain — 基于 LangGraph 的完整任务生命周期编排

核心组件:
- BrainState: 状态机完整状态定义 (state.py)
- 节点函数: analyze, plan, validate_plan, confirm_plan, dispatch, monitor,
            handle_failure, merge, verify_l2, deliver, revision,
            learn_success, learn_failure (nodes.py)
- 条件边: after_validate, after_confirm, after_monitor, after_deliver (graph.py)
- Graph 构建: build_brain_graph(), compile_brain_graph() (graph.py)
- Prompt 模板: 各节点 LLM prompt (prompts.py)
"""

from swarm.brain.state import BrainState
from swarm.brain.graph import (
    build_brain_graph,
    compile_brain_graph,
    compile_brain_graph_with_postgres,
    get_compiled_brain_graph,
)

__all__ = [
    "BrainState",
    "build_brain_graph",
    "compile_brain_graph",
    "compile_brain_graph_with_postgres",
    "get_compiled_brain_graph",
]
