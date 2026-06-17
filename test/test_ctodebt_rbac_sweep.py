"""P0-SEC-03 全 list/read 端点 RBAC 横扫回归测试。

每个项目/任务 scoped 端点必须调用 _require_perm/_require_user，防跨项目水平越权。
用源码静态检查锁定每处闸门（防后续被误删），并校验权限串均为合法 RBAC 词汇。
"""
from __future__ import annotations

import inspect

from swarm.auth.rbac import ROLE_PERMISSIONS

# RBAC 词汇全集（admin 的 "*" 除外）
_VALID_PERMS = set().union(*[p for p in ROLE_PERMISSIONS.values()]) - {"*"}
# 已知约定：task:write 在代码库被既有端点(approve/cancel/delete/apply-diff)广泛使用，但
# 不在任何角色集合 → rbac-on 下实为 admin-only(via "*")。属【既有】RBAC 词汇缺口(已记入
# CTO_GUIDE §12，待 RBAC 词汇评审)，本横扫沿用该约定保持一致，不在此盲改授权语义。
_KNOWN_WRITE_CONVENTION = {"task:write"}


def _src(func) -> str:
    return inspect.getsource(func)


def test_task_endpoints_gated():
    from swarm.api.routers import task

    assert '_require_perm(request, "task:read", project_id)' in _src(task.list_tasks)
    assert "_require_perm" in _src(task.retry_task_endpoint)
    assert "_require_perm" in _src(task.execute_pooled_task)
    assert '_require_perm(request, "task:write"' in _src(task.revise_task)
    assert '_require_perm(request, "task:write"' in _src(task.reject_task)
    assert '_require_perm(request, "task:write"' in _src(task.submit_clarify)
    assert '_require_perm(request, "task:write"' in _src(task.submit_design_review)
    assert "_require_perm" in _src(task.task_audit_endpoint) or "_require_user" in _src(task.task_audit_endpoint)


def test_memory_reads_gated():
    from swarm.api.routers import memory

    for fn in (memory.list_mistakes, memory.list_successes, memory.list_summaries):
        assert '_require_perm(request, "project:read"' in _src(fn), fn.__name__


def test_knowledge_reads_gated():
    from swarm.api.routers import knowledge

    for fn in (
        knowledge.knowledge_overview,
        knowledge.search_symbols,
        knowledge.search_semantic_chunks,
        knowledge.knowledge_retrieve_experiment,
        knowledge.list_norms,
        knowledge.list_behavior_hotspots,
        knowledge.list_pending_embeddings,
        knowledge.knowledge_consistency_check,
    ):
        assert '_require_perm(request, "project:read"' in _src(fn), fn.__name__


def test_project_preprocess_gated():
    from swarm.api.routers import project

    assert '_require_perm(request, "project:write"' in _src(project.trigger_preprocess)
    assert '_require_perm(request, "project:read"' in _src(project.get_preprocess_status)
    assert '_require_perm(request, "project:read"' in _src(project.stream_preprocess_progress)


def test_app_stats_gated():
    # swarm.api.app 名被 FastAPI 实例遮蔽，直接读源文件文本断言闸门存在。
    import sys

    mod = sys.modules.get("swarm.api.app")
    if mod is None:
        import importlib
        mod = importlib.import_module("swarm.api.app")
    src = inspect.getsource(mod)
    assert "async def get_project_stats(project_id: str, request: Request)" in src
    assert '_require_perm(request, "project:read", project_id)  # P0-SEC-03' in src
    assert "async def get_stats(request: Request" in src


def test_worker_stream_gated():
    from swarm.api.routers import worker

    assert "_require_user" in _src(worker.stream_worker_run)


def test_all_swept_perm_strings_valid():
    """横扫用到的权限串必须是合法 RBAC 词汇（防打错权限名静默放行/锁死）。"""
    import re

    from swarm.api.routers import knowledge, memory, project, task

    used = set()
    for mod in (task, memory, knowledge, project):
        for m in re.finditer(r'_require_perm\(request,\s*"([a-z]+:[a-z]+)"', inspect.getsource(mod)):
            used.add(m.group(1))
    assert used, "应至少提取到若干权限串"
    # 允许合法 RBAC 词汇 + 已知 task:write 约定；其余即拼写错误(会静默放行/锁死)。
    bad = used - _VALID_PERMS - _KNOWN_WRITE_CONVENTION
    assert not bad, f"无效权限串(疑似拼写错误，不在 RBAC 词汇表): {bad}"
    # 横扫的【读】端点必须用合法读权限（project:read 等），不得误用 task:write
    assert "project:read" in used


def test_membership_denies_non_member():
    """根本保障：非成员对项目操作被拒（user_can_on_project 走成员校验）。"""
    from unittest.mock import patch

    from swarm.auth.rbac import Role
    from swarm.auth.store import SwarmUser, user_can_on_project

    viewer = SwarmUser(id="u1", username="v", display_name="V",
                       global_role=Role.VIEWER.value, must_change_password=False)
    # 项目有成员、但该用户非成员 → 拒绝
    with patch("swarm.auth.store.count_project_members", return_value=3), \
         patch("swarm.auth.store.get_project_member_role", return_value=None):
        assert user_can_on_project(viewer, "project:read", "proj-x") is False


if __name__ == "__main__":
    import sys

    import pytest

    sys.exit(pytest.main([__file__, "-q", "-p", "no:warnings"]))
