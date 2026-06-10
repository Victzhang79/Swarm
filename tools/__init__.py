"""Swarm Tools — 文件操作、Git、构建、知识检索等 Tool 集"""

from swarm.tools.scope_guard import (
    ScopeGuard,
    set_scope,
    get_scope,
    clear_scope,
    require_readable,
    require_writable,
)
from swarm.tools.file_tools import read_file, write_file, patch_file, search_in_file
from swarm.tools.git_tools import git_checkout, git_diff, git_log, git_blame
from swarm.tools.build_tools import (
    run_command, run_compile, run_tests,
    set_sandbox_context, get_sandbox_context, clear_sandbox_context,
)
from swarm.tools.knowledge_tools import query_knowledge_base

__all__ = [
    # ScopeGuard
    "ScopeGuard",
    "set_scope",
    "get_scope",
    "clear_scope",
    "require_readable",
    "require_writable",
    # File Tools
    "read_file",
    "write_file",
    "patch_file",
    "search_in_file",
    # Git Tools
    "git_checkout",
    "git_diff",
    "git_log",
    "git_blame",
    # Build Tools
    "run_command",
    "run_compile",
    "run_tests",
    # Sandbox Context
    "set_sandbox_context",
    "get_sandbox_context",
    "clear_sandbox_context",
    # Knowledge Tools
    "query_knowledge_base",
]
