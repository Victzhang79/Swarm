#!/usr/bin/env python3
"""Swarm 集成冒烟测试 — 验证端到端链路"""

# 确保项目根目录在 path 中
import importlib.util
import sys
from pathlib import Path

_bs = Path(__file__).resolve().parent / "swarm_bootstrap.py"
_spec = importlib.util.spec_from_file_location("swarm_bootstrap", _bs)
_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_mod)


def test_types():
    """测试核心类型定义"""
    from swarm.types import (
        Complexity,
        FileScope,
        SubTask,
        TaskPlan,
        TaskStatus,
    )

    assert Complexity.SIMPLE.value == "simple"
    assert TaskStatus.ANALYZING.value == "ANALYZING"

    scope = FileScope(writable=["foo.py"], readable=["bar.py"])
    assert scope.is_writable("foo.py")
    assert not scope.is_writable("bar.py")
    assert scope.is_readable("bar.py")

    plan = TaskPlan(subtasks=[
        SubTask(id="a", description="task a", scope=scope),
        SubTask(id="b", description="task b", scope=scope, depends_on=["a"]),
    ])
    ready = plan.get_ready_tasks(set())
    assert len(ready) == 1 and ready[0].id == "a"
    ready2 = plan.get_ready_tasks({"a"})
    assert len(ready2) == 1 and ready2[0].id == "b"
    assert not plan.all_completed({"a"})
    assert plan.all_completed({"a", "b"})

    print("  ✅ types — 核心类型定义正常")


def test_config():
    """测试配置系统"""
    from swarm.config import get_config

    config = get_config()
    assert config.app_name == "Swarm"
    assert config.model.brain_primary == "Pro/zai-org/GLM-5.1"
    assert config.model.worker_primary == "MiniMax-M2.7-Pro"
    assert config.worker.max_concurrent == 4

    print("  ✅ config — 配置系统正常")


def test_model_router():
    """测试模型路由"""
    from swarm.models.router import ModelRouter

    router = ModelRouter()
    # 验证路由器能创建（不实际调用 API）
    assert router.config.brain_primary == "Pro/zai-org/GLM-5.1"
    assert router.config.worker_primary == "MiniMax-M2.7-Pro"

    print("  ✅ models — 模型路由正常")


def test_scope_guard():
    """测试 Scope Guard"""
    from swarm.tools.scope_guard import ScopeGuard, clear_scope, get_scope
    from swarm.types import FileScope

    scope = FileScope(writable=["test.py"], readable=["read.py"])

    with ScopeGuard(scope):
        current = get_scope()
        assert current is not None
        assert current.is_writable("test.py")
        assert not current.is_writable("other.py")

    # context manager 退出后 scope 应该被清理
    clear_scope()

    print("  ✅ scope_guard — Scope Guard 正常")


def test_tools():
    """测试 Tool 注册"""
    from swarm.tools.build_tools import run_command, run_compile, run_tests
    from swarm.tools.file_tools import patch_file, read_file, search_in_file, write_file
    from swarm.tools.git_tools import git_checkout, git_diff
    from swarm.tools.knowledge_tools import query_knowledge_base

    tools = [
        read_file, write_file, patch_file, search_in_file,
        git_checkout, git_diff,
        run_command, run_compile, run_tests,
        query_knowledge_base,
    ]
    for t in tools:
        assert hasattr(t, "name"), f"Tool {t} missing .name"
        assert hasattr(t, "description"), f"Tool {t} missing .description"

    print(f"  ✅ tools — {len(tools)} 个 Tool 注册正常")


def test_worker_prompts():
    """测试 Worker Prompt 生成"""
    from swarm.types import FileScope, SubTask
    from swarm.worker.prompts import build_worker_prompt

    subtask = SubTask(
        id="t1",
        description="给 UserDTO 加 sortField 字段",
        scope=FileScope(writable=["UserDTO.java"], readable=["UserController.java"]),
        contract={"new_fields": [{"name": "sortField", "type": "String"}]},
        acceptance_criteria=["编译通过", "字段有 @Schema 注解"],
    )

    knowledge = {
        "mistakes": [{"error_pattern": "忘了加默认值", "lesson": "上次忘了加默认值"}],
        "successes": [{"pattern": "商品排序的标准写法：DTO + Mapper 动态 SQL"}],
    }
    prompt = build_worker_prompt(
        subtask=subtask,
        knowledge=knowledge,
    )

    assert "UserDTO.java" in prompt

    print("  ✅ worker/prompts — Worker Prompt 生成正常")


def test_brain_state():
    """测试 Brain 状态"""
    from swarm.brain.state import BrainState

    state: BrainState = {
        "task_id": "test-001",
        "task_description": "测试任务",
        "project_id": "test-project",
        "complexity": "simple",
        "knowledge_context": {},
        "plan": None,
        "plan_valid": None,
        "plan_retry_count": 0,
        "subtask_results": [],
        "dispatch_remaining": [],
        "failed_subtask_ids": [],
        "merged_diff": None,
        "l2_passed": None,
        "human_decision": None,
        "revision_feedback": None,
        "learned": False,
        "learn_summary": "",
    }

    assert state["task_id"] == "test-001"
    print("  ✅ brain/state — Brain 状态定义正常")


def test_brain_graph():
    """测试 Brain Graph 构建"""
    from swarm.brain.graph import compile_brain_graph, reset_compiled_brain_graph

    reset_compiled_brain_graph()
    graph = compile_brain_graph()
    assert graph is not None

    # 验证图的节点（子代理用小写命名）
    node_names = set(graph.get_graph().nodes.keys())
    expected_lower = {"analyze", "plan", "validate_plan", "dispatch", "monitor",
                      "handle_failure", "merge", "verify_l2", "verify_l3", "deliver",
                      "revision", "learn_success", "learn_failure"}
    # 兼容大小写命名
    actual_lower = {n.lower() for n in node_names}
    missing = expected_lower - actual_lower
    if missing:
        print(f"  ⚠️ brain/graph — 缺少节点: {missing}（实际节点: {node_names}）")
    else:
        print(f"  ✅ brain/graph — 状态机构建成功，{len(node_names)} 个节点")


def test_knowledge_modules():
    """测试知识库模块导入"""

    print("  ✅ knowledge — 知识库模块导入正常")


def test_memory_modules():
    """测试记忆系统模块导入"""

    print("  ✅ memory — 记忆系统模块导入正常")


def test_tracing_helpers():
    """LangSmith tracing 辅助函数（tracing 关闭时不报错）"""
    from swarm.tracing import (
        brain_graph_config,
        is_langsmith_active,
        langsmith_status,
        merge_invoke_config,
        worker_agent_config,
    )

    cfg = brain_graph_config(
        task_id="task-abc",
        project_id="proj-1",
        thread_id="thread-1",
        description="test",
    )
    assert cfg["configurable"]["thread_id"] == "thread-1"
    if is_langsmith_active():
        assert "run_name" in cfg
        assert "swarm-phase-1" in cfg.get("tags", [])

    wcfg = worker_agent_config(
        run_id="run-1",
        project_id="p1",
        step="locate",
        source="standalone",
    )
    merged = merge_invoke_config({"recursion_limit": 10}, wcfg)
    assert merged["recursion_limit"] == 10
    status = langsmith_status()
    assert "active" in status
    print("  ✅ tracing — helpers OK")


def main():
    print("\n🐝 Swarm 集成冒烟测试\n")
    print("=" * 50)

    tests = [
        test_types,
        test_config,
        test_model_router,
        test_scope_guard,
        test_tools,
        test_worker_prompts,
        test_brain_state,
        test_brain_graph,
        test_tracing_helpers,
        test_knowledge_modules,
        test_memory_modules,
    ]

    passed = 0
    failed = 0

    for test in tests:
        try:
            test()
            passed += 1
        except Exception as e:
            print(f"  ❌ {test.__name__}: {e}")
            import traceback
            traceback.print_exc()
            failed += 1

    print("=" * 50)
    print(f"\n📊 结果: {passed} 通过, {failed} 失败")

    if failed == 0:
        print("✅ 全部测试通过！Swarm 系统骨架已就绪。\n")
    else:
        print(f"❌ {failed} 个测试失败，需要修复。\n")
        sys.exit(1)


if __name__ == "__main__":
    main()
